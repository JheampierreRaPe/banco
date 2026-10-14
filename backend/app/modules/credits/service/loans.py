"""Fachada interna de prestamos y cronograma definitivo (E3-T18, HU12).

Punto de entrada estable para los consumidores (`E3-T17` desembolso,
`E3-T20` pago de cuota, `E3-T19` debito automatico): une el dominio puro
(`credits.domain.schedule`, determinista, sin `float`) con el
repositorio (`repository.loans`, solo schema `credits`). Sin endpoints,
sin eventos, sin `transactions`/`ledger`: **no mueve dinero**.

`create_loan_with_schedule` es la operacion compuesta que usara
`E3-T17`: calcula la TCEA del flujo definitivo, crea la fila `loans`,
persiste el cronograma completo y vincula el juego contractual
(`contracts.loan_id`, G54). La validacion de negocio previa
(expediente `APPROVED` + juego firmado) la hace el caso de uso que
llama, no esta fachada.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.modules.credits.domain import financial
from app.modules.credits.domain.schedule import (
    DefinitiveInstallment,
    ScheduleValidationError,
    build_definitive_schedule,
    first_due_date_for,
)
from app.modules.credits.models import Loan, LoanPayment, LoanSchedule
from app.modules.credits.repository import loans as loans_repo
from app.modules.credits.repository.loans import (
    LoanConflictError,
    LoanError,
    LoanNotFoundError,
    LoanValidationError,
)

__all__ = [
    "DefinitiveInstallment",
    "Loan",
    "LoanConflictError",
    "LoanError",
    "LoanNotFoundError",
    "LoanPayment",
    "LoanSchedule",
    "LoanValidationError",
    "ScheduleValidationError",
    "build_definitive_schedule",
    "create_loan",
    "create_loan_with_schedule",
    "create_schedules",
    "first_due_date_for",
    "flag_overdue_installments",
    "get_loan",
    "get_schedule_installment",
    "link_contracts_to_loan",
    "list_loans_by_user",
    "list_schedule",
    "mark_installment_paid",
    "mark_installment_partial",
    "next_due_installment",
    "record_payment",
    "set_auto_debit",
    "set_own_auto_debit",
    "update_outstanding_principal",
]


def create_loan(
    session: Session,
    *,
    application_id: uuid.UUID | str,
    account_id: uuid.UUID | str,
    principal_minor: int,
    tcea: Decimal | str | int,
    term_months: int,
    insurance_rate: Decimal | str | int = Decimal(0),
    status: str = "ACTIVE",
    disbursed_at: datetime | None = None,
    auto_debit_enabled: bool = False,
) -> Loan:
    """Crea la fila `loans` (delega en el repositorio)."""
    return loans_repo.create_loan(
        session,
        application_id=application_id,
        account_id=account_id,
        principal_minor=principal_minor,
        tcea=tcea,
        term_months=term_months,
        insurance_rate=insurance_rate,
        status=status,
        disbursed_at=disbursed_at,
        auto_debit_enabled=auto_debit_enabled,
    )


def create_schedules(
    session: Session,
    *,
    loan_id: uuid.UUID | str,
    rows: Sequence[Any],
) -> list[LoanSchedule]:
    """Persiste el cronograma definitivo completo (delega en el repositorio)."""
    return loans_repo.create_schedules(session, loan_id=loan_id, rows=rows)


def get_loan(session: Session, loan_id: uuid.UUID | str) -> Loan | None:
    """Prestamo por `id` (`None` si no existe)."""
    return loans_repo.get_loan(session, loan_id)


def list_loans_by_user(session: Session, user_id: uuid.UUID | str) -> list[Loan]:
    """Prestamos del titular (`created_at DESC`)."""
    return loans_repo.list_loans_by_user(session, user_id)


def list_schedule(session: Session, loan_id: uuid.UUID | str) -> list[LoanSchedule]:
    """Cronograma del prestamo ordenado por `installment_no`."""
    return loans_repo.list_schedule(session, loan_id)


def get_schedule_installment(session: Session, schedule_id: uuid.UUID | str) -> LoanSchedule | None:
    """Una cuota por `id` (`None` si no existe)."""
    return loans_repo.get_schedule_installment(session, schedule_id)


def next_due_installment(session: Session, loan_id: uuid.UUID | str) -> LoanSchedule | None:
    """Primera cuota exigible (`PENDING`/`PARTIAL`/`OVERDUE`, por numero)."""
    return loans_repo.next_due_installment(session, loan_id)


def mark_installment_paid(
    session: Session, schedule_id: uuid.UUID | str, *, paid_at: datetime | None = None
) -> LoanSchedule:
    """Marca la cuota `PAID` (ya pagada -> `CONFLICT`)."""
    return loans_repo.mark_installment_paid(session, schedule_id, paid_at=paid_at)


def mark_installment_partial(session: Session, schedule_id: uuid.UUID | str) -> LoanSchedule:
    """Marca la cuota `PARTIAL` (ya pagada -> `CONFLICT`)."""
    return loans_repo.mark_installment_partial(session, schedule_id)


def record_payment(
    session: Session,
    *,
    loan_id: uuid.UUID | str,
    amount_minor: int,
    schedule_id: uuid.UUID | str | None = None,
    transaction_id: uuid.UUID | str | None = None,
    principal_applied_minor: int = 0,
    interest_applied_minor: int = 0,
    insurance_applied_minor: int = 0,
) -> LoanPayment:
    """Registra una aplicacion de pago (`loan_payments`)."""
    return loans_repo.record_payment(
        session,
        loan_id=loan_id,
        amount_minor=amount_minor,
        schedule_id=schedule_id,
        transaction_id=transaction_id,
        principal_applied_minor=principal_applied_minor,
        interest_applied_minor=interest_applied_minor,
        insurance_applied_minor=insurance_applied_minor,
    )


def flag_overdue_installments(session: Session, *, as_of: date) -> int:
    """Marca `OVERDUE` las cuotas vencidas al `as_of` (la consume `E3-T19`)."""
    return loans_repo.flag_overdue_installments(session, as_of=as_of)


def set_auto_debit(session: Session, loan_id: uuid.UUID | str, enabled: bool) -> Loan:
    """Activa/desactiva el debito automatico (lo expone `E3-T19`)."""
    return loans_repo.set_auto_debit(session, loan_id, enabled)


def set_own_auto_debit(
    session: Session,
    owner_user_id: uuid.UUID | str,
    loan_id: uuid.UUID | str,
    enabled: bool,
) -> Loan:
    """Fija `auto_debit_enabled` del prestamo del titular (`E3-T19`).

    Solo el titular (ajeno o inexistente -> `LoanNotFoundError`
    neutro); `enabled` no booleano -> `LoanValidationError`. No mueve
    dinero (solo un booleano). Hace `flush`, no `commit`.
    """
    if isinstance(owner_user_id, uuid.UUID):
        owner = owner_user_id
    else:
        try:
            owner = uuid.UUID(str(owner_user_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise LoanValidationError(
                f"user_id debe ser UUID, recibido: {owner_user_id!r}"
            ) from exc
    if not isinstance(enabled, bool):
        raise LoanValidationError("enabled debe ser bool")
    loan = loans_repo.get_loan(session, loan_id)
    if loan is None:
        raise LoanNotFoundError("prestamo inexistente")
    from app.modules.credits.repository import applications as apps_repo

    app_row = apps_repo.get_application(session, loan.application_id)
    if app_row is None or app_row.user_id != owner:
        raise LoanNotFoundError("prestamo inexistente")
    return loans_repo.set_auto_debit(session, loan.id, enabled)


def update_outstanding_principal(
    session: Session, loan_id: uuid.UUID | str, outstanding_minor: int
) -> Loan:
    """Ajusta el saldo de capital (nunca negativo)."""
    return loans_repo.update_outstanding_principal(session, loan_id, outstanding_minor)


def link_contracts_to_loan(
    session: Session,
    *,
    application_id: uuid.UUID | str,
    loan_id: uuid.UUID | str,
) -> int:
    """Rellena `contracts.loan_id` del juego con el prestamo (G54)."""
    return loans_repo.link_contracts_to_loan(
        session, application_id=application_id, loan_id=loan_id
    )


def create_loan_with_schedule(
    session: Session,
    *,
    application_id: uuid.UUID | str,
    account_id: uuid.UUID | str,
    principal_minor: int,
    annual_rate: Decimal | str | int,
    term_months: int,
    insurance_rate: Decimal | str | int = Decimal(0),
    first_due_date: date,
    disbursed_at: datetime | None = None,
    auto_debit_enabled: bool = False,
) -> tuple[Loan, list[LoanSchedule]]:
    """Crea el prestamo con su cronograma definitivo y vincula el juego.

    Calcula el cronograma con el dominio puro, deriva la TCEA del flujo
    definitivo (`financial.compute_tcea`, incluye desgravamen), crea la
    fila `loans`, persiste las N cuotas y rellena `contracts.loan_id`.
    La TCEA queda congelada en la fila (trazabilidad del costo total).
    """
    definitive = build_definitive_schedule(
        principal_minor=principal_minor,
        annual_rate=annual_rate,
        term_months=term_months,
        insurance_rate=insurance_rate,
        first_due_date=first_due_date,
    )
    try:
        cashflows = financial.schedule_cashflows(
            principal_minor,
            [
                financial.ScheduleRow(
                    installment_no=item.installment_no,
                    due_date=item.due_date,
                    principal_minor=item.principal_minor,
                    interest_minor=item.interest_minor,
                    insurance_minor=item.insurance_minor,
                    total_minor=item.total_minor,
                    balance_minor=0,
                )
                for item in definitive
            ],
        )
        tcea = financial.compute_tcea(cashflows)
    except financial.FinancialValidationError as exc:
        raise LoanValidationError(str(exc)) from exc
    loan = create_loan(
        session,
        application_id=application_id,
        account_id=account_id,
        principal_minor=principal_minor,
        tcea=tcea,
        term_months=term_months,
        insurance_rate=insurance_rate,
        status="ACTIVE",
        disbursed_at=disbursed_at,
        auto_debit_enabled=auto_debit_enabled,
    )
    schedules = create_schedules(session, loan_id=loan.id, rows=definitive)
    link_contracts_to_loan(session, application_id=application_id, loan_id=loan.id)
    return loan, schedules
