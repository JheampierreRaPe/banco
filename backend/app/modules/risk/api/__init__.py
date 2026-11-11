"""Endpoints de reglas antifraude (E6-T01, HU20).

- `GET /risk/rules` (Bearer, rol `fraud_analyst`/`admin`): lista las
  reglas ordenadas (`priority ASC, code ASC`).
- `PUT /risk/rules/{code}` (Bearer, rol `fraud_analyst`/`admin`):
  edicion parcial auditada (`risk.rule.updated`, sin PII) que se
  refleja sin reinicio.

Montados bajo `/api/v1` por `app.main` via `iter_routers` (sin
registro extra: este `router` ya lo recoge el ensamblado del
monolito modular). Sin logica en el router (05#1): valida, traduce
y delega al `service/`. Contrato en `docs/05#6.7`.
"""

from __future__ import annotations

import logging
import uuid

import jwt
from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import decode_token
from app.modules.risk import repository as risk_repo
from app.modules.risk import schemas as risk_schemas
from app.modules.risk import service as risk_service
from app.modules.risk.models import RiskRule

router = APIRouter(tags=["risk"])

logger = logging.getLogger(__name__)

#: Roles aceptados para gestionar reglas (claim `roles` del JWT).
RISK_ROLES = frozenset({"fraud_analyst", "admin"})


class RiskContext:
    """Contexto autenticado del analista (`sub` + `roles` del JWT)."""

    def __init__(self, user_id: uuid.UUID, roles: list[str]) -> None:
        self.user_id = user_id
        self.roles = roles


def get_risk_context(
    authorization: str | None = Header(default=None),
) -> RiskContext:
    """Exige JWT `Bearer` con rol de fraude/admin.

    `401 NOT_AUTHENTICATED` si falta la cabecera, no es `Bearer`, el
    token es invalido/expirado o `sub` no es UUID. `403 NOT_AUTHORIZED`
    si el token es valido pero no trae rol de fraude/admin.
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
        logger.debug("rechazo JWT risk: %s", type(exc).__name__)
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
    if not RISK_ROLES.intersection({role.lower() for role in roles}):
        raise AppError(
            code="NOT_AUTHORIZED",
            message="Se requiere rol de analista de fraude",
            status_code=403,
        )
    return RiskContext(user_id=user_id, roles=roles)


def _to_response(row: RiskRule) -> risk_schemas.RuleResponse:
    return risk_schemas.RuleResponse(
        code=row.code,
        name=row.name,
        enabled=bool(row.enabled),
        priority=int(row.priority),
        action=row.action,
        params_json=dict(row.params_json) if isinstance(row.params_json, dict) else {},
        updated_at=row.updated_at,
    )


def _request_id() -> str:
    return uuid.uuid4().hex


@router.get(
    "/risk/rules",
    response_model=risk_schemas.RulesListResponse,
    summary="Lista las reglas antifraude",
    responses={
        200: {"description": "Reglas ordenadas (`priority ASC, code ASC`)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        403: {"description": "`NOT_AUTHORIZED` (sin rol de fraude/admin)"},
    },
)
def list_risk_rules(
    analyst: RiskContext = Depends(get_risk_context),
    db: Session = Depends(get_db),
) -> dict:
    """Lista las reglas (`E6-T01`): orden determinista, sin PII."""
    _ = analyst  # El rol solo hace gate (401/403 sin analista).
    rows = risk_service.list_rules(db)
    return {
        "data": [_to_response(row) for row in rows],
        "meta": {"request_id": _request_id(), "total": len(rows)},
    }


@router.put(
    "/risk/rules/{code}",
    response_model=risk_schemas.RuleUpdateResponse,
    summary="Actualiza una regla antifraude",
    responses={
        200: {"description": "Regla actualizada (visible sin reiniciar)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        403: {"description": "`NOT_AUTHORIZED` (sin rol de fraude/admin)"},
        404: {"description": "`NOT_FOUND` (codigo inexistente)"},
        422: {"description": "`VALIDATION_ERROR` (accion/prioridad/JSON invalidos)"},
    },
)
def update_risk_rule(
    code: str,
    body: risk_schemas.RuleUpdateRequest,
    analyst: RiskContext = Depends(get_risk_context),
    db: Session = Depends(get_db),
) -> dict:
    """Edicion parcial auditada (`risk.rule.updated`, sin PII).

    `code` inmutable (solo ruta); cada campo del body es opcional
    (`None` = conservar). Se refleja en la siguiente evaluacion sin
    reinicio.
    """
    try:
        row = risk_service.update_rule(
            db,
            code,
            actor=analyst.user_id,
            enabled=body.enabled,
            priority=body.priority,
            action=body.action,
            params_json=body.params_json,
        )
        db.commit()
    except risk_repo.RiskRuleNotFoundError as exc:
        db.rollback()
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except risk_repo.RiskRuleError as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    return {"data": _to_response(row), "meta": {"request_id": _request_id()}}


__all__ = ["get_risk_context", "router"]
