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

from pydantic import BaseModel, ConfigDict, Field


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
    """Edicion parcial de una regla (`code` inmutable, fuera del body).

    `extra="forbid"`: un campo desconocido (p. ej. el typo `enable`) o el
    `code` en el body devuelve `422` en vez de ignorarse en silencio.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=32767)
    action: Literal["ALERT", "HOLD", "BLOCK"] | None = None
    params_json: dict[str, Any] | None = None


class RuleUpdateResponse(BaseModel):
    """Regla actualizada + `meta` (`05#4`)."""

    data: RuleResponse
    meta: dict[str, Any]


class ProfileResponse(BaseModel):
    """Vista minima del perfil de comportamiento (`E6-T03`).

    Solo habitos y severidad (sin PII: `user_id` es UUID, sin
    nombre/documento/cuenta): `avg_amount_minor` (media incremental
    entera), `usual_countries`/`usual_hours` (top 5), ultimo
    dispositivo, `score` y `updated_at`.
    """

    user_id: str
    avg_amount_minor: int
    usual_countries: list[str] | None
    usual_hours: list[int] | None
    last_device_id: str | None
    score: int
    updated_at: datetime


class ProfileDetailResponse(BaseModel):
    """Perfil minimo + `meta` (`05#4`)."""

    data: ProfileResponse
    meta: dict[str, Any]


__all__ = [
    "ProfileDetailResponse",
    "ProfileResponse",
    "RuleResponse",
    "RuleUpdateRequest",
    "RuleUpdateResponse",
    "RulesListResponse",
]
