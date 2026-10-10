"""Esquemas Pydantic del simulador de creditos (E3-T03, HU09).

Fuente: `docs/05-contratos-api.md#64` (`GET /credits/products`,
`POST /credits/simulate`, `POST /credits/simulate/export`), formato
`05#4` (`{"data", "meta"}`) y columnas `03b#81` (`loan_products`) /
`03b#84` (`loan_schedules`).

Reglas:
- Dinero entero en centimos (`*_minor: int`); nunca `float` (regla de oro 3).
- Tasas (`tea`/`tcea`/`insurance_rate`) viajan como **string decimal exacto**
  (`NUMERIC(18,8)` sin perdida en JSON); se rechaza `float` en la entrada.
- La simulacion es efimera: `due_date` llega `null` (sin fecha de
  desembolso, nada se persiste) y el cronograma refleja `03b#84` sin
  escribirlo (`installment_no`, capitales, `total_minor`; se agrega
  `balance_minor` como saldo posterior a la cuota para recalcular sin
  estado externo).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

__all__ = [
    "ProductOut",
    "ProductsListResponse",
    "ScheduleRowOut",
    "SimulateData",
    "SimulateRequest",
    "SimulateResponse",
]


def _reject_float(value: Any, field_name: str) -> Any:
    if isinstance(value, float):
        # ValueError (no TypeError): pydantic solo lo convierte en 422 asi.
        raise ValueError(  # noqa: TRY004
            f"{field_name} no acepta float (use string decimal exacto)"
        )
    return value


class SimulateRequest(BaseModel):
    """Cuerpo de `POST /credits/simulate` (y `/simulate/export`).

    `amount_minor` + `term_months` siempre requeridos; exactamente uno de
    `product_id` (tasa y topes del catalogo) o `tcea` explicita (simulacion
    libre). `insurance` aplica el desgravamen del producto (sin producto
    la tasa es `0`).
    """

    product_id: uuid.UUID | None = Field(
        default=None, description="Producto del catalogo (tasa y topes mandan)"
    )
    amount_minor: int = Field(..., description="Monto en centimos enteros")
    term_months: int = Field(..., description="Plazo en meses")
    tcea: Decimal | None = Field(
        default=None, description="TCEA explicita (solo simulacion libre, 0..5)"
    )
    insurance: bool = Field(default=False, description="Incluir desgravamen del producto")

    @field_validator("tcea", mode="before")
    @classmethod
    def _no_float_tcea(cls, value: Any) -> Any:
        return _reject_float(value, "tcea")

    @field_validator("amount_minor", "term_months", mode="before")
    @classmethod
    def _no_bool_or_float(cls, value: Any) -> Any:
        if isinstance(value, (bool, float)):
            # ValueError (no TypeError): pydantic solo lo convierte en 422 asi.
            raise ValueError("debe ser entero (sin bool ni float)")  # noqa: TRY004
        return value

    @model_validator(mode="after")
    def _exactly_one_rate_source(self) -> SimulateRequest:
        has_product = self.product_id is not None
        has_tcea = self.tcea is not None
        if has_product == has_tcea:
            raise ValueError("exige exactamente uno de product_id / tcea")
        return self


class ScheduleRowOut(BaseModel):
    """Cuota del cronograma provisional (forma de `03b#84`, sin persistir)."""

    installment_no: int
    due_date: date | None = None
    principal_minor: int
    interest_minor: int
    insurance_minor: int
    total_minor: int
    balance_minor: int


class ProductRef(BaseModel):
    """Producto usado en la simulacion (referencia minima, sin tasas)."""

    id: uuid.UUID
    code: str
    name: str


class SimulateData(BaseModel):
    """Resultado de la simulacion (el backend calcula todo: cliente delgado)."""

    product: ProductRef | None = None
    amount_minor: int
    term_months: int
    insurance: bool
    tea: str = Field(..., description="TEA aplicada (string decimal exacto)")
    tcea: str = Field(..., description="TCEA con desgravamen (string decimal exacto)")
    payment_minor: int = Field(..., description="Cuota base francesa (sin seguro)")
    total_principal_minor: int
    total_interest_minor: int
    total_insurance_minor: int
    total_paid_minor: int
    schedule: list[ScheduleRowOut]


class SimulateResponse(BaseModel):
    """Envoltorio `05#4` de la simulacion."""

    data: SimulateData
    meta: dict = Field(default_factory=dict)


class ProductOut(BaseModel):
    """Producto del catalogo `GET /credits/products` (columnas `03b#81`)."""

    id: uuid.UUID
    code: str
    name: str
    tcea: str = Field(..., description="Tasa anual (string decimal exacto)")
    tea: str | None = Field(default=None, description="TEA (string decimal exacto)")
    insurance_rate: str
    min_amount_minor: int
    max_amount_minor: int
    min_term_months: int
    max_term_months: int
    allowed_terms: list[int] | None = None
    enabled: bool


class ProductsListResponse(BaseModel):
    """Envoltorio `05#4` del catalogo (solo habilitados)."""

    data: list[ProductOut]
    meta: dict = Field(default_factory=dict)
