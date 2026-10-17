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
  `POST /payments/qr {qr_content, link_id, amount_minor?}` ->
  `201 {transaction_id, qr_payment_id, charge_id, merchant?,
  payee?, amount_minor, currency, status, failure_code?,
  authorization?}` (`amount_minor` obligatorio solo si el QR no trae
  monto —personal, E4-T21—). Re-exportados aqui.
- QR personal estatico (`E4-T21`, HU14; detalle en
  `schemas/personal_qr.py`, contrato en `docs/05#6.5`):
  `POST /wallet/qr {currency?, concept?}` -> `201` con el QR
  reutilizable sin monto ni caducidad; `GET /wallet/qr/{id}` ->
  `200` con el estado (solo el titular dueno). Re-exportados aqui.
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
    QrPayPayee,
    QrPayRequest,
    QrPayResponse,
    QrPayStatus,
    QrPreviewPayee,
)
from app.modules.wallet.schemas.personal_qr import (
    PersonalQrCreate,
    PersonalQrCreateData,
    PersonalQrCreateResponse,
    PersonalQrStatusData,
    PersonalQrStatusResponse,
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
    """Billetera del titular (`wallet_id` nulo = aun sin billetera).

    `is_merchant` (E4-T23) es el rol de comercio derivado server-side
    (fila propia `ACTIVE` en `wallet.merchant_accounts`); `false` por
    defecto (incluido el estado sin billetera `NONE`). `merchant_status`
    expone el estado de la fila propia (`ACTIVE`/`BLOCKED`/`CLOSED`) o
    `None` si el titular no tiene cuenta de comercio. Solo rol, sin PII
    (ni RUC ni nombre comercial).
    """

    wallet_id: uuid.UUID | None = None
    status: str
    links: list[WalletLinkData] = Field(default_factory=list)
    is_merchant: bool = False
    merchant_status: str | None = None


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
    "PersonalQrCreate",
    "PersonalQrCreateData",
    "PersonalQrCreateResponse",
    "PersonalQrStatusData",
    "PersonalQrStatusResponse",
    "QrCreate",
    "QrCreateData",
    "QrCreateResponse",
    "QrMode",
    "QrPayAuthMethod",
    "QrPayAuthorization",
    "QrPayData",
    "QrPayMerchant",
    "QrPayPayee",
    "QrPayRequest",
    "QrPayResponse",
    "QrPayStatus",
    "QrPreviewPayee",
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
