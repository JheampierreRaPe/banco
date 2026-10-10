"""Endpoints del expediente de credito (E3-T08, HU10 CA-01..CA-04).

- `POST /credits/applications` (solicitud + consentimiento + dictamen;
  `Idempotency-Key` obligatoria; `201` con dictamen final o `202` si se
  difiere a revision).
- `GET /credits/applications/{id}` (estado del expediente, solo el
  titular; ajeno/inexistente -> `404` neutro).
- `GET /credits/applications?status=IN_REVIEW` (bandeja del analista) y
  `POST /credits/applications/{id}/review` (dictamen manual; rol
  analista de credito; rutas no listadas en `docs/05#64`, gap
  registrado en `docs/18`).

Sin logica en el router (`05#1`): valida, traduce y delega al
`service/` (`applications`), que orquesta catalogo (E3-T02), bureau
(E3-T06) y reglas (E3-T07). Auth/RBAC: reutiliza
`app.core.security.decode_token` (JWT `Bearer`, `sub` = user_id);
la revision exige ademas el rol `analyst`/`credit_analyst` en el claim
`roles` (insensible a mayusculas). En tests se inyecta contexto falso
con `app.dependency_overrides[get_current_user_id]`,
`[get_analyst_context]` y `[get_bureau_provider]`.

Respuestas segun `05#4` (`{"data", "meta"}`) y errores problem+json
via `AppError` (`NOT_AUTHENTICATED` 401, `NOT_AUTHORIZED` 403,
`NOT_FOUND` 404, `CONFLICT`/`IDEMPOTENCY_CONFLICT` 409,
`VALIDATION_ERROR` 422). OpenAPI automatico por FastAPI.
"""

from __future__ import annotations

import logging
import uuid

import jwt
from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session

from app.adapters.credit_bureau import (
    CreditBureauProvider,
    create_credit_bureau_provider,
)
from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import decode_token
from app.modules.credits.api import get_current_user_id
from app.modules.credits.schemas.applications import (
    ApplicationCreateRequest,
    ApplicationResponse,
    ApplicationsListResponse,
    ReviewRequest,
)
from app.modules.credits.service import applications as apps_service

router = APIRouter(tags=["credits"])

logger = logging.getLogger(__name__)

#: Roles aceptados para la revision del analista (claim `roles` del JWT).
ANALYST_ROLES = frozenset({"analyst", "credit_analyst"})


class AnalystContext:
    """Contexto autenticado del analista (`sub` + `roles` del JWT)."""

    def __init__(self, user_id: uuid.UUID, roles: list[str]) -> None:
        self.user_id = user_id
        self.roles = roles


def get_bureau_provider() -> CreditBureauProvider:
    """Proveedor de bureau por entorno (mock por defecto, E3-T06)."""
    return create_credit_bureau_provider()


def get_analyst_context(
    authorization: str | None = Header(default=None),
) -> AnalystContext:
    """Exige JWT `Bearer` con rol de analista de credito.

    `401 NOT_AUTHENTICATED` si falta la cabecera, no es `Bearer`, el
    token es invalido/expirado o `sub` no es UUID. `403 NOT_AUTHORIZED`
    si el token es valido pero no trae rol analista.
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
        logger.debug("rechazo JWT analista credits: %s", type(exc).__name__)
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
    if not ANALYST_ROLES.intersection({role.lower() for role in roles}):
        raise AppError(
            code="NOT_AUTHORIZED",
            message="Se requiere rol de analista de credito",
            status_code=403,
        )
    return AnalystContext(user_id=user_id, roles=roles)


def _to_app_error(exc: Exception) -> AppError:
    """Traduce errores del caso de uso a `AppError` (con rollback fuera)."""
    if isinstance(exc, apps_service.ApplicationNotFoundError):
        return AppError(code="NOT_FOUND", message=str(exc), status_code=404)
    if isinstance(exc, apps_service.IdempotencyKeyConflictError):
        return AppError(code="IDEMPOTENCY_CONFLICT", message=str(exc), status_code=409)
    if isinstance(exc, apps_service.ApplicationConflictError):
        return AppError(code="CONFLICT", message=str(exc), status_code=409)
    if isinstance(exc, DataError):
        # Red de seguridad: un numerico fuera de rango nunca es 500
        # (mensaje generico, sin eco del dato: sin PII).
        return AppError(
            code="VALIDATION_ERROR",
            message="dato numerico fuera de rango",
            status_code=422,
        )
    return AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422)


@router.post(
    "/credits/applications",
    response_model=ApplicationResponse,
    status_code=201,
    summary="Solicita un credito con autorizacion de bureau",
    responses={
        201: {"description": "Expediente con dictamen final (`APPROVED`/`REJECTED`)"},
        202: {"description": "Expediente diferido a revision (`IN_REVIEW`)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (producto inexistente)"},
        409: {"description": "`CONFLICT` / `IDEMPOTENCY_CONFLICT`"},
        422: {"description": "`VALIDATION_ERROR` (sin consentimiento, campos o `Idempotency-Key`)"},
    },
)
def create_credit_application(
    body: ApplicationCreateRequest,
    response: Response,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    bureau: CreditBureauProvider = Depends(get_bureau_provider),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Solicitud + evaluacion + dictamen persistido (HU10 CA-01..CA-03).

    `Idempotency-Key` obligatoria (crea recurso): misma clave + mismo
    cuerpo no duplica expediente ni notificacion (`meta.replayed`);
    misma clave + distinto cuerpo -> `409`. Sin consentimiento
    (`bureau_authorized=true`) -> `422` sin evaluar. Bureau caido ->
    `202` `IN_REVIEW` (no error 500).
    """
    payload = body.model_dump(mode="json")
    try:
        view, replayed, status = apps_service.submit_application_idempotent(
            db,
            user_id,
            body=payload,
            idempotency_key=idempotency_key,
            bureau_provider=bureau,
        )
    except (
        apps_service.ApplicationNotFoundError,
        apps_service.ApplicationConflictError,
        apps_service.ApplicationValidationError,
        DataError,
        ValueError,
        TypeError,
    ) as exc:
        db.rollback()
        raise _to_app_error(exc) from exc
    db.commit()
    response.status_code = status
    return {"data": view, "meta": {"replayed": replayed}}


@router.get(
    "/credits/applications",
    response_model=ApplicationsListResponse,
    summary="Bandeja de expedientes en revision (analista)",
    responses={
        200: {"description": "Expedientes `IN_REVIEW` (`data` + `meta.total`)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        403: {"description": "`NOT_AUTHORIZED` (sin rol analista)"},
        422: {"description": "`VALIDATION_ERROR` (solo se lista `IN_REVIEW`)"},
    },
)
def list_review_queue(
    status: str = Query(default="IN_REVIEW", description="Solo IN_REVIEW"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    analyst: AnalystContext = Depends(get_analyst_context),
    db: Session = Depends(get_db),
) -> dict:
    """Bandeja del analista (rol analista; HU10 aprobacion separada)."""
    _ = analyst  # El rol solo gates el acceso (401/403 sin analista).
    if status != "IN_REVIEW":
        raise AppError(
            code="VALIDATION_ERROR",
            message="la bandeja solo lista status=IN_REVIEW",
            status_code=422,
        )
    try:
        items = apps_service.list_review_queue(db, limit=limit, offset=offset)
    except (apps_service.ApplicationValidationError, ValueError, TypeError) as exc:
        db.rollback()
        raise _to_app_error(exc) from exc
    return {"data": items, "meta": {"total": len(items), "limit": limit, "offset": offset}}


@router.get(
    "/credits/applications/{application_id}",
    response_model=ApplicationResponse,
    summary="Estado del expediente (titular)",
    responses={
        200: {"description": "Expediente con dictamen y justificacion"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (inexistente o ajeno, neutro)"},
    },
)
def get_credit_application(
    application_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Estado visible del expediente (HU10 CA-04, solo el titular)."""
    try:
        view = apps_service.get_own_application(db, user_id, application_id)
    except (
        apps_service.ApplicationNotFoundError,
        apps_service.ApplicationValidationError,
        ValueError,
        TypeError,
    ) as exc:
        if not isinstance(exc, apps_service.ApplicationNotFoundError):
            db.rollback()
            raise _to_app_error(exc) from exc
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    return {"data": view, "meta": {}}


@router.post(
    "/credits/applications/{application_id}/review",
    response_model=ApplicationResponse,
    summary="Dictamen manual del analista",
    responses={
        200: {"description": "Expediente resuelto (`APPROVED`/`REJECTED`)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        403: {"description": "`NOT_AUTHORIZED` (sin rol analista)"},
        404: {"description": "`NOT_FOUND` (expediente inexistente)"},
        409: {"description": "`CONFLICT` (no esta `IN_REVIEW`, ya decidido o auto-revision)"},
        422: {"description": "`VALIDATION_ERROR` (dictamen/justificacion invalidos)"},
    },
)
def review_credit_application(
    application_id: uuid.UUID,
    body: ReviewRequest,
    analyst: AnalystContext = Depends(get_analyst_context),
    db: Session = Depends(get_db),
) -> dict:
    """Revision del analista: `IN_REVIEW -> APPROVED|REJECTED` auditado.

    Separacion de funciones: el solicitante no puede dictaminar su
    propio expediente aunque traiga rol analista (`409 CONFLICT`).
    """
    try:
        view = apps_service.review_application(
            db,
            analyst_id=analyst.user_id,
            application_id=application_id,
            decision=body.decision,
            reason=body.reason,
        )
    except (
        apps_service.ApplicationNotFoundError,
        apps_service.ApplicationConflictError,
        apps_service.ApplicationValidationError,
        ValueError,
        TypeError,
    ) as exc:
        db.rollback()
        raise _to_app_error(exc) from exc
    db.commit()
    return {"data": view, "meta": {}}


__all__ = [
    "ANALYST_ROLES",
    "AnalystContext",
    "get_analyst_context",
    "get_bureau_provider",
    "router",
]
