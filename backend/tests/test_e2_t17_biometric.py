"""Integracion motor <-> autorizacion biometrica del dispositivo (E2-T17, HU08).

Sobre el motor y el ledger con adaptadores reales (`ACCOUNTS_BALANCE_PORT`,
cuentas por `accounts.repository`) y fachada real de parametros
(`shared.config`, E2-T18): dinero de juguete (UUIDs sinteticos, sin PII),
SQLite en memoria + schemas ATTACH, sin red, sin `float`.

Mapeo brief -> test:
- bajo umbral -> sin reto, liquida .............. `test_under_threshold_*`
- sobre umbral -> `PENDING_AUTHORIZATION` + hold
  (terceros) / sin hold (propia atomica) ........ `test_over_threshold_*`
- firma valida -> continua sin duplicar ......... `test_authorize_success_*`
- firma invalida -> no completa + evento ........ `test_authorize_invalid_*`
- `nonce` expirado / binding revocado ........... `test_expired_*`, `test_revoked_*`
- N fallos -> bloqueo preventivo (CA-02) ........ `test_preventive_block_*`
- limite diario por perfil (CA-03) .............. `test_daily_limit_*`
- fachada D-03 (sin modelos `identity`) ......... `test_facade_*`
- concurrencia/idempotencia ..................... `test_concurrent_*`, `test_idempotency_*`
- interbancaria: limite + pendiente + reanuda ... `test_interbank_*`
- HTTP `POST /transfers/third-party` ............ `test_http_*`

Parte A: pura (sin BD). Parte B: SQLite. Parte C: Postgres (`db_session`,
con rollback; se omite si no hay BD).
"""

from __future__ import annotations

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
DEVICE_A = "dev-biometric-1"

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
]


def _make_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # registro de tablas
    import app.modules.ledger.models as _l
    import app.modules.shared.models as _s
    import app.modules.transactions.models as _t

    assert _a is not None and _l is not None and _s is not None and _t is not None
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "ledger", "shared", "transactions", "config"):
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


@pytest.fixture(autouse=True)
def _clean_holder_cache():
    from app.modules.transactions import service as svc

    svc.clear_holder_cache()
    yield
    svc.clear_holder_cache()


@pytest.fixture()
def svc_session():
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


class FakeBiometry:
    """Puertos de fachada `identity` en memoria (sin tablas `identity`)."""

    def __init__(self) -> None:
        self.bindings: dict[tuple[str, str], dict] = {}

    def add_binding(self, user_id: uuid.UUID, device_id: str, *, status: str = "ACTIVE") -> None:
        self.bindings[(str(user_id), device_id)] = {
            "device_id": device_id,
            "public_key": "hmac:" + "ab" * 16,
            "status": status,
        }

    def binding_reader(self, session: Session, user_id: uuid.UUID, device_id: str):
        found = self.bindings.get((str(user_id), device_id))
        return dict(found) if found is not None else None

    @staticmethod
    def sign(nonce: str) -> str:
        return f"SIG:{nonce}"

    def verifier(self, nonce: str, signature: str, public_key: str) -> bool:
        return bool(nonce) and signature == f"SIG:{nonce}" and bool(public_key)


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


def _outbox(session, tx_id):
    from app.modules.shared.models import OutboxEntry

    return list(
        session.scalars(sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx_id)).all()
    )


def _outbox_types(session, tx_id) -> set[str]:
    return {r.event_type for r in _outbox(session, tx_id)}


def _security_events(session, tx_id) -> list[dict]:
    return [r.payload for r in _outbox(session, tx_id) if r.event_type == "auth.failed_attempt"]


def _history_reasons(session, tx_id) -> list[str]:
    from app.modules.transactions import repository as tx_repo

    return [h.reason or "" for h in tx_repo.list_history(session, tx_id)]


def _third_party(session, a1, b1, amount, **overrides):
    from app.modules.transactions import service as svc

    params = {
        "initiator_user_id": USER_A,
        "source_account_id": a1,
        "target_account_id": b1,
        "amount_minor": amount,
        "currency": "PEN",
        "fee_minor": 0,
        "idempotency_key": f"e2t17-{uuid.uuid4()}",
        "device_id": DEVICE_A,
        "balance_port": _real_port(),
    }
    params.update(overrides)
    return svc.transfer_third_party(session, **params)


def _authorize(session, tx_id, *, bio: FakeBiometry, device: str = DEVICE_A, **overrides):
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    tx = tx_repo.get_transaction(session, tx_id)
    nonce = (bio_svc.get_block(tx) or {}).get("nonce") or ""
    params = {
        "device_id": device,
        "signature": bio.sign(nonce),
        "balance_port": _real_port(),
        "binding_reader": bio.binding_reader,
        "signature_verifier": bio.verifier,
    }
    params.update(overrides)
    return bio_svc.authorize_transaction(session, tx_id, **params)


# ---------------------------------------------------------------- Parte A: pura
def test_challenge_block_pure():
    from app.modules.transactions.service import biometric as bio_svc

    block, nonce, expires = bio_svc.new_challenge_block("dev-1")
    assert nonce and block["nonce"] == nonce
    assert block["failed_attempts"] == 0 and block["used"] is False
    assert block["authorized"] is False and block["authorized_signature_digest"] is None
    assert (expires - datetime.now(UTC)).total_seconds() <= bio_svc.NONCE_TTL_SECONDS
    assert bio_svc.new_challenge_block(None)[0]["device_id"] is None
    with pytest.raises(bio_svc.BiometricStateError):
        bio_svc.new_challenge_block("x" * 129)
    with pytest.raises(ValueError):
        bio_svc.new_challenge_block("dev-1", ttl_seconds=0)


def test_evaluate_gate_without_user_skips():
    from app.modules.transactions.service import biometric as bio_svc

    decision = bio_svc.evaluate_gate(
        None,  # type: ignore[arg-type]
        user_id=None,
        amount_minor=10**9,
        fee_minor=0,
        threshold_reader=lambda _s: 1,
        daily_limit_reader=lambda _s, _p: 1,
    )
    assert decision.requires_biometry is False
    assert decision.limit_exceeded is False


def test_facade_boundary_static():
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "app" / "modules" / "transactions"
    service_src = (base / "service" / "__init__.py").read_text(encoding="utf-8")
    bio_src = (base / "service" / "biometric.py").read_text(encoding="utf-8")
    for token in ("identity.models", "identity.repository", "identity.models import"):
        assert token not in service_src, f"frontera rota en service: {token}"
        assert token not in bio_src, f"frontera rota en biometric: {token}"
    # La unica via a `identity` es la fachada `device_login` (D-03).
    assert "identity.service.device_login" in bio_src or "identity.service" in bio_src
    assert "verify_signature" in bio_src and "get_device_binding" in bio_src


def test_default_readers_delegate_to_identity_facade(monkeypatch):
    from app.modules.identity.service import device_login as device_login_service
    from app.modules.transactions.service import biometric as bio_svc

    calls: dict = {}

    def _fake_binding(session, user_id, device_id):
        calls["binding"] = (user_id, device_id)
        return {"device_id": device_id, "public_key": "hmac:" + "cd" * 16, "status": "ACTIVE"}

    def _fake_verify(nonce, signature, public_key):
        calls["verify"] = (nonce, signature, public_key)
        return True

    monkeypatch.setattr(device_login_service, "get_device_binding", _fake_binding)
    monkeypatch.setattr(device_login_service, "verify_signature", _fake_verify)
    session = object()
    binding = bio_svc.default_binding_reader(session, USER_A, DEVICE_A)  # type: ignore[arg-type]
    assert binding is not None and binding["status"] == "ACTIVE"
    assert calls["binding"] == (USER_A, DEVICE_A)
    assert bio_svc.default_signature_verifier("n", "s", "k") is True
    assert calls["verify"] == ("n", "s", "k")


def test_real_hmac_signature_verifies_without_identity_db():
    import hashlib
    import hmac

    from app.modules.transactions.service import biometric as bio_svc

    secret = bytes.fromhex("ab" * 16)
    public_key = "hmac:" + "ab" * 16
    nonce = "nonce-de-prueba"
    good = hmac.new(secret, nonce.encode(), hashlib.sha256).hexdigest()
    assert bio_svc.default_signature_verifier(nonce, good, public_key) is True
    assert bio_svc.default_signature_verifier(nonce, "00" * 32, public_key) is False
    assert bio_svc.default_signature_verifier(nonce, good, "hmac:" + "ff" * 16) is False


# --------------------------------------------------------------- Parte B: motor
def test_under_threshold_settles_without_challenge(svc_session):
    session, a1, b1 = svc_session["session"], svc_session["a1"], svc_session["b1"]
    tx = _third_party(session, a1, b1, 1_000)
    session.commit()
    assert tx.status == "SETTLED"
    from app.modules.transactions.service import biometric as bio_svc

    assert bio_svc.get_block(tx) is None
    assert _entry_types(session, tx.id) == ["TRANSFER_HOLD", "TRANSFER_SETTLE"]
    assert _avail(session, a1) == 100_000 - 1_000
    assert "transaction.initiated" in _outbox_types(session, tx.id)


def test_over_threshold_pends_with_hold(svc_session):
    session, a1, b1 = svc_session["session"], svc_session["a1"], svc_session["b1"]
    tx = _third_party(session, a1, b1, 5_000)
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    holds = _holds(session, tx.id)
    assert len(holds) == 1 and holds[0].status == "ACTIVE"
    assert int(holds[0].amount_minor) == 5_000
    assert _entry_types(session, tx.id) == ["TRANSFER_HOLD"]
    assert _avail(session, a1) == 100_000 - 5_000
    assert _avail(session, b1) == 50_000  # destino intacto hasta liquidar
    types = _outbox_types(session, tx.id)
    assert {"transaction.initiated", "funds.held"} <= types
    assert "transfer.settled" not in types
    assert "biometria requerida" in _history_reasons(session, tx.id)
    from app.modules.transactions.service import biometric as bio_svc

    block = bio_svc.get_block(tx)
    assert block is not None and block["nonce"] and block["failed_attempts"] == 0


def test_authorize_success_reuses_hold_without_duplicates(svc_session):
    session, a1, b1 = svc_session["session"], svc_session["a1"], svc_session["b1"]
    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A)
    tx = _third_party(session, a1, b1, 5_000)
    session.commit()
    done = _authorize(session, tx.id, bio=bio)
    session.commit()
    assert done.status == "SETTLED"
    assert done.id == tx.id
    holds = _holds(session, tx.id)
    assert len(holds) == 1 and holds[0].status == "CAPTURED"
    assert _entry_types(session, tx.id) == ["TRANSFER_HOLD", "TRANSFER_SETTLE"]
    assert _avail(session, a1) == 100_000 - 5_000
    assert _avail(session, b1) == 50_000 + 5_000
    assert "biometria superada" in _history_reasons(session, tx.id)
    # Replay con la misma firma: sin duplicar asiento ni hold.
    again = _authorize(session, tx.id, bio=bio)
    session.commit()
    assert again.id == tx.id and again.status == "SETTLED"
    assert len(_holds(session, tx.id)) == 1
    assert _entry_types(session, tx.id) == ["TRANSFER_HOLD", "TRANSFER_SETTLE"]


def test_authorize_invalid_signature_records_security_event(svc_session):
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = svc_session["session"], svc_session["a1"], svc_session["b1"]
    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A)
    tx = _third_party(session, a1, b1, 5_000)
    session.commit()
    with pytest.raises(bio_svc.NotAuthorizedError) as exc:
        _authorize(session, tx.id, bio=bio, signature="SIG:manipulada")
    session.commit()
    assert str(exc.value) == bio_svc.NOT_AUTHORIZED_MESSAGE
    pending = session.get(
        __import__("app.modules.transactions.models", fromlist=["Transaction"]).Transaction,
        tx.id,
    )
    assert pending.status == "PENDING_AUTHORIZATION"
    assert (bio_svc.get_block(pending) or {})["failed_attempts"] == 1
    events = _security_events(session, tx.id)
    assert len(events) == 1
    assert events[0]["blocked"] is False and events[0]["attempt"] == 1
    assert "nonce" not in str(events[0]) and "SIG" not in str(events[0])
    assert _entry_types(session, tx.id) == ["TRANSFER_HOLD"]  # sin liquidar
    assert _avail(session, b1) == 50_000


def test_expired_nonce_rejected_as_expired(svc_session):
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = svc_session["session"], svc_session["a1"], svc_session["b1"]
    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A)
    tx = _third_party(session, a1, b1, 5_000)
    session.commit()
    future = datetime.now(UTC) + timedelta(seconds=bio_svc.NONCE_TTL_SECONDS + 60)
    with pytest.raises(bio_svc.ExpiredNonceError):
        _authorize(session, tx.id, bio=bio, now=future)
    session.commit()
    assert (bio_svc.get_block(session.get(tx.__class__, tx.id)) or {})["failed_attempts"] == 1
    events = _security_events(session, tx.id)
    assert len(events) == 1 and events[0]["reason"] == "expired"


def test_revoked_binding_is_generic_without_oracle(svc_session):
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = svc_session["session"], svc_session["a1"], svc_session["b1"]
    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A, status="REVOKED")
    tx = _third_party(session, a1, b1, 5_000)
    session.commit()
    with pytest.raises(bio_svc.NotAuthorizedError) as exc:
        _authorize(session, tx.id, bio=bio)
    assert str(exc.value) == bio_svc.NOT_AUTHORIZED_MESSAGE  # igual que firma invalida


def test_preventive_block_after_n_failures(svc_session):
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = svc_session["session"], svc_session["a1"], svc_session["b1"]
    _set_param(session, MAX_ATTEMPTS_KEY, 3)
    session.commit()
    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A)
    tx = _third_party(session, a1, b1, 5_000)
    session.commit()
    with pytest.raises(bio_svc.NotAuthorizedError):
        _authorize(session, tx.id, bio=bio, signature="mala-1")
    session.commit()
    with pytest.raises(bio_svc.NotAuthorizedError):
        _authorize(session, tx.id, bio=bio, signature="mala-2")
    session.commit()
    blocked = _authorize(session, tx.id, bio=bio, signature="mala-3")
    session.commit()
    assert blocked.status == "REJECTED"
    assert blocked.failure_reason == bio_svc.PREVENTIVE_BLOCK
    assert (blocked.meta or {}).get("failure_code") == bio_svc.PREVENTIVE_BLOCK
    holds = _holds(session, tx.id)
    assert len(holds) == 1 and holds[0].status == "RELEASED"
    assert _avail(session, a1) == 100_000  # retenido devuelto, sin perdida
    assert _avail(session, b1) == 50_000  # destino nunca tocado
    assert [e for e in _entry_types(session, tx.id) if e == "TRANSFER_SETTLE"] == []
    events = _security_events(session, tx.id)
    assert len(events) == 3
    assert events[-1]["blocked"] is True and events[-1]["attempt"] == 3
    assert "bloqueo preventivo tras N intentos fallidos" in _history_reasons(session, tx.id)
    # Tras el bloqueo ya no se puede completar el pago.
    with pytest.raises(bio_svc.BiometricStateError):
        _authorize(session, tx.id, bio=bio)


def test_daily_limit_exceeded_rejects_without_hold(svc_session):
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = svc_session["session"], svc_session["a1"], svc_session["b1"]
    _set_param(session, DAILY_KEY, 4_000)
    session.commit()
    tx = _third_party(session, a1, b1, 5_000)
    session.commit()
    assert tx.status == "REJECTED"
    assert tx.failure_reason == bio_svc.DAILY_LIMIT_EXCEEDED
    assert (tx.meta or {}).get("failure_code") == bio_svc.DAILY_LIMIT_EXCEEDED
    assert _holds(session, tx.id) == []
    assert _tx_entries(session, tx.id) == []
    assert _avail(session, a1) == 100_000
    assert bio_svc.get_block(tx) is None
    assert "limite diario excedido" in _history_reasons(session, tx.id)


def test_daily_limit_accumulates_and_ignores_rejected(svc_session):
    session, a1, b1 = svc_session["session"], svc_session["a1"], svc_session["b1"]
    _set_param(session, THRESHOLD_KEY, 1_000_000)  # sin biometria en este test
    _set_param(session, DAILY_KEY, 12_000)
    session.commit()
    first = _third_party(session, a1, b1, 5_000)
    session.commit()
    assert first.status == "SETTLED"
    second = _third_party(session, a1, b1, 8_000)  # 5k + 8k > 12k
    session.commit()
    assert second.status == "REJECTED"
    assert second.failure_reason == "DAILY_LIMIT_EXCEEDED"
    third = _third_party(session, a1, b1, 1_000)  # la rechazada no acumula
    session.commit()
    assert third.status == "SETTLED"


def test_own_over_threshold_pends_without_hold_then_settles(svc_session):
    from app.modules.transactions import service as svc
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, a2 = svc_session["session"], svc_session["a1"], svc_session["a2"]
    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A)
    tx = svc.transfer_own(
        session,
        initiator_user_id=USER_A,
        source_account_id=a1,
        target_account_id=a2,
        amount_minor=5_000,
        currency="PEN",
        idempotency_key=f"e2t17-{uuid.uuid4()}",
        device_id=DEVICE_A,
        balance_port=_real_port(),
    )
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    assert _holds(session, tx.id) == []  # atomica: nada se mueve hasta AUTHORIZED
    assert _tx_entries(session, tx.id) == []
    assert _avail(session, a1) == 100_000
    done = _authorize(session, tx.id, bio=bio)
    session.commit()
    assert done.status == "SETTLED"
    assert _entry_types(session, tx.id) == ["OWN_TRANSFER"]  # asiento unico
    assert _avail(session, a1) == 100_000 - 5_000
    assert _avail(session, a2) == 50_000 + 5_000
    assert bio_svc.get_block(done)["authorized"] is True


def test_idempotency_replay_returns_pending_without_duplicates(svc_session):
    session, a1, b1 = svc_session["session"], svc_session["a1"], svc_session["b1"]
    key = f"e2t17-{uuid.uuid4()}"
    first = _third_party(session, a1, b1, 5_000, idempotency_key=key)
    session.commit()
    second = _third_party(session, a1, b1, 5_000, idempotency_key=key)
    session.commit()
    assert second.id == first.id and second.status == "PENDING_AUTHORIZATION"
    assert len(_holds(session, first.id)) == 1
    assert _entry_types(session, first.id) == ["TRANSFER_HOLD"]


def test_challenge_rotation_preserves_failed_attempts(svc_session):
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = svc_session["session"], svc_session["a1"], svc_session["b1"]
    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A)
    tx = _third_party(session, a1, b1, 5_000)
    session.commit()
    before = (bio_svc.get_block(tx) or {})["nonce"]
    with pytest.raises(bio_svc.NotAuthorizedError):
        _authorize(session, tx.id, bio=bio, signature="mala")
    session.commit()
    rotated = bio_svc.request_biometric_challenge(session, tx.id, device_id=DEVICE_A)
    session.commit()
    assert rotated["nonce"] != before and rotated["expires_in"] > 0
    fresh = session.get(tx.__class__, tx.id)
    assert (bio_svc.get_block(fresh) or {})["failed_attempts"] == 1
    done = _authorize(session, tx.id, bio=bio)
    session.commit()
    assert done.status == "SETTLED"


def test_concurrent_double_authorize_settles_once(svc_session):
    """Doble `authorize` concurrente: un solo asiento de liquidacion.

    Emula el `SELECT ... FOR UPDATE` productivo con un lock global (mismo
    criterio honesto que E2-T07: SQLite no bloquea filas ni aisla
    transacciones por hilo en `StaticPool`; sin el lock, el `rollback` de
    un hilo revertiria el `flush` ajeno por compartir conexion).
    """
    session, engine = svc_session["session"], svc_session["engine"]
    a1, b1 = svc_session["a1"], svc_session["b1"]

    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A)
    tx = _third_party(session, a1, b1, 5_000)
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
                done = _authorize(worker, tx_id, bio=bio)
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
    assert errors == []
    assert len(results) == 2
    assert {r[0] for r in results} == {tx_id}
    assert {r[1] for r in results} == {"SETTLED"}
    assert len(_holds(session, tx_id)) == 1
    assert _entry_types(session, tx_id) == ["TRANSFER_HOLD", "TRANSFER_SETTLE"]


# ------------------------------------------------------------- Interbancaria
CCI_OK = "00200100200100200101"
BANK_CODE = "BCP01"


def _holder():
    from app.adapters.holder_lookup import MockHolderLookupProvider

    return MockHolderLookupProvider(mode="success")


def _holder_name() -> str:
    from app.adapters.holder_lookup import mock_holder_name_for

    return mock_holder_name_for(CCI_OK)


def _counting_gateway(monkeypatch, default_send: str = "confirm"):
    from app.adapters.interbank_gateway import MockInterbankGateway

    gw = MockInterbankGateway(default_send=default_send)
    calls: list = []
    original = gw.send_transfer

    def _counting(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(gw, "send_transfer", _counting)
    return gw, calls


def _interbank(session, a1, gateway, amount, **overrides):
    from app.modules.transactions.service import interbank as saga

    params = {
        "initiator_user_id": USER_A,
        "source_account_id": a1,
        "amount_minor": amount,
        "currency": "PEN",
        "cci": CCI_OK,
        "holder_name": _holder_name(),
        "bank_code": BANK_CODE,
        "fee_minor": 200,
        "idempotency_key": f"e2t17-ib-{uuid.uuid4()}",
        "device_id": DEVICE_A,
        "gateway": gateway,
        "holder_provider": _holder(),
        "balance_port": _real_port(),
    }
    params.update(overrides)
    return saga.transfer_interbank(session, **params)


def test_interbank_daily_limit_rejects_without_send(svc_session, monkeypatch):
    from app.modules.transactions.service import biometric as bio_svc

    session, a1 = svc_session["session"], svc_session["a1"]
    _set_param(session, DAILY_KEY, 5_000)
    session.commit()
    gw, calls = _counting_gateway(monkeypatch)
    tx = _interbank(session, a1, gw, 10_000)
    session.commit()
    assert tx.status == "REJECTED"
    assert (tx.meta or {}).get("failure_code") == bio_svc.DAILY_LIMIT_EXCEEDED
    assert calls == []
    assert _holds(session, tx.id) == []
    assert _tx_entries(session, tx.id) == []


def test_interbank_pending_resume_settles_without_resend(svc_session, monkeypatch):
    session, a1 = svc_session["session"], svc_session["a1"]
    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A)
    gw, calls = _counting_gateway(monkeypatch)
    tx = _interbank(session, a1, gw, 10_000)
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    assert calls == []  # sobre el umbral: no se envia sin biometria
    holds = _holds(session, tx.id)
    assert len(holds) == 1 and holds[0].status == "ACTIVE"
    assert _entry_types(session, tx.id) == ["INTERBANK_HOLD"]
    done = _authorize(session, tx.id, bio=bio, gateway=gw)
    session.commit()
    assert done.status == "SETTLED"
    assert len(calls) == 1  # un solo envio
    assert len(_holds(session, tx.id)) == 1
    assert "INTERBANK_HOLD" in _entry_types(session, tx.id)
    # `2200-Transito` cuadra a cero tras liquidar.
    from app.modules.ledger.repository import get_by_code

    transit = get_by_code(session, "2200")
    net = 0
    for entry in _tx_entries(session, tx.id):
        from app.modules.ledger import service as ledger_service

        for row in ledger_service.list_postings(session, entry.id):
            if str(row.ledger_account_id) == str(transit.id):
                net += row.amount_minor if row.direction == "DEBIT" else -row.amount_minor
    assert net == 0


# ------------------------------------------------------------------- HTTP
def test_http_third_party_pending(svc_session, monkeypatch):
    from fastapi.testclient import TestClient

    from app.core.db import get_db
    from app.main import app
    from app.modules.transactions.api import get_current_user_id

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    session = svc_session["session"]

    def _override_db():
        yield session

    def _override_user():
        return USER_A

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = _override_user
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/transfers/third-party",
                json={
                    "source_account_id": str(svc_session["a1"]),
                    "target_account_id": str(svc_session["b1"]),
                    "amount_minor": 5_000,
                    "currency": "PEN",
                    "fee_minor": 0,
                },
                headers={"Idempotency-Key": f"e2t17-{uuid.uuid4()}"},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["status"] == "PENDING_AUTHORIZATION"
    assert resp.json()["data"]["failure_code"] is None


# --------------------------------------------------------------- Parte C: Postgres
def test_postgres_biometric_pending_and_authorize(db_session: Session, monkeypatch):
    """Humo en Postgres real (se omite sin BD): pendiente + autoriza."""
    from app.modules.accounts import repository as accounts_repo
    from app.modules.transactions import service as svc
    from app.modules.transactions.service import biometric as bio_svc

    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A)
    account = accounts_repo.create_account(
        session=db_session,
        user_id=USER_A,
        account_number="00990007777",
        type="AHORRO",
        currency="PEN",
    )
    target = accounts_repo.create_account(
        session=db_session,
        user_id=USER_B,
        account_number="00880007777",
        type="AHORRO",
        currency="PEN",
    )
    accounts_repo.apply_delta(
        db_session, account.id, available_delta=100_000, held_delta=0, currency="PEN"
    )
    db_session.commit()
    tx = svc.transfer_third_party(
        db_session,
        initiator_user_id=USER_A,
        source_account_id=account.id,
        target_account_id=target.id,
        amount_minor=50_000,
        currency="PEN",
        fee_minor=0,
        idempotency_key=f"e2t17-pg-{uuid.uuid4()}",
        device_id=DEVICE_A,
        balance_port=_real_port(),
        threshold_reader=lambda _s: 1_000,
        daily_limit_reader=lambda _s, _p: 10_000_000,
    )
    db_session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    done = bio_svc.authorize_transaction(
        db_session,
        tx.id,
        device_id=DEVICE_A,
        signature=bio.sign((bio_svc.get_block(tx) or {})["nonce"]),
        balance_port=_real_port(),
        binding_reader=bio.binding_reader,
        signature_verifier=bio.verifier,
        max_attempts_reader=lambda _s: 5,
    )
    db_session.commit()
    assert done.status == "SETTLED"
