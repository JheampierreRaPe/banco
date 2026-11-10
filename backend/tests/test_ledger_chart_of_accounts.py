"""Catalogo contable y subcuentas por cliente (E5-T09, HU18, CA-01 base).

- Parte A (sin BD): metadatos del modelo y migracion segun `03b#7.1` y `04#2.1`.
- Parte B (SQLite en memoria + schema ATTACH): catalogo cargado e idempotente,
  creacion idempotente de subcuentas `2000-<cuenta>` / `2100-<cuenta>`,
  errores de `account_ref`/`currency`, fachada `ensure_customer_accounts`.
- Parte C (Postgres `db_session`): humo de integracion; se omite si no hay BD.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base


# ---------------------------------------------------------------- Parte A: modelo
def test_tables_registered_with_schema_and_constraints():
    import app.modules.ledger.models as m  # noqa: F401 (registro)

    key = "ledger.ledger_accounts"
    assert key in Base.metadata.tables, "falta tabla ledger.ledger_accounts"
    table = Base.metadata.tables[key]
    assert table.schema == "ledger"
    cols = {c.name for c in table.columns}
    for expected in (
        "id",
        "code",
        "name",
        "type",
        "currency",
        "owner_type",
        "owner_ref",
        "parent_account_id",
        "is_system",
        "created_at",
    ):
        assert expected in cols, f"falta columna ledger_accounts.{expected}"
    checks = {c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}
    assert "ck_ledger_accounts_type" in checks
    uniques = {c.name for c in table.constraints if isinstance(c, sa.UniqueConstraint)}
    assert "uq_ledger_accounts_code" in uniques
    idx_names = {i.name for i in table.indexes}
    assert "ix_ledger_accounts_owner_ref" in idx_names
    assert "ix_ledger_accounts_type" in idx_names


def test_no_foreign_keys_to_other_schemas():
    table = Base.metadata.tables["ledger.ledger_accounts"]
    for fk in table.foreign_keys:
        target = fk.column.table
        assert target.schema == "ledger", (
            f"FK fuera del schema propio: ledger_accounts -> " f"{target.schema}.{target.name}"
        )


def test_migration_0003_exists_and_matches_models():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0003_ledger_chart_of_accounts.py"
    )
    assert path.exists(), "falta migracion 0003_ledger_chart_of_accounts.py"
    content = path.read_text(encoding="utf-8")
    for token in (
        'down_revision = "0002_transactions_persistence"',
        '"ledger_accounts"',
        '"ledger"',
        "uq_ledger_accounts_code",
        "ck_ledger_accounts_type",
        "ix_ledger_accounts_owner_ref",
        "ON CONFLICT (code) DO NOTHING",
    ):
        assert token in content, f"migracion sin {token}"
    for code in (
        '"1000"',
        '"2000"',
        '"2100"',
        '"2200"',
        '"3000"',
        '"4000"',
        '"4100"',
        '"4200"',
        '"5000"',
        '"6000"',
        '"9100"',
    ):
        assert code in content, f"catalogo sin cuenta {code}"


def test_domain_catalog_and_codes():
    from app.modules.ledger.domain.chart import (
        CATALOG_CODES,
        SYSTEM_CATALOG,
        customer_codes,
        validate_catalog,
    )

    assert len(CATALOG_CODES) == 11
    assert validate_catalog() == []
    assert validate_catalog(SYSTEM_CATALOG) == []
    ref = uuid.uuid4()
    available, hold = customer_codes(ref)
    assert available == f"2000-{ref}"
    assert hold == f"2100-{ref}"


# ---------------------------------------------------------------- Parte B: SQLite
@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schema `ledger` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.ledger.models as m  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS ledger")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[Base.metadata.tables["ledger.ledger_accounts"]],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_catalog_loaded_and_idempotent(sqlite_session: Session):
    from app.modules.ledger import repository as repo
    from app.modules.ledger.domain.chart import CATALOG_CODES

    first = repo.ensure_system_catalog(sqlite_session)
    assert set(first) >= set(CATALOG_CODES)
    assert all(a.is_system for a in first.values())
    second = repo.ensure_system_catalog(sqlite_session)
    assert {k: v.id for k, v in first.items()} == {k: v.id for k, v in second.items()}
    count = sqlite_session.scalar(
        sa.select(sa.func.count()).select_from(Base.metadata.tables["ledger.ledger_accounts"])
    )
    assert count == 11


def test_ensure_customer_accounts_idempotent(sqlite_session: Session):
    from app.modules.ledger import repository as repo

    ref = uuid.uuid4()
    avail, hold = repo.ensure_customer_accounts(sqlite_session, ref, "PEN")
    assert avail.code == f"2000-{ref}"
    assert hold.code == f"2100-{ref}"
    assert avail.owner_ref == ref and hold.owner_ref == ref
    assert avail.owner_type == "CUSTOMER" and hold.owner_type == "CUSTOMER"
    assert avail.currency == "PEN" and hold.currency == "PEN"
    assert not avail.is_system and not hold.is_system
    # Jerarquia: padres del catalogo.
    catalog = repo.ensure_system_catalog(sqlite_session)
    assert avail.parent_account_id == catalog["2000"].id
    assert hold.parent_account_id == catalog["2100"].id

    again_avail, again_hold = repo.ensure_customer_accounts(sqlite_session, ref, "PEN")
    assert again_avail.id == avail.id
    assert again_hold.id == hold.id
    count = sqlite_session.scalar(
        sa.select(sa.func.count()).select_from(Base.metadata.tables["ledger.ledger_accounts"])
    )
    assert count == 13  # 11 catalogo + 2 subcuentas

    # Otro cliente: otras 2 filas, sin colision.
    other = uuid.uuid4()
    o_avail, o_hold = repo.ensure_customer_accounts(sqlite_session, other, "USD")
    assert o_avail.code == f"2000-{other}" and o_hold.code == f"2100-{other}"
    assert o_avail.currency == "USD"


def test_ensure_customer_accounts_rejects_bad_input(sqlite_session: Session):
    from app.modules.ledger import repository as repo

    with pytest.raises(ValueError):
        repo.ensure_customer_accounts(sqlite_session, "no-es-uuid", "PEN")
    with pytest.raises(ValueError):
        repo.ensure_customer_accounts(sqlite_session, uuid.uuid4(), "pen")
    with pytest.raises(ValueError):
        repo.ensure_customer_accounts(sqlite_session, uuid.uuid4(), "PENS")


def test_facade_delegates_to_repository(sqlite_session: Session):
    from app.modules.ledger import service

    ref = uuid.uuid4()
    avail, hold = service.ensure_customer_accounts(sqlite_session, ref, "PEN")
    assert (avail.code, hold.code) == (f"2000-{ref}", f"2100-{ref}")


# ---------------------------------------------------------------- Parte C: Postgres
def test_integration_postgres_catalog_and_subaccounts(db_session: Session):
    from app.modules.ledger import repository as repo

    catalog = repo.ensure_system_catalog(db_session)
    assert len(catalog) >= 11
    ref = uuid.uuid4()
    avail, hold = repo.ensure_customer_accounts(db_session, ref, "PEN")
    assert avail.code == f"2000-{ref}" and hold.code == f"2100-{ref}"
    again, _ = repo.ensure_customer_accounts(db_session, ref, "PEN")
    assert again.id == avail.id
