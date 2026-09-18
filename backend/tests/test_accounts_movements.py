"""Proyeccion de movimientos `accounts.movements_view` (E2-T04, HU05 CA-03).

- Parte A (sin BD): metadatos del modelo y migracion segun `03b#5.4`
  (columnas exactas, UQ natural, indice `(account_id, created_at DESC)`,
  FK solo autocontenida) y reglas (sin `float`, sin `commit`, sin publicar
  eventos, sin imports a `ledger`, consumidor `accounts-movements`).
- Parte B (SQLite en memoria + schemas ATTACH): evento genera filas
  correctas (monto/fecha/direccion/cuenta, con `account_ref` y con fallback
  por `ledger_account_id`); reproceso no duplica; refresh reconstruye tras
  borrado manual (gana el origen).
- Parte C (Postgres `db_session`): humo de integracion + migracion
  `up/down` de la revision propia en base de prueba; se omite sin BD.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0010_accounts_movements.py"
)
MOVEMENTS_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "accounts"
    / "repository"
    / "movements.py"
)

EXPECTED_COLUMNS = {
    "account_id",
    "journal_entry_id",
    "transaction_id",
    "direction",
    "amount_minor",
    "currency",
    "description",
    "value_date",
    "created_at",
}
NATURAL_KEY = {"journal_entry_id", "account_id", "direction", "amount_minor", "currency"}


# ---------------------------------------------------------------- Parte A: modelo
def test_table_registered_with_exact_columns_and_natural_key():
    import app.modules.accounts.repository.movements as m  # noqa: F401 (registro)

    assert "accounts.movements_view" in Base.metadata.tables
    table = Base.metadata.tables["accounts.movements_view"]
    assert table.schema == "accounts"
    cols = {c.name for c in table.columns}
    assert cols == EXPECTED_COLUMNS, f"columnas 03b#5.4, recibido: {sorted(cols)}"
    assert isinstance(table.columns["amount_minor"].type, sa.BigInteger)
    assert isinstance(table.columns["direction"].type, sa.String)
    assert isinstance(table.columns["currency"].type, sa.String)
    pk = {c.name for c in table.primary_key.columns}
    assert pk == NATURAL_KEY, f"UQ natural como PK, recibido: {sorted(pk)}"
    checks = " ".join(
        str(c.sqltext) for c in table.constraints if isinstance(c, sa.CheckConstraint)
    )
    assert "direction IN ('DEBIT', 'CREDIT')" in checks
    assert "amount_minor > 0" in checks
    idx_cols = [{c.name for c in idx.columns} for idx in table.indexes]
    assert {"account_id", "created_at"} in idx_cols, "indice (account_id, created_at DESC)"


def test_no_foreign_keys_to_other_schemas():
    import app.modules.accounts.repository.movements as m  # noqa: F401 (registro)

    table = Base.metadata.tables["accounts.movements_view"]
    for fk in table.foreign_keys:
        target = fk.column.table
        assert (target.schema, target.name) == (
            "accounts",
            "accounts",
        ), f"FK fuera del schema propio: movements_view -> {target.schema}.{target.name}"


def test_migration_0010_exists_and_matches_models():
    assert MIGRATION_PATH.exists(), "falta migracion 0010_accounts_movements.py"
    content = MIGRATION_PATH.read_text(encoding="utf-8")
    # El codigo ejecutable (sin el docstring explicativo) no debe tocar otro schema.
    code = content.split('"""', 2)[2] if content.count('"""') >= 2 else content
    for token in (
        'revision = "0010_accounts_movements"',
        'down_revision = "0009_accounts_repository"',
        '"movements_view"',
        "journal_entry_id",
        "fk_movements_account",
        "ck_movements_direction",
        "ix_movements_account_created",
        "created_at DESC",
        "def upgrade",
        "def downgrade",
    ):
        assert token in content, f"migracion sin {token}"
    for other in (
        'schema="ledger"',
        "schema='ledger'",
        'schema="transactions"',
        'schema="identity"',
        "ledger_balances",
        "journal_entries",
        "postings",
        "float(",
    ):
        assert other not in code, f"la migracion no debe tocar otro schema: {other}"


def test_movements_module_rules_and_consumer():
    from app.modules.accounts.repository import movements as mov

    assert mov.MOVEMENTS_CONSUMER == "accounts-movements"
    content = MOVEMENTS_PATH.read_text(encoding="utf-8")
    assert "float(" not in content
    assert ".commit(" not in content, "flush sin commit: quien llama decide"
    assert "publish(" not in content, "nada de publish dentro del repositorio"
    assert "try_mark_processed" in content, "consumidor idempotente via shared"
    # Sin imports a otros modulos de negocio (solo perezoso a shared dentro de funciones).
    for line in content.splitlines():
        if line.startswith(("from app.modules.", "import app.modules.")):
            assert line.startswith(
                ("from app.modules.accounts.", "from app.modules.shared.")
            ), f"import top no permitido: {line}"
    assert "from app.modules.ledger" not in content
    assert "import app.modules.ledger" not in content
    # El unico DELETE permitido es el refresh de la propia proyeccion.
    for pattern in (r"session\.delete", r"sa\.update\s*\("):
        assert not re.search(pattern, content), f"repositorio con {pattern}"
    for match in re.finditer(r"sa\.delete\s*\(([^)]+)\)", content):
        assert "MovementView" in match.group(1), "DELETE solo sobre la proyeccion propia"


def test_repository_reexports_movements():
    from app.modules.accounts import repository as repo

    for name in (
        "MovementView",
        "MOVEMENTS_CONSUMER",
        "handle_movement_event",
        "rebuild_entry_movements",
        "list_movements",
        "resolve_movement_account_id",
    ):
        assert name in repo.__all__, f"re-export faltante: {name}"
        assert getattr(repo, name) is not None


# ---------------------------------------------------------------- Parte B: SQLite
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
            Base.metadata.tables["accounts.account_balances"],
            Base.metadata.tables["accounts.movements_view"],
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["ledger.journal_entries"],
            Base.metadata.tables["ledger.postings"],
            Base.metadata.tables["ledger.ledger_balances"],
            Base.metadata.tables["shared.outbox"],
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
    from app.modules.accounts import repository as repo

    params = {
        "user_id": uuid.uuid4(),
        "account_number": f"002-{uuid.uuid4().hex[:8]}",
        "type": "AHORRO",
        "currency": "PEN",
    }
    params.update(kwargs)
    return repo.create_account(session, **params)


def _transfer_postings(source, target, amount=10_000, currency="PEN"):
    """Postings de transferencia `DEBIT 2000-S -> CREDIT 2000-D` (forma ledger)."""
    return [
        {
            "ledger_account_id": source.ledger_account_id,
            "direction": "DEBIT",
            "amount_minor": amount,
            "currency": currency,
            "account_ref": source.id,
        },
        {
            "ledger_account_id": target.ledger_account_id,
            "direction": "CREDIT",
            "amount_minor": amount,
            "currency": currency,
            "account_ref": target.id,
        },
    ]


def test_event_projects_rows_with_amount_date_direction_account(sqlite_session: Session):
    from app.modules.accounts import repository as repo

    source = _new_account(sqlite_session, account_number="002-0001")
    target = _new_account(sqlite_session, account_number="002-0002")
    entry_id, tx_id = uuid.uuid4(), uuid.uuid4()
    postings = _transfer_postings(source, target)
    assert (
        repo.handle_movement_event(
            sqlite_session,
            event_id=uuid.uuid4(),
            entry_id=entry_id,
            postings=postings,
            transaction_id=tx_id,
            description="Transferencia",
            value_date=date(2026, 9, 17),
        )
        is True
    )

    rows = {r.account_id: r for r in repo.list_movements(sqlite_session, source.id)}
    assert set(rows) == {source.id}
    debit = rows[source.id]
    assert (debit.direction, debit.amount_minor, debit.currency) == ("DEBIT", 10_000, "PEN")
    assert debit.journal_entry_id == entry_id
    assert debit.transaction_id == tx_id
    assert debit.description == "Transferencia"
    assert debit.value_date == date(2026, 9, 17)

    credit = repo.list_movements(sqlite_session, target.id)
    assert len(credit) == 1
    assert (credit[0].direction, credit[0].amount_minor) == ("CREDIT", 10_000)
    assert credit[0].journal_entry_id == entry_id


def test_account_resolution_fallback_and_system_skip(sqlite_session: Session):
    from app.modules.accounts import repository as repo

    source = _new_account(sqlite_session, account_number="002-0003")
    entry_id = uuid.uuid4()
    postings = [
        # Sin account_ref: fallback por ledger_account_id (subcuenta de retenido).
        {
            "ledger_account_id": source.ledger_hold_account_id,
            "direction": "CREDIT",
            "amount_minor": 5_000,
            "currency": "PEN",
            "account_ref": None,
        },
        # Cuenta del sistema sin dueno: se omite.
        {
            "ledger_account_id": uuid.uuid4(),
            "direction": "DEBIT",
            "amount_minor": 5_000,
            "currency": "PEN",
            "account_ref": None,
        },
    ]
    assert (
        repo.handle_movement_event(
            sqlite_session, event_id=uuid.uuid4(), entry_id=entry_id, postings=postings
        )
        is True
    )
    rows = repo.list_movements(sqlite_session, source.id)
    assert len(rows) == 1
    assert (rows[0].direction, rows[0].amount_minor) == ("CREDIT", 5_000)


def test_reprocessing_does_not_duplicate(sqlite_session: Session):
    from app.modules.accounts import repository as repo

    source = _new_account(sqlite_session, account_number="002-0004")
    target = _new_account(sqlite_session, account_number="002-0005")
    entry_id = uuid.uuid4()
    postings = _transfer_postings(source, target)
    event_id = uuid.uuid4()
    assert (
        repo.handle_movement_event(
            sqlite_session, event_id=event_id, entry_id=entry_id, postings=postings
        )
        is True
    )
    # Reproceso con el mismo event_id: se ignora sin re-aplicar.
    assert (
        repo.handle_movement_event(
            sqlite_session, event_id=event_id, entry_id=entry_id, postings=postings
        )
        is False
    )
    assert len(repo.list_movements(sqlite_session, source.id)) == 1
    assert len(repo.list_movements(sqlite_session, target.id)) == 1
    # Re-entrega con distinto event_id: la UQ natural tampoco duplica.
    assert (
        repo.handle_movement_event(
            sqlite_session, event_id=uuid.uuid4(), entry_id=entry_id, postings=postings
        )
        is True
    )
    assert len(repo.list_movements(sqlite_session, source.id)) == 1
    assert len(repo.list_movements(sqlite_session, target.id)) == 1


def test_refresh_rebuilds_after_manual_delete(sqlite_session: Session):
    from app.modules.accounts import repository as repo
    from app.modules.accounts.repository.movements import MovementView

    source = _new_account(sqlite_session, account_number="002-0006")
    target = _new_account(sqlite_session, account_number="002-0007")
    entry_id, tx_id = uuid.uuid4(), uuid.uuid4()
    postings = _transfer_postings(source, target)
    repo.handle_movement_event(
        sqlite_session,
        event_id=uuid.uuid4(),
        entry_id=entry_id,
        postings=postings,
        transaction_id=tx_id,
        description="Transferencia",
        value_date=date(2026, 9, 17),
    )
    before = {
        (
            r.journal_entry_id,
            r.account_id,
            r.direction,
            r.amount_minor,
            r.currency,
            r.transaction_id,
            r.description,
            r.value_date,
        )
        for r in repo.list_movements(sqlite_session, source.id)
    } | {
        (
            r.journal_entry_id,
            r.account_id,
            r.direction,
            r.amount_minor,
            r.currency,
            r.transaction_id,
            r.description,
            r.value_date,
        )
        for r in repo.list_movements(sqlite_session, target.id)
    }
    assert len(before) == 2

    # Borrado manual (divergencia): gana el origen al refrescar.
    sqlite_session.execute(sa.delete(MovementView).where(MovementView.journal_entry_id == entry_id))
    sqlite_session.flush()
    assert repo.list_movements(sqlite_session, source.id) == []
    assert repo.list_movements(sqlite_session, target.id) == []

    rebuilt = repo.rebuild_entry_movements(
        sqlite_session,
        entry_id=entry_id,
        postings=postings,
        transaction_id=tx_id,
        description="Transferencia",
        value_date=date(2026, 9, 17),
    )
    assert rebuilt == 2
    after = {
        (
            r.journal_entry_id,
            r.account_id,
            r.direction,
            r.amount_minor,
            r.currency,
            r.transaction_id,
            r.description,
            r.value_date,
        )
        for r in repo.list_movements(sqlite_session, source.id)
    } | {
        (
            r.journal_entry_id,
            r.account_id,
            r.direction,
            r.amount_minor,
            r.currency,
            r.transaction_id,
            r.description,
            r.value_date,
        )
        for r in repo.list_movements(sqlite_session, target.id)
    }
    assert after == before


# ---------------------------------------------------------------- Parte C: Postgres
def test_integration_postgres_event_and_list(db_session: Session):
    from app.modules.accounts import repository as repo

    account = repo.create_account(
        db_session,
        user_id=uuid.uuid4(),
        account_number=f"002-{uuid.uuid4().hex[:8]}",
        type="AHORRO",
        currency="PEN",
    )
    entry_id = uuid.uuid4()
    assert (
        repo.handle_movement_event(
            db_session,
            event_id=uuid.uuid4(),
            entry_id=entry_id,
            postings=[
                {
                    "ledger_account_id": account.ledger_account_id,
                    "direction": "CREDIT",
                    "amount_minor": 1_500,
                    "currency": "PEN",
                    "account_ref": account.id,
                }
            ],
            description="Abono",
            value_date=date(2026, 9, 17),
        )
        is True
    )
    rows = repo.list_movements(db_session, account.id)
    assert len(rows) == 1
    assert (rows[0].direction, rows[0].amount_minor, rows[0].value_date) == (
        "CREDIT",
        1_500,
        date(2026, 9, 17),
    )


def test_migration_up_down_on_test_database(db_session: Session):
    """`up/down` de la revision propia en base de prueba (restaura `head` al final)."""
    import subprocess
    import sys

    from sqlalchemy import inspect

    from tests.db_utils import BACKEND_DIR, resolve_test_database_url

    url = resolve_test_database_url()

    def _alembic(*args: str) -> None:
        import os

        env = os.environ.copy()
        env["DATABASE_URL"] = url
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=str(BACKEND_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert (
            proc.returncode == 0
        ), f"alembic {' '.join(args)} fallo:\n{proc.stdout}\n{proc.stderr}"

    try:
        assert inspect(db_session.get_bind()).has_table("movements_view", schema="accounts")
        _alembic("downgrade", "0009_accounts_repository")
        assert not inspect(db_session.get_bind()).has_table("movements_view", schema="accounts")
        assert inspect(db_session.get_bind()).has_table("accounts", schema="accounts")
    finally:
        _alembic("upgrade", "head")
    assert inspect(db_session.get_bind()).has_table("movements_view", schema="accounts")
