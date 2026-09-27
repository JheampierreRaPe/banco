"""Endpoints de transferencias (E2-T07, HU06 CA-01/CA-03).

- `POST /transfers/own` (ruta atomica SIN hold, `04#7.1`) y
  `POST /transfers/third-party` (hold + liquidacion, `04#7.2`), montados
  bajo `/api/v1` por `app.main` via `iter_routers` (sin registro extra).
- Sin logica en el router (05#1): valida, traduce y delega al `service/`.
- Auth: `Authorization: Bearer <jwt>` (`sub` = user_id, 05#2, mismo patron
  que `accounts.api`); sin token valido -> 401. En tests se inyecta
  contexto falso con `app.dependency_overrides[get_current_user_id]`.
- Idempotencia (regla de oro 5, `04#5`): `Idempotency-Key` obligatoria;
  misma clave + mismo cuerpo = misma respuesta (`meta.replayed=true`, sin
  doble debito); misma clave + cuerpo distinto = 409. El handler dentro de
  `execute_with_idempotency` no publica eventos al bus (los eventos salen
  por `outbox` del motor, regla de oro 8).
- La validacion de cuentas por fachada corre ANTES del reclamo de
  idempotencia (solo lectura: un 404/403/400 no consume la clave).
- Saldo insuficiente -> 201 con `data.status=REJECTED` y
  `data.failure_code=INSUFFICIENT_FUNDS` (sin tocar saldos, `04#10`); la
  respuesta queda guardada y el reintento devuelve lo mismo.
- Respuestas 05#4 (`{"data", "meta"}`) y errores problem+json via
  `AppError`. OpenAPI automatico por FastAPI (`response_model`).
"""

from __future__ import annotations

import logging
import uuid

import jwt
from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.orm import Session

from app.adapters.holder_lookup import HolderLookupError
from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import decode_token
from app.modules.transactions import repository as tx_repository
from app.modules.transactions import service as transfers_service
from app.modules.transactions.api.idempotency import (
    IdempotencyConflictError,
    IdempotencyKeyMissingError,
    execute_with_idempotency,
    require_idempotency_key,
)
from app.modules.transactions.schemas import (
    InterbankTransferRequest,
    InterbankTransferResponse,
    TransactionDetailResponse,
    TransferOwnRequest,
    TransferResponse,
    TransferThirdPartyRequest,
)
from app.modules.transactions.service import receipt as receipt_service
from app.modules.transactions.service.interbank import (
    ExternalRefConflictError,
    InterbankError,
    failure_code_of,
    tracking_of,
)

router = APIRouter(tags=["transactions"])

logger = logging.getLogger(__name__)


def get_current_user_id(authorization: str | None = Header(default=None)) -> uuid.UUID:
    """Extrae el `user_id` (`sub`) del JWT `Bearer` (05#2).

    401 si falta la cabecera, no es `Bearer`, el token es invalido/expirado
    o `sub` no es UUID. Delega en `core.security` (sin mecanismo propio).
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise AppError(
            code="NOT_AUTHENTICATED",
            message="Se requiere Authorization: Bearer <jwt>",
            status_code=401,
        )
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = decode_token(token)
    except jwt.PyJWTError as exc:
        logger.debug("rechazo JWT transfers: %s", type(exc).__name__)
        raise AppError(
            code="NOT_AUTHENTICATED",
            message="Token invalido o expirado",
            status_code=401,
        ) from exc
    try:
        return uuid.UUID(str(payload.get("sub")))
    except (ValueError, AttributeError, TypeError) as exc:
        raise AppError(
            code="NOT_AUTHENTICATED",
            message="Token sin sujeto valido",
            status_code=401,
        ) from exc


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


def _transfer_data(tx) -> dict:
    """Serializa la `Transaction` al `data` de `TransferResponse` (solo UUIDs).

    Todo en texto/enteros JSON-serializables: el snapshot de idempotencia
    exige `json.dumps` y `response_model` vuelve a coaccionar a UUID.
    """
    failure_code = "INSUFFICIENT_FUNDS" if tx.status == "REJECTED" else None
    return {
        "transaction_id": str(tx.id),
        "type": tx.type,
        "status": tx.status,
        "source_account_id": str(tx.source_account_id) if tx.source_account_id else None,
        "target_account_id": str(tx.target_account_id) if tx.target_account_id else None,
        "amount_minor": int(tx.amount_minor),
        "currency": tx.currency,
        "fee_minor": int(tx.fee_minor or 0),
        "failure_code": failure_code,
    }


def _require_key(request: Request, endpoint: str) -> str:
    """Exige `Idempotency-Key` (-> 422 si falta en ruta de dinero)."""
    try:
        return require_idempotency_key(request.headers, endpoint=endpoint)  # type: ignore[return-value]
    except IdempotencyKeyMissingError as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc


def _execute_idempotent(
    db: Session,
    *,
    key: str,
    user_id: uuid.UUID,
    endpoint: str,
    body: dict,
    run,
) -> tuple[dict, bool]:
    """Envuelve el caso de uso con idempotencia (hace `flush`, no `commit`)."""
    try:
        result = execute_with_idempotency(
            db,
            key=key,
            user_id=user_id,
            endpoint=endpoint,
            method="POST",
            body=body,
            handler=run,
        )
    except IdempotencyConflictError as exc:
        raise AppError(code="CONFLICT", message=str(exc), status_code=409) from exc
    return result.response, result.replayed


def _map_transfer_error(exc: Exception) -> AppError:
    """Traduce errores del caso de uso a `AppError` (05#4/05#5)."""
    if isinstance(exc, transfers_service.AccountNotFoundError):
        return AppError(code="NOT_FOUND", message=str(exc), status_code=404)
    if isinstance(
        exc, (transfers_service.AccountNotAuthorizedError, transfers_service.AccountBlockedError)
    ):
        return AppError(code="NOT_AUTHORIZED", message=str(exc), status_code=403)
    return AppError(code="VALIDATION_ERROR", message=str(exc), status_code=400)


@router.post(
    "/transfers/own",
    response_model=TransferResponse,
    status_code=201,
    summary="Transferencia entre cuentas propias (atomica, sin comision)",
)
def transfer_own(
    body: TransferOwnRequest,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Debito/credito inmediatos con un solo asiento `2000-S -> 2000-D` (HU06 CA-01)."""
    endpoint = "/transfers/own"
    key = _require_key(request, endpoint)
    payload = body.model_dump(mode="json")
    try:
        # Solo lectura (no consume la clave si la cuenta/moneda falla).
        transfers_service.validate_transfer_parties(
            db,
            initiator_user_id=user_id,
            source_account_id=body.source_account_id,
            target_account_id=body.target_account_id,
            currency=body.currency,
            kind="OWN_TRANSFER",
        )

        def _run():
            tx = transfers_service.transfer_own(
                db,
                initiator_user_id=user_id,
                source_account_id=body.source_account_id,
                target_account_id=body.target_account_id,
                amount_minor=body.amount_minor,
                currency=body.currency,
                idempotency_key=key,
                external_ref=body.external_ref,
                metadata=body.metadata,
            )
            return ({"data": _transfer_data(tx)}, tx.id)

        response, replayed = _execute_idempotent(
            db, key=key, user_id=user_id, endpoint=endpoint, body=payload, run=_run
        )
    except AppError:
        db.rollback()
        raise
    except (transfers_service.TransferError, ValueError, TypeError) as exc:
        db.rollback()
        raise _map_transfer_error(exc) from exc
    except Exception:
        db.rollback()
        raise
    db.commit()
    data = dict(response.get("data", {}))
    return {"data": data, "meta": {"request_id": _request_id(request), "replayed": replayed}}


@router.post(
    "/transfers/third-party",
    response_model=TransferResponse,
    status_code=201,
    summary="Transferencia a tercero del mismo banco (con comision a 4100)",
)
def transfer_third_party(
    body: TransferThirdPartyRequest,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Hold + liquidacion; la comision se acredita a `4100` (HU06, `04#7.2`)."""
    endpoint = "/transfers/third-party"
    key = _require_key(request, endpoint)
    payload = body.model_dump(mode="json")
    try:
        # Solo lectura (no consume la clave si la cuenta/moneda falla).
        transfers_service.validate_transfer_parties(
            db,
            initiator_user_id=user_id,
            source_account_id=body.source_account_id,
            target_account_id=body.target_account_id,
            currency=body.currency,
            kind="THIRD_PARTY_TRANSFER",
        )

        def _run():
            tx = transfers_service.transfer_third_party(
                db,
                initiator_user_id=user_id,
                source_account_id=body.source_account_id,
                target_account_id=body.target_account_id,
                amount_minor=body.amount_minor,
                currency=body.currency,
                fee_minor=body.fee_minor,
                idempotency_key=key,
                external_ref=body.external_ref,
                metadata=body.metadata,
            )
            return ({"data": _transfer_data(tx)}, tx.id)

        response, replayed = _execute_idempotent(
            db, key=key, user_id=user_id, endpoint=endpoint, body=payload, run=_run
        )
    except AppError:
        db.rollback()
        raise
    except (transfers_service.TransferError, ValueError, TypeError) as exc:
        db.rollback()
        raise _map_transfer_error(exc) from exc
    except Exception:
        db.rollback()
        raise
    db.commit()
    data = dict(response.get("data", {}))
    return {"data": data, "meta": {"request_id": _request_id(request), "replayed": replayed}}


def _map_holder_error(exc: Exception) -> AppError:
    """Traduce errores de `validate_holder` (E2-T08) a codigos neutros.

    Sin filtrar existencia: "titular no coincide" y "CCI inexistente" son
    el mismo `404 HOLDER_NOT_FOUND`; timeout/caida son `503` (sin
    distinguir la falla de red); formato es `422`; rate limit es `429`.
    """
    from app.adapters.holder_lookup import (
        HolderLookupTimeoutError,
        HolderLookupUnavailableError,
        HolderNotFoundError,
        HolderRateLimitedError,
        HolderValidationError,
    )

    if isinstance(exc, HolderNotFoundError):
        return AppError(code="HOLDER_NOT_FOUND", message=str(exc), status_code=404)
    if isinstance(exc, HolderValidationError):
        return AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422)
    if isinstance(exc, HolderRateLimitedError):
        return AppError(code="RATE_LIMITED", message=str(exc), status_code=429)
    if isinstance(exc, (HolderLookupTimeoutError, HolderLookupUnavailableError)):
        return AppError(code="HOLDER_LOOKUP_UNAVAILABLE", message=str(exc), status_code=503)
    return AppError(code="VALIDATION_ERROR", message=str(exc), status_code=400)


def _interbank_data(tx) -> dict:
    """Serializa la saga al `data` de `InterbankTransferResponse` (JSON-serializable)."""
    return {
        "transaction_id": str(tx.id),
        "type": tx.type,
        "status": tx.status,
        "source_account_id": str(tx.source_account_id) if tx.source_account_id else None,
        "amount_minor": int(tx.amount_minor),
        "currency": tx.currency,
        "fee_minor": int(tx.fee_minor or 0),
        "external_ref": tx.external_ref,
        "tracking": tracking_of(tx),
        "failure_code": failure_code_of(tx),
    }


@router.post(
    "/transfers/interbank",
    response_model=InterbankTransferResponse,
    status_code=202,
    summary="Transferencia interbancaria saliente (saga con polling)",
)
def transfer_interbank(
    body: InterbankTransferRequest,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Retencion + envio por pasarela con seguimiento (`04#7.3`, HU06 CA-02).

    `202 Accepted` siempre (incluye `REJECTED` por fondos y `FAILED` tras
    compensacion): el `status` + `tracking.poll_url`
    (`GET /transactions/{id}`) dicen como seguir. `Idempotency-Key`
    obligatoria; misma clave + mismo cuerpo = replay sin doble debito.
    El titular/CCI se valida ANTES de reclamar la clave (un 404/422/403
    no la consume).
    """
    from app.modules.transactions.service.interbank import resolve_gateway

    endpoint = "/transfers/interbank"
    key = _require_key(request, endpoint)
    payload = body.model_dump(mode="json")
    try:
        # Solo lectura (no consume la clave si el titular/cuenta falla).
        transfers_service.validate_interbank_parties(
            db,
            initiator_user_id=user_id,
            source_account_id=body.source_account_id,
            currency=body.currency,
            cci=body.cci,
            holder_name=body.holder_name,
            bank_code=body.bank_code,
        )

        def _run():
            tx = transfers_service.transfer_interbank(
                db,
                initiator_user_id=user_id,
                source_account_id=body.source_account_id,
                amount_minor=body.amount_minor,
                currency=body.currency,
                cci=body.cci,
                holder_name=body.holder_name,
                bank_code=body.bank_code,
                fee_minor=body.fee_minor,
                idempotency_key=key,
                external_ref=body.external_ref,
                metadata=body.metadata,
                gateway=resolve_gateway(None),
            )
            return ({"data": _interbank_data(tx)}, tx.id)

        response, replayed = _execute_idempotent(
            db, key=key, user_id=user_id, endpoint=endpoint, body=payload, run=_run
        )
    except AppError:
        db.rollback()
        raise
    except ExternalRefConflictError as exc:
        db.rollback()
        raise AppError(code="CONFLICT", message=str(exc), status_code=409) from exc
    except (
        transfers_service.TransferError,
        InterbankError,
        HolderLookupError,
        ValueError,
        TypeError,
    ) as exc:
        db.rollback()
        mapped = _try_map_holder_or_transfer(exc)
        raise mapped from exc
    except Exception:
        db.rollback()
        raise
    db.commit()
    data = dict(response.get("data", {}))
    return {"data": data, "meta": {"request_id": _request_id(request), "replayed": replayed}}


def _try_map_holder_or_transfer(exc: Exception) -> AppError:
    """Mapea errores de titular (neutros) o de transferencia/cuentas."""
    if isinstance(exc, HolderLookupError):
        return _map_holder_error(exc)
    return _map_transfer_error(exc)


@router.get(
    "/transactions/{transaction_id}",
    response_model=TransactionDetailResponse,
    status_code=200,
    summary="Estado y seguimiento de una transaccion (polling interbancario)",
)
def get_transaction_detail(
    transaction_id: str,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Detalle + `tracking` + historial (sin PII: titular enmascarado).

    `404 NOT_FOUND` si no existe o el id no es UUID (neutro);
    `403 NOT_AUTHORIZED` si es de otro titular. Solo lectura (sin
    `Idempotency-Key`: no mueve dinero).
    """
    try:
        wanted = uuid.UUID(str(transaction_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise AppError(
            code="NOT_FOUND", message="transaccion inexistente", status_code=404
        ) from exc
    tx = tx_repository.get_transaction(db, wanted)
    if tx is None:
        raise AppError(code="NOT_FOUND", message="transaccion inexistente", status_code=404)
    if tx.initiator_user_id is not None and tx.initiator_user_id != user_id:
        raise AppError(
            code="NOT_AUTHORIZED",
            message="la transaccion no pertenece al titular",
            status_code=403,
        )
    history = [
        {
            "from_status": h.from_status,
            "to_status": h.to_status,
            "reason": h.reason,
            "actor_type": h.actor_type,
            "created_at": h.created_at.isoformat() if h.created_at is not None else None,
        }
        for h in tx_repository.list_history(db, tx.id)
    ]
    data = {
        "transaction_id": str(tx.id),
        "type": tx.type,
        "status": tx.status,
        "source_account_id": str(tx.source_account_id) if tx.source_account_id else None,
        "target_account_id": str(tx.target_account_id) if tx.target_account_id else None,
        "amount_minor": int(tx.amount_minor),
        "currency": tx.currency,
        "fee_minor": int(tx.fee_minor or 0),
        "external_ref": tx.external_ref,
        "tracking": tracking_of(tx) if tx.type == "INTERBANK_TRANSFER" else {},
        "failure_code": failure_code_of(tx),
        "history": history,
    }
    return {"data": data, "meta": {"request_id": _request_id(request)}}


@router.get(
    "/transactions/{transaction_id}/receipt",
    status_code=200,
    summary="Comprobante PDF de una transferencia (numero de operacion unico)",
    responses={
        200: {"description": "PDF del comprobante (`application/pdf`)"},
        404: {"description": "`NOT_FOUND` (inexistente)"},
        403: {"description": "`NOT_AUTHORIZED` (ajena)"},
        409: {"description": "`VALIDATION_ERROR` (no emitible)"},
        501: {"description": "`RECEIPT_FORMAT_NOT_SUPPORTED` (sin backend PDF)"},
    },
)
def get_transaction_receipt(
    transaction_id: str,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> Response:
    """Comprobante en PDF con fecha, montos, cuentas enmascaradas y nº de operacion (E2-T10, HU06 CA-04).

    Solo lectura (sin `Idempotency-Key`: no mueve dinero; repetir la
    consulta devuelve el mismo nº de operacion sin crear filas).
    `404 NOT_FOUND` si no existe o el id no es UUID (neutro);
    `403 NOT_AUTHORIZED` si es de otro titular; `409 VALIDATION_ERROR`
    si no esta en estado emitible (`POSTED`/`SETTLED`/`CONCILIATED`);
    `501 RECEIPT_FORMAT_NOT_SUPPORTED` si no hay backend PDF.
    """
    try:
        wanted = uuid.UUID(str(transaction_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise AppError(
            code="NOT_FOUND", message="transaccion inexistente", status_code=404
        ) from exc
    tx = tx_repository.get_transaction(db, wanted)
    if tx is None:
        raise AppError(code="NOT_FOUND", message="transaccion inexistente", status_code=404)
    if tx.initiator_user_id is not None and tx.initiator_user_id != user_id:
        raise AppError(
            code="NOT_AUTHORIZED",
            message="la transaccion no pertenece al titular",
            status_code=403,
        )
    try:
        data, pdf = receipt_service.get_receipt_pdf(db, tx)
    except receipt_service.ReceiptNotIssuableError as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=409) from exc
    except receipt_service.ReceiptFormatUnavailableError as exc:
        raise AppError(
            code="RECEIPT_FORMAT_NOT_SUPPORTED", message=str(exc), status_code=501
        ) from exc
    logger.info("receipt served transaction_id=%s status=%s", tx.id, tx.status)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="comprobante-{data["operation_number"]}.pdf"'
            )
        },
    )


__all__ = ["get_current_user_id", "router"]
