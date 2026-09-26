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
from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import decode_token
from app.modules.transactions import service as transfers_service
from app.modules.transactions.api.idempotency import (
    IdempotencyConflictError,
    IdempotencyKeyMissingError,
    execute_with_idempotency,
    require_idempotency_key,
)
from app.modules.transactions.schemas import (
    TransferOwnRequest,
    TransferResponse,
    TransferThirdPartyRequest,
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


__all__ = ["get_current_user_id", "router"]
