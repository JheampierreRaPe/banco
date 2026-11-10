"""Paso 2 del login en dispositivo nuevo (E1-T46, HU03/HU04).

`POST /auth/login/device/complete {email, doc_type, document_number, code, pin,
device_id, device_public_key[, platform, biometric_type, device_info]}` -> valida el OTP
`LOGIN` (paso 1, E1-T45) + el PIN de la MISMA cuenta `ACTIVE`, registra/actualiza
el `device_binding` (best-effort en savepoint: un fallo no revierte la sesion) y
RECIEN entonces abre sesion (JWT corto + refresh opaco + `user_ref`).

Montado bajo `/api/v1` por `app.main` via `iter_routers` (este `router` lo
recoge `api/__init__.py`; sin registro extra). Sin logica en el router
(05#1): valida, traduce y delega a `service/device_login_complete`. Sin auth
todavia: es un endpoint pre-sesion (el OTP + PIN validos SON la autorizacion).

Respuestas 05#4 (`{"data", "meta"}` + `request_id`); errores problem+json
via `AppError`:

- 200 `{access_token, refresh_token, token_type, session_id, expires_in,
  user_ref, biometric_enabled}` (sesion abierta en el dispositivo).
- 401 `INVALID_LOGIN`: cuenta inexistente/no elegible, documento que no
  coincide, sin credencial, OTP invalido/vencido/bloqueado o PIN erroneo
  (mismo cuerpo para todos: sin enumeracion ni oraculo de campo).
- 423 `ACCOUNT_LOCKED`: bloqueo de PIN vigente (sin precision del tiempo).
- 429 `RATE_LIMITED`: ventana por `email+IP` excedida (verificada antes de la
  existencia, compartida con el paso 1; sin filtrar).
- 422 estandar de FastAPI (`{"detail": [...]}`) para esquema malformado y 422
  problem+json para PIN/documento debil a nivel servicio (antes de tocar
  estado, sin oraculo sobre la cuenta).

Transaccion: el servicio hace `flush`; el endpoint confirma (`commit`) en
exito Y ante error de negocio tipado (los contadores de lockout/OTP DEBEN
persistir, como `api/pin_login.py` y `api/pin_reset.py`; en la rama ciega el
`commit` es no-op), y revierte (`rollback`) ante formato debil (422, antes de
tocar estado), rate-limit o error inesperado. El documento/OTP/PIN/clave jamas
salen en respuestas ni logs. OpenAPI automatico por FastAPI (`response_model`).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.modules.identity.schemas.device_login_complete import (
    DeviceLoginCompleteRequest,
    DeviceLoginCompleteResponse,
)
from app.modules.identity.service import device_login_complete as device_login_complete_service
from app.modules.identity.service import recovery as recovery_service

router = APIRouter(tags=["identity"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post(
    "/auth/login/device/complete",
    response_model=DeviceLoginCompleteResponse,
    summary="Valida OTP + PIN, asocia el dispositivo y abre sesion",
)
def complete_device_login(
    body: DeviceLoginCompleteRequest, request: Request, db: Session = Depends(get_db)
) -> dict:
    """Cierra el login sin `userRef`: OTP `LOGIN` + PIN -> binding + sesion (E1-T46)."""
    try:
        result = device_login_complete_service.complete_device_login(
            db,
            email=body.email,
            doc_type=body.doc_type,
            document_number=body.document_number,
            code=body.code,
            pin=body.pin,
            device_id=body.device_id,
            device_public_key=body.device_public_key,
            platform=body.platform,
            biometric_type=body.biometric_type,
            device_info=body.device_info,
            ip=_client_ip(request),
        )
    except device_login_complete_service.DeviceLoginInvalidError as exc:
        # El intento fallido (lockout PIN y/o intentos OTP) ya quedo en `flush`:
        # se confirma para que los contadores avancen entre peticiones (en la
        # rama ciega el `commit` es no-op).
        db.commit()
        raise AppError(code="INVALID_LOGIN", message=str(exc), status_code=401) from exc
    except device_login_complete_service.DeviceLoginLockedError as exc:
        # El bloqueo/la notificacion ya quedaron en `flush`: se confirman.
        db.commit()
        raise AppError(code="ACCOUNT_LOCKED", message=str(exc), status_code=423) from exc
    except recovery_service.RecoveryRateLimitedError as exc:
        # Ventana por `email+IP` excedida (verificada antes de la existencia,
        # compartida con el paso 1): nada que persistir.
        db.rollback()
        raise AppError(code="RATE_LIMITED", message=str(exc), status_code=429) from exc
    except ValueError as exc:
        # PIN/documento debil a nivel servicio (el esquema ya filtra en HTTP
        # con 422 estandar): 422 generico, sin oraculo sobre la cuenta; nada
        # que persistir (se valido antes de tocar estado).
        db.rollback()
        raise AppError(code="INVALID_PIN_FORMAT", message=str(exc), status_code=422) from exc
    except Exception:
        # Fallo inesperado posterior al `flush`: revierte todo (sin sesion/
        # OTP `USED`/binding a medias).
        db.rollback()
        raise
    db.commit()
    return {"data": result, "meta": {"request_id": _request_id(request)}}


__all__ = ["router"]
