"""Persistencia del motor (E5-T02, HU17): transactions, historial y holds.

- Parte A (sin BD): metadatos del modelo y migracion segun `03b#6`.
- Parte B (SQLite en memoria + schema ATTACH): CRUD, historial por
  transicion, idempotencia y holds con estado/expiracion.
- Parte C (Postgres `db_session`): humo de integracion; se omite si no hay BD.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- Parte A: modelo
def test_tables_registered_with_schema_and_constraints():
    import app.modules.transactions.models as m  # noqa: F401 (registro)

    for key in (
        "transactions.transactions",
        "transactions.transaction_status_history",
        "transactions.holds",
    ):
        assert key in Base.metadata.tables, f"falta tabla {key}"
    tx = Base.metadata.tables["transactions.transactions"]
    assert tx.schema == "transactions"
    cols = {c.name for c in tx.columns}
    for expected in (
        "id",
        "type",
        "status",
        "idempotency_key",
        "initiator_user_id",
        "source_account_id",
        "target_account_id",
        "external_ref",
        "amount_minor",
        "currency",
        "fee_minor",
        "risk_level",
        "metadata",
        "version",
        "created_at",
        "settled_at",
    ):
        assert expected in cols, f"falta columna transactions.{expected}"
    checks = {c.name for c in tx.constraints if isinstance(c, sa.CheckConstraint)}
    assert "ck_transactions_amount_positive" in checks
    assert "ck_transactions_fee_non_negative" in checks
    idx_names = {i.name for i in tx.indexes}
    for expected in (
        "ix_transactions_idempotency_key",
        "ix_transactions_status_created",
        "ix_transactions_source_account",
        "ix_transactions_external_ref",
    ):
        assert expected in idx_names

    hist = Base.metadata.tables["transactions.transaction_status_history"]
    assert {c.name for c in hist.columns} >= {
        "id",
        "transaction_id",
        "from_status",
        "to_status",
        "reason",
        "actor_type",
        "created_at",
    }
    holds = Base.metadata.tables["transactions.holds"]
    assert {c.name for c in holds.columns} >= {
        "id",
        "transaction_id",
        "account_id",
        "amount_minor",
        "currency",
        "status",
        "held_at",
        "expires_at",
        "released_at",
    }
    hold_checks = {c.name for c in holds.constraints if isinstance(c, sa.CheckConstraint)}
    assert "ck_holds_amount_positive" in hold_checks


def test_no_foreign_keys_to_other_schemas():
    tables = [
        Base.metadata.tables["transactions.transactions"],
        Base.metadata.tables["transactions.transaction_status_history"],
        Base.metadata.tables["transactions.holds"],
    ]
    for table in tables:
        for fk in table.foreign_keys:
            target = fk.column.table
            assert (
                target.schema == "transactions"
            ), f"FK fuera del schema propio: {table.name} -> {target.schema}.{target.name}"


def test_migration_0002_exists_and_matches_models():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0002_transactions_persistence.py"
    )
    assert path.exists(), "falta migracion 0002_transactions_persistence.py"
    content = path.read_text(encoding="utf-8")
    for token in (
        'down_revision = "0001_init_schemas"',
        '"transactions"',
        '"transaction_status_history"',
        '"holds"',
        "ck_transactions_amount_positive",
        "ck_holds_amount_positive",
        "ix_transactions_idempotency_key",
        "ix_holds_status_expires",
    ):
        assert token in content, f"migracion sin {token}"


# ---------------------------------------------------------------- Parte B: SQLite
@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schema `transactions` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.transactions.models as m

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS transactions")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            m.Transaction.__table__,
            m.TransactionStatusHistory.__table__,
            m.Hold.__table__,
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_tx(session: Session, **kwargs):
    from app.modules.transactions import repository as repo

    kwargs.setdefault("type", "OWN_TRANSFER")
    kwargs.setdefault("amount_minor", 1500)
    kwargs.setdefault("currency", "PEN")
    return repo.create_transaction(session, **kwargs)


def test_crud_transaction_and_idempotency(sqlite_session: Session):
    from app.modules.transactions import repository as repo

    tx = _make_tx(sqlite_session, idempotency_key="idem-001", external_ref="ext-1")
    assert tx.id is not None and tx.version == 0 and tx.status == "INITIATED"
    assert tx.fee_minor == 0

    found = repo.get_transaction(sqlite_session, tx.id)
    assert found is not None and found.amount_minor == 1500
    by_key = repo.get_by_idempotency_key(sqlite_session, "idem-001")
    assert by_key is not None and by_key.id == tx.id
    assert repo.get_by_idempotency_key(sqlite_session, "no-existe") is None


def test_amount_and_fee_rules(sqlite_session: Session):
    with pytest.raises(ValueError):
        _make_tx(sqlite_session, amount_minor=0)
    with pytest.raises(ValueError):
        _make_tx(sqlite_session, amount_minor=-10)
    with pytest.raises(TypeError):
        _make_tx(sqlite_session, amount_minor=10.5)  # nunca float
    with pytest.raises(ValueError):
        _make_tx(sqlite_session, currency="pen")
    with pytest.raises(ValueError):
        _make_tx(sqlite_session, fee_minor=-1)


def test_history_per_transition(sqlite_session: Session):
    from app.modules.transactions import repository as repo
    from app.modules.transactions.domain import InvalidTransitionError

    tx = _make_tx(sqlite_session)
    hist = repo.list_history(sqlite_session, tx.id)
    assert len(hist) == 1
    assert hist[0].from_status is None and hist[0].to_status == "INITIATED"

    repo.transition_transaction(sqlite_session, tx.id, "VALIDATED", reason="validada")
    assert tx.status == "VALIDATED" and tx.version == 1
    hist = repo.list_history(sqlite_session, tx.id)
    assert [h.to_status for h in hist] == ["INITIATED", "VALIDATED"]
    assert hist[1].from_status == "INITIATED" and hist[1].reason == "validada"

    with pytest.raises(InvalidTransitionError):
        repo.transition_transaction(sqlite_session, tx.id, "SETTLED")
    # El intento fallido no deja historial ni avanza version.
    assert tx.status == "VALIDATED" and tx.version == 1
    assert len(repo.list_history(sqlite_session, tx.id)) == 2


def test_transition_sets_settled_at_and_concurrency_guard(sqlite_session: Session):
    from app.modules.transactions import repository as repo
    from app.modules.transactions.domain import ConcurrencyError

    tx = _make_tx(sqlite_session)
    for target in ("VALIDATED", "AUTHORIZED", "FUNDS_HELD", "POSTED", "SETTLED"):
        repo.transition_transaction(sqlite_session, tx.id, target)
    assert tx.status == "SETTLED" and tx.settled_at is not None

    with pytest.raises(ConcurrencyError):
        repo.transition_transaction(sqlite_session, tx.id, "CONCILIATED", expected_version=0)
    repo.transition_transaction(
        sqlite_session,
        tx.id,
        "CONCILIATED",
        expected_version=tx.version,
    )
    assert tx.status == "CONCILIATED"


def test_hold_lifecycle_and_expiration(sqlite_session: Session):
    from app.modules.transactions import repository as repo

    tx = _make_tx(sqlite_session)
    account_id = uuid.uuid4()
    hold = repo.create_hold(
        sqlite_session,
        transaction_id=tx.id,
        account_id=account_id,
        amount_minor=1500,
        currency="PEN",
        expires_at=_utcnow() + timedelta(hours=1),
    )
    assert hold.status == "ACTIVE" and hold.released_at is None
    assert repo.get_hold(sqlite_session, hold.id) is not None
    assert len(repo.list_holds_by_transaction(sqlite_session, tx.id)) == 1
    assert len(repo.list_active_holds(sqlite_session)) == 1

    repo.update_hold_status(sqlite_session, hold.id, "CAPTURED")
    assert hold.status == "CAPTURED" and hold.released_at is not None
    assert repo.list_active_holds(sqlite_session) == []
    with pytest.raises(ValueError):
        repo.update_hold_status(sqlite_session, hold.id, "RELEASED")

    expired_hold = repo.create_hold(
        sqlite_session,
        transaction_id=tx.id,
        account_id=account_id,
        amount_minor=500,
        currency="PEN",
        expires_at=_utcnow() - timedelta(seconds=1),
    )
    due = repo.expire_due_holds(sqlite_session)
    assert [h.id for h in due] == [expired_hold.id]
    assert expired_hold.status == "EXPIRED" and expired_hold.released_at is not None

    with pytest.raises(ValueError):
        repo.create_hold(
            sqlite_session,
            transaction_id=tx.id,
            account_id=account_id,
            amount_minor=0,
            currency="PEN",
        )


# ---------------------------------------------------------------- Parte C: Postgres
def test_pg_tables_exist(db_session: Session):
    insp = sa.inspect(db_session.bind)
    for table in ("transactions", "transaction_status_history", "holds"):
        assert insp.has_table(
            table, schema="transactions"
        ), f"falta tabla transactions.{table} (aplica `alembic upgrade head`)"


def test_pg_crud_smoke(db_session: Session):
    from app.modules.transactions import repository as repo

    tx = repo.create_transaction(
        db_session,
        type="OWN_TRANSFER",
        amount_minor=2000,
        currency="PEN",
        idempotency_key=f"smoke-{uuid.uuid4()}",
    )
    repo.transition_transaction(db_session, tx.id, "VALIDATED", reason="smoke")
    hold = repo.create_hold(
        db_session,
        transaction_id=tx.id,
        account_id=uuid.uuid4(),
        amount_minor=2000,
        currency="PEN",
    )
    assert tx.status == "VALIDATED"
    assert len(repo.list_history(db_session, tx.id)) == 2
    assert hold.status == "ACTIVE"
