"""Contador de fallos biometricos con CAS por `version` (OBS-3, HU08 CA-02/CA-04).

Sobre el motor y el ledger con adaptadores reales (`ACCOUNTS_BALANCE_PORT`,
cuentas por `accounts.repository`) y fachada real de parametros
(`shared.config`, E2-T18): dinero de juguete (UUIDs sinteticos, sin PII),
SQLite en memoria + schemas ATTACH, sin red, sin `float`.

Mapeo OBS-3 -> test:
- N fallos suman y el N-esimo bloquea ........... `test_failed_attempts_sum_*`
  (via PIN con `pin_verifier` inyectado: el `nonce` no aplica a esa via,
  asi el conteo se ejerce sin interferencia del un solo uso de OBS-4).
- un conflicto de version no pierde el incremento
  (reintento con merge) ........................ `test_version_conflict_*`
  (`save_block` simulado que pierde la primera carrera CAS).
- `save_block` es CAS: version obsoleta pierde,
  fresco guarda ............................... `test_save_block_is_cas_*`.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
DEVICE_A = "dev-obs3-1"

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


@pytest.fixture()
def obs3_session():
    """Sesion SQLite aislada con cuentas fondeadas y parametros E2-T18."""
    from app.modules.accounts import repository as accounts_repo

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        a1 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="00990001111", type="AHORRO", currency="PEN"
        )
        b1 = accounts_repo.create_account(
            session, user_id=USER_B, account_number="00880003333", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a1.id, available_delta=100_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, b1.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        _seed_param(session, THRESHOLD_KEY, "2000")
        _seed_param(session, DAILY_KEY, "500000")
        _seed_param(session, DAILY_STANDARD_KEY, "500000")
        _seed_param(session, MAX_ATTEMPTS_KEY, "5", module="identity")
        session.commit()
        yield {"session": session, "engine": engine, "a1": a1.id, "b1": b1.id}
    finally:
        session.close()
        engine.dispose()


def _real_port():
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT

    return ACCOUNTS_BALANCE_PORT


def _third_party(session, a1, b1, amount=5_000):
    """Transferencia sobre el umbral (-> `PENDING_AUTHORIZATION` con hold)."""
    from app.modules.transactions import service as svc

    return svc.transfer_third_party(
        session,
        initiator_user_id=USER_A,
        source_account_id=a1,
        target_account_id=b1,
        amount_minor=amount,
        currency="PEN",
        fee_minor=0,
        idempotency_key=f"obs3-{uuid.uuid4()}",
        device_id=DEVICE_A,
        balance_port=_real_port(),
    )


def _security_events(session, tx_id) -> list[dict]:
    from app.modules.shared.models import OutboxEntry

    rows = session.scalars(sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx_id)).all()
    return [r.payload for r in rows if r.event_type == "auth.failed_attempt"]


def _pin_fail(session, tx_id, *, pin="0000", max_attempts=3):
    """Un intento PIN fallido (fachada inyectada, sin tablas `identity`)."""
    from app.modules.transactions.service import biometric as bio_svc

    return bio_svc.authorize_transaction_with_pin(
        session,
        tx_id,
        pin=pin,
        balance_port=_real_port(),
        max_attempts_reader=lambda _s: max_attempts,
        pin_verifier=lambda _s, _u, _p: False,
    )


# ------------------------------------------------- Conteo correcto (suma)
def test_failed_attempts_sum_sequentially_and_block_at_n(obs3_session):
    """Tres fallos PIN suman (`failed_attempts` 1..3) y el 3o bloquea (CA-02)."""
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = obs3_session["session"], obs3_session["a1"], obs3_session["b1"]
    tx = _third_party(session, a1, b1)
    session.commit()
    with pytest.raises(bio_svc.NotAuthorizedError):
        _pin_fail(session, tx.id)
    session.commit()
    with pytest.raises(bio_svc.NotAuthorizedError):
        _pin_fail(session, tx.id)
    session.commit()
    blocked = _pin_fail(session, tx.id)
    session.commit()
    assert blocked.status == "REJECTED"
    assert blocked.failure_reason == bio_svc.PREVENTIVE_BLOCK
    fresh = tx_repo.get_transaction(session, tx.id)
    assert (bio_svc.get_block(fresh) or {})["failed_attempts"] == 3
    events = _security_events(session, tx.id)
    assert [e["attempt"] for e in events] == [1, 2, 3]
    assert events[-1]["blocked"] is True
    holds = list(tx_repo.list_holds_by_transaction(session, tx.id))
    assert len(holds) == 1 and holds[0].status == "RELEASED"


# --------------------------------- Conflicto de version: no pierde el incremento
def test_version_conflict_retries_without_losing_increment(obs3_session, monkeypatch):
    """Si `save_block` pierde la primera carrera CAS, el reintento cuenta igual."""
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.domain.state_machine import ConcurrencyError
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = obs3_session["session"], obs3_session["a1"], obs3_session["b1"]
    tx = _third_party(session, a1, b1)
    session.commit()
    real_save = bio_svc.save_block
    calls = {"n": 0}

    def _flaky_save(session_, tx_, block_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConcurrencyError("carrera simulada")
        return real_save(session_, tx_, block_)

    monkeypatch.setattr(bio_svc, "save_block", _flaky_save)
    with pytest.raises(bio_svc.NotAuthorizedError):
        _pin_fail(session, tx.id, max_attempts=5)
    session.commit()
    assert calls["n"] >= 2  # hubo reintento tras el conflicto
    fresh = tx_repo.get_transaction(session, tx.id)
    assert fresh.status == "PENDING_AUTHORIZATION"
    assert (bio_svc.get_block(fresh) or {})["failed_attempts"] == 1
    assert len(_security_events(session, tx.id)) == 1


# ------------------------------------------------- `save_block` es CAS por `version`
def test_save_block_is_cas_stale_version_loses(obs3_session):
    """Version obsoleta -> `ConcurrencyError`; fresco guarda y preserva `meta`."""
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.domain.state_machine import ConcurrencyError
    from app.modules.transactions.models import Transaction
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = obs3_session["session"], obs3_session["a1"], obs3_session["b1"]
    engine = obs3_session["engine"]
    tx = _third_party(session, a1, b1)
    session.commit()
    stale_version = int(tx.version)
    # Otro worker avanza la fila por conexion directa (sin sincronizar esta
    # sesion ORM: el objeto `tx` queda obsoleto de verdad).
    with engine.begin() as conn:
        conn.execute(
            sa.update(Transaction)
            .where(Transaction.id == tx.id, Transaction.version == stale_version)
            .values(version=stale_version + 1)
        )
    block = bio_svc.get_block(tx) or {}
    block["failed_attempts"] = 99
    with pytest.raises(ConcurrencyError):
        bio_svc.save_block(session, tx, block)
    session.rollback()
    fresh = tx_repo.get_transaction(session, tx.id)
    assert int(fresh.version) == stale_version + 1
    assert (bio_svc.get_block(fresh) or {})["failed_attempts"] == 0  # intacto
    good = dict(bio_svc.get_block(fresh) or {})
    good["failed_attempts"] = 1
    bio_svc.save_block(session, fresh, good)
    session.commit()
    reread = tx_repo.get_transaction(session, tx.id)
    assert int(reread.version) == stale_version + 2
    assert (bio_svc.get_block(reread) or {})["failed_attempts"] == 1
