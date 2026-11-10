"""Endpoints del desembolso, la lectura de prestamos, el pago de cuota y el
debito automatico (E3-T17/E3-T19/E3-T20, HU12).

- `POST /credits/applications/{id}/disburse` (Bearer, `Idempotency-Key`
  obligatoria; body `{account_id?}`) -> `201` (nuevo) / `200` (replay)
  con `{loan_id, transaction_id, status, disbursed_at, account_masked}`
  y `meta.replayed`. Solo el titular (ajeno/inexistente -> `404`
  neutro); expediente no `APPROVED` o juego no `SIGNED` -> `409`;
  cuenta ajena -> `404` neutro; cuenta no `ACTIVE`/moneda incoherente
  o sin clave -> `422`. Mueve dinero por el motor (asiento `docs/04#7.5`).
- `GET /credits/loans` (Bearer, solo lectura) -> prestamos del titular.
- `GET /credits/loans/{id}/schedule` (Bearer, solo lectura) ->
  cronograma definitivo; `404` neutro si es ajeno/inexistente.
- `POST /credits/loans/{id}/pay` (Bearer, `Idempotency-Key`
   obligatoria; body `{schedule_id?}`, omitido = proxima cuota
   pendiente/vencida) -> `200` con `{loan_id, schedule_id,
   transaction_id, status, amount_minor, outstanding_principal_minor,
   paid_at}` y `meta.replayed`. Cobra la cuota **total** por el motor
   (asiento `docs/04#7.6`); sin fondos -> `200` con `status=REJECTED`
   + `failure_code=INSUFFICIENT_FUNDS` sin tocar saldos ni la cuota.
   Cuota ya `PAID` o prestamo no `ACTIVE` -> `409`; `schedule_id` de
   otro prestamo o malformado, o sin clave -> `422`.
- `PUT /credits/loans/{id}/auto-debit` (Bearer; body
   `{enabled: bool}` estricto) -> `200` con `{loan_id,
   auto_debit_enabled}`. Solo el titular (ajeno/inexistente ->
   `404` neutro); `enabled` ausente/no booleano -> `422`. No mueve
   dinero (sin `Idempotency-Key`): el job de `E3-T19` cobra despues
   por el caso de uso de `E3-T20`.

Sin logica en el router (`05#1`): valida, traduce y delega al
`service/` (`disbursement`, `loan_payments`). Sin PII ni secretos en logs.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.modules.credits.api import get_current_user_id
from app.modules.credits.repository.applications import (
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationValidationError,
    IdempotencyKeyConflictError,
)
from app.modules.credits.repository.contracts import (
    ContractConflictError,
    ContractNotFoundError,
    ContractValidationError,
)
from app.modules.credits.repository.loans import (
    LoanConflictError,
    LoanNotFoundError,
    LoanValidationError,
)
from app.modules.credits.schemas.loans import (
    AutoDebitRequest,
    AutoDebitResponse,
    DisburseRequest,
    DisburseResponse,
    LoanScheduleResponse,
    LoansListResponse,
    PayInstallmentRequest,
    PayInstallmentResponse,
)
from app.modules.credits.service import disbursement as disburse_service
from app.modules.credits.service import loan_payments as pay_service
from app.modules.credits.service import loans as loans_service

router = APIRouter(tags=["credits"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


def _map_disburse_error(exc: Exception) -> AppError:
    """Traduce errores del caso de uso a `AppError` (con rollback fuera)."""
    if isinstance(exc, (ApplicationNotFoundError, ContractNotFoundError, LoanNotFoundError)):
        return AppError(code="NOT_FOUND", message=str(exc), status_code=404)
    if isinstance(exc, IdempotencyKeyConflictError):
        return AppError(code="IDEMPOTENCY_CONFLICT", message=str(exc), status_code=409)
    if isinstance(exc, (ApplicationConflictError, ContractConflictError, LoanConflictError)):
        return AppError(code="CONFLICT", message=str(exc), status_code=409)
    if isinstance(exc, DataError):
        return AppError(
            code="VALIDATION_ERROR",
            message="dato numerico fuera de rango",
            status_code=422,
        )
    return AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422)


@router.post(
    "/credits/applications/{application_id}/disburse",
    response_model=DisburseResponse,
    status_code=201,
    summary="Desembolsa el prestamo a la cuenta elegida",
    responses={
        200: {"description": "Replay idempotente (misma clave + mismo cuerpo)"},
        201: {"description": "Prestamo creado y desembolsado (`SETTLED`)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (expediente/cuenta ajeno o inexistente)"},
        409: {"description": "`CONFLICT` / `IDEMPOTENCY_CONFLICT`"},
        422: {"description": "`VALIDATION_ERROR` (sin `Idempotency-Key`, cuenta o cuerpo)"},
    },
)
def disburse_credit_application(
    application_id: uuid.UUID,
    body: DisburseRequest,
    request: Request,
    response: Response,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Crea el prestamo + cronograma y acredita la cuenta (HU12 CA-01/CA-02).

    `Idempotency-Key` obligatoria (mueve dinero): misma clave + mismo
    cuerpo no duplica prestamo, asiento ni evento (`meta.replayed`);
    misma clave + cuerpo distinto -> `409`. Sin `account_id` se usa la
    cuenta principal del titular. Todo o nada en una transaccion.
    """
    try:
        data, replayed = disburse_service.disburse_application_idempotent(
            db,
            user_id,
            application_id,
            account_id=body.account_id,
            idempotency_key=idempotency_key,
        )
    except (
        ApplicationNotFoundError,
        ApplicationConflictError,
        ApplicationValidationError,
        IdempotencyKeyConflictError,
        ContractNotFoundError,
        ContractConflictError,
        ContractValidationError,
        LoanNotFoundError,
        LoanConflictError,
        LoanValidationError,
        DataError,
        ValueError,
        TypeError,
    ) as exc:
        db.rollback()
        raise _map_disburse_error(exc) from exc
    db.commit()
    response.status_code = 200 if replayed else 201
    return {"data": data, "meta": {"request_id": _request_id(request), "replayed": replayed}}


@router.get(
    "/credits/loans",
    response_model=LoansListResponse,
    summary="Prestamos del titular",
    responses={
        200: {"description": "Prestamos propios (`data` + `meta.total`)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
    },
)
def list_credit_loans(
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Lista los prestamos desembolsados del titular (solo lectura)."""
    try:
        items = disburse_service.list_own_loans(db, user_id)
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    return {
        "data": items,
        "meta": {"total": len(items), "request_id": _request_id(request)},
    }


@router.get(
    "/credits/loans/{loan_id}/schedule",
    response_model=LoanScheduleResponse,
    summary="Cronograma definitivo del prestamo",
    responses={
        200: {"description": "Prestamo + cronograma (`data.loan`, `data.schedule`)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (prestamo ajeno o inexistente)"},
    },
)
def get_credit_loan_schedule(
    loan_id: uuid.UUID,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Cronograma definitivo del prestamo del titular (solo lectura)."""
    try:
        loan_view, rows = disburse_service.get_own_schedule(db, user_id, loan_id)
    except (LoanNotFoundError, ApplicationNotFoundError) as exc:
        db.rollback()
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    return {
        "data": {"loan": loan_view, "schedule": rows},
        "meta": {"request_id": _request_id(request)},
    }


@router.post(
    "/credits/loans/{loan_id}/pay",
    response_model=PayInstallmentResponse,
    status_code=200,
    summary="Paga la cuota total por el motor contable",
    responses={
        200: {"description": "Cuota cobrada (`SETTLED`) o rechazada sin fondos (`REJECTED`)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (prestamo/cuenta ajeno o inexistente)"},
        409: {
            "description": "`CONFLICT` / `IDEMPOTENCY_CONFLICT` (cuota ya pagada, prestamo no activo)"
        },
        422: {"description": "`VALIDATION_ERROR` (sin `Idempotency-Key` o cuerpo)"},
    },
)
def pay_credit_loan_installment(
    loan_id: uuid.UUID,
    body: PayInstallmentRequest,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Cobra la cuota total indicada (o la proxima exigible) (HU12, E3-T20).

    `Idempotency-Key` obligatoria (mueve dinero): misma clave + mismo
    cuerpo no duplica debito, asiento ni pago (`meta.replayed`); misma
    clave + cuerpo distinto -> `409`. Sin fondos -> `200` con
    `status=REJECTED` + `failure_code=INSUFFICIENT_FUNDS` (la cuota y
    los saldos quedan intactos). Todo o nada en una transaccion.
    """
    try:
        data, replayed = pay_service.pay_installment_idempotent(
            db,
            user_id,
            loan_id,
            schedule_id=body.schedule_id,
            idempotency_key=idempotency_key,
        )
    except (
        ApplicationNotFoundError,
        ApplicationConflictError,
        ApplicationValidationError,
        IdempotencyKeyConflictError,
        ContractNotFoundError,
        ContractConflictError,
        ContractValidationError,
        LoanNotFoundError,
        LoanConflictError,
        LoanValidationError,
        DataError,
        ValueError,
        TypeError,
    ) as exc:
        db.rollback()
        raise _map_disburse_error(exc) from exc
    db.commit()
    return {"data": data, "meta": {"request_id": _request_id(request), "replayed": replayed}}


@router.put(
    "/credits/loans/{loan_id}/auto-debit",
    response_model=AutoDebitResponse,
    status_code=200,
    summary="Activa o desactiva el debito automatico de cuotas",
    responses={
        200: {"description": "Flag persistido (`data.auto_debit_enabled`)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (prestamo ajeno o inexistente)"},
        422: {"description": "`VALIDATION_ERROR` (`enabled` ausente o no booleano)"},
    },
)
def set_credit_loan_auto_debit(
    loan_id: uuid.UUID,
    body: AutoDebitRequest,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Fija `loans.auto_debit_enabled` del prestamo del titular (HU12, E3-T19).

    Solo lectura/escritura de un booleano: **no mueve dinero** (sin
    `Idempotency-Key`). El job de debito automatico cobra las cuotas
    vencidas/hoy solo de los prestamos con el flag en `true`.
    """
    try:
        loan = loans_service.set_own_auto_debit(db, user_id, loan_id, body.enabled)
    except LoanNotFoundError as exc:
        db.rollback()
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except (LoanValidationError, ValueError, TypeError) as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    db.commit()
    return {
        "data": {"loan_id": str(loan.id), "auto_debit_enabled": bool(loan.auto_debit_enabled)},
        "meta": {"request_id": _request_id(request)},
    }


__all__ = ["router"]
