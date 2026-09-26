"""Endpoint del consentimiento biometrico post-login (E1-T39, HU02/HU03).

`POST /auth/biometric/consent {enabled}` (Bearer) -> fija
`identity.credentials.biometric_enabled` del `user_id` del JWT (`sub`) y
devuelve el flag vigente. Reutiliza la columna existente: sin migracion.

Montado bajo `/api/v1` por `app.main` via `iter_routers` (este `router` lo
recoge `api/__init__.py`; sin registro extra). Sin logica en el router
(05#1): valida, traduce y delega a `service/biometric_consent`.

Auth Bearer (05#2): reutiliza `app.core.security.decode_token` con el mismo
patron y el mismo 401 `NOT_AUTHENTICATED` que `accounts/api/__init__.py`
(sin importar `accounts`: la logica vive en `core`). Sin cabecera/`Bearer`
invalido/token invalido/`sub` no UUID -> 401.

Respuestas 05#4 (`{"data", "meta"}` + `request_id`); errores problem+json
via `AppError`:

- 401 `NOT_AUTHENTICATED`: sin cabecera, `Bearer` invalido o `sub` no UUID.
- 404 `NOT_FOUND`: usuario/credencial inexistente (neutro, sin filtrar datos).
- 422 estandar de FastAPI (`{"detail": [...]}`) para esquema malformado.

Transaccion: el servicio hace `flush`; el endpoint confirma (`commit`) en
exito y revierte (`rollback`) ante error. Idempotente: el mismo valor no es
error. Sin PII/secretos en logs ni respuestas. OpenAPI automatico por
FastAPI (`response_model`).
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
from app.modules.identity.schemas.biometric_consent import (
    BiometricConsentRequest,
    BiometricConsentResponse,
)
from app.modules.identity.service import biometric_consent as biometric_consent_service

router = APIRouter(tags=["identity"])

logger = logging.getLogger(__name__)


def get_current_user_id(authorization: str | None = Header(default=None)) -> uuid.UUID:
    """Extrae el `user_id` (`sub`) del JWT `Bearer` (05#2).

    401 si falta la cabecera, no es `Bearer`, el token es invalido/expirado
    o `sub` no es UUID. Mismo patron que `accounts/api/__init__.py`
    (delega en `core.security`, sin importar `accounts`).
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
        logger.debug("rechazo JWT biometric: %s", type(exc).__name__)
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


@router.post(
    "/auth/biometric/consent",
    response_model=BiometricConsentResponse,
    summary="Fija el consentimiento biometrico del usuario autenticado",
)
def set_consent(
    body: BiometricConsentRequest,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Activa o revoca el acceso biometrico (E1-T39, HU02/HU03)."""
    try:
        result = biometric_consent_service.set_biometric_consent(
            db, user_id=user_id, enabled=body.enabled
        )
    except biometric_consent_service.BiometricConsentUserNotFoundError as exc:
        db.rollback()
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except Exception:
        db.rollback()
        raise
    db.commit()
    return {"data": result, "meta": {"request_id": _request_id(request)}}


__all__ = ["get_current_user_id", "router"]
