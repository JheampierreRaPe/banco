"""Pago de cuota del prestamo (E3-T20, HU12 CA-02 parte de cuotas).

Con un prestamo `ACTIVE` del titular, cobra la cuota indicada (o la
proxima pendiente/vencida) debitando la cuenta del prestamo por la
fachada del motor `transactions.service.loan_payment`
(`pay_loan_installment`, asiento `docs/04#7.6`) y, en la **misma
transaccion**, registra `loan_payments` (con el desglose aplicado),
marca la cuota `PAID` (`paid_at` del servidor), baja
`loans.outstanding_principal_minor` por el capital aplicado y, si ya
no quedan cuotas exigibles, deja `loans.status = PAID`. Emite
`loan.installment.paid` por `outbox` (payload sin PII) + auditoria
best-effort. Todo o nada (una sola sesion, solo `flush`; el `commit`
es del llamante y ante fallo se revierte todo).

- La cuota se paga **total** (MVP, decision del dueno): el monto y el
  desglose vienen de `loan_schedules` (servidor); `credits` no
  recalcula ni acepta abonos parciales (`PARTIAL` como estado
  heredado se salda en total; documentado en `docs/18`).
- Solo el titular; ajeno o inexistente -> `NOT_FOUND` neutro.
- Solo cuotas `PENDING`/`PARTIAL`/`OVERDUE` se pagan; `PAID` ->
  `CONFLICT`; prestamo no `ACTIVE` -> `CONFLICT`.
- Saldo insuficiente: el motor deja `REJECTED` +
  `INSUFFICIENT_FUNDS` sin tocar saldos; el caso de uso no crea
  `loan_payments` ni marca la cuota (reintentable con la misma
  clave: el snapshot guarda el rechazo, patron `E2-T07`).
- Idempotencia doble: HTTP por `Idempotency-Key` obligatoria (patron
  `disbursement`: misma clave + mismo cuerpo = replay con
  `meta.replayed`; cuerpo distinto = `409`; validaciones de
  titular/estado no consumen la clave) + del motor con clave anclada
  a la cuota (`loan-installment:<schedule>`): dos pagos concurrentes
  de la misma cuota convergen en una sola `Transaction` sin doble
  debito (el perdedor ve la cuota `PAID` y recibe `CONFLICT`).

Sin PII ni secretos en logs (la cuenta solo enmascarada `****dddd`);
dinero entero en centimos, sin `float`. `credits` no escribe tablas
de `transactions`/`ledger`/`accounts` (solo fachadas, regla de oro 4).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.modules.credits import events as credit_events
from app.modules.credits.models import Loan
from app.modules.credits.repository import applications as apps_repo
from app.modules.credits.repository.applications import (
    ApplicationNotFoundError,
    ApplicationValidationError,
    IdempotencyKeyConflictError,
)
from app.modules.credits.repository.loans import (
    DUE_STATUSES,
    LoanConflictError,
    LoanNotFoundError,
)

logger = logging.getLogger(__name__)

#: Endpoint logico para la idempotencia del `POST .../pay` (clave `shared`, UQ por usuario).
IDEMPOTENCY_ENDPOINT = "/credits/loans/pay"

#: Prefijo de la clave del motor (anclada a la cuota, no a la clave HTTP).
MOTOR_KEY_PREFIX = "loan-installment"


def _motor_key_for(schedule_id: uuid.UUID) -> str:
    """Clave de idempotencia del motor para una cuota (estable y unica).

    Anclar el dinero a la cuota (y no a la `Idempotency-Key` HTTP)
    hace que cualquier reintento —misma clave, otra clave o carrera
    concurrente— converja en la misma `Transaction` sin doble debito.
    """
    return f"{MOTOR_KEY_PREFIX}:{schedule_id}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_owner(owner_user_id: uuid.UUID | str) -> uuid.UUID:
    if isinstance(owner_user_id, uuid.UUID):
        return owner_user_id
    try:
        return uuid.UUID(str(owner_user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApplicationValidationError(
            f"user_id debe ser UUID, recibido: {owner_user_id!r}"
        ) from exc


def _coerce_loan_id(loan_id: object) -> uuid.UUID:
    if isinstance(loan_id, uuid.UUID):
        return loan_id
    try:
        return uuid.UUID(str(loan_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise LoanNotFoundError("prestamo inexistente") from exc


def _coerce_schedule_id(schedule_id: object) -> uuid.UUID | None:
    if schedule_id is None:
        return None
    if isinstance(schedule_id, uuid.UUID):
        return schedule_id
    try:
        return uuid.UUID(str(schedule_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApplicationValidationError(
            f"schedule_id debe ser UUID, recibido: {schedule_id!r}"
        ) from exc


def _require_owned_loan(session: Session, owner: uuid.UUID, loan_id: uuid.UUID) -> Loan:
    """Prestamo del titular (`404` neutro si es ajeno o inexistente)."""
    from app.modules.credits.service.loans import get_loan

    loan = get_loan(session, loan_id)
    if loan is None:
        raise LoanNotFoundError("prestamo inexistente")
    app_row = apps_repo.get_application(session, loan.application_id)
    if app_row is None or app_row.user_id != owner:
        raise LoanNotFoundError("prestamo inexistente")
    return loan


def _check_owned_schedule(session: Session, loan: Loan, schedule_id: uuid.UUID) -> None:
    """La cuota explicita debe existir y ser del prestamo (`VALIDATION_ERROR`)."""
    from app.modules.credits.service.loans import get_schedule_installment

    row = get_schedule_installment(session, schedule_id)
    if row is None or row.loan_id != loan.id:
        raise ApplicationValidationError("schedule_id no pertenece al prestamo")


def _resolve_source_account(session: Session, owner: uuid.UUID, loan: Loan) -> dict:
    """Cuenta de cobro = la del prestamo, por fachada `accounts` (D-01).

    Inexistente o de otro titular -> `NOT_FOUND` neutro; no `ACTIVE`
    -> `VALIDATION_ERROR`. El numero en claro nunca se loguea.
    """
    from app.modules.accounts.service import get_account as _get_account_info

    try:
        info = _get_account_info(session, loan.account_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApplicationNotFoundError("cuenta del prestamo inexistente") from exc
    if info is None or info["user_id"] != owner:
        raise ApplicationNotFoundError("cuenta del prestamo inexistente")
    if info["status"] != "ACTIVE":
        raise ApplicationValidationError(f"cuenta del prestamo no disponible ({info['status']})")
    return info


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _view_settled(loan: Loan, schedule, tx) -> dict[str, Any]:
    """Vista de respuesta del pago liquidado (sin PII)."""
    return {
        "loan_id": str(loan.id),
        "schedule_id": str(schedule.id),
        "transaction_id": str(tx.id),
        "status": str(tx.status),
        "amount_minor": int(schedule.total_minor),
        "outstanding_principal_minor": int(loan.outstanding_principal_minor),
        "paid_at": _iso(schedule.paid_at),
    }


def _view_rejected(loan: Loan, schedule, tx) -> dict[str, Any]:
    """Vista del rechazo por fondos (`200`, sin mover dinero ni marcar)."""
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    code = meta.get("failure_code")
    failure_code = str(code) if isinstance(code, str) and code else "INSUFFICIENT_FUNDS"
    return {
        "loan_id": str(loan.id),
        "schedule_id": str(schedule.id),
        "transaction_id": str(tx.id),
        "status": str(tx.status),
        "amount_minor": int(schedule.total_minor),
        "outstanding_principal_minor": int(loan.outstanding_principal_minor),
        "paid_at": None,
        "failure_code": failure_code,
    }


def _record_paid_event(session: Session, owner: uuid.UUID, loan: Loan, schedule, tx) -> None:
    """Registra `loan.installment.paid` en el outbox (payload sin PII)."""
    from app.modules.shared.repository.outbox import insert_event

    insert_event(
        session,
        aggregate_type="loan_payment",
        aggregate_id=tx.id,
        event_type=credit_events.LOAN_INSTALLMENT_PAID,
        payload={
            "loan_id": str(loan.id),
            "schedule_id": str(schedule.id),
            "user_id": str(owner),
            "transaction_id": str(tx.id),
            "amount_minor": int(schedule.total_minor),
            "currency": str(tx.currency),
        },
    )


def _audit_payment(session: Session, owner: uuid.UUID, loan: Loan, schedule, tx) -> None:
    """Auditoria best-effort del pago (sin PII ni cifras)."""
    try:
        from app.modules.audit.service import record as audit_record

        audit_record(
            session,
            actor=owner,
            action="loan.installment.paid",
            entity="loan_payment",
            entity_id=tx.id,
            metadata={
                "loan_id": str(loan.id),
                "schedule_id": str(schedule.id),
                "transaction_id": str(tx.id),
                "status": str(tx.status),
            },
        )
    except Exception as exc:  # noqa: BLE001 - la auditoria no bloquea el pago
        logger.warning("loan installment audit best-effort omitida err=%s", type(exc).__name__)


def _execute_payment(
    session: Session,
    row,
    owner: uuid.UUID,
    loan_id: uuid.UUID,
    wanted_schedule: uuid.UUID | None,
) -> tuple[dict[str, Any], bool]:
    """Cobra la cuota: motor + `loan_payments` + estado + saldo, todo o nada."""
    from app.modules.credits.service.loans import (
        flag_overdue_installments,
        get_schedule_installment,
        mark_installment_paid,
        next_due_installment,
        record_payment,
    )
    from app.modules.shared.repository.idempotency import store_response
    from app.modules.transactions.service import loan_payment as motor

    loan = _require_owned_loan(session, owner, loan_id)
    if loan.status != "ACTIVE":
        raise LoanConflictError("el prestamo no esta activo para pagos")

    # Marca de vencidas antes de resolver (las `PENDING`/`PARTIAL` con
    # `due_date` pasada quedan `OVERDUE`, exigibles igual).
    flag_overdue_installments(session, as_of=_utcnow().date())

    if wanted_schedule is not None:
        schedule = get_schedule_installment(session, wanted_schedule)
        if schedule is None or schedule.loan_id != loan.id:
            raise ApplicationValidationError("schedule_id no pertenece al prestamo")
    else:
        schedule = next_due_installment(session, loan.id)
        if schedule is None:
            raise LoanConflictError("el prestamo no tiene cuotas pendientes")
    if schedule.status == "PAID":
        raise LoanConflictError("la cuota ya esta pagada")
    if schedule.status not in DUE_STATUSES:
        raise LoanConflictError(f"la cuota no es exigible ({schedule.status})")

    target = _resolve_source_account(session, owner, loan)
    currency = str(target["currency"])
    amount = int(schedule.total_minor)
    principal = int(schedule.principal_minor)
    interest = int(schedule.interest_minor)
    insurance = int(schedule.insurance_minor)

    tx = motor.pay_loan_installment(
        session,
        source_account_id=target["account_id"],
        amount_minor=amount,
        currency=currency,
        principal_minor=principal,
        interest_minor=interest,
        insurance_minor=insurance,
        loan_ref=loan.id,
        schedule_ref=schedule.id,
        initiator_user_id=owner,
        idempotency_key=_motor_key_for(schedule.id),
    )
    if tx.status == "REJECTED":
        # Sin fondos: sin `loan_payments`, cuota intacta, saldo intacto.
        # Se snapshottea el rechazo (patron `E2-T07`: reintentable con
        # la misma clave sin doble debito).
        view = _view_rejected(loan, schedule, tx)
        store_response(session, row, response_snapshot=view, transaction_id=tx.id)
        logger.info("loan installment rejected status=%s", tx.status)
        return view, False

    payment = record_payment(
        session,
        loan_id=loan.id,
        amount_minor=amount,
        schedule_id=schedule.id,
        transaction_id=tx.id,
        principal_applied_minor=principal,
        interest_applied_minor=interest,
        insurance_applied_minor=insurance,
    )
    schedule = mark_installment_paid(session, schedule.id)
    remaining = int(loan.outstanding_principal_minor) - principal
    loan.outstanding_principal_minor = max(0, remaining)
    session.flush()
    still_due = next_due_installment(session, loan.id)
    if still_due is None or int(loan.outstanding_principal_minor) == 0:
        loan.status = "PAID"
        session.flush()
        logger.info("loan fully paid installment_no=%s", int(schedule.installment_no))
    else:
        logger.info("loan installment paid installment_no=%s", int(schedule.installment_no))
    _record_paid_event(session, owner, loan, schedule, tx)
    _audit_payment(session, owner, loan, schedule, tx)
    _ = payment
    view = _view_settled(loan, schedule, tx)
    store_response(session, row, response_snapshot=view, transaction_id=tx.id)
    return view, False


def pay_installment_idempotent(
    session: Session,
    owner_user_id: uuid.UUID | str,
    loan_id: object,
    *,
    schedule_id: object = None,
    idempotency_key: str | None,
) -> tuple[dict[str, Any], bool]:
    """Paga la cuota con `Idempotency-Key` obligatoria (debita + aplica).

    Retorna `(data, replayed)`. Las validaciones de titular/estado
    (`NOT_FOUND`/`CONFLICT`/`VALIDATION_ERROR`) **no consumen la
    clave** (el endpoint revierte y el llamado es reintentable); tras
    reclamarla, repetir el mismo cuerpo devuelve el snapshot y un
    cuerpo distinto da `409`. Sin `commit` (decide el llamante).
    """
    from app.modules.shared.repository.idempotency import (
        compute_request_hash,
        find_key,
        is_expired,
        renew_expired,
        resolve_ttl_seconds,
        try_claim,
    )

    owner = _coerce_owner(owner_user_id)
    key = str(idempotency_key).strip() if isinstance(idempotency_key, str) else ""
    if not key:
        raise ApplicationValidationError("cabecera Idempotency-Key obligatoria")
    if len(key) > 80:
        raise ApplicationValidationError("Idempotency-Key supera 80 caracteres")
    clean_loan = _coerce_loan_id(loan_id)
    wanted = _coerce_schedule_id(schedule_id)

    loan = _require_owned_loan(session, owner, clean_loan)
    if wanted is not None:
        _check_owned_schedule(session, loan, wanted)
    digest = compute_request_hash(
        {
            "loan_id": str(loan.id),
            "schedule_id": str(wanted) if wanted is not None else None,
        }
    )

    row, created = try_claim(
        session,
        key=key,
        user_id=owner,
        endpoint=IDEMPOTENCY_ENDPOINT,
        method="POST",
        request_hash=digest,
        ttl_seconds=resolve_ttl_seconds(),
    )
    if created:
        return _execute_payment(session, row, owner, loan.id, wanted)
    stored = find_key(session, key=key, user_id=owner)
    if stored is None:  # pragma: no cover - carrera extrema
        raise LoanConflictError("clave de idempotencia en conflicto")
    if is_expired(stored):
        renew_expired(
            session, stored, request_hash=digest, endpoint=IDEMPOTENCY_ENDPOINT, method="POST"
        )
        return _execute_payment(session, stored, owner, loan.id, wanted)
    if stored.request_hash != digest:
        raise IdempotencyKeyConflictError("Idempotency-Key ya usada con un cuerpo distinto")
    snapshot = stored.response_snapshot
    if not isinstance(snapshot, dict) or not snapshot:
        raise LoanConflictError("operacion en curso para esta Idempotency-Key")
    logger.info("loan installment replay status=%s", snapshot.get("status"))
    return dict(snapshot), True


__all__ = [
    "IDEMPOTENCY_ENDPOINT",
    "pay_installment_idempotent",
]
