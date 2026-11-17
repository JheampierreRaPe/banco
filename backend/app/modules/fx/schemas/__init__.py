"""Esquemas de bolsillos de ahorro (E7-T02, HU24) y cotizaciones (E7-T09, HU25).

Dinero entero en centimos (`int`, nunca `float`, regla de oro 3);
tasas/spread/`yield_rate` como `Decimal` en el request (se
serializan a string). Sin PII: solo UUIDs, monedas y montos.
"""

from __future__ import annotations

import uuid
from datetime import datetime
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


class QuoteCreate(BaseModel):
    """Body de `POST /fx/quotes` (validacion estructural; el servicio valida negocio)."""

    base: str = Field(..., description="Moneda origen: PEN, USD o EUR")
    quote: str = Field(..., description="Moneda destino: PEN, USD o EUR (distinta de base)")
    source_amount_minor: int = Field(..., description="Monto origen en centimos (> 0)")


class QuoteData(BaseModel):
    """Vista publica de una cotizacion congelada (E7-T09, HU25)."""

    quote_id: uuid.UUID
    base: str
    quote: str
    rate: Decimal
    spread: Decimal
    source_amount_minor: int
    target_amount_minor: int
    expires_at: datetime
    status: str


class QuoteResponse(BaseModel):
    data: QuoteData
    meta: dict[str, Any] = Field(default_factory=dict)


class RateData(BaseModel):
    """Vista publica de la tasa vigente (E7-T09, HU25)."""

    base: str
    quote: str
    rate: Decimal
    source: str
    fetched_at: datetime
    ttl_seconds: int


class RateResponse(BaseModel):
    data: RateData
    meta: dict[str, Any] = Field(default_factory=dict)


class SpreadPreviewData(BaseModel):
    """Vista previa del spread por volumen (E7-T10, HU25, solo lectura)."""

    base: str
    quote: str
    amount_minor: int
    spread_bps: int
    rate: Decimal
    effective_rate: Decimal


class SpreadPreviewResponse(BaseModel):
    data: SpreadPreviewData
    meta: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "PocketCreate",
    "PocketData",
    "PocketResponse",
    "PocketsListResponse",
    "QuoteCreate",
    "QuoteData",
    "QuoteResponse",
    "RateData",
    "RateResponse",
    "SpreadPreviewData",
    "SpreadPreviewResponse",
]
