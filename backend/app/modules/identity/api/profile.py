"""Perfil autenticado minimo para saludo/avatar (E1-T44, HU01/HU02).

`GET /me` (Bearer) -> devuelve **solo** el nombre del titular del token
(`first_name`, `last_name` y, si aplica, `business_name`/razon social) para
que el cliente componga el saludo y las iniciales del avatar (cliente
delgado: el servidor es la autoridad del nombre, el cliente solo presenta).
Es el `/me` canonico del catalogo (`docs/05#6.1`), no `/auth/me`.

Montado bajo `/api/v1` por `app.main` via `iter_routers` (este `router` lo
recoge `api/__init__.py`; sin registro extra). Sin logica en el router
(05#1): valida, traduce y delega a `service/profile`.

Auth Bearer (05#2): reutiliza exactamente el patron de
`identity/api/biometric.py:get_current_user_id` con el mismo 401
`NOT_AUTHENTICATED` (sin importar `accounts`: la logica vive en
`core.security`). Sin cabecera/`Bearer` invalido/token invalido/`sub` no
UUID -> 401.

Respuestas 05#4 (`{"data", "meta"}` + `request_id`); errores problem+json
via `AppError`:

- 401 `NOT_AUTHENTICATED`: sin cabecera, `Bearer` invalido o `sub` no UUID.
- 404 `NOT_FOUND`: usuario inexistente (neutro, sin filtrar datos).

Solo lectura: sin `flush`/`commit`; sin eventos; sin `Idempotency-Key`.
Sin PII/secretos en logs ni respuestas. OpenAPI automatico por FastAPI
(`response_model`).
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
from app.modules.identity.schemas.profile import ProfileResponse
from app.modules.identity.service import profile as profile_service

router = APIRouter(tags=["identity"])

logger = logging.getLogger(__name__)


def get_current_user_id(authorization: str | None = Header(default=None)) -> uuid.UUID:
    """Extrae el `user_id` (`sub`) del JWT `Bearer` (05#2).

    401 si falta la cabecera, no es `Bearer`, el token es invalido/expirado
    o `sub` no es UUID. Mismo patron que `api/biometric.py`
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
        logger.debug("rechazo JWT profile: %s", type(exc).__name__)
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


@router.get(
    "/me",
    response_model=ProfileResponse,
    summary="Nombre del titular autenticado para saludo/avatar",
)
def get_me(
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Devuelve solo nombre/razon social del `user_id` del JWT (E1-T44)."""
    try:
        result = profile_service.get_profile(db, user_id=user_id)
    except profile_service.ProfileUserNotFoundError as exc:
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    return {"data": result, "meta": {"request_id": _request_id(request)}}


__all__ = ["get_current_user_id", "router"]
