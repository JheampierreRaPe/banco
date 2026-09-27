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
        description="`INSUFFICIENT_FUNDS`/`DAILY_LIMIT_EXCEEDED`/"
        "`BIOMETRIC_PREVENTIVE_BLOCK` cuando `status` es `REJECTED`",
    )


class TransferResponse(BaseModel):
    """Envoltorio `05#4` (`data` + `meta` con `request_id` y `replayed`)."""

    data: TransferData
    meta: dict = Field(default_factory=dict)


class InterbankTransferRequest(BaseModel):
    """`POST /transfers/interbank`: salida a otro banco (E2-T09, HU06).

    El titular NO se confia del cliente: `cci` + `holder_name` se validan
    contra la fachada `validate_holder` (E2-T08) antes de mover dinero.
    """

    source_account_id: uuid.UUID
    amount_minor: int = Field(..., ge=1, description="Monto en centimos (> 0)")
    currency: str = Field(..., pattern=CURRENCY_PATTERN, description="ISO-4217")
    cci: str = Field(..., min_length=20, max_length=24, description="CCI destino (20 digitos)")
    holder_name: str | None = Field(
        default=None, max_length=150, description="Titular esperado (debe coincidir)"
    )
    bank_code: str | None = Field(default=None, max_length=6, description="Banco destino")
    fee_minor: int | None = Field(
        default=None,
        ge=0,
        description="Comision en centimos; None = `transfer.interbank_fee_minor`",
    )
    external_ref: str | None = Field(
        default=None, max_length=80, description="Referencia externa unica (se genera si falta)"
    )
    metadata: dict | None = None


class InterbankTransferData(BaseModel):
    """Detalle de la saga (`202` + seguimiento por `GET /transactions/{id}`)."""

    transaction_id: uuid.UUID
    type: str
    status: str
    source_account_id: uuid.UUID | None = None
    amount_minor: int
    currency: str
    fee_minor: int = 0
    external_ref: str | None = None
    tracking: dict = Field(default_factory=dict)
    failure_code: str | None = Field(
        default=None,
        description="`INSUFFICIENT_FUNDS`/`TRANSFER_TIMEOUT`/`INTERBANK_REJECTED`",
    )


class InterbankTransferResponse(BaseModel):
    """Envoltorio `05#4` (`data` + `meta` con `request_id` y `replayed`)."""

    data: InterbankTransferData
    meta: dict = Field(default_factory=dict)


class TransactionHistoryItem(BaseModel):
    """Un cambio de estado del historial (`transaction_status_history`)."""

    from_status: str | None = None
    to_status: str
    reason: str | None = None
    actor_type: str | None = None
    created_at: str | None = None


class TransactionDetailData(BaseModel):
    """Detalle y seguimiento de una transaccion (`GET /transactions/{id}`)."""

    transaction_id: uuid.UUID
    type: str
    status: str
    source_account_id: uuid.UUID | None = None
    target_account_id: uuid.UUID | None = None
    amount_minor: int
    currency: str
    fee_minor: int = 0
    external_ref: str | None = None
    tracking: dict = Field(default_factory=dict)
    failure_code: str | None = None
    history: list[TransactionHistoryItem] = Field(default_factory=list)


class TransactionDetailResponse(BaseModel):
    """Envoltorio `05#4` para el detalle (`data` + `meta`)."""

    data: TransactionDetailData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "InterbankTransferData",
    "InterbankTransferRequest",
    "InterbankTransferResponse",
    "TransactionDetailData",
    "TransactionDetailResponse",
    "TransactionHistoryItem",
    "TransferData",
    "TransferOwnRequest",
    "TransferResponse",
    "TransferThirdPartyRequest",
]
