"""Correccion del catalogo contable `4200`/`5000` (E5-T23, HU12, CA-06).

- Parte A (sin BD): `SYSTEM_CATALOG` deja `5000` = asset "Cartera de
  prestamos" y `4200` = income "Ingresos por seguro"; `CATALOG_CODES`
  intacto (11 cuentas); `validate_catalog() == []`.
- Parte B (SQLite en memoria + schema ATTACH): el catalogo sembrado por
  `repo.ensure_system_catalog` trae los valores corregidos; la migracion
  `0035_ledger_catalog_fix` corrige filas con los valores viejos de
  `0003` (idempotente) y su `downgrade` las restaura.
- Parte C (Postgres `db_session`): humo de integracion sobre
  `ledger.ledger_accounts`; se omite si no hay BD (rollback por prueba).
- Parte D (estructural, sin BD): literales de la migracion y cadena
  lineal con un solo head (`0035`, hija de `0034_credits_loans`).
"""

from __future__ import annotations

import importlib.util
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"
MIGRATION_FILE = VERSIONS_DIR / "0035_ledger_catalog_fix.py"

OLD_5000 = ("5000", "Costos financieros", "expense")
OLD_4200 = ("4200", "Otros ingresos", "income")
NEW_5000 = ("5000", "Cartera de prestamos", "asset")
NEW_4200 = ("4200", "Ingresos por seguro", "income")


def _load_migration():
    """Carga `0035_ledger_catalog_fix.py` sin Alembic CLI (solo `op` proxy)."""
    assert MIGRATION_FILE.exists(), "falta migracion 0035_ledger_catalog_fix.py"
    spec = importlib.util.spec_from_file_location("m0035_ledger_catalog_fix", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- Parte A: dominio puro
def test_system_catalog_has_fixed_4200_and_5000():
    from app.modules.ledger.domain.chart import (
        CATALOG_CODES,
        SYSTEM_CATALOG,
        validate_catalog,
    )

    assert len(CATALOG_CODES) == 11
    assert len(SYSTEM_CATALOG) == 11
    by_code = {entry.code: entry for entry in SYSTEM_CATALOG}
    assert set(by_code) == set(CATALOG_CODES)

    entry_5000 = by_code["5000"]
    assert (entry_5000.name, entry_5000.type) == ("Cartera de prestamos", "asset")
    assert (entry_5000.currency, entry_5000.owner_type, entry_5000.is_system) == (
        "PEN",
        "SYSTEM",
        True,
    )
    entry_4200 = by_code["4200"]
    assert (entry_4200.name, entry_4200.type) == ("Ingresos por seguro", "income")
    assert validate_catalog() == []
    assert validate_catalog(SYSTEM_CATALOG) == []


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


def _insert_old_rows(session: Session) -> None:
    table = Base.metadata.tables["ledger.ledger_accounts"]
    for code, name, type_ in (OLD_5000, OLD_4200, ("4000", "Ingresos por intereses", "income")):
        session.execute(
            table.insert().values(
                id=uuid.uuid4(),
                code=code,
                name=name,
                type=type_,
                currency="PEN",
                owner_type="SYSTEM",
                owner_ref=None,
                parent_account_id=None,
                is_system=True,
            )
        )
    session.commit()


def _row(session: Session, code: str):
    table = Base.metadata.tables["ledger.ledger_accounts"]
    return session.execute(table.select().where(table.c.code == code)).mappings().one()


def test_seeded_catalog_carries_fixed_values(sqlite_session: Session):
    from app.modules.ledger import repository as repo

    catalog = repo.ensure_system_catalog(sqlite_session)
    assert (catalog["5000"].name, catalog["5000"].type) == ("Cartera de prestamos", "asset")
    assert (catalog["4200"].name, catalog["4200"].type) == ("Ingresos por seguro", "income")


def test_migration_upgrade_fixes_old_rows_and_is_idempotent(sqlite_session: Session):
    module = _load_migration()
    _insert_old_rows(sqlite_session)
    bind = sqlite_session.connection()
    module.apply_fix(bind)
    module.apply_fix(bind)  # re-ejecutable: mismo resultado
    assert (_row(sqlite_session, "5000")["name"], _row(sqlite_session, "5000")["type"]) == NEW_5000[
        1:
    ]
    assert (_row(sqlite_session, "4200")["name"], _row(sqlite_session, "4200")["type"]) == NEW_4200[
        1:
    ]
    control = _row(sqlite_session, "4000")
    assert (control["name"], control["type"]) == ("Ingresos por intereses", "income")


def test_migration_downgrade_restores_old_rows(sqlite_session: Session):
    module = _load_migration()
    _insert_old_rows(sqlite_session)
    bind = sqlite_session.connection()
    module.apply_fix(bind)
    module.revert_fix(bind)
    assert (_row(sqlite_session, "5000")["name"], _row(sqlite_session, "5000")["type"]) == OLD_5000[
        1:
    ]
    assert (_row(sqlite_session, "4200")["name"], _row(sqlite_session, "4200")["type"]) == OLD_4200[
        1:
    ]


# ---------------------------------------------------------------- Parte C: Postgres
def test_integration_postgres_migration_roundtrip(db_session: Session):
    from app.modules.ledger import repository as repo

    module = _load_migration()
    table = Base.metadata.tables["ledger.ledger_accounts"]
    repo.ensure_system_catalog(db_session)
    # Simula el estado pre-0035 (valores de `0003`) sobre filas existentes.
    for code, name, type_ in (OLD_5000, OLD_4200):
        db_session.execute(table.update().where(table.c.code == code).values(name=name, type=type_))
    bind = db_session.connection()
    module.apply_fix(bind)
    assert (_row(db_session, "5000")["name"], _row(db_session, "5000")["type"]) == NEW_5000[1:]
    assert (_row(db_session, "4200")["name"], _row(db_session, "4200")["type"]) == NEW_4200[1:]
    module.revert_fix(bind)
    assert (_row(db_session, "5000")["name"], _row(db_session, "5000")["type"]) == OLD_5000[1:]
    assert (_row(db_session, "4200")["name"], _row(db_session, "4200")["type"]) == OLD_4200[1:]


# ---------------------------------------------------------------- Parte D: estructural
def test_migration_file_literals_and_chain():
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    assert 'revision = "0035_ledger_catalog_fix"' in content
    assert 'down_revision = "0034_credits_loans"' in content
    for token in (
        "UPDATE ledger.ledger_accounts",
        "Cartera de prestamos",
        "Ingresos por seguro",
        "Costos financieros",
        "Otros ingresos",
        "def upgrade",
        "def downgrade",
        "WHERE code = '5000'",
        "WHERE code = '4200'",
    ):
        assert token in content, f"migracion sin {token}"


def test_single_alembic_head_ending_at_0035():
    revisions: dict[str, str | None] = {}
    for path in VERSIONS_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision\s*=\s*"([^"]+)"', text, re.MULTILINE)
        down = re.search(r'^down_revision\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if rev is None:
            continue
        assert len(rev.group(1)) <= 32, f"revision > 32 chars: {rev.group(1)}"
        revisions[rev.group(1)] = down.group(1) if down else None
    assert revisions.get("0035_ledger_catalog_fix") == "0034_credits_loans"
    # E3-T19 (2026-10-13): la cabeza avanza a 0036_loan_reminder_param
    # (parametro loan.reminder_days_before; un solo head tras 0035).
    assert revisions.get("0036_loan_reminder_param") == "0035_ledger_catalog_fix"
    # E4-T11 (2026-10-13): la cabeza avanza a 0040_wallet_qr_payments
    # (cobro QR + qr_payments; un solo head tras 0039). Fix guarda
    # (2026-10-13): la cabeza avanza a 0041_wallet_qr_payment_guard
    # (indice unico parcial anti doble cobro, tras 0040). E4-T16
    # (2026-10-13): la cabeza avanza a 0042_wallet_services
    # (catalogo `billers` + servicios, tras 0041). E6-T12
    # (2026-10-13): la cabeza avanza a 0044_notifications_preferences
    # (preferencias por canal + claves de seguridad, tras 0043). Pulido de
    # correos (2026-10-15): la cabeza avanza a
    # 0045_notification_email_content (textos de semillas, tras 0044).
    assert revisions.get("0046_services_settlement") == "0045_notification_email_content"
    assert revisions.get("0045_notification_email_content") == "0044_notifications_preferences"
    assert revisions.get("0042_wallet_services") == "0041_wallet_qr_payment_guard"
    assert revisions.get("0041_wallet_qr_payment_guard") == "0040_wallet_qr_payments"
    assert revisions.get("0040_wallet_qr_payments") == "0039_wallet_qr"
    assert revisions.get("0039_wallet_qr") == "0038_wallet_express_params"
    assert revisions.get("0038_wallet_express_params") == "0037_wallet"
    assert revisions.get("0037_wallet") == "0036_loan_reminder_param"
    # E4-T21 (2026-10-15): la cabeza avanza a 0047_wallet_personal_qr
    # (QR personal estatico, tras 0046).
    assert revisions.get("0047_wallet_personal_qr") == "0046_services_settlement"
    referenced = {down for down in revisions.values() if down is not None}
    heads = [rev for rev in revisions if rev not in referenced]
    assert heads == ["0047_wallet_personal_qr"], f"heads inesperados: {heads}"
    assert "0034_credits_loans" in revisions
