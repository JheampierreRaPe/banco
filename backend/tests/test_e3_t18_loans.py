"""Prestamos y cronograma definitivo: `loans`/`loan_schedules`/`loan_payments` (E3-T18, HU12).

- Camino feliz: condiciones conocidas -> `build_definitive_schedule`
  (12 cuotas con fechas/montos) -> `create_loan` + `create_schedules`
  (`outstanding == principal`, invariantes de cuadre) + `link` del juego.
- Orquestador: `create_loan_with_schedule` persiste prestamo +
  cronograma + `contracts.loan_id` y congela la TCEA.
- Errores: `application_id` duplicado -> `CONFLICT`; `principal <= 0`,
  `term_months <= 0`, `insurance_rate < 0` -> `VALIDATION_ERROR`;
  cuota descuadrada (`p+i+s != total`) rechazada; cuota ya pagada ->
  `CONFLICT`; prestamo/cuota inexistente -> `NOT_FOUND`.
- Dominio puro: determinista (misma entrada -> mismo cronograma), sin
  `float`, cubre `insurance_rate = 0` y tope de fin de mes.
- Migracion: un solo head (`0034_credits_loans`) encadenado tras
  `0033_credits_contracts`.
- Aislamiento: 0 `transactions`/`postings` (ni se importan esos
  modulos); sin PII en logs.

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron del
repo). Sin red.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.modules.credits.models as _credits
from app.core.db import Base
from app.modules.credits.domain import schedule as schedule_domain
from app.modules.credits.domain.schedule import (
    ScheduleValidationError,
    build_definitive_schedule,
    first_due_date_for,
)
from app.modules.credits.models import LoanApplication
from app.modules.credits.repository import contracts as contracts_repo
from app.modules.credits.repository import loans as loans_repo
from app.modules.credits.repository.loans import (
    LoanConflictError,
    LoanNotFoundError,
    LoanValidationError,
)
from app.modules.credits.service import loans as loans_service

assert _credits is not None

USER = uuid.uuid4()
ACCOUNT = uuid.uuid4()

PRINCIPAL = 1_200_000  # S/ 12 000.00 en centimos
TEA = Decimal("0.28000000")
INSURANCE = Decimal("0.00350000")
TERM = 12
FIRST_DUE = date(2026, 2, 15)

TABLES = [
    "credits.loan_products",
    "credits.loan_applications",
    "credits.loans",
    "credits.loan_schedules",
    "credits.loan_payments",
    "credits.contracts",
]


def _make_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS credits")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    return Session(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def session():
    db = _make_session()
    try:
        yield db
    finally:
        db.close()


def _application(
    db: Session, *, user_id: uuid.UUID = USER, status: str = "APPROVED"
) -> LoanApplication:
    row = LoanApplication(
        user_id=user_id,
        product_id=uuid.uuid4(),
        requested_amount_minor=PRINCIPAL,
        term_months=TERM,
        status=status,
        bureau_authorized=True,
    )
    db.add(row)
    db.flush()
    return row


def _definitive():
    return build_definitive_schedule(
        principal_minor=PRINCIPAL,
        annual_rate=TEA,
        term_months=TERM,
        insurance_rate=INSURANCE,
        first_due_date=FIRST_DUE,
    )


def _loan(db: Session, app_id: uuid.UUID):
    return loans_repo.create_loan(
        db,
        application_id=app_id,
        account_id=ACCOUNT,
        principal_minor=PRINCIPAL,
        tcea=TEA,
        term_months=TERM,
        insurance_rate=INSURANCE,
    )


# ---------------------------------------------------------------- camino feliz


def test_happy_definitive_schedule_has_dates_and_balances():
    rows = _definitive()
    assert len(rows) == TERM
    assert [r.installment_no for r in rows] == list(range(1, TERM + 1))
    assert rows[0].due_date == FIRST_DUE
    assert rows[1].due_date == date(2026, 3, 15)
    assert rows[-1].due_date == date(2027, 1, 15)
    for row in rows:
        assert row.principal_minor + row.interest_minor + row.insurance_minor == row.total_minor
    assert sum(r.principal_minor for r in rows) == PRINCIPAL
    assert all(r.insurance_minor >= 0 for r in rows)


def test_happy_create_loan_and_schedule(session):
    app_row = _application(session)
    loan = _loan(session, app_row.id)
    assert loan.outstanding_principal_minor == PRINCIPAL
    assert loan.status == "ACTIVE"
    assert loan.auto_debit_enabled is False

    schedules = loans_repo.create_schedules(session, loan_id=loan.id, rows=_definitive())
    assert len(schedules) == TERM
    assert all(s.status == "PENDING" for s in schedules)

    listed = loans_repo.list_schedule(session, loan.id)
    assert [s.installment_no for s in listed] == list(range(1, TERM + 1))
    assert sum(s.principal_minor for s in listed) == PRINCIPAL

    assert loans_repo.get_loan(session, loan.id).id == loan.id
    mine = loans_repo.list_loans_by_user(session, USER)
    assert [row.id for row in mine] == [loan.id]
    assert loans_repo.next_due_installment(session, loan.id).installment_no == 1


def test_happy_orchestrator_links_contracts_and_freezes_tcea(session):
    app_row = _application(session)
    for doc_type in ("CONTRATO", "PAGARE", "RESUMEN"):
        contracts_repo.create_contract(
            session,
            application_id=app_row.id,
            doc_type=doc_type,
            doc_ref=f"credits/contracts/{app_row.id}/{doc_type}.pdf",
            doc_hash="ab" * 32,
        )
    loan, schedules = loans_service.create_loan_with_schedule(
        session,
        application_id=app_row.id,
        account_id=ACCOUNT,
        principal_minor=PRINCIPAL,
        annual_rate=TEA,
        term_months=TERM,
        insurance_rate=INSURANCE,
        first_due_date=FIRST_DUE,
    )
    assert len(schedules) == TERM
    assert isinstance(loan.tcea, Decimal) and loan.tcea > 0
    linked = contracts_repo.list_by_application(session, app_row.id)
    assert len(linked) == 3
    assert {row.loan_id for row in linked} == {loan.id}


def test_happy_lifecycle_paid_partial_overdue_autodebit(session):
    app_row = _application(session)
    loan = _loan(session, app_row.id)
    schedules = loans_repo.create_schedules(session, loan_id=loan.id, rows=_definitive())

    loans_repo.mark_installment_paid(session, schedules[0].id)
    assert loans_repo.get_schedule_installment(session, schedules[0].id).status == "PAID"
    assert loans_repo.next_due_installment(session, loan.id).installment_no == 2

    loans_repo.mark_installment_partial(session, schedules[1].id)
    assert loans_repo.get_schedule_installment(session, schedules[1].id).status == "PARTIAL"

    marked = loans_repo.flag_overdue_installments(session, as_of=date(2027, 6, 1))
    assert marked == TERM - 1  # la PAID no se toca
    assert loans_repo.get_schedule_installment(session, schedules[1].id).status == "OVERDUE"

    assert loans_repo.set_auto_debit(session, loan.id, True).auto_debit_enabled is True
    assert loans_repo.set_auto_debit(session, loan.id, False).auto_debit_enabled is False

    assert (
        loans_repo.update_outstanding_principal(session, loan.id, 999).outstanding_principal_minor
        == 999
    )

    payment = loans_repo.record_payment(
        session,
        loan_id=loan.id,
        schedule_id=schedules[0].id,
        amount_minor=schedules[0].total_minor,
        principal_applied_minor=schedules[0].principal_minor,
        interest_applied_minor=schedules[0].interest_minor,
        insurance_applied_minor=schedules[0].insurance_minor,
    )
    assert payment.amount_minor == schedules[0].total_minor
    assert payment.transaction_id is None  # la puebla E3-T20


# ---------------------------------------------------------------- errores


def test_error_duplicate_application_conflict(session):
    app_row = _application(session)
    _loan(session, app_row.id)
    with pytest.raises(LoanConflictError) as exc:
        _loan(session, app_row.id)
    assert exc.value.code == "CONFLICT"


def test_error_invalid_amounts_terms_rates(session):
    app_row = _application(session)
    base = {
        "application_id": app_row.id,
        "account_id": ACCOUNT,
        "principal_minor": PRINCIPAL,
        "tcea": TEA,
        "term_months": TERM,
    }
    for bad in (0, -1, True, 12.5, "100"):
        with pytest.raises(LoanValidationError) as exc:
            loans_repo.create_loan(session, **{**base, "principal_minor": bad})
        assert exc.value.code == "VALIDATION_ERROR"
    for bad in (0, -3, True, 12.5):
        with pytest.raises(LoanValidationError):
            loans_repo.create_loan(session, **{**base, "term_months": bad})
    for bad in (Decimal("-0.01"), -1.0, True):
        with pytest.raises(LoanValidationError):
            loans_repo.create_loan(session, **{**base, "insurance_rate": bad})
    with pytest.raises(LoanValidationError):
        loans_repo.create_loan(session, **{**base, "tcea": 0.28})  # float jamas
    with pytest.raises(LoanValidationError):
        loans_repo.create_loan(session, **{**base, "status": "VIGENTE"})
    with pytest.raises(LoanValidationError):
        loans_repo.create_loan(session, **{**base, "auto_debit_enabled": 1})


def test_error_unbalanced_schedule_rejected(session):
    app_row = _application(session)
    loan = _loan(session, app_row.id)
    rows = [dict(r.__dict__) for r in _definitive()]
    rows[0]["total_minor"] += 1  # rompe p+i+s == total
    with pytest.raises(LoanValidationError) as exc:
        loans_repo.create_schedules(session, loan_id=loan.id, rows=rows)
    assert exc.value.code == "VALIDATION_ERROR"
    assert loans_repo.list_schedule(session, loan.id) == []


def test_error_incomplete_schedule_rejected(session):
    app_row = _application(session)
    loan = _loan(session, app_row.id)
    with pytest.raises(LoanValidationError):
        loans_repo.create_schedules(session, loan_id=loan.id, rows=list(_definitive())[:11])


def test_error_paid_twice_conflict_and_missing_not_found(session):
    app_row = _application(session)
    loan = _loan(session, app_row.id)
    schedules = loans_repo.create_schedules(session, loan_id=loan.id, rows=_definitive())
    loans_repo.mark_installment_paid(session, schedules[0].id)
    with pytest.raises(LoanConflictError) as exc:
        loans_repo.mark_installment_paid(session, schedules[0].id)
    assert exc.value.code == "CONFLICT"
    with pytest.raises(LoanConflictError):
        loans_repo.mark_installment_partial(session, schedules[0].id)
    missing = uuid.uuid4()
    with pytest.raises(LoanNotFoundError) as exc:
        loans_repo.mark_installment_paid(session, missing)
    assert exc.value.code == "NOT_FOUND"
    assert loans_repo.get_loan(session, missing) is None
    with pytest.raises(LoanNotFoundError):
        loans_repo.set_auto_debit(session, missing, True)
    with pytest.raises(LoanNotFoundError):
        loans_repo.update_outstanding_principal(session, missing, 0)
    with pytest.raises(LoanNotFoundError):
        loans_repo.create_schedules(session, loan_id=missing, rows=[])
    with pytest.raises(LoanValidationError):
        loans_repo.update_outstanding_principal(session, loan.id, -1)
    with pytest.raises(LoanValidationError):
        loans_repo.flag_overdue_installments(session, as_of="2026-01-01")  # type: ignore[arg-type]


# ---------------------------------------------------------------- dominio puro


def test_domain_deterministic_and_without_float():
    first = _definitive()
    second = build_definitive_schedule(
        principal_minor=PRINCIPAL,
        annual_rate=TEA,
        term_months=TERM,
        insurance_rate=INSURANCE,
        first_due_date=FIRST_DUE,
    )
    assert first == second  # misma entrada -> mismo cronograma
    with pytest.raises(ScheduleValidationError):
        build_definitive_schedule(
            principal_minor=PRINCIPAL,
            annual_rate=0.28,  # float jamas
            term_months=TERM,
            insurance_rate=INSURANCE,
            first_due_date=FIRST_DUE,
        )
    with pytest.raises(ScheduleValidationError):
        build_definitive_schedule(
            principal_minor=PRINCIPAL,
            annual_rate=TEA,
            term_months=TERM,
            insurance_rate=INSURANCE,
            first_due_date="2026-02-15",  # type: ignore[arg-type]
        )


def test_domain_zero_insurance_and_month_end():
    rows = build_definitive_schedule(
        principal_minor=100_000,
        annual_rate=Decimal("0.10"),
        term_months=3,
        insurance_rate=Decimal(0),
        first_due_date=date(2026, 1, 31),
    )
    assert [r.due_date for r in rows] == [
        date(2026, 1, 31),
        date(2026, 2, 28),  # tope de fin de mes
        date(2026, 3, 31),
    ]
    assert all(r.insurance_minor == 0 for r in rows)
    assert all(r.principal_minor + r.interest_minor == r.total_minor for r in rows)
    assert sum(r.principal_minor for r in rows) == 100_000
    assert first_due_date_for(date(2026, 1, 31)) == date(2026, 2, 28)


# ---------------------------------------------------------------- migracion y aislamiento


def test_migration_single_head_chained():
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend_dir = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "migrations"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    # E4-T11 (2026-10-13): la cabeza avanza a 0040_wallet_qr_payments
    # (cobro QR; un solo head encadenado tras 0039). Fix guarda
    # (2026-10-13): la cabeza avanza a 0041_wallet_qr_payment_guard
    # (indice unico parcial anti doble cobro, tras 0040). E4-T16
    # (2026-10-13): la cabeza avanza a 0042_wallet_services
    # (catalogo `billers` + servicios, tras 0041). E6-T12
    # (2026-10-13): la cabeza avanza a 0044_notifications_preferences
    # (preferencias por canal + claves de seguridad, tras 0043). Pulido de
    # correos (2026-10-15): la cabeza avanza a
    # 0045_notification_email_content (textos de semillas, tras 0044). La
    # cabeza vigente es 0046_services_settlement (wallet, tras 0045).
    # E4-T21 (2026-10-15): la cabeza avanza a 0047_wallet_personal_qr
    # (QR personal estatico, tras 0046).
    assert tuple(heads) == ("0047_wallet_personal_qr",)
    script = ScriptDirectory.from_config(cfg).get_revision("0042_wallet_services")
    assert script.down_revision == "0041_wallet_qr_payment_guard"
    script = ScriptDirectory.from_config(cfg).get_revision("0041_wallet_qr_payment_guard")
    assert script.down_revision == "0040_wallet_qr_payments"
    script = ScriptDirectory.from_config(cfg).get_revision("0040_wallet_qr_payments")
    assert script.down_revision == "0039_wallet_qr"
    script = ScriptDirectory.from_config(cfg).get_revision("0039_wallet_qr")
    assert script.down_revision == "0038_wallet_express_params"
    prev_express = ScriptDirectory.from_config(cfg).get_revision("0038_wallet_express_params")
    assert prev_express.down_revision == "0037_wallet"
    prev0 = ScriptDirectory.from_config(cfg).get_revision("0037_wallet")
    assert prev0.down_revision == "0036_loan_reminder_param"
    prev = ScriptDirectory.from_config(cfg).get_revision("0036_loan_reminder_param")
    assert prev.down_revision == "0035_ledger_catalog_fix"
    prev2 = ScriptDirectory.from_config(cfg).get_revision("0035_ledger_catalog_fix")
    assert prev2.down_revision == "0034_credits_loans"


def test_no_transactions_or_ledger_coupling():
    from pathlib import Path

    module_dir = Path(__file__).resolve().parents[1] / "app" / "modules" / "credits"
    sources = [
        module_dir / "models" / "__init__.py",
        module_dir / "domain" / "schedule.py",
        module_dir / "repository" / "loans.py",
        module_dir / "service" / "loans.py",
    ]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        assert "modules.transactions" not in text, path.name
        assert "modules.ledger" not in text, path.name
        assert "journal_entries" not in text, path.name
        assert "postings" not in text, path.name


def test_no_pii_in_logs(session, caplog):
    app_row = _application(session)
    with caplog.at_level(logging.DEBUG, logger="app.modules.credits"):
        loan = _loan(session, app_row.id)
        loans_repo.create_schedules(session, loan_id=loan.id, rows=_definitive())
        loans_repo.list_loans_by_user(session, USER)
        loans_repo.set_auto_debit(session, loan.id, True)
    text = caplog.text
    assert str(USER) not in text
    assert str(ACCOUNT) not in text
    assert "principal_minor" not in text or str(PRINCIPAL) not in text


def test_service_facade_reexports_repository_and_domain():
    assert loans_service.build_definitive_schedule is build_definitive_schedule
    assert schedule_domain.build_definitive_schedule is build_definitive_schedule
    for name in (
        "create_loan",
        "create_schedules",
        "get_loan",
        "list_loans_by_user",
        "list_schedule",
        "next_due_installment",
        "mark_installment_paid",
        "set_auto_debit",
        "build_definitive_schedule",
    ):
        assert callable(getattr(loans_service, name)), name


def test_tables_registered_in_metadata():
    for key in ("credits.loans", "credits.loan_schedules", "credits.loan_payments"):
        assert key in Base.metadata.tables
    loans_table = Base.metadata.tables["credits.loans"]
    assert "uq_loans_application_id" in {c.name for c in loans_table.constraints}
    assert "auto_debit_enabled" in loans_table.columns
