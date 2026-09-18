"""Repositorio de cuentas y proyeccion de saldos `accounts` (E2-T01, HU05).

- Parte A (sin BD): metadatos de los modelos y migracion segun
  `03b#5` (`accounts`, `account_balances`, `daily_balance_snapshots`),
  puertos fase 3 (`BalancePort` de transactions, `AccountProjectionPort`
  de ledger) y reglas (sin `float`, sin `commit`, sin publicar eventos,
  sin FK entre schemas, sin imports top a otros modulos de negocio).
- Parte B (SQLite en memoria + schemas ATTACH): crear cuenta + balance
  inicial en cero y leerla, CK `available >= 0`, conflicto de version,
  `lock_and_get`, adaptadores que satisfacen los protocolos (incluye
  `execute_transfer` con el port real y `post_entry_and_update_balances`
  con la proyeccion real), consumidor idempotente, snapshots.
- Parte C (Postgres `db_session`): humo de integracion + migracion
  `up/down` de la revision propia en base de prueba; se omite sin BD.
"""

from __future__ import annotations

import inspect
import re
import uuid
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import Base

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0009_accounts_repository.py"
)
MODELS_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "accounts" / "models" / "__init__.py"
)
REPO_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "accounts"
    / "repository"
    / "__init__.py"
)


# ---------------------------------------------------------------- Parte A: modelo
def test_tables_registered_with_schema_columns_constraints():
    import app.modules.accounts.models as m  # noqa: F401 (registro)

    assert "accounts.accounts" in Base.metadata.tables
    assert "accounts.account_balances" in Base.metadata.tables
    assert "accounts.daily_balance_snapshots" in Base.metadata.tables

    accounts = Base.metadata.tables["accounts.accounts"]
    assert accounts.schema == "accounts"
    cols = {c.name for c in accounts.columns}
    assert cols == {
        "id",
        "user_id",
        "account_number",
        "type",
        "currency",
        "status",
        "ledger_account_id",
        "ledger_hold_account_id",
        "opened_at",
        "created_at",
        "updated_at",
    }, f"columnas 03b#5.1, recibido: {sorted(cols)}"
    assert isinstance(accounts.columns["account_number"].type, sa.String)
    assert isinstance(accounts.columns["currency"].type, sa.String)
    uq = [c for c in accounts.constraints if isinstance(c, sa.UniqueConstraint)]
    assert any(c.name for c in uq if c.name == "uq_accounts_account_number") or any(
        {c.name for c in con.columns} == {"account_number"} for con in uq
    ), "account_number debe ser UQ"

    balances = Base.metadata.tables["accounts.account_balances"]
    assert balances.schema == "accounts"
    bcols = {c.name for c in balances.columns}
    assert bcols == {
        "account_id",
        "currency",
        "available_minor",
        "held_minor",
        "version",
        "updated_at",
    }, f"columnas 03b#5.2, recibido: {sorted(bcols)}"
    assert isinstance(balances.columns["available_minor"].type, sa.BigInteger)
    assert isinstance(balances.columns["held_minor"].type, sa.BigInteger)
    assert isinstance(balances.columns["version"].type, sa.Integer)
    assert isinstance(balances.columns["currency"].type, sa.String)
    pk = {c.name for c in balances.primary_key.columns}
    assert pk == {"account_id"}, "PK debe ser account_id (1:1)"
    checks = " ".join(
        str(c.sqltext) for c in balances.constraints if isinstance(c, sa.CheckConstraint)
    )
    assert "available_minor >= 0" in checks
    assert "held_minor >= 0" in checks

    snaps = Base.metadata.tables["accounts.daily_balance_snapshots"]
    assert snaps.schema == "accounts"
    scols = {c.name for c in snaps.columns}
    assert scols == {
        "account_id",
        "snapshot_date",
        "available_minor",
        "held_minor",
    }, f"columnas 03b#5.5, recibido: {sorted(scols)}"
    spk = {c.name for c in snaps.primary_key.columns}
    assert spk == {"account_id", "snapshot_date"}, "PK compuesta (account_id, snapshot_date)"


def test_no_foreign_keys_to_other_schemas():
    import app.modules.accounts.models as m  # noqa: F401 (registro)

    for key in (
        "accounts.accounts",
        "accounts.account_balances",
        "accounts.daily_balance_snapshots",
    ):
        table = Base.metadata.tables[key]
        for fk in table.foreign_keys:
            target = fk.column.table
            assert (
                target.schema == "accounts"
            ), f"FK fuera del schema propio: {key} -> {target.schema}.{target.name}"


def test_migration_0009_exists_and_matches_models():
    assert MIGRATION_PATH.exists(), "falta migracion 0009_accounts_repository.py"
    content = MIGRATION_PATH.read_text(encoding="utf-8")
    for token in (
        'revision = "0009_accounts_repository"',
        'down_revision = "0008_ledger_daily_closing"',
        '"accounts"',
        '"account_balances"',
        '"daily_balance_snapshots"',
        "uq_accounts_account_number",
        "fk_account_balances_account",
        "fk_daily_snapshots_account",
        "ck_account_balances_available_min",
        "ck_account_balances_held_min",
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
    ):
        assert other not in content, f"la migracion no debe tocar otro schema: {other}"


def test_ports_signatures_match_seams():
    from app.modules.accounts import repository as repo

    # BalancePort (transactions/service): lock_and_get(session, account_id),
    # apply_delta(session, account_id, delta_minor, currency).
    assert list(inspect.signature(repo.AccountsBalanceAdapter.lock_and_get).parameters) == [
        "self",
        "session",
        "account_id",
    ]
    assert list(inspect.signature(repo.AccountsBalanceAdapter.apply_delta).parameters) == [
        "self",
        "session",
        "account_id",
        "delta_minor",
        "currency",
    ]
    # AccountProjectionPort (ledger/repository/balances): apply_projection
    # (session, ledger_account_id, signed_delta_minor, currency).
    assert list(
        inspect.signature(repo.AccountsLedgerProjectionAdapter.apply_projection).parameters
    ) == ["self", "session", "ledger_account_id", "signed_delta_minor", "currency"]

    from app.modules.ledger.repository.balances import AccountProjectionPort as LedgerPort
    from app.modules.transactions.service import BalancePort as TxPort

    for port, method, params in (
        (TxPort, "lock_and_get", ["self", "session", "account_id"]),
        (TxPort, "apply_delta", ["self", "session", "account_id", "delta_minor", "currency"]),
        (
            LedgerPort,
            "apply_projection",
            ["self", "session", "ledger_account_id", "signed_delta_minor", "currency"],
        ),
    ):
        assert list(inspect.signature(getattr(port, method)).parameters) == params
    # Los adaptadores exponen los mismos metodos con las mismas firmas.
    assert list(inspect.signature(repo.AccountsBalanceAdapter.lock_and_get).parameters) == list(
        inspect.signature(TxPort.lock_and_get).parameters
    )
    assert list(inspect.signature(repo.AccountsBalanceAdapter.apply_delta).parameters) == list(
        inspect.signature(TxPort.apply_delta).parameters
    )
    assert list(
        inspect.signature(repo.AccountsLedgerProjectionAdapter.apply_projection).parameters
    ) == list(inspect.signature(LedgerPort.apply_projection).parameters)


def test_repository_has_no_float_commit_publish_nor_cross_imports():
    content = REPO_PATH.read_text(encoding="utf-8")
    assert "float(" not in content
    assert ".commit(" not in content, "flush sin commit: quien llama decide"
    assert "publish(" not in content, "nada de publish dentro del repositorio"
    assert "outbox.insert_event" not in content and "record(" not in content.replace(
        "record_snapshot(", ""
    ).replace("_record", ""), "la emision queda para outbox/E5-T05"
    for pattern in (
        r"session\.delete",
        r"sa\.delete\s*\(",
        r"sa\.update\s*\(",
        r"def\s+(update|delete|remove|purge)_",
    ):
        assert not re.search(pattern, content), f"repositorio con {pattern}"
    # Sin imports top a otros modulos de negocio (solo perezosos dentro de funciones).
    for line in content.splitlines():
        if line.startswith(("from app.modules.", "import app.modules.")):
            assert line.startswith(
                ("from app.modules.accounts.", "from app.modules.shared.")
            ), f"import top no permitido: {line}"
        if line.startswith("from app.core"):
            assert "outbox" not in line, f"import top de outbox no permitido: {line}"
    assert "ensure_customer_accounts" in content, "debe asegurar subcuentas via fachada ledger"
    assert "try_mark_processed" in content, "consumidor idempotente via shared"
    models_content = MODELS_PATH.read_text(encoding="utf-8")
    assert "float(" not in models_content


# ---------------------------------------------------------------- Parte B: SQLite
@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schemas `accounts`/`ledger`/`shared` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401
    import app.modules.transactions.models as _t  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "ledger", "shared", "transactions"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["accounts.accounts"],
            Base.metadata.tables["accounts.account_balances"],
            Base.metadata.tables["accounts.daily_balance_snapshots"],
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["ledger.journal_entries"],
            Base.metadata.tables["ledger.postings"],
            Base.metadata.tables["ledger.ledger_balances"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["shared.processed_events"],
            Base.metadata.tables["transactions.transactions"],
            Base.metadata.tables["transactions.transaction_status_history"],
            Base.metadata.tables["transactions.holds"],
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
        "account_number": f"001-{uuid.uuid4().hex[:8]}",
        "type": "AHORRO",
        "currency": "PEN",
    }
    params.update(kwargs)
    return repo.create_account(session, **params)


def test_create_account_with_initial_zero_balance_and_read(sqlite_session: Session):
    from app.modules.accounts import repository as repo

    user_id = uuid.uuid4()
    account = repo.create_account(
        sqlite_session, user_id=user_id, account_number="001-0001", type="AHORRO", currency="PEN"
    )
    assert account.id is not None
    assert account.ledger_account_id is not None
    assert account.ledger_hold_account_id is not None
    assert account.ledger_account_id != account.ledger_hold_account_id

    balance = repo.get_balance(sqlite_session, account.id)
    assert (balance.available_minor, balance.held_minor, balance.version) == (0, 0, 0)
    assert balance.currency == "PEN"

    assert repo.get_account(sqlite_session, account.id).account_number == "001-0001"
    assert repo.get_by_number(sqlite_session, "001-0001").id == account.id
    assert [a.id for a in repo.list_by_user(sqlite_session, user_id)] == [account.id]

    # Subcuentas reales en el ledger con los codigos 2000/2100-<cuenta>.
    from app.modules.ledger import repository as ledger_repo

    codes = {a.code for a in ledger_repo.get_by_owner(sqlite_session, account.id)}
    assert codes == {f"2000-{account.id}", f"2100-{account.id}"}


def test_negative_delta_rejected_and_check_constraint(sqlite_session: Session):

    from app.modules.accounts import repository as repo
    from app.modules.accounts.models import AccountBalance

    account = _new_account(sqlite_session)
    with pytest.raises(ValueError, match="negativo"):
        repo.apply_delta(sqlite_session, account.id, available_delta=-1, currency="PEN")
    row = repo.get_balance(sqlite_session, account.id)
    assert (row.available_minor, row.held_minor, row.version) == (0, 0, 0)

    # CK a nivel BD: insercion cruda negativa falla aunque se evite el repositorio.
    dup = AccountBalance(
        account_id=uuid.uuid4(), currency="PEN", available_minor=-5, held_minor=0, version=0
    )
    sqlite_session.add(dup)
    with pytest.raises(IntegrityError):
        sqlite_session.flush()
    sqlite_session.rollback()


def test_version_increments_and_conflict_rejected(sqlite_session: Session):
    from app.modules.accounts import repository as repo
    from app.modules.accounts.repository import VersionConflictError

    account = _new_account(sqlite_session)
    row = repo.apply_delta(sqlite_session, account.id, available_delta=5_000, currency="PEN")
    assert (row.available_minor, row.version) == (5_000, 1)
    row = repo.apply_delta(
        sqlite_session,
        account.id,
        available_delta=1_000,
        held_delta=2_000,
        currency="PEN",
        expected_version=1,
    )
    assert (row.available_minor, row.held_minor, row.version) == (6_000, 2_000, 2)
    with pytest.raises(VersionConflictError):
        repo.apply_delta(
            sqlite_session,
            account.id,
            available_delta=1_000,
            currency="PEN",
            expected_version=1,
        )
    row = repo.get_balance(sqlite_session, account.id)
    assert (row.available_minor, row.held_minor, row.version) == (6_000, 2_000, 2)


def test_lock_and_get_returns_locked_row(sqlite_session: Session):
    from app.modules.accounts import repository as repo

    account = _new_account(sqlite_session)
    row = repo.lock_and_get(sqlite_session, account.id)
    assert row.account_id == account.id
    assert row.available_minor == 0
    # Orden estable multi-fila: mismo conjunto, mismo orden.
    other = _new_account(sqlite_session)
    locked = repo.lock_many_in_order(sqlite_session, [other.id, account.id])
    assert [str(k) for k in locked] == sorted([str(account.id), str(other.id)])
    with pytest.raises(LookupError):
        repo.lock_and_get(sqlite_session, uuid.uuid4())


def test_balance_port_adapter_drives_motor(sqlite_session: Session):
    """El adaptador satisface `BalancePort`: `execute_transfer` lo usa de punta a punta."""
    from app.modules.accounts import repository as repo
    from app.modules.transactions.service import execute_transfer

    source = _new_account(sqlite_session, account_number="001-0002")
    target = _new_account(sqlite_session, account_number="001-0003")
    repo.apply_delta(sqlite_session, source.id, available_delta=50_000, currency="PEN")

    tx = execute_transfer(
        sqlite_session,
        source_account_id=source.id,
        target_account_id=target.id,
        amount_minor=10_000,
        currency="PEN",
        balance_port=repo.ACCOUNTS_BALANCE_PORT,
    )
    assert tx.status == "SETTLED"
    assert repo.get_balance(sqlite_session, source.id).available_minor == 40_000
    assert repo.get_balance(sqlite_session, target.id).available_minor == 10_000


def test_ledger_projection_hold_flow_keeps_invariant(sqlite_session: Session):
    """Flujo hold+settle del motor: port (available) + proyeccion (held).

    Replica `execute_transfer`: el port mueve `available` y el asiento
    (`DEBIT 2000-S -> CREDIT 2100-S`) refleja `held` via la proyeccion
    (la pata de disponible se omite: contarla duplicaria). La suma
    `available + held` queda invariante (`03c#15.1`).
    """
    from app.modules.accounts import repository as repo
    from app.modules.ledger.repository import post_entry_and_update_balances

    source = _new_account(sqlite_session, account_number="001-0004")
    target = _new_account(sqlite_session, account_number="001-0005")
    repo.apply_delta(sqlite_session, source.id, available_delta=20_000, currency="PEN")

    # Paso hold del motor: port (-8000 en available) + asiento con proyeccion.
    repo.apply_delta(sqlite_session, source.id, available_delta=-8_000, currency="PEN")
    post_entry_and_update_balances(
        sqlite_session,
        entry_type="TRANSFER_HOLD",
        postings=[
            {
                "ledger_account_id": source.ledger_account_id,
                "direction": "DEBIT",
                "amount_minor": 8_000,
                "currency": "PEN",
                "account_ref": source.id,
            },
            {
                "ledger_account_id": source.ledger_hold_account_id,
                "direction": "CREDIT",
                "amount_minor": 8_000,
                "currency": "PEN",
                "account_ref": source.id,
            },
        ],
        account_projection=repo.ACCOUNTS_LEDGER_PROJECTION,
    )
    row = repo.get_balance(sqlite_session, source.id)
    assert (row.available_minor, row.held_minor) == (12_000, 8_000)
    assert row.available_minor + row.held_minor == 20_000

    # Paso settle del motor: port (+8000 al destino) + asiento
    # (`DEBIT 2100-S -> CREDIT 2000-D`) con proyeccion.
    repo.apply_delta(sqlite_session, target.id, available_delta=8_000, currency="PEN")
    post_entry_and_update_balances(
        sqlite_session,
        entry_type="TRANSFER_SETTLE",
        postings=[
            {
                "ledger_account_id": source.ledger_hold_account_id,
                "direction": "DEBIT",
                "amount_minor": 8_000,
                "currency": "PEN",
                "account_ref": source.id,
            },
            {
                "ledger_account_id": target.ledger_account_id,
                "direction": "CREDIT",
                "amount_minor": 8_000,
                "currency": "PEN",
                "account_ref": target.id,
            },
        ],
        account_projection=repo.ACCOUNTS_LEDGER_PROJECTION,
    )
    src = repo.get_balance(sqlite_session, source.id)
    dst = repo.get_balance(sqlite_session, target.id)
    assert (src.available_minor, src.held_minor) == (12_000, 0)
    assert (dst.available_minor, dst.held_minor) == (8_000, 0)
    assert (src.available_minor + src.held_minor) + (dst.available_minor + dst.held_minor) == 20_000


def test_consumer_is_idempotent(sqlite_session: Session):
    from app.modules.accounts import repository as repo

    account = _new_account(sqlite_session)
    repo.apply_delta(sqlite_session, account.id, available_delta=20_000, currency="PEN")
    repo.apply_delta(sqlite_session, account.id, available_delta=-8_000, currency="PEN")
    # Asiento hold del motor: DEBIT disponible (firmado +8000, omitido: es
    # dueno del port) + CREDIT retenido (firmado -8000 -> held +8000).
    postings = [
        {
            "ledger_account_id": account.ledger_account_id,
            "signed_delta_minor": 8_000,
            "currency": "PEN",
        },
        {
            "ledger_account_id": account.ledger_hold_account_id,
            "signed_delta_minor": -8_000,
            "currency": "PEN",
        },
    ]
    event_id = uuid.uuid4()
    assert (
        repo.handle_ledger_entry_posted(
            sqlite_session, event_id=event_id, entry_id=uuid.uuid4(), postings=postings
        )
        is True
    )
    row = repo.get_balance(sqlite_session, account.id)
    assert (row.available_minor, row.held_minor) == (12_000, 8_000)
    # Duplicado: se ignora sin re-aplicar.
    assert (
        repo.handle_ledger_entry_posted(
            sqlite_session, event_id=event_id, entry_id=uuid.uuid4(), postings=postings
        )
        is False
    )
    row = repo.get_balance(sqlite_session, account.id)
    assert (row.available_minor, row.held_minor) == (12_000, 8_000)


def test_snapshots_record_and_list(sqlite_session: Session):
    from app.modules.accounts import repository as repo

    account = _new_account(sqlite_session)
    repo.apply_delta(sqlite_session, account.id, available_delta=9_000, currency="PEN")
    snap = repo.record_snapshot(sqlite_session, account.id, date(2026, 9, 17))
    assert (snap.available_minor, snap.held_minor) == (9_000, 0)
    assert repo.get_snapshot(sqlite_session, account.id, date(2026, 9, 17)).available_minor == 9_000
    assert [s.snapshot_date for s in repo.list_snapshots(sqlite_session, account.id)] == [
        date(2026, 9, 17)
    ]


# ---------------------------------------------------------------- Parte C: Postgres
def test_integration_postgres_create_and_snapshot(db_session: Session):
    from app.modules.accounts import repository as repo

    account = repo.create_account(
        db_session,
        user_id=uuid.uuid4(),
        account_number=f"001-{uuid.uuid4().hex[:8]}",
        type="CORRIENTE",
        currency="PEN",
    )
    assert repo.get_balance(db_session, account.id).available_minor == 0
    assert repo.lock_and_get(db_session, account.id).account_id == account.id
    repo.apply_delta(db_session, account.id, available_delta=1_500, currency="PEN")
    snap = repo.record_snapshot(db_session, account.id, date(2026, 9, 17))
    assert (snap.available_minor, snap.held_minor) == (1_500, 0)


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
        assert inspect(db_session.get_bind()).has_table("accounts", schema="accounts")
        _alembic("downgrade", "0008_ledger_daily_closing")
        assert not inspect(db_session.get_bind()).has_table("accounts", schema="accounts")
        assert not inspect(db_session.get_bind()).has_table("account_balances", schema="accounts")
        assert not inspect(db_session.get_bind()).has_table(
            "daily_balance_snapshots", schema="accounts"
        )
    finally:
        _alembic("upgrade", "head")
    assert inspect(db_session.get_bind()).has_table("accounts", schema="accounts")
    assert inspect(db_session.get_bind()).has_table("account_balances", schema="accounts")
    assert inspect(db_session.get_bind()).has_table("daily_balance_snapshots", schema="accounts")
