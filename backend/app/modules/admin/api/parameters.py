"""Endpoints `GET/PUT /admin/parameters` (E2-T26, HU08/HU20).

Fachada de administracion sobre `shared/config` (dueno del esquema y ciclo
de vida de `config.parameters`): valida, traduce y delega a
`service/parameters` (sin logica en el router, `docs/05#1`); respuestas
`{"data", "meta"}` (`05#4`) y errores problem+json via `AppError`.

Auth (`05#2` + matriz de accesos):

- Sin cabecera / `Bearer` invalido / token invalido / `sub` no UUID ->
  `401 NOT_AUTHENTICATED` (mismo patron que `identity/api/profile.py`).
- Token valido sin rol `admin` (claim `roles` del JWT, `core.security`) ->
  `403 NOT_AUTHORIZED`.

Supuesto documentado: no existe gestion de roles/permisos (fuera de
alcance del brief: "la gestion de roles/permisos" esta en No incluye); el
rol se lee del claim `roles` del JWT (p. ej. `["admin"]`, emitido por
`create_access_token(..., roles=[...])`). Comparacion insensible a
mayusculas.

Errores de negocio:

- `404 NOT_FOUND`: `PUT` de clave inexistente (el espacio de claves lo
  gobiernan las migraciones; en caliente solo se editan valores).
- `422 VALIDATION_ERROR`: valor de tipo/formato invalido (p. ej. negativo);
  no escribe.
- Los cuerpos malformados (no entero) los rechaza FastAPI con `422` antes
  de tocar la BD.

El `PUT` es idempotente por naturaleza (misma clave + mismo valor = mismo
estado) y no mueve dinero: no exige `Idempotency-Key`. No se emite
`parameter.updated` (decision E2-T18: la trazabilidad la dan
`updated_by`/`updated_at`).

Sin PII ni secretos en logs: solo la clave (jamas valores).
"""

from __future__ import annotations

import logging
import uuid

import jwt
from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import decode_token
from app.modules.admin.schemas.parameters import (
    ParametersListResponse,
    ParameterUpdateRequest,
    ParameterUpdateResponse,
)
from app.modules.admin.service import parameters as parameters_service
from app.modules.shared.config import ParameterNotFoundError, ParameterValidationError

router = APIRouter(tags=["admin"])

logger = logging.getLogger(__name__)

#: Rol exigido en el claim `roles` del JWT (insensible a mayusculas).
ADMIN_ROLE = "admin"


class AdminContext:
    """Contexto autenticado de administracion (`sub` + `roles` del JWT)."""

    def __init__(self, user_id: uuid.UUID, roles: list[str]) -> None:
        self.user_id = user_id
        self.roles = roles


def get_admin_context(
    authorization: str | None = Header(default=None),
) -> AdminContext:
    """Exige JWT `Bearer` con rol admin (`05#2`).

    `401 NOT_AUTHENTICATED` si falta la cabecera, no es `Bearer`, el token
    es invalido/expirado o `sub` no es UUID. `403 NOT_AUTHORIZED` si el
    token es valido pero no trae el rol admin.
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
        logger.debug("rechazo JWT admin: %s", type(exc).__name__)
        raise AppError(
            code="NOT_AUTHENTICATED",
            message="Token invalido o expirado",
            status_code=401,
        ) from exc
    try:
        user_id = uuid.UUID(str(payload.get("sub")))
    except (ValueError, AttributeError, TypeError) as exc:
        raise AppError(
            code="NOT_AUTHENTICATED",
            message="Token sin sujeto valido",
            status_code=401,
        ) from exc
    raw_roles = payload.get("roles") or []
    roles = [str(role) for role in raw_roles if isinstance(role, str) and role.strip()]
    if ADMIN_ROLE not in {role.lower() for role in roles}:
        raise AppError(
            code="NOT_AUTHORIZED",
            message="Se requiere rol admin",
            status_code=403,
        )
    return AdminContext(user_id=user_id, roles=roles)


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


@router.get(
    "/admin/parameters",
    response_model=ParametersListResponse,
    summary="Listar parametros administrables",
)
def list_parameters(
    request: Request,
    module: str | None = Query(default=None, description="Filtra por modulo dueno"),
    prefix: str | None = Query(default=None, description="Filtra por prefijo de clave"),
    admin: AdminContext = Depends(get_admin_context),
    db: Session = Depends(get_db),
) -> dict:
    """Lista parametros con valor efectivo y metadatos (solo rol admin)."""
    items = parameters_service.list_parameters(db, module=module, prefix=prefix)
    return {
        "data": items,
        "meta": {"total": len(items), "request_id": _request_id(request)},
    }


@router.put(
    "/admin/parameters/{key}",
    response_model=ParameterUpdateResponse,
    summary="Editar el valor de un parametro",
)
def update_parameter(
    key: str,
    body: ParameterUpdateRequest,
    request: Request,
    admin: AdminContext = Depends(get_admin_context),
    db: Session = Depends(get_db),
) -> dict:
    """Edita un parametro existente (rol admin); visible de inmediato."""
    try:
        result = parameters_service.update_parameter(
            db, key=key, value=body.value_json, updated_by=admin.user_id
        )
    except ParameterNotFoundError as exc:
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except ParameterValidationError as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    return {"data": result, "meta": {"request_id": _request_id(request)}}


__all__ = ["ADMIN_ROLE", "AdminContext", "get_admin_context", "router"]
