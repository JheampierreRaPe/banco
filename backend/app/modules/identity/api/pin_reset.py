"""Endpoint del reseteo de PIN con email+DNI/RUC+OTP (E1-T34, HU02/HU04, SCR-005).

`POST /auth/pin-reset {email, doc_number, [doc_type,] code, pin}` -> valida
`email + documento + OTP` (OTP `RECOVERY` emitido por `recovery.request`, que lo
consume: un solo uso) y fija/actualiza `credentials.pin_hash`, resetea
`failed_attempts`/`locked_until`, registra `access_recovery` con
`new_credential_set=true` y responde `{user_ref, pin_set: true}` SIN abrir
sesion (la unica sesion la abre `POST /auth/login/pin`).

E1-T40: `doc_type` (`DNI`/`RUC`, default `DNI` por compatibilidad F-T43)
gobierna SOLO la validacion de formato/longitud (DNI 8 / RUC 11 digitos);
la resolucion sigue por `email` + `doc_number_hash` HMAC (sin cruzar con
`users.doc_type`).

Montado bajo `/api/v1` por `app.main` via `iter_routers` (este `router` lo
recoge `api/__init__.py`; sin registro extra). Sin logica en el router
(05#1): valida, traduce y delega a `service/pin_reset`. Sin auth todavia:
es un endpoint pre-sesion (el OTP valido ES la autorizacion).

Respuestas 05#4 (`{"data", "meta"}` + `request_id`); errores problem+json
via `AppError`:

- 200 `{user_ref, pin_set: true}` (sin `access_token`/`refresh_token`/
  `session_id`: no abre sesion).
- 401 `INVALID_PIN_RESET`: email no registrado, usuario no elegible, DNI
  que no coincide, sin OTP pendiente, codigo incorrecto, OTP vencido y OTP
  bloqueado por intentos agotados (mismo cuerpo para todos: vencido y
  bloqueado colapsan al generico para no filtrar existencia ni campo).
- 429 `RATE_LIMITED`: ventana por `email+IP` excedida (verificada antes de
  la existencia, sin filtrar).
- 422 estandar de FastAPI (`{"detail": [...]}`) para esquema malformado
  (email/PIN con mal formato, `doc_type` desconocido o longitud de
  documento invalida) y 422 problem+json para PIN/documento debil a nivel
  servicio.

Transaccion: el servicio hace `flush`; el endpoint confirma (`commit`) en
exito Y ante error de negocio tipado (el contador de intentos del OTP y la
expiracion DEBEN persistir, como `api/recovery.py`; en la rama ciega el
`commit` es no-op), y revierte (`rollback`) ante PIN malformado (422, antes
de tocar estado) o error inesperado. El DNI/OTP/PIN jamas salen en
respuestas ni logs. OpenAPI automatico por FastAPI (`response_model`).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.modules.identity.schemas.pin_reset import (
    PinResetRequest,
    PinResetResponse,
)
from app.modules.identity.service import pin_reset as pin_reset_service
from app.modules.identity.service import recovery as recovery_service

router = APIRouter(tags=["identity"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post(
    "/auth/pin-reset",
    response_model=PinResetResponse,
    summary="Resetea el PIN con email, documento y OTP de recuperacion",
)
def reset_pin(body: PinResetRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    """Fija el PIN con `email + DNI/RUC + OTP` (sin abrir sesion; E1-T34/E1-T40)."""
    try:
        result = pin_reset_service.reset_pin(
            db,
            email=body.email,
            doc_number=body.doc_number,
            doc_type=body.doc_type,
            code=body.code,
            pin=body.pin,
            ip=_client_ip(request),
        )
    except pin_reset_service.PinResetInvalidError as exc:
        # El intento fallido / la expiracion / el bloqueo del OTP ya
        # quedaron en `flush`: se confirman para que el contador avance y
        # el OTP vencido/bloqueado no quede reutilizable (en la rama ciega
        # el `commit` es no-op).
        db.commit()
        raise AppError(code="INVALID_PIN_RESET", message=str(exc), status_code=401) from exc
    except recovery_service.RecoveryRateLimitedError as exc:
        # Ventana por `email+IP` excedida (verificada antes de la
        # existencia, sin filtrar): se confirma (no-op).
        db.commit()
        raise AppError(code="RATE_LIMITED", message=str(exc), status_code=429) from exc
    except ValueError as exc:
        # PIN/documento debil a nivel servicio (el esquema ya filtra en HTTP
        # con 422 estandar): 422 generico, sin oraculo sobre la cuenta; nada
        # que persistir (se valido antes de tocar estado).
        db.rollback()
        raise AppError(code="INVALID_PIN_FORMAT", message=str(exc), status_code=422) from exc
    except Exception:
        # Fallo inesperado posterior al `flush` (p. ej. al registrar
        # `access_recovery`): revierte todo (sin OTP `USED`/
        # `access_recovery`/PIN a medias).
        db.rollback()
        raise
    db.commit()
    return {"data": result, "meta": {"request_id": _request_id(request)}}


__all__ = ["router"]
