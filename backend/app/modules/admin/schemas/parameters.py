"""Esquemas Pydantic de la fachada `admin/parameters` (E2-T26, HU08/HU20).

Contrato (`docs/05#6.8`, formato `05#4`):

- `GET /api/v1/admin/parameters` -> `{data: [ParameterItem], meta: {...}}`.
- `PUT /api/v1/admin/parameters/{key} {value_json}` ->
  `{data: {key, value_json, updated_at}, meta: {...}}`.

Reglas:

- Dinero entero en centimos; nunca `float` (regla de oro 3). El cuerpo del
  `PUT` transporta un numero JSON entero; `bool` se rechaza aqui mismo
  (Pydantic lax lo aceptaria como `1`/`0` y la fachada lo rechazaria
  despues: fallar pronto, sin escribir).
- Los minimos por clave (`>= 0`, `auth.* >= 1`) viven en la fachada duena
  (`shared/config`, E2-T18): aqui NO se duplican.
- Sin PII ni secretos: los valores son umbrales/limites operativos, nunca
  se loguean (ver `service/parameters.py`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ParameterUpdateRequest(BaseModel):
    """Cuerpo del `PUT /admin/parameters/{key}`: solo el valor nuevo."""

    value_json: int = Field(..., description="Nuevo valor entero del parametro")

    @field_validator("value_json", mode="before")
    @classmethod
    def _reject_bool(cls, raw: Any) -> Any:
        """`True`/`False` no son enteros validos (sin escribir, 422)."""
        if isinstance(raw, bool):
            # Pydantic exige ValueError (TypeError escaparia como 500, no 422).
            raise ValueError("value_json debe ser entero, no booleano")  # noqa: TRY004
        return raw


class ParameterItem(BaseModel):
    """Parametro expuesto por `GET /admin/parameters` (solo lectura)."""

    key: str
    value_json: Any = Field(..., description="Valor efectivo (entero si aplica)")
    module: str
    description: str | None = None
    updated_by: uuid.UUID | None = None
    updated_at: datetime | None = None


class ParametersListResponse(BaseModel):
    """Envoltorio de lista segun `05#4` (`data` + `meta`)."""

    data: list[ParameterItem]
    meta: dict = Field(default_factory=dict)


class ParameterUpdated(BaseModel):
    """Dato del `PUT /admin/parameters/{key}` (contrato del brief)."""

    key: str
    value_json: Any
    updated_at: datetime | None = None


class ParameterUpdateResponse(BaseModel):
    """Envoltorio de detalle segun `05#4` (`data` + `meta`)."""

    data: ParameterUpdated
    meta: dict = Field(default_factory=dict)


__all__ = [
    "ParameterItem",
    "ParameterUpdateRequest",
    "ParameterUpdateResponse",
    "ParameterUpdated",
    "ParametersListResponse",
]
