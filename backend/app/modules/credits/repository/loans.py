"""Repositorio de prestamos y cronograma definitivo (E3-T18, HU12).

Capa de datos sin endpoints ni logica de negocio: solo SQLAlchemy sobre
el schema propio (`credits`), sin `JOIN` entre schemas ni imports de
modelos ajenos (regla de oro 4). Excepcion interna: `LoanApplication`
y `Contract` viven en el MISMO schema (`credits`), asi que el `JOIN`
de titularidad y el `link_contracts_to_loan` (G54: el `loans.id`
recien creado rellena el juego contractual) no cruzan fronteras.
`account_id` / `transaction_id` se tratan como UUID logicos (REF sin
FK fisica): jamas se leen sus tablas. Convencion: `flush` sin
`commit` (decide el llamante).

`loans` es una fila por `application_id` (UQ: crear dos veces el mismo
prestamo -> `CONFLICT`, idempotencia de negocio). El cronograma se
persiste completo (`installment_no` 1..N, con `due_date`) y cuadrado
(`principal+interest+insurance == total` por cuota y
`sum(principal) == principal` del prestamo): lo genera el dominio puro
`credits.domain.schedule` y aqui solo se valida antes de escribir.

Errores de negocio con `code` estable (`docs/05`, formato de errores):
`NOT_FOUND` (prestamo/cuota inexistente), `VALIDATION_ERROR`
(montos/plazos/tasas invalidos, cronograma descuadrado) y `CONFLICT`
(cuota ya pagada / UQ de `application_id` / cronograma duplicado).

Dinero entero en centimos (`int`); tasas `Decimal` exactas; jamas
`float` (regla de oro 3). Sin PII ni secretos en logs (regla de oro 7):
aqui solo se loguean conteos, numeros de cuota y estados, jamas
`user_id`, `account_id`, montos ni tasas.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.credits.models import (
    INSTALLMENT_STATUSES,
    LOAN_STATUSES,
    Contract,
    Loan,
    LoanApplication,
    LoanPayment,
    LoanSchedule,
)

logger = logging.getLogger(__name__)

__all__ = [
    "LoanConflictError",
    "LoanError",
    "LoanNotFoundError",
    "LoanValidationError",
    "create_loan",
    "create_schedules",
    "flag_overdue_installments",
    "get_loan",
    "get_schedule_installment",
    "link_contracts_to_loan",
    "list_auto_debit_dues",
    "list_loans_by_user",
    "list_schedule",
    "list_upcoming_installments",
    "mark_installment_paid",
    "mark_installment_partial",
    "next_due_installment",
    "record_payment",
    "set_auto_debit",
    "update_outstanding_principal",
]

#: Cuotas que aun exigen cobro (ordenadas por `installment_no`).
DUE_STATUSES: tuple[str, ...] = ("PENDING", "PARTIAL", "OVERDUE")

_MAX_TERM_MONTHS = 32767  # SMALLINT


class LoanError(ValueError):
    """Error base de prestamos (mapeable a 4xx)."""

    code = "CREDITS_LOAN_ERROR"


class LoanNotFoundError(LoanError, LookupError):
    """Prestamo o cuota inexistente (`NOT_FOUND` neutro)."""

    code = "NOT_FOUND"


class LoanValidationError(LoanError):
    """Dato del prestamo/cronograma invalido (`VALIDATION_ERROR`)."""

    code = "VALIDATION_ERROR"


class LoanConflictError(LoanError):
    """Duplicado por UQ o transicion de estado ilegal (`CONFLICT`)."""

    code = "CONFLICT"


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise LoanValidationError(f"{field} debe ser UUID") from exc


def _coerce_minor(name: str, value: object, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LoanValidationError(f"{name} debe ser entero de centimos")
    if value < 0 or (value == 0 and not allow_zero):
        bound = ">= 0" if allow_zero else "> 0"
        raise LoanValidationError(f"{name} debe ser {bound}")
    return value


def _coerce_term(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LoanValidationError("term_months debe ser entero de meses")
    if not 1 <= value <= _MAX_TERM_MONTHS:
        raise LoanValidationError(f"term_months debe estar en 1..{_MAX_TERM_MONTHS}")
    return value


def _coerce_rate(name: str, value: object) -> Decimal:
    if isinstance(value, (bool, float)):
        raise LoanValidationError(f"{name} debe ser Decimal/str/int (nunca float)")
    try:
        rate = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, AttributeError) as exc:
        raise LoanValidationError(f"{name} debe ser numerico") from exc
    if rate < 0:
        raise LoanValidationError(f"{name} no puede ser negativa")
    return rate


def _coerce_status(value: object, allowed: tuple[str, ...], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise LoanValidationError(f"{field} debe ser uno de {list(allowed)}")
    return value


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
    """Crea la fila `loans` (`ACTIVE`, `outstanding == principal`).

    `account_id` es REF logica (no se valida contra `accounts`).
    Un `application_id` duplicado lanza `LoanConflictError` (UQ) sin
    dejar cambios. Hace `flush`, no `commit`.
    """
    app_id = _coerce_uuid(application_id, "application_id")
    acct_id = _coerce_uuid(account_id, "account_id")
    principal = _coerce_minor("principal_minor", principal_minor)
    clean_tcea = _coerce_rate("tcea", tcea)
    term = _coerce_term(term_months)
    clean_insurance = _coerce_rate("insurance_rate", insurance_rate)
    clean_status = _coerce_status(status, LOAN_STATUSES, "status")
    if not isinstance(auto_debit_enabled, bool):
        raise LoanValidationError("auto_debit_enabled debe ser bool")
    if disbursed_at is not None and not isinstance(disbursed_at, datetime):
        raise LoanValidationError("disbursed_at debe ser datetime o None")
    row = Loan(
        application_id=app_id,
        account_id=acct_id,
        principal_minor=principal,
        tcea=clean_tcea,
        term_months=term,
        insurance_rate=clean_insurance,
        outstanding_principal_minor=principal,
        status=clean_status,
        disbursed_at=disbursed_at,
        auto_debit_enabled=auto_debit_enabled,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        logger.debug("loans duplicado por application_id: conflicto UQ")
        raise LoanConflictError("el expediente ya tiene prestamo creado") from exc
    logger.info("loan creado term_months=%s status=%s", term, clean_status)
    return row


def link_contracts_to_loan(
    session: Session,
    *,
    application_id: uuid.UUID | str,
    loan_id: uuid.UUID | str,
) -> int:
    """Rellena `contracts.loan_id` del juego con el prestamo (G54).

    Solo toca filas aun sin vincular (`loan_id IS NULL`) del mismo
    schema; devuelve cuantas vinculo. Hace `flush`, no `commit`.
    """
    app_id = _coerce_uuid(application_id, "application_id")
    clean_loan = _coerce_uuid(loan_id, "loan_id")
    stmt = (
        sa.update(Contract)
        .where(Contract.application_id == app_id, Contract.loan_id.is_(None))
        .values(loan_id=clean_loan)
    )
    result = session.execute(stmt)
    session.flush()
    count = int(result.rowcount or 0)
    logger.info("contracts vinculados al prestamo: %s filas", count)
    return count


def _schedule_row_to_dict(row: Any, pos: int) -> dict[str, Any]:
    if isinstance(row, Mapping):
        data = dict(row)
    else:
        try:
            data = {
                "installment_no": row.installment_no,
                "due_date": row.due_date,
                "principal_minor": row.principal_minor,
                "interest_minor": row.interest_minor,
                "insurance_minor": row.insurance_minor,
                "total_minor": row.total_minor,
            }
        except AttributeError as exc:
            raise LoanValidationError(f"cuota {pos}: forma invalida") from exc
    return data


def create_schedules(
    session: Session,
    *,
    loan_id: uuid.UUID | str,
    rows: Sequence[Any],
) -> list[LoanSchedule]:
    """Persiste el cronograma definitivo completo del prestamo.

    Exige el juego completo (`len(rows) == loan.term_months`),
    `installment_no` 1..N sin huecos, `due_date` por fila y cuadre
    (`principal+interest+insurance == total`,
    `sum(principal) == loan.principal_minor`); si no, `VALIDATION_ERROR`
    sin escribir nada. Un cronograma ya persistido -> `CONFLICT` (UQ).
    Hace `flush`, no `commit`.
    """
    clean_loan = _coerce_uuid(loan_id, "loan_id")
    loan = session.get(Loan, clean_loan)
    if loan is None:
        raise LoanNotFoundError("prestamo inexistente")
    items = list(rows or [])
    if len(items) != int(loan.term_months):
        raise LoanValidationError(
            f"el cronograma debe traer {int(loan.term_months)} cuotas, llegaron {len(items)}"
        )
    seen: set[int] = set()
    total_principal = 0
    prepared: list[LoanSchedule] = []
    for pos, raw in enumerate(items, start=1):
        data = _schedule_row_to_dict(raw, pos)
        number = data.get("installment_no")
        if isinstance(number, bool) or not isinstance(number, int) or number != pos:
            raise LoanValidationError(f"la cuota {pos} debe tener installment_no={pos}")
        if number in seen:  # pragma: no cover - defensa en profundidad
            raise LoanValidationError(f"installment_no duplicado: {number}")
        seen.add(number)
        due = data.get("due_date")
        if isinstance(due, bool) or not isinstance(due, date):
            raise LoanValidationError(f"la cuota {pos} debe tener due_date")
        principal = _coerce_minor(f"cuota {pos}: principal_minor", data.get("principal_minor"))
        interest = _coerce_minor(
            f"cuota {pos}: interest_minor", data.get("interest_minor"), allow_zero=True
        )
        insurance = _coerce_minor(
            f"cuota {pos}: insurance_minor", data.get("insurance_minor"), allow_zero=True
        )
        total = _coerce_minor(f"cuota {pos}: total_minor", data.get("total_minor"))
        if principal + interest + insurance != total:
            raise LoanValidationError(
                f"la cuota {pos} no cuadra: principal+interest+insurance != total"
            )
        total_principal += principal
        prepared.append(
            LoanSchedule(
                loan_id=clean_loan,
                installment_no=number,
                due_date=due,
                principal_minor=principal,
                interest_minor=interest,
                insurance_minor=insurance,
                total_minor=total,
                status="PENDING",
                paid_at=None,
            )
        )
    if total_principal != int(loan.principal_minor):
        raise LoanValidationError("el cronograma no cuadra: sum(principal) != principal_minor")
    try:
        with session.begin_nested():
            session.add_all(prepared)
            session.flush()
    except IntegrityError as exc:
        logger.debug("loan_schedules duplicado: conflicto UQ")
        raise LoanConflictError("el prestamo ya tiene cronograma creado") from exc
    logger.info("cronograma creado: %s cuotas", len(prepared))
    return prepared


def get_loan(session: Session, loan_id: uuid.UUID | str) -> Loan | None:
    """Prestamo por `id` (`None` si no existe)."""
    return session.get(Loan, _coerce_uuid(loan_id, "loan_id"))


def list_loans_by_user(session: Session, user_id: uuid.UUID | str) -> list[Loan]:
    """Prestamos del titular (`created_at DESC`; mismo schema, sin PII en logs)."""
    clean_user = _coerce_uuid(user_id, "user_id")
    stmt = (
        sa.select(Loan)
        .join(LoanApplication, LoanApplication.id == Loan.application_id)
        .where(LoanApplication.user_id == clean_user)
        .order_by(Loan.created_at.desc(), Loan.id)
    )
    rows = list(session.scalars(stmt).all())
    logger.debug("loans listados del titular: %s", len(rows))
    return rows


def list_schedule(session: Session, loan_id: uuid.UUID | str) -> list[LoanSchedule]:
    """Cronograma del prestamo ordenado por `installment_no`."""
    clean_loan = _coerce_uuid(loan_id, "loan_id")
    stmt = (
        sa.select(LoanSchedule)
        .where(LoanSchedule.loan_id == clean_loan)
        .order_by(LoanSchedule.installment_no)
    )
    return list(session.scalars(stmt).all())


def get_schedule_installment(session: Session, schedule_id: uuid.UUID | str) -> LoanSchedule | None:
    """Una cuota por `id` (`None` si no existe)."""
    return session.get(LoanSchedule, _coerce_uuid(schedule_id, "schedule_id"))


def next_due_installment(session: Session, loan_id: uuid.UUID | str) -> LoanSchedule | None:
    """Primera cuota exigible (`PENDING`/`PARTIAL`/`OVERDUE`, por numero)."""
    clean_loan = _coerce_uuid(loan_id, "loan_id")
    stmt = (
        sa.select(LoanSchedule)
        .where(
            LoanSchedule.loan_id == clean_loan,
            LoanSchedule.status.in_(DUE_STATUSES),
        )
        .order_by(LoanSchedule.installment_no)
        .limit(1)
    )
    return session.scalars(stmt).first()


def mark_installment_paid(
    session: Session, schedule_id: uuid.UUID | str, *, paid_at: datetime | None = None
) -> LoanSchedule:
    """Marca la cuota `PAID` (ya pagada -> `CONFLICT`). Hace `flush`, no `commit`."""
    row = get_schedule_installment(session, schedule_id)
    if row is None:
        raise LoanNotFoundError("cuota inexistente")
    if row.status == "PAID":
        raise LoanConflictError("la cuota ya esta pagada")
    _coerce_status(row.status, INSTALLMENT_STATUSES, "status")
    moment = paid_at if paid_at is not None else _utcnow()
    if not isinstance(moment, datetime):
        raise LoanValidationError("paid_at debe ser datetime o None")
    row.status = "PAID"
    row.paid_at = moment
    session.flush()
    logger.info("cuota marcada PAID installment_no=%s", int(row.installment_no))
    return row


def mark_installment_partial(session: Session, schedule_id: uuid.UUID | str) -> LoanSchedule:
    """Marca la cuota `PARTIAL` (ya pagada -> `CONFLICT`). Hace `flush`, no `commit`."""
    row = get_schedule_installment(session, schedule_id)
    if row is None:
        raise LoanNotFoundError("cuota inexistente")
    if row.status == "PAID":
        raise LoanConflictError("la cuota ya esta pagada")
    _coerce_status(row.status, INSTALLMENT_STATUSES, "status")
    row.status = "PARTIAL"
    session.flush()
    logger.info("cuota marcada PARTIAL installment_no=%s", int(row.installment_no))
    return row


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
    """Registra una aplicacion de pago (`loan_payments`).

    `transaction_id` es REF logica al motor (la puebla `E3-T20`; aqui
    no se valida contra `transactions`). Hace `flush`, no `commit`.
    """
    clean_loan = _coerce_uuid(loan_id, "loan_id")
    if session.get(Loan, clean_loan) is None:
        raise LoanNotFoundError("prestamo inexistente")
    amount = _coerce_minor("amount_minor", amount_minor)
    clean_schedule: uuid.UUID | None = None
    if schedule_id is not None:
        clean_schedule = _coerce_uuid(schedule_id, "schedule_id")
        if session.get(LoanSchedule, clean_schedule) is None:
            raise LoanNotFoundError("cuota inexistente")
    clean_tx: uuid.UUID | None = None
    if transaction_id is not None:
        clean_tx = _coerce_uuid(transaction_id, "transaction_id")
    applied = (
        _coerce_minor("principal_applied_minor", principal_applied_minor, allow_zero=True),
        _coerce_minor("interest_applied_minor", interest_applied_minor, allow_zero=True),
        _coerce_minor("insurance_applied_minor", insurance_applied_minor, allow_zero=True),
    )
    if sum(applied) > amount:
        raise LoanValidationError("el desglose aplicado supera amount_minor")
    row = LoanPayment(
        loan_id=clean_loan,
        schedule_id=clean_schedule,
        transaction_id=clean_tx,
        amount_minor=amount,
        principal_applied_minor=applied[0],
        interest_applied_minor=applied[1],
        insurance_applied_minor=applied[2],
    )
    session.add(row)
    session.flush()
    logger.debug("pago registrado amount_splits=%s partes", len(applied))
    return row


def flag_overdue_installments(session: Session, *, as_of: date) -> int:
    """Marca `OVERDUE` las cuotas vencidas (`PENDING`/`PARTIAL` con `due_date < as_of`).

    La consume el job de `E3-T19`. Devuelve cuantas marco. Hace
    `flush`, no `commit`.
    """
    moment = as_of.date() if isinstance(as_of, datetime) else as_of
    if isinstance(moment, bool) or not isinstance(moment, date):
        raise LoanValidationError("as_of debe ser date")
    stmt = (
        sa.update(LoanSchedule)
        .where(
            LoanSchedule.status.in_(("PENDING", "PARTIAL")),
            LoanSchedule.due_date < moment,
        )
        .values(status="OVERDUE")
    )
    result = session.execute(stmt)
    session.flush()
    count = int(result.rowcount or 0)
    logger.info("cuotas marcadas OVERDUE: %s", count)
    return count


def set_auto_debit(session: Session, loan_id: uuid.UUID | str, enabled: bool) -> Loan:
    """Activa/desactiva el debito automatico (lo consume `E3-T19`)."""
    if not isinstance(enabled, bool):
        raise LoanValidationError("enabled debe ser bool")
    row = get_loan(session, loan_id)
    if row is None:
        raise LoanNotFoundError("prestamo inexistente")
    row.auto_debit_enabled = enabled
    session.flush()
    logger.info("auto_debit_enabled=%s", enabled)
    return row


def _coerce_day(value: object, field: str) -> date:
    moment = value.date() if isinstance(value, datetime) else value
    if isinstance(moment, bool) or not isinstance(moment, date):
        raise LoanValidationError(f"{field} debe ser date")
    return moment


def _coerce_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LoanValidationError(f"limit debe ser entero > 0, recibido: {value!r}")
    return value


def list_auto_debit_dues(
    session: Session, *, as_of: date, limit: int = 100
) -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
    """Cuotas exigibles de prestamos con debito automatico (`E3-T19`).

    Prestamos `ACTIVE` con `auto_debit_enabled` y cuotas
    `PENDING`/`PARTIAL`/`OVERDUE` con `due_date <= as_of`, ordenadas
    por vencimiento. Retorna tuplas planas
    `(loan_id, schedule_id, owner_user_id)` (valores, no ORM: el job
    confirma por item y los objetos expirarian). Solo lectura del
    mismo schema (`LoanApplication` aporta el titular). Hace `flush`,
    no `commit`.
    """
    moment = _coerce_day(as_of, "as_of")
    clean_limit = _coerce_limit(limit)
    stmt = (
        sa.select(Loan.id, LoanSchedule.id, LoanApplication.user_id)
        .join(Loan, Loan.id == LoanSchedule.loan_id)
        .join(LoanApplication, LoanApplication.id == Loan.application_id)
        .where(
            Loan.status == "ACTIVE",
            Loan.auto_debit_enabled.is_(True),
            LoanSchedule.status.in_(DUE_STATUSES),
            LoanSchedule.due_date <= moment,
        )
        .order_by(
            LoanSchedule.due_date.asc(),
            LoanSchedule.installment_no.asc(),
            LoanSchedule.id.asc(),
        )
        .limit(clean_limit)
    )
    rows = session.execute(stmt).all()
    result = [(loan_id, schedule_id, owner) for loan_id, schedule_id, owner in rows]
    logger.debug("auto-debit candidatas: %s", len(result))
    return result


def list_upcoming_installments(
    session: Session, *, target: date, limit: int = 100
) -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID, date]]:
    """Cuotas proximas a vencer en `target` (`E3-T19`, recordatorio).

    Prestamos `ACTIVE` con cuotas `PENDING`/`PARTIAL` y
    `due_date == target` (las `OVERDUE` ya vencieron: no son
    "proximas"). Retorna tuplas planas
    `(loan_id, schedule_id, owner_user_id, due_date)`. Solo lectura
    del mismo schema. Hace `flush`, no `commit`.
    """
    day = _coerce_day(target, "target")
    clean_limit = _coerce_limit(limit)
    stmt = (
        sa.select(Loan.id, LoanSchedule.id, LoanApplication.user_id, LoanSchedule.due_date)
        .join(Loan, Loan.id == LoanSchedule.loan_id)
        .join(LoanApplication, LoanApplication.id == Loan.application_id)
        .where(
            Loan.status == "ACTIVE",
            LoanSchedule.status.in_(("PENDING", "PARTIAL")),
            LoanSchedule.due_date == day,
        )
        .order_by(
            LoanSchedule.installment_no.asc(),
            LoanSchedule.id.asc(),
        )
        .limit(clean_limit)
    )
    rows = session.execute(stmt).all()
    result = [(loan_id, schedule_id, owner, due) for loan_id, schedule_id, owner, due in rows]
    logger.debug("recordatorio candidatas: %s", len(result))
    return result


def update_outstanding_principal(
    session: Session, loan_id: uuid.UUID | str, outstanding_minor: int
) -> Loan:
    """Ajusta el saldo de capital (nunca negativo). Hace `flush`, no `commit`."""
    row = get_loan(session, loan_id)
    if row is None:
        raise LoanNotFoundError("prestamo inexistente")
    row.outstanding_principal_minor = _coerce_minor(
        "outstanding_principal_minor", outstanding_minor, allow_zero=True
    )
    session.flush()
    logger.debug("saldo de capital actualizado")
    return row
