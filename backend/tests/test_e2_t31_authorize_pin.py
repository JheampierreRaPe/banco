"""Autorizar por PIN como alternativa a la firma (E2-T31, HU08).

Sobre el motor y el ledger con adaptadores reales (`ACCOUNTS_BALANCE_PORT`,
cuentas por `accounts.repository`), fachada real de parametros
(`shared.config`, E2-T18) y fachada REAL de PIN de `identity`
(`pin_authorization.verify_holder_pin` sin parche, con credencial real en
`identity.credentials`): dinero de juguete (UUIDs sinteticos, sin PII),
SQLite en memoria + schemas ATTACH, sin red, sin `float`.

Mapeo brief -> test:
- feliz: `PENDING_AUTHORIZATION` + PIN correcto -> `SETTLED` .. `test_pin_ok_*`
- PIN incorrecto -> 403 + intento persistido ............. `test_pin_wrong_*`
- sin credencial -> mismo 403 (sin enumeracion) ......... `test_pin_no_credential_*`
- N fallos -> `REJECTED` + `BIOMETRIC_PREVENTIVE_BLOCK` . `test_pin_block_*`
- exclusion mutua / validacion .......................... `test_pin_validation_*`
- `nonce` ignorado en la via PIN ........................ `test_pin_ignores_nonce`
- ajena -> 403 / inexistente -> 404 ..................... `test_pin_foreign_*`
- firma intacta (regresion E2-T19) ...................... `test_signature_*`
- idempotencia + digest sin PIN en snapshot ............. `test_pin_idempotency_*`
- concurrencia: doble `authorize` liquida una vez ...... `test_pin_concurrent_*`
- frontera (sin modelos ajenos) ......................... `test_facade_*`
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

HOLDER = uuid.uuid4()
USER_B = uuid.uuid4()
DEVICE_A = "dev-e2t31-1"
PIN = "4829"
WRONG_PIN = "0000"

THRESHOLD_KEY = "transfer.biometric_threshold_minor"
DAILY_KEY = "transfer.daily_limit_minor"
DAILY_STANDARD_KEY = "transfer.daily_limit_STANDARD_minor"
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
    "identity.credentials",
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
def t31_session():
    """Sesion SQLite aislada con titular + PIN real y cuentas fondeadas."""
    from app.modules.accounts import repository as accounts_repo
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import pin_login as pin_login_service

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        holder = identity_repo.create_user(
            session,
            doc_type="DNI",
            doc_number_hash="h-holder-e2t31",
            first_name="Rosa",
            last_name="Pin",
            email="holder-e2t31@test.xx",
        )
        assert holder.id is not None  # id generado por la BD; se usa el real
        identity_repo.create_credential(
            session, holder.id, pin_hash=pin_login_service.hash_pin(PIN)
        )
        src = accounts_repo.create_account(
            session, user_id=holder.id, account_number="00991001111", type="AHORRO", currency="PEN"
        )
        dst = accounts_repo.create_account(
            session,
            user_id=holder.id,
            account_number="00991002222",
            type="CORRIENTE",
            currency="PEN",
        )
        other = accounts_repo.create_account(
            session, user_id=USER_B, account_number="00881003333", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, src.id, available_delta=100_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, dst.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, other.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        _seed_param(session, THRESHOLD_KEY, "2000")
        _seed_param(session, DAILY_KEY, "500000")
        _seed_param(session, DAILY_STANDARD_KEY, "500000")
        _seed_param(session, MAX_ATTEMPTS_KEY, "5", module="identity")
        session.commit()
        yield {
            "session": session,
            "engine": engine,
            "holder": holder.id,
            "src": src.id,
            "dst": dst.id,
            "other": other.id,
        }
    finally:
        session.close()
        engine.dispose()


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


def _third_party_pending(session, holder, src, other, amount=5_000, **overrides):
    """Crea una transferencia sobre el umbral (-> `PENDING_AUTHORIZATION`)."""
    from app.modules.transactions import service as svc

    params = {
        "initiator_user_id": holder,
        "source_account_id": src,
        "target_account_id": other,
        "amount_minor": amount,
        "currency": "PEN",
        "fee_minor": 0,
        "idempotency_key": f"e2t31-{uuid.uuid4()}",
        "device_id": DEVICE_A,
        "balance_port": _real_port(),
    }
    params.update(overrides)
    return svc.transfer_third_party(session, **params)


def _own_pending(session, holder, src, dst, amount=5_000, **overrides):
    """Propia atomica sobre el umbral (-> `PENDING_AUTHORIZATION`, sin hold)."""
    from app.modules.transactions import service as svc

    params = {
        "initiator_user_id": holder,
        "source_account_id": src,
        "target_account_id": dst,
        "amount_minor": amount,
        "currency": "PEN",
        "idempotency_key": f"e2t31-own-{uuid.uuid4()}",
        "device_id": DEVICE_A,
        "balance_port": _real_port(),
    }
    params.update(overrides)
    return svc.transfer_own(session, **params)


@contextlib.contextmanager
def _http(session, user_id, monkeypatch):
    """TestClient con BD y usuario inyectados (mismo patron que E2-T19)."""
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


_HttpCtx = _http


def _auth_headers(key=None):
    return {"Idempotency-Key": key or f"e2t31-{uuid.uuid4()}"}


# ------------------------------------------------------- Camino feliz (CA-01)
def test_pin_ok_settles_with_single_settlement(t31_session, monkeypatch):
    session = t31_session["session"]
    holder, src, other = t31_session["holder"], t31_session["src"], t31_session["other"]
    tx = _third_party_pending(session, holder, src, other)
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    with _HttpCtx(session, holder, monkeypatch) as client:
        resp = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"pin": PIN},
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
    assert _avail(session, src) == 100_000 - 5_000
    assert _avail(session, other) == 50_000 + 5_000


def test_pin_ok_own_atomic_settles(t31_session, monkeypatch):
    """Propia atomica: el PIN continua sin hold y liquida una vez."""
    session = t31_session["session"]
    holder, src, dst = t31_session["holder"], t31_session["src"], t31_session["dst"]
    tx = _own_pending(session, holder, src, dst)
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    with _HttpCtx(session, holder, monkeypatch) as client:
        resp = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"pin": PIN},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "SETTLED"
    session.expire_all()
    assert _avail(session, src) == 100_000 - 5_000
    assert _avail(session, dst) == 50_000 + 5_000
    assert len(_holds(session, tx.id)) == 0


# ------------------------------------------- PIN incorrecto (CA-02, sin mover)
def test_pin_wrong_403_no_money_attempt_event(t31_session, monkeypatch):
    """403 generico + intento contado + evento, sin tocar saldos."""
    from app.modules.transactions.service import biometric as bio_svc

    session = t31_session["session"]
    holder, src, other = t31_session["holder"], t31_session["src"], t31_session["other"]
    tx = _third_party_pending(session, holder, src, other)
    session.commit()
    with _HttpCtx(session, holder, monkeypatch) as client:
        resp = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"pin": WRONG_PIN},
            headers=_auth_headers(),
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"
    assert resp.json()["error"]["message"] == bio_svc.NOT_AUTHORIZED_MESSAGE
    session.expire_all()
    # Sin liquidar: el hold previo sigue ACTIVE (disponible retenido) y no
    # hay asiento de settle.
    assert _avail(session, src) == 100_000 - 5_000
    assert _avail(session, other) == 50_000
    assert [e.entry_type for e in _tx_entries(session, tx.id)] == ["TRANSFER_HOLD"]
    holds = _holds(session, tx.id)
    assert len(holds) == 1 and holds[0].status == "ACTIVE"
    from app.modules.transactions import repository as tx_repo

    fresh = tx_repo.get_transaction(session, tx.id)
    assert fresh is not None and fresh.status == "PENDING_AUTHORIZATION"
    assert (bio_svc.get_block(fresh) or {})["failed_attempts"] == 1
    events = _security_events(session, tx.id)
    assert len(events) == 1 and events[0]["blocked"] is False
    assert "pin" not in json.dumps(events[0]).lower()


def test_pin_no_credential_same_403_without_oracle(t31_session, monkeypatch):
    """Titular sin credencial: mismo 403 generico (sin enumeracion)."""
    from app.modules.transactions.service import biometric as bio_svc

    session = t31_session["session"]
    holder, src, other = t31_session["holder"], t31_session["src"], t31_session["other"]
    from app.modules.identity import repository as identity_repo

    identity_repo.get_credential(session, holder).pin_hash = None
    session.commit()
    tx = _third_party_pending(session, holder, src, other)
    session.commit()
    with _HttpCtx(session, holder, monkeypatch) as client:
        resp = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"pin": PIN},
            headers=_auth_headers(),
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"
    assert resp.json()["error"]["message"] == bio_svc.NOT_AUTHORIZED_MESSAGE
    session.expire_all()
    assert _avail(session, src) == 100_000 - 5_000  # hold previo intacto
    assert [e.entry_type for e in _tx_entries(session, tx.id)] == ["TRANSFER_HOLD"]


# --------------------------------------- Bloqueo preventivo por PIN (CA-02)
def test_pin_block_after_max_attempts_releases_hold(t31_session, monkeypatch):
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    session = t31_session["session"]
    holder, src, other = t31_session["holder"], t31_session["src"], t31_session["other"]
    _set_param(session, MAX_ATTEMPTS_KEY, 2)
    session.commit()
    tx = _third_party_pending(session, holder, src, other)
    session.commit()
    with _HttpCtx(session, holder, monkeypatch) as client:
        first = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"pin": WRONG_PIN},
            headers=_auth_headers("k-pin-b1"),
        )
        second = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"pin": WRONG_PIN},
            headers=_auth_headers("k-pin-b2"),
        )
    assert first.status_code == 403, first.text
    assert second.status_code == 200, second.text
    assert second.json()["data"]["status"] == "REJECTED"
    assert second.json()["data"]["failure_code"] == "BIOMETRIC_PREVENTIVE_BLOCK"
    session.expire_all()
    fresh = tx_repo.get_transaction(session, tx.id)
    assert fresh is not None and fresh.status == "REJECTED"
    assert fresh.failure_reason == bio_svc.PREVENTIVE_BLOCK
    assert (bio_svc.get_block(fresh) or {})["failed_attempts"] == 2
    holds = _holds(session, tx.id)
    assert len(holds) == 1 and holds[0].status == "RELEASED"
    assert _avail(session, src) == 100_000
    assert _avail(session, other) == 50_000
    events = _security_events(session, tx.id)
    assert len(events) == 2 and events[-1]["blocked"] is True
    with _HttpCtx(session, holder, monkeypatch) as client:
        again = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"pin": PIN},
            headers=_auth_headers("k-pin-b3"),
        )
    assert again.status_code == 409, again.text


# --------------------------------------- Validacion (mutua exclusion, CA-05)
def test_pin_validation_exactly_one_factor(t31_session, monkeypatch):
    session = t31_session["session"]
    holder, src, other = t31_session["holder"], t31_session["src"], t31_session["other"]
    tx = _third_party_pending(session, holder, src, other)
    session.commit()
    bodies = [
        {"device_id": DEVICE_A, "signature": "ab" * 32, "pin": PIN},  # ambos
        {},  # ninguno
        {"signature": "ab" * 32},  # firma sin device_id
    ]
    with _HttpCtx(session, holder, monkeypatch) as client:
        for i, body in enumerate(bodies):
            resp = client.post(
                f"/api/v1/transactions/{tx.id}/authorize",
                json=body,
                headers=_auth_headers(f"k-pin-v{i}"),
            )
            assert resp.status_code == 422, (body, resp.text)


def test_pin_ignores_nonce_and_device(t31_session, monkeypatch):
    """`nonce`/`device_id` con PIN se ignoran de forma neutra (sin 422)."""
    session = t31_session["session"]
    holder, src, other = t31_session["holder"], t31_session["src"], t31_session["other"]
    tx = _third_party_pending(session, holder, src, other)
    session.commit()
    with _HttpCtx(session, holder, monkeypatch) as client:
        resp = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"pin": PIN, "nonce": "nonce-ajeno", "device_id": DEVICE_A},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "SETTLED"


def test_pin_foreign_and_missing(t31_session, monkeypatch):
    session = t31_session["session"]
    holder, src, other = t31_session["holder"], t31_session["src"], t31_session["other"]
    tx = _third_party_pending(session, holder, src, other)
    session.commit()
    with _HttpCtx(session, USER_B, monkeypatch) as client:
        foreign = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"pin": PIN},
            headers=_auth_headers(),
        )
    assert foreign.status_code == 403, foreign.text
    with _HttpCtx(session, holder, monkeypatch) as client:
        missing = client.post(
            f"/api/v1/transactions/{uuid.uuid4()}/authorize",
            json={"pin": PIN},
            headers=_auth_headers(),
        )
    assert missing.status_code == 404, missing.text


# --------------------------------------- Regresion de firma (CA-03, E2-T19)
def test_signature_still_works(t31_session, monkeypatch):
    """La via por firma sigue intacta tras agregar el PIN."""
    import hashlib as _hashlib
    import hmac as _hmac

    from app.modules.identity.service import device_login as device_login_service

    session = t31_session["session"]
    holder, src, other = t31_session["holder"], t31_session["src"], t31_session["other"]
    tx = _third_party_pending(session, holder, src, other)
    session.commit()
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    stored = tx_repo.get_transaction(session, tx.id)
    nonce = (bio_svc.get_block(stored) or {}).get("nonce") or ""
    secret = bytes.fromhex("ab" * 16)
    real_binding = device_login_service.get_device_binding

    def _fake_binding(_session, _user_id, _device_id):
        return {"device_id": DEVICE_A, "public_key": "hmac:" + "ab" * 16, "status": "ACTIVE"}

    monkeypatch.setattr(device_login_service, "get_device_binding", _fake_binding)
    try:
        signature = _hmac.new(secret, nonce.encode("utf-8"), _hashlib.sha256).hexdigest()
        with _HttpCtx(session, holder, monkeypatch) as client:
            resp = client.post(
                f"/api/v1/transactions/{tx.id}/authorize",
                json={"device_id": DEVICE_A, "signature": signature},
                headers=_auth_headers(),
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["status"] == "SETTLED"
    finally:
        monkeypatch.setattr(device_login_service, "get_device_binding", real_binding)


# --------------------------------------- Idempotencia (CA-05, sin PIN guardado)
def test_pin_idempotency_replay_and_no_pin_in_snapshot(t31_session, monkeypatch):
    from app.modules.shared.models import IdempotencyKey
    from app.modules.shared.repository.idempotency import compute_request_hash

    session = t31_session["session"]
    holder, src, other = t31_session["holder"], t31_session["src"], t31_session["other"]
    tx = _third_party_pending(session, holder, src, other)
    session.commit()
    key = "k-pin-idem-1"
    with _HttpCtx(session, holder, monkeypatch) as client:
        first = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"pin": PIN},
            headers=_auth_headers(key),
        )
        second = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"pin": PIN},
            headers=_auth_headers(key),
        )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["meta"]["replayed"] is True
    assert second.json()["data"] == first.json()["data"]
    session.expire_all()
    assert _entry_types(session, tx.id).count("TRANSFER_SETTLE") == 1
    row = session.scalars(
        sa.select(IdempotencyKey).where(IdempotencyKey.key == key, IdempotencyKey.user_id == holder)
    ).one()
    snapshot_raw = json.dumps(row.response_snapshot, ensure_ascii=False)
    assert PIN not in snapshot_raw
    assert WRONG_PIN not in snapshot_raw
    expected_payload = {
        "transaction_id": str(tx.id),
        "pin_digest": hashlib.sha256(PIN.encode("utf-8")).hexdigest(),
    }
    assert row.request_hash == compute_request_hash(expected_payload)


def test_pin_idempotency_conflict_on_different_pin(t31_session, monkeypatch):
    session = t31_session["session"]
    holder, src, other = t31_session["holder"], t31_session["src"], t31_session["other"]
    tx = _third_party_pending(session, holder, src, other)
    session.commit()
    key = "k-pin-idem-2"
    with _HttpCtx(session, holder, monkeypatch) as client:
        first = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"pin": PIN},
            headers=_auth_headers(key),
        )
        conflict = client.post(
            f"/api/v1/transactions/{tx.id}/authorize",
            json={"pin": WRONG_PIN},
            headers=_auth_headers(key),
        )
    assert first.status_code == 200, first.text
    assert conflict.status_code == 409, conflict.text


# --------------------------------------- Concurrencia (un solo asiento)
def test_pin_concurrent_double_authorize_settles_once(t31_session):
    """Doble `authorize` con PIN concurrente a nivel motor: un solo settle."""
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    session, engine = t31_session["session"], t31_session["engine"]
    holder, src, other = t31_session["holder"], t31_session["src"], t31_session["other"]
    tx = _third_party_pending(session, holder, src, other)
    session.commit()
    tx_id = tx.id
    gate = threading.Lock()
    start = threading.Barrier(2)
    results: list = []
    errors: list = []

    def _run():
        worker = Session(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            start.wait(timeout=30)
            with gate:
                done = bio_svc.authorize_transaction_with_pin(worker, tx_id, pin=PIN)
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


# --------------------------------------- Frontera (sin modelos ajenos)
def test_facade_boundary_static():
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "app" / "modules" / "transactions"
    api_src = (base / "api" / "__init__.py").read_text(encoding="utf-8")
    schemas_src = (base / "schemas" / "__init__.py").read_text(encoding="utf-8")
    bio_src = (base / "service" / "biometric.py").read_text(encoding="utf-8")
    for token in ("identity.models", "identity.repository", "risk.models"):
        assert token not in api_src, f"frontera rota en api: {token}"
        assert token not in schemas_src, f"frontera rota en schemas: {token}"
        assert token not in bio_src, f"frontera rota en service: {token}"
    facade_src = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "modules"
        / "identity"
        / "service"
        / "pin_authorization.py"
    ).read_text(encoding="utf-8")
    assert "verify_holder_pin" in facade_src
