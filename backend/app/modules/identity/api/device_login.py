"""Endpoints del login con dispositivo (E1-T13, HU03 CA-01).

`POST /auth/login/challenge {user_ref[, device_id]}` -> `nonce` de un solo
uso con TTL corto (`NONCE_TTL_SECONDS = 120`).
`POST /auth/login/facial {nonce, device_id, signature}` -> verifica la firma
contra `device_bindings.public_key`, abre `sessions` y devuelve JWT corto +
refresh opaco.

Montados bajo `/api/v1` por `app.main` via `iter_routers` (este `router` lo
recoge `api/__init__.py`; sin registro extra). Sin logica en el router
(05#1): valida, traduce y delega a `service/device_login`. Sin auth todavia:
son endpoints pre-sesion.

Respuestas 05#4 (`{"data", "meta"}` + `request_id`); errores problem+json
via `AppError`:

- 400 `INVALID_LOGIN`: firma invalida, nonce reutilizado/inexistente,
  binding ausente/revocado, usuario inexistente o `user_ref` malformado
  (respuesta identica: no filtra existencia ni estado).
- 400 `EXPIRED_NONCE`: nonce con TTL agotado.
- 422 estandar de FastAPI (`{"detail": [...]}`) para esquema malformado.

Transaccion: el servicio hace `flush`; el endpoint confirma (`commit`) en
exito y revierte (`rollback`) ante error de negocio. La firma y el refresh
jamas salen en logs. OpenAPI automatico por FastAPI (`response_model`).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.modules.identity.schemas.device_login import (
    ChallengeRequest,
    ChallengeResponse,
    FacialRequest,
    FacialResponse,
)
from app.modules.identity.service import device_login as device_login_service

router = APIRouter(tags=["identity"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


@router.post(
    "/auth/login/challenge",
    response_model=ChallengeResponse,
    summary="Emite el nonce a firmar con el dispositivo",
)
def login_challenge(
    body: ChallengeRequest, request: Request, db: Session = Depends(get_db)
) -> dict:
    """Emite un desafio de un solo uso (HU03 CA-01)."""
    try:
        result = device_login_service.request_challenge(
            db, user_ref=body.user_ref, device_id=body.device_id
        )
    except device_login_service.LoginInvalidError as exc:
        db.rollback()
        raise AppError(code="INVALID_LOGIN", message=str(exc), status_code=400) from exc
    return {"data": result, "meta": {"request_id": _request_id(request)}}


@router.post(
    "/auth/login/facial",
    response_model=FacialResponse,
    summary="Verifica la firma del nonce y abre sesion",
)
def login_facial(body: FacialRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    """Valida el nonce firmado y devuelve JWT + refresh (HU03 CA-01)."""
    try:
        result = device_login_service.login_with_device(
            db,
            nonce=body.nonce,
            device_id=body.device_id,
            signature=body.signature,
            user_ref=body.user_ref,
            platform=body.platform,
            biometric_type=body.biometric_type,
            device_info=body.device_info,
            ip=body.ip,
        )
    except device_login_service.LoginExpiredError as exc:
        db.rollback()
        raise AppError(code="EXPIRED_NONCE", message=str(exc), status_code=400) from exc
    except device_login_service.LoginInvalidError as exc:
        db.rollback()
        raise AppError(code="INVALID_LOGIN", message=str(exc), status_code=400) from exc
    db.commit()
    return {"data": result, "meta": {"request_id": _request_id(request)}}


__all__ = ["router"]
