"""Endpoint `POST /transactions/{id}/authorize` (E2-T19, HU08).

Sobre el motor y el ledger con adaptadores reales (`ACCOUNTS_BALANCE_PORT`,
cuentas por `accounts.repository`), fachada real de parametros
(`shared.config`, E2-T18) y verificacion HMAC real de `identity`
(`device_login.verify_signature` sin parche; solo el binding se simula por
fachada): dinero de juguete (UUIDs sinteticos, sin PII), SQLite en memoria
+ schemas ATTACH, sin red, sin `float`.

Mapeo brief -> test:
- feliz: `PENDING_AUTHORIZATION` + firma valida -> `SETTLED` .. `test_authorize_ok_*`
- firma invalida -> 403 + intento persistido ............. `test_invalid_*` (OBS-3)
- `nonce` expirado -> 400 / reusado -> replay/409 ...... `test_expired_*`, `test_reused_*`
- no pendiente -> 409 .................................. `test_not_pending_*`
- ajena -> 403 / inexistente -> 404 .................... `test_foreign_*`, `test_missing_*`
- replay idempotente + conflicto de cuerpo ............. `test_idempotency_*`
- limite por perfil distinto de STANDARD (OBS-1) ....... `test_profile_*`
- bloqueo preventivo persistido (OBS-3) ................ `test_preventive_block_*`
- concurrencia: doble `authorize` liquida una vez ..... `test_concurrent_*`
- frontera (sin modelos ajenos) ........................ `test_facade_*`
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
DEVICE_A = "dev-e2t19-1"

THRESHOLD_KEY = "transfer.biometric_threshold_minor"
DAILY_KEY = "transfer.daily_limit_minor"
DAILY_STANDARD_KEY = "transfer.daily_limit_STANDARD_minor"
DAILY_PREMIUM_KEY = "transfer.daily_limit_PREMIUM_minor"
MAX_ATTEMPTS_KEY = "auth.max_failed_attempts"

TABLES = [
    "accounts.accounts",
    "accounts.account_balances",
    "accounts.daily_balance_snapshots",
    "ledger.ledger_accounts",
    "ledger.journal_entries",
    "ledger.postings",
    "ledger.ledger_balances",
    "shared.outbox",
    "shared.processed_events",
    "shared.idempotency_keys",
    "transactions.transactions",
    "transactions.transaction_status_history",
    "transactions.holds",
    "identity.users",
]


def _make_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # registro de tablas
    import app.modules.identity.models as _i
    import app.modules.ledger.models as _l
    import app.modules.shared.models as _s
    import app.modules.transactions.models as _t

    assert _a is not None and _i is not None and _l is not None
    assert _s is not None and _t is not None
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "ledger", "shared", "transactions", "config", "identity"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE config.parameters (id TEXT PRIMARY KEY, "
                "key TEXT NOT NULL UNIQUE, value_json TEXT NOT NULL, "
                "module TEXT NOT NULL, description TEXT, updated_by TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
        )
    return engine


def _seed_param(session: Session, key: str, value: str, module: str = "transactions") -> None:
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, :module)"
        ),
        {"id": uuid.uuid4().hex, "key": key, "value": value, "module": module},
    )
    session.flush()


def _set_param(session: Session, key: str, value: int) -> None:
    session.execute(
        sa.text("UPDATE config.parameters SET value_json = :value WHERE key = :key"),
        {"value": str(value), "key": key},
    )
    session.flush()


@pytest.fixture()
def t19_session():
    """Sesion SQLite aislada con cuentas fondeadas y parametros E2-T18."""
    from app.modules.accounts import repository as accounts_repo

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        a1 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="00990001111", type="AHORRO", currency="PEN"
        )
        a2 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="00990002222", type="CORRIENTE", currency="PEN"
        )
        b1 = accounts_repo.create_account(
            session, user_id=USER_B, account_number="00880003333", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a1.id, available_delta=100_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a2.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, b1.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        _seed_param(session, THRESHOLD_KEY, "2000")
        _seed_param(session, DAILY_KEY, "500000")
        _seed_param(session, DAILY_STANDARD_KEY, "500000")
        _seed_param(session, MAX_ATTEMPTS_KEY, "5", module="identity")
        session.commit()
        yield {"session": session, "engine": engine, "a1": a1.id, "a2": a2.id, "b1": b1.id}
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def hmac_identity(monkeypatch):
    """Fachada `identity` simulada: binding en memoria + firma HMAC real.

    `get_device_binding` se parchea (sin tablas `identity` en la ruta feliz);
    `verify_signature` es la REAL (cripto de verdad con secreto conocido).
    """
    from app.modules.identity.service import device_login as device_login_service

    secret = bytes.fromhex("ab" * 16)
    bindings: dict[tuple[str, str], dict] = {}

    def _fake_binding(session, user_id, device_id):
        found = bindings.get((str(user_id), device_id))
        return dict(found) if found is not None else None

    monkeypatch.setattr(device_login_service, "get_device_binding", _fake_binding)
    return {"secret": secret, "bindings": bindings}


def _add_binding(hmac_identity, user_id, device_id=DEVICE_A, status="ACTIVE"):
    hmac_identity["bindings"][(str(user_id), device_id)] = {
        "device_id": device_id,
        "public_key": "hmac:" + "ab" * 16,
        "status": status,
    }


def _sign(secret: bytes, nonce: str) -> str:
    return hmac.new(secret, nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def _real_port():
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT

    return ACCOUNTS_BALANCE_PORT


def _avail(session, account_id) -> int:
    from app.modules.accounts import repository as accounts_repo

    return int(accounts_repo.get_balance(session, account_id).available_minor)


def _holds(session, tx_id):
    from app.modules.transactions import repository as tx_repo

    return list(tx_repo.list_holds_by_transaction(session, tx_id))


def _tx_entries(session, tx_id):
    from app.modules.ledger.models import JournalEntry

    return list(
        session.scalars(sa.select(JournalEntry).where(JournalEntry.transaction_id == tx_id)).all()
    )


def _entry_types(session, tx_id) -> list[str]:
    return sorted(e.entry_type for e in _tx_entries(session, tx_id))


def _security_events(session, tx_id) -> list[dict]:
    from app.modules.shared.models import OutboxEntry

    rows = session.scalars(sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx_id)).all()
    return [r.payload for r in rows if r.event_type == "auth.failed_attempt"]


def _third_party_pending(session, a1, b1, amount=5_000, **overrides):
    """Crea una transferencia sobre el umbral (-> `PENDING_AUTHORIZATION`)."""
    from app.modules.transactions import service as svc

    params = {
        "initiator_user_id": USER_A,
        "source_account_id": a1,
        "target_account_id": b1,
        "amount_minor": amount,
        "currency": "PEN",
        "fee_minor": 0,
        "idempotency_key": f"e2t19-{uuid.uuid4()}",
        "device_id": DEVICE_A,
        "balance_port": _real_port(),
    }
    params.update(overrides)
    return svc.transfer_third_party(session, **params)


def _nonce_of(tx_id, session) -> str:
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    tx = tx_repo.get_transaction(session, tx_id)
    return (bio_svc.get_block(tx) or {}).get("nonce") or ""


@contextlib.contextmanager
def _http(session, user_id, monkeypatch):
    """TestClient con BD y usuario inyectados (mismo patron que E2-T17)."""
    from fastapi.testclient import TestClient

    from app.core.db import get_db
    from app.main import app
    from app.modules.transactions.api import get_current_user_id

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")

    def _override_db():
        yield session

    def _override_user():
        return user_id

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = _override_user
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)


def _auth_headers(key=None):
    headers = {}
    headers["Idempotency-Key"] = key or f"e2t19-{uuid.uuid4()}"
    return headers


# ------------------------------------------------------- Camino feliz (CA-01)
def test_authorize_ok_settles_with_single_settlement(t19_session, hmac_identity, monkeypatch):
    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    _add_binding(hmac_identity, USER_A)
    tx = _third_party_pending(session, a1, b1)
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    nonce = _nonce_of(tx.id, session)
    body = {"device_id": DEVICE_A, "signature": _sign(hmac_identity["secret"], nonce)}
    with _http(session, USER_A, monkeypatch) as client:
        resp = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json=body,
            headers=_auth_headers(),
        )
    assert resp.status_code == 200, resp.text
    data, meta = resp.json()["data"], resp.json()["meta"]
    assert data["transaction_id"] == str(tx.id)
    assert data["status"] == "SETTLED"
    assert data["failure_code"] is None
    assert meta["replayed"] is False and meta["request_id"]
    session.expire_all()
    assert _entry_types(session, tx.id) == ["TRANSFER_HOLD", "TRANSFER_SETTLE"]
    assert len(_holds(session, tx.id)) == 1
    assert _avail(session, a1) == 100_000 - 5_000
    assert _avail(session, b1) == 50_000 + 5_000


def test_authorize_ok_with_nonce_echo(t19_session, hmac_identity, monkeypatch):
    """El `nonce` del cuerpo (eco del reto) no impide el exito si coincide."""
    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    _add_binding(hmac_identity, USER_A)
    tx = _third_party_pending(session, a1, b1)
    session.commit()
    nonce = _nonce_of(tx.id, session)
    body = {
        "device_id": DEVICE_A,
        "signature": _sign(hmac_identity["secret"], nonce),
        "nonce": nonce,
    }
    with _http(session, USER_A, monkeypatch) as client:
        resp = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json=body,
            headers=_auth_headers(),
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "SETTLED"


def test_authorize_ok_interbank_returns_tracking(t19_session, hmac_identity, monkeypatch):
    """La respuesta trae el nuevo estado y `tracking` en interbancaria."""
    from app.adapters.holder_lookup import MockHolderLookupProvider, mock_holder_name_for
    from app.adapters.interbank_gateway import MockInterbankGateway
    from app.modules.transactions.service import interbank as saga

    session, a1 = t19_session["session"], t19_session["a1"]
    _add_binding(hmac_identity, USER_A)
    cci = "00200100200100200101"
    tx = saga.transfer_interbank(
        session,
        initiator_user_id=USER_A,
        source_account_id=a1,
        amount_minor=10_000,
        currency="PEN",
        cci=cci,
        holder_name=mock_holder_name_for(cci),
        bank_code="BCP01",
        fee_minor=200,
        idempotency_key=f"e2t19-ib-{uuid.uuid4()}",
        device_id=DEVICE_A,
        gateway=MockInterbankGateway(default_send="confirm"),
        holder_provider=MockHolderLookupProvider(mode="success"),
        balance_port=_real_port(),
    )
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    nonce = _nonce_of(tx.id, session)
    body = {"device_id": DEVICE_A, "signature": _sign(hmac_identity["secret"], nonce)}
    with _http(session, USER_A, monkeypatch) as client:
        resp = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json=body,
            headers=_auth_headers(),
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    # El mock decide determinista por referencia: via rapida (`SETTLED`) o
    # aceptada (`POSTED`, sigue por polling). Ambas prueban la continuacion.
    assert data["status"] in ("POSTED", "SETTLED")
    assert data["tracking"]["poll_url"] == f"/api/v1/transactions/{tx.id}"


# ----------------------------------------------- Firma invalida (CA-04, OBS-3)
def test_invalid_signature_403_and_attempt_is_persisted(t19_session, hmac_identity, monkeypatch):
    """403 generico + el intento queda commiteado (OBS-3: no se pierde en rollback)."""
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    _add_binding(hmac_identity, USER_A)
    tx = _third_party_pending(session, a1, b1)
    session.commit()
    body = {"device_id": DEVICE_A, "signature": "00" * 32}
    with _http(session, USER_A, monkeypatch) as client:
        resp = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json=body,
            headers=_auth_headers(),
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"
    assert resp.json()["error"]["message"] == bio_svc.NOT_AUTHORIZED_MESSAGE
    # El fallo persiste aunque el HTTP sea error: lectura fresca.
    session.expire_all()
    from app.modules.transactions import repository as tx_repo

    fresh = tx_repo.get_transaction(session, tx.id)
    assert fresh.status == "PENDING_AUTHORIZATION"
    assert (bio_svc.get_block(fresh) or {})["failed_attempts"] == 1
    events = _security_events(session, tx.id)
    assert len(events) == 1 and events[0]["blocked"] is False
    assert _entry_types(session, tx.id) == ["TRANSFER_HOLD"]  # sin liquidar
    assert _avail(session, b1) == 50_000


def test_wrong_nonce_echo_counts_as_failed_attempt(t19_session, hmac_identity, monkeypatch):
    """Eco de `nonce` ajeno al reto vigente -> 403 generico con conteo."""
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    _add_binding(hmac_identity, USER_A)
    tx = _third_party_pending(session, a1, b1)
    session.commit()
    nonce = _nonce_of(tx.id, session)
    body = {
        "device_id": DEVICE_A,
        "signature": _sign(hmac_identity["secret"], nonce),
        "nonce": "nonce-ajeno-al-reto",
    }
    with _http(session, USER_A, monkeypatch) as client:
        resp = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json=body,
            headers=_auth_headers(),
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"
    session.expire_all()
    from app.modules.transactions import repository as tx_repo

    fresh = tx_repo.get_transaction(session, tx.id)
    assert fresh.status == "PENDING_AUTHORIZATION"
    assert (bio_svc.get_block(fresh) or {})["failed_attempts"] == 1


# ------------------------------------------------- Nonce expirado / reusado
def test_expired_nonce_400_and_attempt_is_persisted(t19_session, hmac_identity, monkeypatch):
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    _add_binding(hmac_identity, USER_A)
    tx = _third_party_pending(session, a1, b1)
    session.commit()
    stored = tx_repo.get_transaction(session, tx.id)
    block = bio_svc.get_block(stored)
    assert block is not None
    block["expires_at"] = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    bio_svc.save_block(session, tx_repo.get_transaction(session, tx.id), block)
    session.commit()
    body = {"device_id": DEVICE_A, "signature": _sign(hmac_identity["secret"], block["nonce"])}
    with _http(session, USER_A, monkeypatch) as client:
        resp = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json=body,
            headers=_auth_headers(),
        )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "EXPIRED_NONCE"
    session.expire_all()
    from app.modules.transactions import repository as tx_repo

    fresh = tx_repo.get_transaction(session, tx.id)
    assert (bio_svc.get_block(fresh) or {})["failed_attempts"] == 1
    events = _security_events(session, tx.id)
    assert len(events) == 1 and events[0]["reason"] == "expired"


def test_same_signature_after_success_with_new_key_is_conflict(
    t19_session, hmac_identity, monkeypatch
):
    """Misma firma con clave NUEVA tras el exito: 409 (ya no esta pendiente, sin repost)."""
    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    _add_binding(hmac_identity, USER_A)
    tx = _third_party_pending(session, a1, b1)
    session.commit()
    nonce = _nonce_of(tx.id, session)
    body = {"device_id": DEVICE_A, "signature": _sign(hmac_identity["secret"], nonce)}
    with _http(session, USER_A, monkeypatch) as client:
        first = client.post(
            f"/api/v1/transactions/{tx.id}/authorize", json=body, headers=_auth_headers("k-ok")
        )
        assert first.status_code == 200, first.text
        # Misma firma con clave NUEVA: el motor la reconoce (digest) y no re-liquida.
        second = client.post(
            f"/api/v1/transactions/{tx.id}/authorize", json=body, headers=_auth_headers("k-ok-2")
        )
    assert second.status_code == 409, second.text  # ya no esta pendiente
    session.expire_all()
    assert _entry_types(session, tx.id) == ["TRANSFER_HOLD", "TRANSFER_SETTLE"]


def test_other_signature_after_success_is_conflict(t19_session, hmac_identity, monkeypatch):
    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    _add_binding(hmac_identity, USER_A)
    tx = _third_party_pending(session, a1, b1)
    session.commit()
    nonce = _nonce_of(tx.id, session)
    good = {"device_id": DEVICE_A, "signature": _sign(hmac_identity["secret"], nonce)}
    with _http(session, USER_A, monkeypatch) as client:
        assert (
            client.post(
                f"/api/v1/transactions/{tx.id}/authorize", json=good, headers=_auth_headers()
            ).status_code
            == 200
        )
        bad = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"device_id": DEVICE_A, "signature": "ff" * 32},
            headers=_auth_headers(),
        )
    assert bad.status_code == 409, bad.text
    assert bad.json()["error"]["code"] == "CONFLICT"


# ------------------------------------------------- Estado / titular / forma
def test_not_pending_is_conflict(t19_session, hmac_identity, monkeypatch):
    """Transaccion ya `SETTLED` (bajo umbral) -> 409 sin consumir nada."""
    from app.modules.transactions import service as svc

    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    tx = svc.transfer_third_party(
        session,
        initiator_user_id=USER_A,
        source_account_id=a1,
        target_account_id=b1,
        amount_minor=1_000,
        currency="PEN",
        fee_minor=0,
        idempotency_key=f"e2t19-{uuid.uuid4()}",
        balance_port=_real_port(),
    )
    session.commit()
    assert tx.status == "SETTLED"
    with _http(session, USER_A, monkeypatch) as client:
        resp = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"device_id": DEVICE_A, "signature": "00" * 32},
            headers=_auth_headers(),
        )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CONFLICT"


def test_foreign_transaction_is_forbidden(t19_session, hmac_identity, monkeypatch):
    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    # Origen de USER_B (b1) con titular USER_B: el titular USER_A es ajeno.
    tx = _third_party_pending(session, b1, a1, initiator_user_id=USER_B)
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    with _http(session, USER_A, monkeypatch) as client:
        resp = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"device_id": DEVICE_A, "signature": "00" * 32},
            headers=_auth_headers(),
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"


def test_missing_and_malformed_id_is_not_found(t19_session, hmac_identity, monkeypatch):
    session = t19_session["session"]
    with _http(session, USER_A, monkeypatch) as client:
        missing = client.post(
            f"/api/v1/transactions/{uuid.uuid4()}/authorize",
            json={"device_id": DEVICE_A, "signature": "00" * 32},
            headers=_auth_headers(),
        )
        malformed = client.post(
            "/api/v1/transactions/no-es-uuid/authorize",
            json={"device_id": DEVICE_A, "signature": "00" * 32},
            headers=_auth_headers(),
        )
    assert missing.status_code == 404, missing.text
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    assert malformed.status_code == 404, malformed.text
    assert malformed.json()["error"]["code"] == "NOT_FOUND"


def test_missing_idempotency_key_is_422(t19_session, hmac_identity, monkeypatch):
    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    _add_binding(hmac_identity, USER_A)
    tx = _third_party_pending(session, a1, b1)
    session.commit()
    nonce = _nonce_of(tx.id, session)
    with _http(session, USER_A, monkeypatch) as client:
        resp = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"device_id": DEVICE_A, "signature": _sign(hmac_identity["secret"], nonce)},
        )
    assert resp.status_code == 422, resp.text


def test_malformed_body_is_422(t19_session, hmac_identity, monkeypatch):
    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    tx = _third_party_pending(session, a1, b1)
    session.commit()
    with _http(session, USER_A, monkeypatch) as client:
        resp = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"device_id": "", "signature": ""},
            headers=_auth_headers(),
        )
    assert resp.status_code == 422, resp.text


# ------------------------------------------------- Idempotencia de dinero
def test_same_key_same_body_replays_without_reposting(t19_session, hmac_identity, monkeypatch):
    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    _add_binding(hmac_identity, USER_A)
    tx = _third_party_pending(session, a1, b1)
    session.commit()
    nonce = _nonce_of(tx.id, session)
    body = {"device_id": DEVICE_A, "signature": _sign(hmac_identity["secret"], nonce)}
    with _http(session, USER_A, monkeypatch) as client:
        first = client.post(
            f"/api/v1/transactions/{tx.id}/authorize", json=body, headers=_auth_headers("k-re")
        )
        second = client.post(
            f"/api/v1/transactions/{tx.id}/authorize", json=body, headers=_auth_headers("k-re")
        )
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["data"] == second.json()["data"]
    assert first.json()["meta"]["replayed"] is False
    assert second.json()["meta"]["replayed"] is True
    session.expire_all()
    assert _entry_types(session, tx.id) == ["TRANSFER_HOLD", "TRANSFER_SETTLE"]
    assert len(_holds(session, tx.id)) == 1


def test_same_key_different_body_is_conflict(t19_session, hmac_identity, monkeypatch):
    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    _add_binding(hmac_identity, USER_A)
    tx = _third_party_pending(session, a1, b1)
    session.commit()
    nonce = _nonce_of(tx.id, session)
    good = {"device_id": DEVICE_A, "signature": _sign(hmac_identity["secret"], nonce)}
    other = {"device_id": DEVICE_A, "signature": "ff" * 32}
    with _http(session, USER_A, monkeypatch) as client:
        first = client.post(
            f"/api/v1/transactions/{tx.id}/authorize", json=good, headers=_auth_headers("k-cf")
        )
        assert first.status_code == 200, first.text
        second = client.post(
            f"/api/v1/transactions/{tx.id}/authorize", json=other, headers=_auth_headers("k-cf")
        )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "CONFLICT"


def test_same_key_same_bad_body_replays_error_without_new_attempt(
    t19_session, hmac_identity, monkeypatch
):
    """El fallo tambien es idempotente: no cuenta otro intento."""
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    _add_binding(hmac_identity, USER_A)
    tx = _third_party_pending(session, a1, b1)
    session.commit()
    bad = {"device_id": DEVICE_A, "signature": "00" * 32}
    with _http(session, USER_A, monkeypatch) as client:
        first = client.post(
            f"/api/v1/transactions/{tx.id}/authorize", json=bad, headers=_auth_headers("k-bad")
        )
        second = client.post(
            f"/api/v1/transactions/{tx.id}/authorize", json=bad, headers=_auth_headers("k-bad")
        )
    assert first.status_code == 403 and second.status_code == 403
    assert first.json() == second.json()
    session.expire_all()
    fresh = tx_repo.get_transaction(session, tx.id)
    assert (bio_svc.get_block(fresh) or {})["failed_attempts"] == 1


# --------------------------------------- Limite por perfil (OBS-1, CA-03)
def test_per_profile_daily_limit_applies_end_to_end(t19_session, hmac_identity, monkeypatch):
    """Un perfil distinto de STANDARD rige su propio limite (no el generico).

    `identity.users.risk_profile=PREMIUM` + `transfer.daily_limit_PREMIUM_minor`
    bajo: el alta HTTP resuelve el perfil del titular y rechaza
    `DAILY_LIMIT_EXCEEDED`; un STANDARD con el mismo monto liquida.
    """
    from app.modules.identity import repository as identity_repo

    session = t19_session["session"]
    _set_param(session, THRESHOLD_KEY, 1_000_000)  # sin biometria en este test
    _seed_param(session, DAILY_PREMIUM_KEY, "3000")
    session.commit()

    premium = identity_repo.create_user(
        session,
        doc_type="DNI",
        doc_number_hash="h-premium-e2t19",
        first_name="Ana",
        last_name="Premium",
        email="premium-e2t19@test.xx",
        risk_profile="PREMIUM",
    )
    standard = identity_repo.create_user(
        session,
        doc_type="DNI",
        doc_number_hash="h-standard-e2t19",
        first_name="Ben",
        last_name="Standard",
        email="standard-e2t19@test.xx",
    )
    session.commit()

    from app.modules.accounts import repository as accounts_repo

    p_src = accounts_repo.create_account(
        session, user_id=premium.id, account_number="00990009991", type="AHORRO", currency="PEN"
    )
    p_dst = accounts_repo.create_account(
        session, user_id=USER_B, account_number="00990009992", type="AHORRO", currency="PEN"
    )
    s_src = accounts_repo.create_account(
        session, user_id=standard.id, account_number="00990009993", type="AHORRO", currency="PEN"
    )
    s_dst = accounts_repo.create_account(
        session, user_id=USER_B, account_number="00990009994", type="AHORRO", currency="PEN"
    )
    accounts_repo.apply_delta(
        session, p_src.id, available_delta=100_000, held_delta=0, currency="PEN"
    )
    accounts_repo.apply_delta(
        session, s_src.id, available_delta=100_000, held_delta=0, currency="PEN"
    )
    session.commit()

    def _transfer(client, src, dst, amount):
        return client.post(
            "/api/v1/transfers/third-party",
            json={
                "source_account_id": str(src),
                "target_account_id": str(dst),
                "amount_minor": amount,
                "currency": "PEN",
                "fee_minor": 0,
            },
            headers=_auth_headers(),
        )

    with _http(session, premium.id, monkeypatch) as client:
        rejected = _transfer(client, p_src.id, p_dst.id, 5_000)
    assert rejected.status_code == 201, rejected.text
    assert rejected.json()["data"]["status"] == "REJECTED"
    assert rejected.json()["data"]["failure_code"] == "DAILY_LIMIT_EXCEEDED"

    with _http(session, standard.id, monkeypatch) as client:
        settled = _transfer(client, s_src.id, s_dst.id, 5_000)
    assert settled.status_code == 201, settled.text
    assert settled.json()["data"]["status"] == "SETTLED"
    assert settled.json()["data"]["failure_code"] is None


def test_resolve_risk_profile_fallbacks():
    """Sin tabla/usuario/perfil valido rige el generico (`None`)."""
    from app.modules.transactions.service import biometric as bio_svc

    assert bio_svc.resolve_risk_profile(session=None, user_id=None) is None  # type: ignore[arg-type]
    assert bio_svc.resolve_risk_profile(session=None, user_id="no-es-uuid") is None  # type: ignore[arg-type]


# --------------------------------------- Bloqueo preventivo (CA-02, OBS-3)
def test_preventive_block_persists_and_releases_hold(t19_session, hmac_identity, monkeypatch):
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    _set_param(session, MAX_ATTEMPTS_KEY, 2)
    session.commit()
    _add_binding(hmac_identity, USER_A)
    tx = _third_party_pending(session, a1, b1)
    session.commit()
    bad = {"device_id": DEVICE_A, "signature": "00" * 32}
    with _http(session, USER_A, monkeypatch) as client:
        first = client.post(
            f"/api/v1/transactions/{tx.id}/authorize", json=bad, headers=_auth_headers("k-b1")
        )
        second = client.post(
            f"/api/v1/transactions/{tx.id}/authorize", json=bad, headers=_auth_headers("k-b2")
        )
    assert first.status_code == 403, first.text
    assert second.status_code == 200, second.text
    assert second.json()["data"]["status"] == "REJECTED"
    assert second.json()["data"]["failure_code"] == "BIOMETRIC_PREVENTIVE_BLOCK"
    # Persistido: lectura fresca tras los HTTP de error/exito.
    session.expire_all()
    fresh = tx_repo.get_transaction(session, tx.id)
    assert fresh is not None and fresh.status == "REJECTED"
    assert fresh.failure_reason == bio_svc.PREVENTIVE_BLOCK
    assert (fresh.meta or {}).get("failure_code") == bio_svc.PREVENTIVE_BLOCK
    assert (bio_svc.get_block(fresh) or {})["failed_attempts"] == 2
    holds = _holds(session, tx.id)
    assert len(holds) == 1 and holds[0].status == "RELEASED"
    assert _avail(session, a1) == 100_000  # retenido devuelto
    assert _avail(session, b1) == 50_000  # destino intacto
    assert [e for e in _entry_types(session, tx.id) if e == "TRANSFER_SETTLE"] == []
    events = _security_events(session, tx.id)
    assert len(events) == 2 and events[-1]["blocked"] is True
    # Tras el bloqueo ya no autoriza: 409.
    with _http(session, USER_A, monkeypatch) as client:
        again = client.post(
            f"/api/v1/transactions/{tx.id}/authorize", json=bad, headers=_auth_headers("k-b3")
        )
    assert again.status_code == 409, again.text


# ------------------------------------------------- Concurrencia (sin doble asiento)
def test_concurrent_double_authorize_settles_once(t19_session, hmac_identity):
    """Doble `authorize` concurrente a nivel motor: un solo `TRANSFER_SETTLE`."""
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    session, engine = t19_session["session"], t19_session["engine"]
    a1, b1 = t19_session["a1"], t19_session["b1"]
    secret, real_bindings = hmac_identity["secret"], hmac_identity["bindings"]
    real_bindings[(str(USER_A), DEVICE_A)] = {
        "device_id": DEVICE_A,
        "public_key": "hmac:" + "ab" * 16,
        "status": "ACTIVE",
    }
    tx = _third_party_pending(session, a1, b1)
    session.commit()
    tx_id = tx.id
    nonce = _nonce_of(tx_id, session)
    signature = _sign(secret, nonce)
    gate = threading.Lock()
    start = threading.Barrier(2)
    results: list = []
    errors: list = []

    def _run():
        worker = Session(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            start.wait(timeout=30)
            with gate:
                done = bio_svc.authorize_transaction(
                    worker,
                    tx_id,
                    device_id=DEVICE_A,
                    signature=signature,
                )
                worker.commit()
                results.append((done.id, done.status))
        except Exception as exc:  # noqa: BLE001 - carrera: replay o CAS perdedor
            errors.append(type(exc).__name__)
            try:
                worker.rollback()
            except Exception:  # noqa: BLE001, S110 - best-effort
                pass
        finally:
            worker.close()

    # El motor usa la fachada real: el worker necesita el binding. Como el
    # parche vive en el modulo (proceso), ambos hilos lo ven.
    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    session.expire_all()
    assert len(results) + len(errors) == 2
    assert {r[0] for r in results} <= {tx_id}
    assert _entry_types(session, tx_id).count("TRANSFER_SETTLE") <= 1
    assert len(_holds(session, tx_id)) == 1
    _ = tx_repo.get_transaction(session, tx_id)


# ------------------------------------------------- Frontera (sin modelos ajenos)
def test_facade_boundary_static():
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "app" / "modules" / "transactions"
    api_src = (base / "api" / "__init__.py").read_text(encoding="utf-8")
    schemas_src = (base / "schemas" / "__init__.py").read_text(encoding="utf-8")
    bio_src = (base / "service" / "__init__.py").read_text(encoding="utf-8")
    bio_src += (base / "service" / "biometric.py").read_text(encoding="utf-8")
    for token in ("identity.models", "identity.repository", "risk.models"):
        assert token not in api_src, f"frontera rota en api: {token}"
        assert token not in schemas_src, f"frontera rota en schemas: {token}"
        assert token not in bio_src, f"frontera rota en service: {token}"


# ------------------------- Rotacion del reto tras `EXPIRED_NONCE` (fix E2-T21)
def test_challenge_rotates_nonce_and_preserves_attempts(t19_session, hmac_identity, monkeypatch):
    """`POST .../authorize/challenge` emite un `nonce` NUEVO sin contar intento."""
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    _add_binding(hmac_identity, USER_A)
    tx = _third_party_pending(session, a1, b1)
    session.commit()
    before = _nonce_of(tx.id, session)
    with _http(session, USER_A, monkeypatch) as client:
        resp = client.post(f"/api/v1/transactions/{tx.id}/authorize/challenge")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["transaction_id"] == str(tx.id)
    assert data["nonce"] and data["nonce"] != before
    assert data["expires_in"] > 0
    session.expire_all()
    fresh = tx_repo.get_transaction(session, tx.id)
    assert fresh.status == "PENDING_AUTHORIZATION"
    assert (bio_svc.get_block(fresh) or {})["nonce"] == data["nonce"]
    assert (bio_svc.get_block(fresh) or {})["failed_attempts"] == 0


def test_challenge_rejects_when_not_pending_or_foreign(t19_session, hmac_identity, monkeypatch):
    """Sin espera no hay rotacion: `SETTLED` -> 409, ajena -> 403, inexistente -> 404."""
    from app.modules.transactions import service as svc

    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    settled = svc.transfer_third_party(
        session,
        initiator_user_id=USER_A,
        source_account_id=a1,
        target_account_id=b1,
        amount_minor=1_000,
        currency="PEN",
        fee_minor=0,
        idempotency_key=f"e2t19-{uuid.uuid4()}",
        balance_port=_real_port(),
    )
    session.commit()
    assert settled.status == "SETTLED"
    foreign = _third_party_pending(session, b1, a1, initiator_user_id=USER_B)
    session.commit()
    with _http(session, USER_A, monkeypatch) as client:
        assert (
            client.post(f"/api/v1/transactions/{settled.id}/authorize/challenge").status_code == 409
        )
        assert (
            client.post(f"/api/v1/transactions/{foreign.id}/authorize/challenge").status_code == 403
        )
        assert (
            client.post(f"/api/v1/transactions/{uuid.uuid4()}/authorize/challenge").status_code
            == 404
        )


def test_expired_nonce_retry_with_fresh_challenge_and_new_key_settles(
    t19_session, hmac_identity, monkeypatch
):
    """Flujo real `authorize` -> `EXPIRED_NONCE` -> challenge -> reintento: autoriza.

    Regresion del fix E2-T21: con el `nonce` expirado el primer `authorize`
    (clave `k-1`) responde `400 EXPIRED_NONCE`; tras rotar el reto, el
    reintento con el `nonce` nuevo y una clave NUEVA (`k-2`, la anterior
    snapshotteo el fallo: misma clave + cuerpo distinto = `409`) liquida.
    """
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = t19_session["session"], t19_session["a1"], t19_session["b1"]
    _add_binding(hmac_identity, USER_A)
    tx = _third_party_pending(session, a1, b1)
    session.commit()
    stored = tx_repo.get_transaction(session, tx.id)
    block = bio_svc.get_block(stored)
    assert block is not None
    block["expires_at"] = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    bio_svc.save_block(session, tx_repo.get_transaction(session, tx.id), block)
    session.commit()
    old_body = {
        "device_id": DEVICE_A,
        "signature": _sign(hmac_identity["secret"], block["nonce"]),
    }
    with _http(session, USER_A, monkeypatch) as client:
        expired = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json=old_body,
            headers=_auth_headers("k-fix-1"),
        )
        assert expired.status_code == 400, expired.text
        assert expired.json()["error"]["code"] == "EXPIRED_NONCE"
        fresh = client.post(f"/api/v1/transactions/{tx.id}/authorize/challenge")
        assert fresh.status_code == 200, fresh.text
        fresh_nonce = fresh.json()["data"]["nonce"]
        assert fresh_nonce != block["nonce"]
        # Misma clave + cuerpo distinto (nonce/firma nuevos) = 409: por eso
        # el reintento exige clave nueva.
        conflict = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={
                "device_id": DEVICE_A,
                "signature": _sign(hmac_identity["secret"], fresh_nonce),
                "nonce": fresh_nonce,
            },
            headers=_auth_headers("k-fix-1"),
        )
        assert conflict.status_code == 409, conflict.text
        retry = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={
                "device_id": DEVICE_A,
                "signature": _sign(hmac_identity["secret"], fresh_nonce),
                "nonce": fresh_nonce,
            },
            headers=_auth_headers("k-fix-2"),
        )
    assert retry.status_code == 200, retry.text
    assert retry.json()["data"]["status"] == "SETTLED"
    session.expire_all()
    assert _entry_types(session, tx.id) == ["TRANSFER_HOLD", "TRANSFER_SETTLE"]
