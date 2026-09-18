"""Endpoints de activacion y reenvio de OTP (E1-T10, HU02 CA-02/CA-03/CA-04).

`POST /auth/activate {user_ref, code}` -> valida el OTP `ACTIVATION` y deja
`users.status = ACTIVE` en la misma sesion; `user.activated` ya lo encolo
`otp_service.validate_otp` via outbox (no se duplica).
`POST /auth/otp/resend {user_ref[, channel]}` -> codigo nuevo que invalida
el anterior + notificacion best-effort via la fachada `notifications.send`.

Montados bajo `/api/v1` por `app.main` via `iter_routers` (este `router` lo
recoge `api/__init__.py`; sin registro extra). Sin logica en el router
(05#1): valida, traduce y delega a `service/activation`. Sin auth todavia:
pre-activacion (el usuario aun no tiene sesion).

Respuestas 05#4 (`{"data", "meta"}` + `request_id`); errores problem+json
via `AppError`:

- 400 `INVALID_OTP`: codigo incorrecto, sin OTP pendiente, usuario
  inexistente o `user_ref` malformado (respuesta identica: no filtra
  existencia ni estado).
- 400 `EXPIRED_OTP`: codigo vencido.
- 429 `RESEND_LIMIT`: maximo de reenvios o espera minima entre reenvios
  (`details.retry_after_seconds` cuando aplica).
- 429 `RATE_LIMITED`: ventana en memoria de reenvios excedida (prod: Redis).
- 422 estandar de FastAPI (`{"detail": [...]}`) para esquema malformado.

Transaccion: el servicio hace `flush`; el endpoint confirma (`commit`) en
exito y revierte (`rollback`) ante error de negocio. El codigo OTP jamas
sale en respuestas ni logs. OpenAPI automatico por FastAPI
(`response_model`).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.modules.identity.schemas.activation import (
    ActivateRequest,
    ActivateResponse,
    ResendRequest,
    ResendResponse,
)
from app.modules.identity.service import activation as activation_service

router = APIRouter(tags=["identity"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


@router.post(
    "/auth/activate",
    response_model=ActivateResponse,
    summary="Valida OTP y activa la cuenta",
)
def activate_account(
    body: ActivateRequest, request: Request, db: Session = Depends(get_db)
) -> dict:
    """Activa la cuenta con el OTP de un solo uso (HU02 CA-02/CA-03)."""
    try:
        result = activation_service.activate_account(db, user_ref=body.user_ref, code=body.code)
    except activation_service.ActivationExpiredError as exc:
        db.rollback()
        raise AppError(code="EXPIRED_OTP", message=str(exc), status_code=400) from exc
    except activation_service.ActivationInvalidError as exc:
        db.rollback()
        raise AppError(code="INVALID_OTP", message=str(exc), status_code=400) from exc
    db.commit()
    return {"data": result, "meta": {"request_id": _request_id(request)}}


@router.post(
    "/auth/otp/resend",
    response_model=ResendResponse,
    summary="Reenvia el OTP de activacion con control de intentos",
)
def resend_activation_otp(
    body: ResendRequest, request: Request, db: Session = Depends(get_db)
) -> dict:
    """Emite un codigo nuevo y lo notifica (HU02 CA-04 + rate limit)."""
    try:
        result = activation_service.resend_activation_otp(
            db, user_ref=body.user_ref, channel=body.channel
        )
    except activation_service.ActivationExpiredError as exc:
        db.rollback()
        raise AppError(code="EXPIRED_OTP", message=str(exc), status_code=400) from exc
    except activation_service.ActivationInvalidError as exc:
        db.rollback()
        raise AppError(code="INVALID_OTP", message=str(exc), status_code=400) from exc
    except activation_service.ActivationResendLimitError as exc:
        db.rollback()
        details = (
            {"retry_after_seconds": exc.retry_after_seconds}
            if exc.retry_after_seconds is not None
            else {}
        )
        raise AppError(
            code="RESEND_LIMIT", message=str(exc), status_code=429, details=details
        ) from exc
    except activation_service.ActivationRateLimitedError as exc:
        db.rollback()
        raise AppError(code="RATE_LIMITED", message=str(exc), status_code=429) from exc
    db.commit()
    return {"data": result, "meta": {"request_id": _request_id(request)}}


__all__ = ["router"]
