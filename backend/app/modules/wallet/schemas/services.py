"""Esquemas Pydantic del catalogo y el pago de servicios (E4-T16/E4-T17, HU16).

Contrato fijado (`docs/05#6.5`, cierra `docs/18` G77 en lo que toca a
`/services/catalog` y `/services/subscriptions`; `E4-T17` fija
`/services/payments` y `/topups`):

- `GET /services/catalog` (filtros `category?`/`q?`): catalogo de
  `billers` habilitados con sus `required_fields` (extensible sin
  cambios estructurales: filas nuevas, columnas fijas).
- `POST /services/subscriptions {biller_id, supply_ref,
  notify_days_before?}`: registra un servicio a recordar (lo consume
  el job de `E4-T18`); `notify_days_before` default `3` (HU16).
- `POST /services/payments {biller_id, supply_ref, amount_minor,
  currency?, link_id}` (`Idempotency-Key` obligatoria, solo el
  titular) -> `201` con el pago + comprobante (`receipt` con
  `receipt_no` oficial `OP-…`).
- `POST /topups {operator, phone, amount_minor, currency?, link_id}`
  (`Idempotency-Key` obligatoria, solo el titular) -> `201` con la
  recarga + comprobante.

Respuestas segun `05#4` (`{"data", "meta"}`); `supply_ref`/`phone`
solo salen hacia el titular dueno (lo necesario, enmascarados en el
comprobante) y nunca en logs.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, field_validator

from app.modules.wallet.schemas.payment import QrPayAuthorization, QrPayStatus


class BillerData(BaseModel):
    """Empresa del catalogo (vista publica, sin secretos)."""

    biller_id: uuid.UUID
    category: str
    name: str
    required_fields: list[str] = Field(default_factory=list)


class CatalogResponse(BaseModel):
    data: list[BillerData]
    meta: dict = Field(default_factory=dict)


class SubscriptionCreate(BaseModel):
    """Registro de un servicio a recordar (lo consume `E4-T18`)."""

    biller_id: uuid.UUID
    supply_ref: str = Field(min_length=1, max_length=80)
    notify_days_before: int = Field(default=3, ge=1, le=30)

    @field_validator("supply_ref")
    @classmethod
    def _strip_supply_ref(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("supply_ref no puede ser vacio")
        if any(ord(ch) < 32 for ch in text):
            raise ValueError("supply_ref con caracteres invalidos")
        return text


class SubscriptionData(BaseModel):
    """Suscripcion registrada (solo el titular dueno la ve)."""

    subscription_id: uuid.UUID
    biller_id: uuid.UUID
    supply_ref: str
    notify_days_before: int
    status: str


class SubscriptionResponse(BaseModel):
    data: SubscriptionData
    meta: dict = Field(default_factory=dict)


def _normalize_currency(value: str) -> str:
    code = str(value or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError("currency debe ser ISO-4217 (3 letras)")
    return code


class ServicePaymentCreate(BaseModel):
    """Pago de un servicio por el motor (HU16; `Idempotency-Key` obligatoria)."""

    biller_id: uuid.UUID
    supply_ref: str = Field(min_length=1, max_length=80)
    amount_minor: int = Field(gt=0, description="Monto en centimos (entero > 0, sin float)")
    currency: str = Field(default="PEN", min_length=3, max_length=3)
    link_id: uuid.UUID = Field(description="Vinculo ACCOUNT del titular a debitar")

    @field_validator("supply_ref")
    @classmethod
    def _strip_supply_ref(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("supply_ref no puede ser vacio")
        if any(ord(ch) < 32 for ch in text):
            raise ValueError("supply_ref con caracteres invalidos")
        return text

    @field_validator("amount_minor")
    @classmethod
    def _strict_amount(cls, value: int) -> int:
        if isinstance(value, bool):
            raise TypeError("amount_minor debe ser entero de centimos")
        return value

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, value: str) -> str:
        return _normalize_currency(value)


class ReceiptData(BaseModel):
    """Comprobante estructurado (CA-04; referencias enmascaradas, sin PII)."""

    receipt_no: str = Field(description="Numero de transaccion oficial (`OP-` + 12 hex)")
    transaction_id: uuid.UUID
    concept: str
    supply_masked: str | None = Field(default=None)
    phone_masked: str | None = Field(default=None)
    amount_minor: int
    currency: str
    paid_at: str
    status: str = Field(default="PAID")


class ServicePaymentData(BaseModel):
    """Resultado del pago de servicio (o rechazo sin mover dinero)."""

    payment_id: uuid.UUID
    transaction_id: uuid.UUID
    status: QrPayStatus
    amount_minor: int
    currency: str
    receipt: ReceiptData | None = Field(
        default=None, description="Comprobante (solo cuando `status` es `SETTLED`)"
    )
    failure_code: str | None = Field(
        default=None,
        description="`INSUFFICIENT_FUNDS`/`DAILY_LIMIT_EXCEEDED` cuando `REJECTED`",
    )
    authorization: QrPayAuthorization | None = Field(
        default=None, description="Reto del motor cuando `PENDING_AUTHORIZATION`"
    )


class ServicePaymentResponse(BaseModel):
    data: ServicePaymentData
    meta: dict = Field(default_factory=dict)


class TopupCreate(BaseModel):
    """Recarga de celular por el motor (HU16; `Idempotency-Key` obligatoria)."""

    operator: str = Field(min_length=1, max_length=40)
    phone: str = Field(min_length=1, max_length=20)
    amount_minor: int = Field(gt=0, description="Monto en centimos (entero > 0, sin float)")
    currency: str = Field(default="PEN", min_length=3, max_length=3)
    link_id: uuid.UUID = Field(description="Vinculo ACCOUNT del titular a debitar")

    @field_validator("operator")
    @classmethod
    def _strip_operator(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("operator no puede ser vacio")
        return text

    @field_validator("amount_minor")
    @classmethod
    def _strict_amount(cls, value: int) -> int:
        if isinstance(value, bool):
            raise TypeError("amount_minor debe ser entero de centimos")
        return value

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, value: str) -> str:
        return _normalize_currency(value)


class TopupData(BaseModel):
    """Resultado de la recarga (o rechazo sin mover dinero)."""

    topup_id: uuid.UUID
    transaction_id: uuid.UUID
    status: QrPayStatus
    amount_minor: int
    currency: str
    receipt: ReceiptData | None = Field(
        default=None, description="Comprobante (solo cuando `status` es `SETTLED`)"
    )
    failure_code: str | None = Field(
        default=None,
        description="`INSUFFICIENT_FUNDS`/`DAILY_LIMIT_EXCEEDED` cuando `REJECTED`",
    )
    authorization: QrPayAuthorization | None = Field(
        default=None, description="Reto del motor cuando `PENDING_AUTHORIZATION`"
    )


class TopupResponse(BaseModel):
    data: TopupData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "BillerData",
    "CatalogResponse",
    "ReceiptData",
    "ServicePaymentCreate",
    "ServicePaymentData",
    "ServicePaymentResponse",
    "SubscriptionCreate",
    "SubscriptionData",
    "SubscriptionResponse",
    "TopupCreate",
    "TopupData",
    "TopupResponse",
]
