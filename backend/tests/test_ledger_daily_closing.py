"""Cierre contable diario y control de consistencia (E5-T13, HU18, CA-04).

- Parte A (sin BD): metadatos del modelo y migracion segun `03b#7.5`,
  reglas (sin `float`, sin `commit`, sin publicacion de eventos,
  sin UPDATE/DELETE de postings ni balances, reutiliza `postings`,
  `ledger_balances` y el chequeo de E5-T12 en vez de duplicarlos).
- Parte B (SQLite en memoria + schema ATTACH): cierre cuadrado crea fila
  `balanced=true` con totales correctos; descuadre inyectado ->
  `balanced=false`, sin `closed_at`, sin modificar nada; consistencia de
  proyeccion verificada (y desviacion detectada); idempotencia (doble
  corrida mismo dia = 1 fila).
- Parte C (Postgres `db_session`): humo de integracion; se omite si no hay BD.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base


def _valid_postings(avail_id, hold_id, amount=10_000, currency="PEN"):
    return [
        {
            "ledger_account_id": avail_id,
            "direction": "DEBIT",
            "amount_minor": amount,
            "currency": currency,
        },
        {
            "ledger_account_id": hold_id,
            "direction": "CREDIT",
            "amount_minor": amount,
            "currency": currency,
        },
    ]


# ---------------------------------------------------------------- Parte A: modelo
def test_closing_table_registered_with_schema_columns_constraints():
    import app.modules.ledger.models as m  # noqa: F401 (registro)

    key = "ledger.daily_closings"
    assert key in Base.metadata.tables, "falta tabla ledger.daily_closings"
    table = Base.metadata.tables[key]
    assert table.schema == "ledger"
    cols = {c.name for c in table.columns}
    assert cols == {
        "id",
        "closing_date",
        "currency",
        "total_debits_minor",
        "total_credits_minor",
        "balanced",
        "postings_count",
        "closed_at",
        "closed_by",
    }, f"columnas 03b#7.5, recibido: {sorted(cols)}"
    assert isinstance(table.columns["total_debits_minor"].type, sa.BigInteger)
    assert isinstance(table.columns["total_credits_minor"].type, sa.BigInteger)
    assert isinstance(table.columns["postings_count"].type, sa.BigInteger)
    assert isinstance(table.columns["balanced"].type, sa.Boolean)
    assert isinstance(table.columns["currency"].type, sa.String)
    assert isinstance(table.columns["closing_date"].type, sa.Date)
    assert table.columns["closed_at"].nullable, "closed_at debe ser nulable"
    assert table.columns["closed_by"].nullable, "closed_by debe ser nulable"
    uq = [
        c
        for c in table.constraints
        if isinstance(c, sa.UniqueConstraint)
        and {col.name for col in c.columns} == {"closing_date", "currency"}
    ]
    assert uq, "falta UQ (closing_date, currency): idempotencia del cierre"


def test_no_foreign_keys_outside_ledger():
    table = Base.metadata.tables["ledger.daily_closings"]
    for fk in table.foreign_keys:
        target = fk.column.table
        assert target.schema == "ledger", (
            f"FK fuera del schema propio: daily_closings -> "
            f"{target.schema}.{target.name}"
        )


def test_existing_tables_untouched_by_e5_t13():
    accounts = Base.metadata.tables["ledger.ledger_accounts"]
    assert {c.name for c in accounts.columns} == {
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
    }, "E5-T13 no debe modificar LedgerAccount (solo aditivo)"
    balances = Base.metadata.tables["ledger.ledger_balances"]
    assert {c.name for c in balances.columns} == {
        "ledger_account_id",
        "currency",
        "balance_minor",
        "version",
        "updated_at",
    }, "E5-T13 no debe modificar LedgerBalance (solo aditivo)"


def test_migration_0008_exists_and_matches_models():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0008_ledger_daily_closing.py"
    )
    assert path.exists(), "falta migracion 0008_ledger_daily_closing.py"
    content = path.read_text(encoding="utf-8")
    for token in (
        'revision = "0008_ledger_daily_closing"',
        'down_revision = "0007_shared_idempotency"',
        '"daily_closings"',
        '"ledger"',
        "uq_daily_closings_date_currency",
    ):
        assert token in content, f"migracion sin {token}"
    assert "daily_closings" in content
    for forbidden in ("account_balances", "journal_entries", "postings", "ledger_balances"):
        assert f'create_table(\n        "{forbidden}"' not in content, (
            f"la migracion solo crea daily_closings, no {forbidden}"
        )
    assert 'schema="accounts"' not in content, "la migracion no debe tocar accounts"
    assert "schema='accounts'" not in content, "la migracion no debe tocar accounts"


def test_job_has_no_float_commit_publish_nor_edits():
    path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "modules"
        / "ledger"
        / "jobs"
        / "daily_closing.py"
    )
    assert path.exists(), "falta jobs/daily_closing.py"
    content = path.read_text(encoding="utf-8")
    assert "float(" not in content, "dinero entero en centimos, nunca float"
    assert ".commit(" not in content, "flush sin commit: commitea el llamante"
    assert "publish(" not in content, "nada de publish: la alerta se retorna"
    assert "outbox" not in content, "sin outbox directo: la alerta se retorna"
    for pattern in (
        r"session\.delete",
        r"sa\.delete\s*\(",
        r"sa\.update\s*\(",
        r"def\s+(update|delete|remove|purge)_posting",
    ):
        assert not re.search(pattern, content), f"job con {pattern}"
    assert "check_projection_consistency" in content, (
        "debe reutilizar el chequeo de E5-T12, no duplicarlo"
    )
    assert "validate_currency" in content, "moneda ISO-4217 validada"
    assert "value_date" in content, "el dia se recorta por value_date (documentado)"
    assert "def run_daily_closing" in content, "fachada run_daily_closing"


# ---------------------------------------------------------------- Parte B: SQLite
def _sqlite_session():
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
        tables=[
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["ledger.journal_entries"],
            Base.metadata.tables["ledger.postings"],
            Base.metadata.tables["ledger.ledger_balances"],
            Base.metadata.tables["ledger.daily_closings"],
        ],
    )
    return engine, Session(bind=engine, autoflush=False, expire_on_commit=False)


def _seed(session):
    from app.modules.ledger import repository as repo

    return repo.ensure_customer_accounts(session, uuid.uuid4(), "PEN")


def _closings_count(session):
    return session.scalar(
        sa.select(sa.func.count()).select_from(
            Base.metadata.tables["ledger.daily_closings"]
        )
    )


def _postings_count(session):
    return session.scalar(
        sa.select(sa.func.count()).select_from(
            Base.metadata.tables["ledger.postings"]
        )
    )


def test_balanced_close_creates_row_with_totals():
    from app.modules.ledger import repository as repo
    from app.modules.ledger.jobs.daily_closing import run_daily_closing

    engine, session = _sqlite_session()
    try:
        avail, hold = _seed(session)
        day = date(2026, 9, 10)
        repo.post_entry_and_update_balances(
            session,
            entry_type="OWN_TRANSFER",
            postings=_valid_postings(avail.id, hold.id, 10_000),
            value_date=day,
        )
        repo.post_entry_and_update_balances(
            session,
            entry_type="OWN_TRANSFER",
            postings=_valid_postings(avail.id, hold.id, 4_000),
            value_date=day,
        )
        closing, alert = run_daily_closing(session, closing_date=day, currency="PEN")
        assert alert is None, f"cierre limpio no emite alerta: {alert}"
        assert closing.balanced is True
        assert closing.closed_at is not None
        assert closing.total_debits_minor == 14_000
        assert closing.total_credits_minor == 14_000
        assert closing.postings_count == 4
        assert closing.closing_date == day
        assert closing.currency == "PEN"
    finally:
        session.close()
        engine.dispose()


def test_unbalanced_close_marks_not_closed_without_touching_anything():
    from app.modules.ledger import repository as repo
    from app.modules.ledger.jobs.daily_closing import run_daily_closing
    from app.modules.ledger.models import Posting

    engine, session = _sqlite_session()
    try:
        avail, hold = _seed(session)
        day = date(2026, 9, 11)
        repo.post_entry_and_update_balances(
            session,
            entry_type="OWN_TRANSFER",
            postings=_valid_postings(avail.id, hold.id, 5_000),
            value_date=day,
        )
        # Descuadre inyectado (corrupcion simulada, bypass de validacion):
        # un DEBIT huerfano en el dia. El job debe detectarlo sin editar nada.
        from app.modules.ledger.models import JournalEntry

        entry_id = session.scalar(
            sa.select(JournalEntry.id).where(JournalEntry.value_date == day).limit(1)
        )
        session.add(
            Posting(
                journal_entry_id=entry_id,
                ledger_account_id=avail.id,
                direction="DEBIT",
                amount_minor=1,
                currency="PEN",
            )
        )
        session.flush()
        before_postings = _postings_count(session)
        before_bal_avail = repo.get_balance(session, avail.id).balance_minor
        before_bal_hold = repo.get_balance(session, hold.id).balance_minor

        closing, alert = run_daily_closing(session, closing_date=day, currency="PEN")
        assert closing.balanced is False
        assert closing.closed_at is None, "descuadre: no cierra"
        assert closing.total_debits_minor == 5_001
        assert closing.total_credits_minor == 5_000
        assert alert is not None and alert["type"] == "daily_closing.unbalanced"
        assert alert["severity"] == "CRITICAL"
        assert alert["total_debits_minor"] == 5_001
        # Sin modificar nada: postings y balances intactos.
        assert _postings_count(session) == before_postings
        assert repo.get_balance(session, avail.id).balance_minor == before_bal_avail
        assert repo.get_balance(session, hold.id).balance_minor == before_bal_hold
    finally:
        session.close()
        engine.dispose()


def test_projection_mismatch_blocks_close():
    from app.modules.ledger import repository as repo
    from app.modules.ledger.jobs.daily_closing import run_daily_closing

    engine, session = _sqlite_session()
    try:
        avail, hold = _seed(session)
        day = date(2026, 9, 12)
        repo.post_entry_and_update_balances(
            session,
            entry_type="OWN_TRANSFER",
            postings=_valid_postings(avail.id, hold.id, 6_000),
            value_date=day,
        )
        assert repo.check_projection_consistency(session) == []
        # Corrupcion simulada de la proyeccion: el cierre la detecta.
        row = repo.get_balance(session, avail.id)
        row.balance_minor = 1
        session.flush()
        closing, alert = run_daily_closing(session, closing_date=day, currency="PEN")
        # El cuadre del dia sigue (debits == credits) pero no cierra.
        assert closing.balanced is True
        assert closing.closed_at is None, "desvio de proyeccion: no cierra"
        assert alert is not None
        assert alert["type"] == "daily_closing.projection_mismatch"
        assert len(alert["projection_diffs"]) == 1
    finally:
        session.close()
        engine.dispose()


def test_idempotent_double_run_same_day_is_one_row():
    from app.modules.ledger import repository as repo
    from app.modules.ledger.jobs.daily_closing import run_daily_closing

    engine, session = _sqlite_session()
    try:
        avail, hold = _seed(session)
        day = date(2026, 9, 13)
        repo.post_entry_and_update_balances(
            session,
            entry_type="OWN_TRANSFER",
            postings=_valid_postings(avail.id, hold.id, 2_000),
            value_date=day,
        )
        first, alert1 = run_daily_closing(session, closing_date=day, currency="PEN")
        assert alert1 is None
        second, alert2 = run_daily_closing(session, closing_date=day, currency="PEN")
        assert second.id == first.id, "segunda corrida retorna la fila existente"
        assert alert2 is None
        assert _closings_count(session) == 1, "doble corrida = 1 fila (UQ dia+moneda)"
        # Otro dia si genera su propia fila.
        other, _ = run_daily_closing(
            session, closing_date=date(2026, 9, 14), currency="PEN"
        )
        assert other.id != first.id
        assert _closings_count(session) == 2
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------- Parte C: Postgres
def test_integration_postgres_daily_closing(db_session: Session):
    from app.modules.ledger import repository as repo
    from app.modules.ledger.jobs.daily_closing import run_daily_closing

    avail, hold = repo.ensure_customer_accounts(db_session, uuid.uuid4(), "PEN")
    day = date(2026, 9, 15)
    repo.post_entry_and_update_balances(
        db_session,
        entry_type="OWN_TRANSFER",
        postings=_valid_postings(avail.id, hold.id, 2_500),
        description="humo E5-T13",
        value_date=day,
    )
    closing, alert = run_daily_closing(
        db_session, closing_date=day, currency="PEN"
    )
    assert alert is None
    assert closing.balanced is True and closing.closed_at is not None
    assert closing.total_debits_minor == 2_500
    assert repo.check_projection_consistency(db_session) == []
