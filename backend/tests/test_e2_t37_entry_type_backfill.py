"""Backfill de `entry_type` en el historial (E2-T37, HU05).

- `backfill_data` puebla las filas `NULL` de `accounts.movements_view`
  desde `ledger.journal_entries` (join por `journal_entry_id`); verifica
  el valor por fila (ambas patas de un asiento comparten `entry_type`).
- Idempotencia: segunda ejecucion sin cambios; filas ya pobladas intactas
  (no se sobrescriben aunque el asiento diga otro valor).
- No resolubles: fila huerfana (sin asiento) y asiento con `entry_type`
  vacio (defensivo) quedan `NULL` sin excepcion (fallback del cliente).
- Round-trip: `reset_data` (downgrade) deja `NULL` y el backfill repuebla.
- Cadena de migracion `0028` acotada (<=32 chars, read-only sobre `ledger`).
- Postgres `banca_test`: backfill real + ciclo alembic down/up; se omite
  sin BD.

SQLite en memoria + schemas ATTACH (patron de `test_e2_t35_*`); sin
Postgres, sin red. Sin PII ni secretos en los datos.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

BACKEND_DIR = Path(__file__).resolve().parents[1]
MIGRATION_PATH = BACKEND_DIR / "migrations" / "versions" / "0028_backfill_entry_type.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_0028_backfill", str(MIGRATION_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schemas `accounts`/`ledger`/`shared` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401
    import app.modules.accounts.repository.movements as _m  # noqa: F401
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "ledger", "shared"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["accounts.accounts"],
            Base.metadata.tables["accounts.movements_view"],
            Base.metadata.tables["ledger.journal_entries"],
            Base.metadata.tables["shared.processed_events"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _new_account(session, **kwargs):
    """Cuenta directa (sin ledger/balances: el backfill solo cruza UUIDs)."""
    from app.modules.accounts.models import Account

    params = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "account_number": f"037-{uuid.uuid4().hex[:8]}",
        "type": "AHORRO",
        "currency": "PEN",
        "ledger_account_id": uuid.uuid4(),
        "ledger_hold_account_id": uuid.uuid4(),
    }
    params.update(kwargs)
    account = Account(**params)
    session.add(account)
    session.flush()
    return account


def _journal(session, entry_id, entry_type, **kwargs):
    """Asiento en `ledger.journal_entries` (fuente de verdad del backfill)."""
    from app.modules.ledger.models import JournalEntry

    params = {
        "id": entry_id,
        "transaction_id": uuid.uuid4(),
        "entry_type": entry_type,
        "description": "asiento E2-T37",
        "value_date": date(2026, 10, 9),
        "status": "POSTED",
        "hash": "y" * 64,
    }
    params.update(kwargs)
    entry = JournalEntry(**params)
    session.add(entry)
    session.flush()
    return entry


def _legacy_row(
    session, account, entry_id, direction="DEBIT", amount_minor=10_000, description=None
):
    """Fila historica con `entry_type = NULL` (estado post-`0027`)."""
    from app.modules.accounts.repository.movements import MovementView

    row = MovementView(
        journal_entry_id=entry_id,
        account_id=account.id,
        direction=direction,
        amount_minor=amount_minor,
        currency="PEN",
        transaction_id=uuid.uuid4(),
        description=description or "fila historica",
        entry_type=None,
        value_date=date(2026, 10, 9),
    )
    session.add(row)
    session.flush()
    return row


def _rows_by_entry(session, entry_id):
    from app.modules.accounts.repository.movements import MovementView

    return list(
        session.scalars(
            sa.select(MovementView).where(MovementView.journal_entry_id == entry_id)
        ).all()
    )


# ------------------------------------------------- CA-01: puebla desde el asiento
def test_backfill_populates_null_rows_from_journal_entries(sqlite_session: Session):
    """`upgrade` copia `journal_entries.entry_type` por `journal_entry_id`."""
    mig = _load_migration()
    source = _new_account(sqlite_session)
    target = _new_account(sqlite_session)
    entry_id, own_id = uuid.uuid4(), uuid.uuid4()
    _journal(sqlite_session, entry_id, "TRANSFER_SETTLE")
    _journal(sqlite_session, own_id, "OWN_TRANSFER")
    _legacy_row(sqlite_session, source, entry_id, "DEBIT", 10_000)
    _legacy_row(sqlite_session, target, entry_id, "CREDIT", 10_000)
    _legacy_row(sqlite_session, source, own_id, "DEBIT", 3_000)
    sqlite_session.commit()

    assert mig.backfill_data(sqlite_session.connection()) == 3
    sqlite_session.expire_all()

    settle = {(r.direction, r.entry_type) for r in _rows_by_entry(sqlite_session, entry_id)}
    assert settle == {("DEBIT", "TRANSFER_SETTLE"), ("CREDIT", "TRANSFER_SETTLE")}
    own = _rows_by_entry(sqlite_session, own_id)
    assert len(own) == 1 and own[0].entry_type == "OWN_TRANSFER"


def test_backfill_does_not_overwrite_populated_rows(sqlite_session: Session):
    """Filas ya pobladas (proyeccion E2-T35) no se tocan aunque difieran."""
    mig = _load_migration()

    account = _new_account(sqlite_session)
    entry_id = uuid.uuid4()
    _journal(sqlite_session, entry_id, "OWN_TRANSFER")
    row = _legacy_row(sqlite_session, account, entry_id)
    row.entry_type = "TRANSFER_SETTLE"  # poblada por la proyeccion en runtime
    sqlite_session.commit()

    assert mig.backfill_data(sqlite_session.connection()) == 0
    sqlite_session.expire_all()
    assert [r.entry_type for r in _rows_by_entry(sqlite_session, entry_id)] == ["TRANSFER_SETTLE"]


# ------------------------------------------------- CA-02: idempotencia
def test_backfill_is_idempotent(sqlite_session: Session):
    """Re-ejecutar no altera valores ni duplica filas."""
    mig = _load_migration()
    account = _new_account(sqlite_session)
    entry_id = uuid.uuid4()
    _journal(sqlite_session, entry_id, "TRANSFER_SETTLE")
    _legacy_row(sqlite_session, account, entry_id)
    sqlite_session.commit()

    assert mig.backfill_data(sqlite_session.connection()) == 1
    sqlite_session.expire_all()
    assert [r.entry_type for r in _rows_by_entry(sqlite_session, entry_id)] == ["TRANSFER_SETTLE"]
    assert mig.backfill_data(sqlite_session.connection()) == 0
    sqlite_session.expire_all()
    assert [r.entry_type for r in _rows_by_entry(sqlite_session, entry_id)] == ["TRANSFER_SETTLE"]


# ------------------------------------------------- CA-03: no resolubles
def test_backfill_leaves_orphan_rows_null(sqlite_session: Session):
    """Fila sin asiento asociado queda `NULL` sin error (fallback en cliente)."""
    mig = _load_migration()
    from app.modules.accounts.repository.movements import MovementView

    account = _new_account(sqlite_session)
    _legacy_row(sqlite_session, account, uuid.uuid4())
    sqlite_session.commit()

    assert mig.backfill_data(sqlite_session.connection()) == 0
    sqlite_session.expire_all()
    rows = sqlite_session.scalars(sa.select(MovementView)).all()
    assert len(rows) == 1 and rows[0].entry_type is None


def test_backfill_skips_empty_journal_entry_type(sqlite_session: Session):
    """Asiento con `entry_type` vacio (defensivo) no se copia: fila queda `NULL`."""
    mig = _load_migration()
    account = _new_account(sqlite_session)
    entry_id = uuid.uuid4()
    _journal(sqlite_session, entry_id, "")
    _legacy_row(sqlite_session, account, entry_id)
    sqlite_session.commit()

    assert mig.backfill_data(sqlite_session.connection()) == 0
    sqlite_session.expire_all()
    assert [r.entry_type for r in _rows_by_entry(sqlite_session, entry_id)] == [None]


# ------------------------------------------------- CA-04: reversible (bind de prueba)
def test_reset_then_backfill_round_trip(sqlite_session: Session):
    """`reset_data` (downgrade) deja `NULL` y el backfill repuebla."""
    mig = _load_migration()
    account = _new_account(sqlite_session)
    entry_id = uuid.uuid4()
    _journal(sqlite_session, entry_id, "TRANSFER_SETTLE")
    _legacy_row(sqlite_session, account, entry_id)
    sqlite_session.commit()

    assert mig.backfill_data(sqlite_session.connection()) == 1
    sqlite_session.expire_all()
    assert [r.entry_type for r in _rows_by_entry(sqlite_session, entry_id)] == ["TRANSFER_SETTLE"]
    assert mig.reset_data(sqlite_session.connection()) == 1
    sqlite_session.expire_all()
    assert [r.entry_type for r in _rows_by_entry(sqlite_session, entry_id)] == [None]
    assert mig.backfill_data(sqlite_session.connection()) == 1
    sqlite_session.expire_all()
    assert [r.entry_type for r in _rows_by_entry(sqlite_session, entry_id)] == ["TRANSFER_SETTLE"]


# ------------------------------------------------- CA-05/CA-06: cadena y alcance
def test_migration_0028_exists_and_reads_ledger_only():
    """`0028`: revision acotada (<=32 chars), sin imports de la app, sin escrituras."""
    assert MIGRATION_PATH.exists(), "falta migracion 0028_backfill_entry_type.py"
    assert len("0028_backfill_entry_type") <= 32
    content = MIGRATION_PATH.read_text(encoding="utf-8")
    for token in (
        'revision = "0028_backfill_entry_type"',
        'down_revision = "0027_movements_entry_type"',
        "movements_view",
        "journal_entries",
        "entry_type",
        "journal_entry_id",
        "entry_type IS NULL",
        "def backfill_data",
        "def reset_data",
        "def upgrade",
        "def downgrade",
    ):
        assert token in content, f"migracion sin {token}"
    assert "postgresql" in content, "falta la variante Postgres (UPDATE..FROM)"
    code = content.split('"""', 2)[2] if content.count('"""') >= 2 else content
    for forbidden in (
        "app.modules",
        "from app",
        "import app",
        "INSERT INTO",
        "DELETE FROM",
        "INTO ledger",
        "UPDATE ledger",
        "postings",
        "float(",
    ):
        assert forbidden not in code, f"la migracion no debe contener: {forbidden}"


# ------------------------------------------------- Postgres
def test_pg_backfill_populates_and_is_idempotent(db_session: Session):
    """En Postgres el backfill puebla y es no-op al repetirse (CA-01/CA-02)."""
    mig = _load_migration()
    account = _new_account(db_session)
    entry_id = uuid.uuid4()
    _journal(db_session, entry_id, "TRANSFER_SETTLE")
    _legacy_row(db_session, account, entry_id)
    db_session.commit()

    assert mig.backfill_data(db_session.connection()) == 1
    db_session.expire_all()
    assert [r.entry_type for r in _rows_by_entry(db_session, entry_id)] == ["TRANSFER_SETTLE"]
    assert mig.backfill_data(db_session.connection()) == 0


def test_pg_alembic_downgrade_upgrade_round_trip(test_engine):
    """`downgrade -1` deja `NULL` y `upgrade head` repuebla (CA-04)."""
    from sqlalchemy.orm import Session as SASession

    from tests.db_utils import (
        downgrade_in_process,
        resolve_test_database_url,
        run_migrations_in_process,
    )

    url = resolve_test_database_url()
    session = SASession(bind=test_engine, autoflush=False, expire_on_commit=False)
    try:
        orphans = session.execute(
            sa.text(
                "SELECT count(*) FROM accounts.movements_view mv "
                "WHERE mv.entry_type IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM ledger.journal_entries je "
                "WHERE je.id = mv.journal_entry_id)"
            )
        ).scalar()
        if orphans:
            pytest.skip(f"ciclo con perdida: {orphans} fila(s) poblada(s) sin asiento")
        marker = f"e2t37-{uuid.uuid4().hex[:8]}"
        account = _new_account(session, account_number=f"037-{uuid.uuid4().hex[:8]}")
        entry_id = uuid.uuid4()
        _journal(session, entry_id, "OWN_TRANSFER", description=marker)
        _legacy_row(session, account, entry_id, description=marker)
        session.commit()
        try:
            downgrade_in_process(url, "0027_movements_entry_type")
            session.expire_all()
            assert [r.entry_type for r in _rows_by_entry(session, entry_id)] == [None]
            run_migrations_in_process(url, "head")
            session.expire_all()
            assert [r.entry_type for r in _rows_by_entry(session, entry_id)] == ["OWN_TRANSFER"]
        finally:
            run_migrations_in_process(url, "head")
    finally:
        from app.modules.accounts.models import Account
        from app.modules.accounts.repository.movements import MovementView
        from app.modules.ledger.models import JournalEntry

        session.execute(sa.delete(MovementView).where(MovementView.description == marker))
        session.execute(sa.delete(JournalEntry).where(JournalEntry.description == marker))
        session.delete(session.get(Account, account.id))
        session.commit()
        session.close()
