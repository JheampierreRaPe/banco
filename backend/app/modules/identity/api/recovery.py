"""Endpoints de recuperacion de acceso por email (E1-T33, HU04).

`POST /auth/recovery/request {email}` -> SIEMPRE 200 con cuerpo identico
exista o no el email (sin enumeracion) + OTP `RECOVERY` por email si el
usuario es elegible (cooldown: reutiliza el `PENDING` vigente).
`POST /auth/recovery/verify {email, code[, device_id/device_public_key/
platform/biometric_type]}` -> valida el OTP SIN abrir sesion (SCR-005),
inserta `access_recovery` y registra el binding del dispositivo nuevo
best-effort; devuelve `{user_ref, device_bound}` (contrato que consume
`F-T29`; la unica sesion la abre `POST /auth/login/pin`).

Montados bajo `/api/v1` por `app.main` via `iter_routers` (este `router` lo
recoge `api/__init__.py`; sin registro extra). Sin logica en el router
(05#1): valida, traduce y delega a `service/recovery`. Sin auth todavia:
son endpoints pre-sesion.

Respuestas 05#4 (`{"data", "meta"}` + `request_id`); errores problem+json
via `AppError`:

- `request`: 200 `{"accepted", "ttl_seconds", "resend_wait_seconds"}`
  (constantes globales, identico exista o no); 429 `RATE_LIMITED`
  (ventana por email+IP, verificada antes de la existencia); 422 estandar
  de FastAPI para email malformado/vacio.
- `verify`: 200 `{user_ref, device_bound}` (SIN `access_token`/
  `refresh_token`/`session_id`: no abre sesion desde E1-T33/SCR-005);
  401 `INVALID_RECOVERY_CODE`
  (mismo cuerpo para email no registrado, no elegible, sin OTP pendiente,
  codigo incorrecto, OTP vencido y OTP bloqueado por intentos agotados:
  vencido/bloqueado colapsan al generico para no filtrar existencia);
  429 `RATE_LIMITED` (ventana de verificacion por `email+IP` excedida);
  422 estandar de FastAPI para esquema malformado.

Transaccion: el servicio hace `flush`; el endpoint confirma (`commit`) en
exito Y ante error de negocio tipado (el contador de intentos del OTP y la
expiracion DEBEN persistir, como `api/pin_login.py`; en la rama ciega el
`commit` es no-op), y revierte (`rollback`) ante error inesperado. El
codigo OTP jamas sale en respuestas ni logs. OpenAPI automatico por
FastAPI (`response_model`).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.modules.identity.schemas.recovery import (
    RecoveryRequest,
    RecoveryRequestResponse,
    RecoveryVerifyRequest,
    RecoveryVerifyResponse,
)
from app.modules.identity.service import recovery as recovery_service

router = APIRouter(tags=["identity"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post(
    "/auth/recovery/request",
    response_model=RecoveryRequestResponse,
    summary="Solicita el OTP de recuperacion por email",
)
def request_recovery(
    body: RecoveryRequest, request: Request, db: Session = Depends(get_db)
) -> dict:
    """Emite (o reutiliza en cooldown) el OTP `RECOVERY` (HU04: inicio sin clave)."""
    try:
        result = recovery_service.request_recovery(db, email=body.email, ip=_client_ip(request))
    except recovery_service.RecoveryRateLimitedError as exc:
        db.rollback()
        raise AppError(code="RATE_LIMITED", message=str(exc), status_code=429) from exc
    except Exception:
        # Fallo inesperado posterior al `flush`: revierte todo (sin OTP a medias).
        db.rollback()
        raise
    db.commit()
    return {"data": result, "meta": {"request_id": _request_id(request)}}


@router.post(
    "/auth/recovery/verify",
    response_model=RecoveryVerifyResponse,
    summary="Valida el OTP de recuperacion (sin sesion)",
)
def verify_recovery(
    body: RecoveryVerifyRequest, request: Request, db: Session = Depends(get_db)
) -> dict:
    """Valida el OTP y devuelve `{user_ref, device_bound}`; no abre sesion (HU04, E1-T33)."""
    try:
        result = recovery_service.verify_recovery(
            db,
            email=body.email,
            code=body.code,
            device_id=body.device_id,
            device_public_key=body.device_public_key,
            platform=body.platform,
            biometric_type=body.biometric_type,
            ip=_client_ip(request),
        )
    except recovery_service.RecoveryInvalidError as exc:
        # El intento fallido / la expiracion / el bloqueo del OTP ya
        # quedaron en `flush`: se confirman para que el contador avance y
        # el OTP vencido/bloqueado no quede reutilizable (en la rama ciega
        # el `commit` es no-op).
        db.commit()
        raise AppError(code="INVALID_RECOVERY_CODE", message=str(exc), status_code=401) from exc
    except recovery_service.RecoveryRateLimitedError as exc:
        # Ventana de verificacion por `email+IP` excedida (verificada antes
        # de la existencia, sin filtrar): se confirma (no-op).
        db.commit()
        raise AppError(code="RATE_LIMITED", message=str(exc), status_code=429) from exc
    except Exception:
        # Fallo inesperado posterior al `flush` (p. ej. al registrar
        # `access_recovery`): revierte todo (sin OTP `USED`/
        # `access_recovery`/binding a medias).
        db.rollback()
        raise
    db.commit()
    return {"data": result, "meta": {"request_id": _request_id(request)}}


__all__ = ["router"]
