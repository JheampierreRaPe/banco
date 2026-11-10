"""Esquemas Pydantic de reglas antifraude (E6-T01, HU20).

Contrato `docs/05#6.7`: `GET /risk/rules` lista `{code, name,
enabled, priority, action, params_json, updated_at}`; `PUT
/risk/rules/{code}` edita `{enabled?, priority?, action?,
params_json?}` (parcial; `code` inmutable). `params_json` es siempre
un objeto JSON (solo umbrales, sin PII). Respuestas segun `05#4`
(`{"data", "meta"}`) y errores problem+json via `AppError`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class RuleResponse(BaseModel):
    """Vista de una regla (`GET` item / `PUT` respuesta)."""

    code: str
    name: str
    enabled: bool
    priority: int
    action: str
    params_json: dict[str, Any]
    updated_at: datetime


class RulesListResponse(BaseModel):
    """Listado ordenado (`priority ASC, code ASC`) + `meta` (`05#4`)."""

    data: list[RuleResponse]
    meta: dict[str, Any]


class RuleUpdateRequest(BaseModel):
    """Edicion parcial de una regla (`code` inmutable, fuera del body)."""

    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=32767)
    action: Literal["ALERT", "HOLD", "BLOCK"] | None = None
    params_json: dict[str, Any] | None = None


class RuleUpdateResponse(BaseModel):
    """Regla actualizada + `meta` (`05#4`)."""

    data: RuleResponse
    meta: dict[str, Any]


__all__ = [
    "RuleResponse",
    "RuleUpdateRequest",
    "RuleUpdateResponse",
    "RulesListResponse",
]
