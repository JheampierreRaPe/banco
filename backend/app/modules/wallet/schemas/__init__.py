"""Esquemas Pydantic de `wallet` (E4-T01, HU13).

Contrato fijado (`docs/05#6.5`, cierra `docs/18` G66 en lo que toca a
`GET /wallet` y `POST /wallet/links`):

- `POST /wallet/links {type: ACCOUNT|CARD, account_id?, card?}`:
  exactamente uno de `account_id`/`card`; `card` acepta el PAN crudo
  (`pan`, tokenizado server-side y descartado) o un `token_ref`
  pre-tokenizado (con `last4`/`brand` opcionales). El PAN nunca viaja
  en respuestas ni en logs.
- Respuestas segun `05#4` (`{"data", "meta"}`); el `token_ref` y el
  PAN nunca se exponen (solo `account_masked`/`last4`/`brand`).
- QR de comercio (`E4-T08`, HU14; detalle en `schemas/qr.py`,
  contrato en `docs/05#6.5`): `POST /merchants/qr
  {amount_minor, currency, concept?, mode?}` -> `201` con el cargo
  firmado (`payload`, `payload_hash`, `signature`, `expires_at`);
  `GET /merchants/qr/{id}` -> `200` con el estado efectivo
  (`EXPIRED` on-read si vencio). Re-exportados aqui.
- Pago QR del pagador (`E4-T12`, HU15; detalle en
  `schemas/payment.py`, contrato en `docs/05#6.5`):
  `POST /payments/qr {qr_content, link_id}` ->
  `201 {transaction_id, qr_payment_id, charge_id, merchant,
  amount_minor, currency, status, failure_code?, authorization?}`.
  Re-exportados aqui.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.modules.wallet.schemas.payment import (
    QrPayAuthMethod,
    QrPayAuthorization,
    QrPayData,
    QrPayMerchant,
    QrPayRequest,
    QrPayResponse,
    QrPayStatus,
)
from app.modules.wallet.schemas.qr import (
    QrCreate,
    QrCreateData,
    QrCreateResponse,
    QrMode,
    QrStatusData,
    QrStatusResponse,
)
from app.modules.wallet.schemas.services import (
    BillerData,
    CatalogResponse,
    ReceiptData,
    ServicePaymentCreate,
    ServicePaymentData,
    ServicePaymentResponse,
    SubscriptionCreate,
    SubscriptionData,
    SubscriptionResponse,
    TopupCreate,
    TopupData,
    TopupResponse,
)

LinkType = Literal["ACCOUNT", "CARD"]


class CardInput(BaseModel):
    """Tarjeta a vincular: PAN crudo (server-side) o token previo."""

    pan: str | None = Field(default=None, description="PAN crudo; se tokeniza y se descarta")
    token_ref: str | None = Field(default=None, max_length=120)
    last4: str | None = Field(default=None, min_length=4, max_length=4)
    brand: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> CardInput:
        if (self.pan is None) == (self.token_ref is None):
            raise ValueError("card exige exactamente uno de `pan` / `token_ref`")
        return self


class LinkCreate(BaseModel):
    """Alta de vinculo: exactamente uno de `account_id` / `card`."""

    type: LinkType
    account_id: uuid.UUID | None = None
    card: CardInput | None = None

    @model_validator(mode="after")
    def _exactly_one_fund_source(self) -> LinkCreate:
        if (self.account_id is None) == (self.card is None):
            raise ValueError("se exige exactamente uno de `account_id` / `card`")
        if self.type == "ACCOUNT" and (self.account_id is None or self.card is not None):
            raise ValueError("type=ACCOUNT exige `account_id` y prohibe `card`")
        if self.type == "CARD" and (self.card is None or self.account_id is not None):
            raise ValueError("type=CARD exige `card` y prohibe `account_id`")
        return self


class WalletLinkData(BaseModel):
    """Vinculo enmascarado (nunca PAN ni `token_ref`)."""

    id: uuid.UUID
    type: LinkType
    account_masked: str | None = None
    last4: str | None = None
    brand: str | None = None
    status: str
    linked_at: datetime


class WalletData(BaseModel):
    """Billetera del titular (`wallet_id` nulo = aun sin billetera)."""

    wallet_id: uuid.UUID | None = None
    status: str
    links: list[WalletLinkData] = Field(default_factory=list)


class WalletResponse(BaseModel):
    data: WalletData
    meta: dict = Field(default_factory=dict)


class WalletLinkResponse(BaseModel):
    data: WalletLinkData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "BillerData",
    "CardInput",
    "CatalogResponse",
    "LinkCreate",
    "LinkType",
    "QrCreate",
    "QrCreateData",
    "QrCreateResponse",
    "QrMode",
    "QrPayAuthMethod",
    "QrPayAuthorization",
    "QrPayData",
    "QrPayMerchant",
    "QrPayRequest",
    "QrPayResponse",
    "QrPayStatus",
    "QrStatusData",
    "QrStatusResponse",
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
    "WalletData",
    "WalletLinkData",
    "WalletLinkResponse",
    "WalletResponse",
]
