"""Esquemas de bolsillos de ahorro (E7-T02, HU24).

Dinero entero en centimos (`int`, nunca `float`, regla de oro 3);
`yield_rate` como `Decimal` en el request (se serializa a string).
Sin PII: solo UUIDs, nombre del bolsillo, moneda y montos.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class PocketCreate(BaseModel):
    """Body de `POST /pockets` (validacion estructural; el servicio valida negocio)."""

    name: str = Field(..., description="Nombre del bolsillo (1..80 caracteres)")
    currency: str = Field(..., description="Moneda MVP: PEN, USD o EUR")
    target_minor: int | None = Field(default=None, description="Meta en centimos (opcional, >= 0)")
    roundup_enabled: bool = Field(default=False, description="Redondeo automatico (E7-T03)")
    yield_rate: Decimal | None = Field(
        default=None, description="Tasa anual pactada (la aplica E7-T04)"
    )


class PocketData(BaseModel):
    """Vista publica de un bolsillo (sin numero de cuenta: solo UUIDs)."""

    pocket_id: uuid.UUID
    account_id: uuid.UUID | None
    name: str
    currency: str
    target_minor: int | None
    balance_minor: int
    roundup_enabled: bool
    yield_rate: Decimal
    status: str


class PocketResponse(BaseModel):
    data: PocketData
    meta: dict[str, Any] = Field(default_factory=dict)


class PocketsListResponse(BaseModel):
    data: list[PocketData]
    meta: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "PocketCreate",
    "PocketData",
    "PocketResponse",
    "PocketsListResponse",
]
