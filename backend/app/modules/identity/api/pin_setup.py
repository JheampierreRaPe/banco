"""Endpoint del fijado inicial del PIN (addendum E1-T14, HU02/HU03).

`POST /auth/pin/setup {user_ref, code, pin}` -> consume el OTP `ACTIVATION`
y fija `pin_hash` una sola vez (cierra el hueco de `onboard_customer`, que
crea la credencial sin PIN). Sin el, `POST /auth/login/pin` jamas funciona
para usuarios creados por la app.

Montado bajo `/api/v1` por `app.main` via `iter_routers` (este `router` lo
recoge `api/__init__.py`; sin registro extra). Sin logica en el router
(05#1): valida, traduce y delega a `service/pin_setup`. Sin auth todavia:
es un endpoint pre-sesion (el OTP valido ES la autorizacion).

Respuestas 05#4 (`{"data", "meta"}` + `request_id`); errores problem+json
via `AppError`:

- 401 `INVALID_SETUP_CODE`: codigo invalido/expirado/usado, usuario
  inexistente, `user_ref` malformado o credencial ausente (respuesta
  identica: no filtra existencia ni estado).
- 409 `PIN_ALREADY_SET`: la credencial ya tiene PIN (solo con OTP valido).
- 422 estandar de FastAPI (`{"detail": [...]}`) para esquema malformado
  (PIN debil o codigo no numerico); 422 problem+json para PIN debil a nivel
  servicio (uso directo).

Transaccion: el servicio hace `flush`; el endpoint confirma (`commit`) en
exito y ante 409 (el OTP validado queda consumido: la validacion exitosa es
un hecho consumado); revierte (`rollback`) ante 401 como E1-T10 (nada debe
sobrevivir a una validacion fallida). El PIN jamas sale en logs ni
respuestas. OpenAPI automatico por FastAPI (`response_model`).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.modules.identity.schemas.pin_setup import (
    PinSetupRequest,
    PinSetupResponse,
)
from app.modules.identity.service import pin_setup as pin_setup_service

router = APIRouter(tags=["identity"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


@router.post(
    "/auth/pin/setup",
    response_model=PinSetupResponse,
    summary="Fija el PIN inicial con OTP de activacion",
)
def setup_pin(body: PinSetupRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    """Fija el PIN una sola vez (habilita el login con PIN, E1-T14)."""
    try:
        result = pin_setup_service.setup_pin(
            db,
            user_ref=body.user_ref,
            code=body.code,
            pin=body.pin,
        )
    except pin_setup_service.PinAlreadySetError as exc:
        # El OTP validado ya quedo en `flush` como USED: se confirma para
        # que el consumo persista (la validacion exitosa no se revierte).
        db.commit()
        raise AppError(code="PIN_ALREADY_SET", message=str(exc), status_code=409) from exc
    except pin_setup_service.PinSetupInvalidError as exc:
        db.rollback()
        raise AppError(code="INVALID_SETUP_CODE", message=str(exc), status_code=401) from exc
    except ValueError as exc:
        # PIN debil a nivel servicio (el esquema ya filtra en HTTP con 422
        # estandar): 422 generico, sin oraculo sobre la cuenta.
        db.rollback()
        raise AppError(code="INVALID_PIN_FORMAT", message=str(exc), status_code=422) from exc
    db.commit()
    return {"data": result, "meta": {"request_id": _request_id(request)}}


__all__ = ["router"]
