"""Esquemas Pydantic de transferencias (E2-T07, HU06 CA-01/CA-03).

Fuente: `docs/05-contratos-api.md#63` (`POST /transfers/own`,
`POST /transfers/third-party`) y formato `05#4` (`{"data", "meta"}`).

Reglas:
- Dinero entero en centimos (`amount_minor >= 1`, `fee_minor >= 0`);
  nunca `float` (regla de oro 3).
- Moneda ISO-4217 (`^[A-Z]{3}$`).
- Solo UUIDs de cuentas (la API nunca expone numeros en claro, regla 9).
- `Idempotency-Key` obligatoria (cabecera, regla de oro 5; la exige el
  router antes de ejecutar).
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

CURRENCY_PATTERN = r"^[A-Z]{3}$"


class TransferOwnRequest(BaseModel):
    """`POST /transfers/own`: entre cuentas del mismo titular (sin comision)."""

    source_account_id: uuid.UUID
    target_account_id: uuid.UUID
    amount_minor: int = Field(..., ge=1, description="Monto en centimos (> 0)")
    currency: str = Field(..., pattern=CURRENCY_PATTERN, description="ISO-4217")
    external_ref: str | None = Field(default=None, max_length=80)
    metadata: dict | None = None


class TransferThirdPartyRequest(BaseModel):
    """`POST /transfers/third-party`: a otra cuenta del banco (con comision)."""

    source_account_id: uuid.UUID
    target_account_id: uuid.UUID
    amount_minor: int = Field(..., ge=1, description="Monto en centimos (> 0)")
    currency: str = Field(..., pattern=CURRENCY_PATTERN, description="ISO-4217")
    fee_minor: int | None = Field(
        default=None,
        ge=0,
        description="Comision en centimos; None = `transfer.third_party_fee_minor`",
    )
    external_ref: str | None = Field(default=None, max_length=80)
    metadata: dict | None = None


class TransferData(BaseModel):
    """Detalle de la transferencia creada (o rechazada por fondos)."""

    transaction_id: uuid.UUID
    type: str
    status: str
    source_account_id: uuid.UUID | None = None
    target_account_id: uuid.UUID | None = None
    amount_minor: int
    currency: str
    fee_minor: int = 0
    failure_code: str | None = Field(
        default=None,
        description="`INSUFFICIENT_FUNDS` cuando `status` es `REJECTED`",
    )


class TransferResponse(BaseModel):
    """Envoltorio `05#4` (`data` + `meta` con `request_id` y `replayed`)."""

    data: TransferData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "TransferData",
    "TransferOwnRequest",
    "TransferResponse",
    "TransferThirdPartyRequest",
]
