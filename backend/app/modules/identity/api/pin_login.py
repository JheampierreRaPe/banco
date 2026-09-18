"""Endpoint del login con PIN (E1-T14, HU03 CA-02/CA-03).

`POST /auth/login/pin {user_ref, pin[, device_id/device_info/ip]}` ->
verifica el PIN (hash PBKDF2, tiempo constante, contador de intentos y
bloqueo temporal tras 5 fallos) y devuelve JWT corto + refresh opaco.

Montado bajo `/api/v1` por `app.main` via `iter_routers` (este `router` lo
recoge `api/__init__.py`; sin registro extra). Sin logica en el router
(05#1): valida, traduce y delega a `service/pin_login`. Sin auth todavia:
es un endpoint pre-sesion.

Respuestas 05#4 (`{"data", "meta"}` + `request_id`); errores problem+json
via `AppError`:

- 401 `INVALID_CREDENTIALS`: PIN erroneo, usuario inexistente, `user_ref`
  malformado o credencial sin PIN (respuesta identica: no filtra existencia
  ni estado; sin intentos restantes ni `locked_until` en el cuerpo).
- 423 `ACCOUNT_LOCKED`: bloqueo vigente por intentos fallidos (sin precision
  del tiempo restante).
- 422 estandar de FastAPI (`{"detail": [...]}`) para esquema malformado.

Transaccion: el servicio hace `flush`; el endpoint confirma (`commit`) en
exito Y ante error de negocio (`INVALID_CREDENTIALS`/`ACCOUNT_LOCKED`):
el intento fallido, el bloqueo y la notificacion DEBEN persistir para que
el contador avance entre peticiones (desviacion documentada de
`api/activation.py` y `api/device_login.py`, donde el `rollback` ante error
es seguro porque ningun estado debe sobrevivir al fallo). El PIN jamas sale
en logs. OpenAPI automatico por FastAPI (`response_model`).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.modules.identity.schemas.pin_login import (
    PinLoginRequest,
    PinLoginResponse,
)
from app.modules.identity.service import pin_login as pin_login_service

router = APIRouter(tags=["identity"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


@router.post(
    "/auth/login/pin",
    response_model=PinLoginResponse,
    summary="Verifica el PIN y abre sesion",
)
def login_pin(body: PinLoginRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    """Login alterno con PIN (HU03 CA-02) con bloqueo temporal (CA-03)."""
    try:
        result = pin_login_service.login_with_pin(
            db,
            user_ref=body.user_ref,
            pin=body.pin,
            device_id=body.device_id,
            device_info=body.device_info,
            ip=body.ip,
        )
    except pin_login_service.PinLockedError as exc:
        # El bloqueo/la notificacion ya quedaron en `flush`: se confirman
        # para que el contador no se pierda entre intentos.
        db.commit()
        raise AppError(code="ACCOUNT_LOCKED", message=str(exc), status_code=423) from exc
    except pin_login_service.PinInvalidError as exc:
        # El intento fallido ya quedo en `flush`: se confirma para que el
        # contador avance (sin esto, el bloqueo del 5to fallo jamas se
        # alcanzaria; en la rama ciega el `commit` es no-op).
        db.commit()
        raise AppError(code="INVALID_CREDENTIALS", message=str(exc), status_code=401) from exc
    db.commit()
    return {"data": result, "meta": {"request_id": _request_id(request)}}


__all__ = ["router"]
