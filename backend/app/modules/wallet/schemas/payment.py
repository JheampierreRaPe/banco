"""Esquemas Pydantic del pago QR del pagador (E4-T12, HU15).

Contrato fijado (`docs/05#6.5`):

- `POST /payments/qr {qr_content, link_id}` (Bearer + `Idempotency-Key`
  obligatoria, solo el titular pagador) -> `201 {"data":
  {transaction_id, qr_payment_id, charge_id, merchant {merchant_id,
  business_name}, amount_minor, currency, status
  (SETTLED/PENDING_AUTHORIZATION/REJECTED), failure_code?,
  authorization?}, "meta": {request_id, replayed}}`.
- `status=SETTLED`: pago en un toque (dentro del limite base), sin reto.
- `status=PENDING_AUTHORIZATION`: sobre el limite base; `authorization`
  trae el reto del motor (`method=BIOMETRIC`, `nonce`, `expires_at`)
  para confirmar con `POST /transactions/{id}/authorize` (firma o PIN,
  patron `E2-T19`/`E2-T31`).
- `status=REJECTED`: `failure_code` (`INSUFFICIENT_FUNDS`/
  `DAILY_LIMIT_EXCEEDED`, patron `E2-T07`), sin tocar saldos.

El secreto HMAC y el RUC jamas viajan en respuestas (solo la
referencia del cargo y el nombre publico del comercio). Dinero entero
en centimos.

Preview (MEDIO-3, HU15 CA-01): `POST /payments/qr/preview
{qr_content}` (Bearer, sin `Idempotency-Key`: no mueve dinero) ->
`200 {"data": {charge_id, merchant {merchant_id, business_name},
amount_minor, currency, expires_at, status}, "meta": {request_id}}`.
Elegido `POST` sobre `GET`: el `qr_content` (hasta 8192 chars) no cabe
bien en una URL y no debe quedar en access logs; el cuerpo JSON lo
transporta sin exponerlo en la respuesta (la respuesta nunca incluye
el `qr_content` crudo, la firma ni secretos).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

QrPayStatus = Literal["SETTLED", "PENDING_AUTHORIZATION", "REJECTED"]
QrPayAuthMethod = Literal["BIOMETRIC", "PIN"]


class QrPayRequest(BaseModel):
    """Cuerpo del pago QR: contenido escaneado + fuente de fondos."""

    qr_content: str = Field(
        min_length=1,
        max_length=8192,
        description="Sobre canonico BO-QR1 escaneado (lo decodifica y "
        "verifica el servicio de E4-T11)",
    )
    link_id: uuid.UUID = Field(description="Vinculo ACCOUNT del titular a debitar")


class QrPayMerchant(BaseModel):
    """Comercio cobrado (nombre publico para confirmar antes de pagar)."""

    merchant_id: uuid.UUID
    business_name: str


class QrPayAuthorization(BaseModel):
    """Reto del motor cuando el pago espera autorizacion (E2-T19)."""

    method: QrPayAuthMethod = Field(
        default="BIOMETRIC",
        description="Factor sugerido (el authorize admite firma o PIN)",
    )
    nonce: str | None = Field(default=None, description="`nonce` a firmar con el dispositivo")
    expires_at: str | None = Field(default=None, description="Vencimiento ISO-8601 del reto")


class QrPayData(BaseModel):
    """Resultado del pago QR (o rechazo sin mover dinero)."""

    transaction_id: uuid.UUID
    qr_payment_id: uuid.UUID
    charge_id: uuid.UUID
    merchant: QrPayMerchant
    amount_minor: int
    currency: str
    status: QrPayStatus
    failure_code: str | None = Field(
        default=None,
        description="`INSUFFICIENT_FUNDS`/`DAILY_LIMIT_EXCEEDED` cuando "
        "`status` es `REJECTED` (patron E2-T07)",
    )
    authorization: QrPayAuthorization | None = Field(
        default=None,
        description="Reto del motor cuando `status` es `PENDING_AUTHORIZATION`",
    )


class QrPayResponse(BaseModel):
    data: QrPayData
    meta: dict = Field(default_factory=dict)


class QrPreviewRequest(BaseModel):
    """Cuerpo del preview: solo el contenido escaneado (no mueve dinero)."""

    qr_content: str = Field(
        min_length=1,
        max_length=8192,
        description="Sobre canonico BO-QR1 escaneado (se decodifica y "
        "verifica en solo lectura, sin persistir nada)",
    )


class QrPreviewMerchant(BaseModel):
    """Comercio del QR (nombre publico para confirmar antes de pagar)."""

    merchant_id: uuid.UUID
    business_name: str


class QrPreviewData(BaseModel):
    """Vista previa del cobro (comercio + monto, sin mover dinero)."""

    charge_id: uuid.UUID
    merchant: QrPreviewMerchant
    amount_minor: int
    currency: str
    expires_at: datetime | None = Field(
        default=None, description="Vencimiento ISO-8601 (`None` en STATIC)"
    )
    status: str = Field(description="Estado del cargo (`ACTIVE` si previsualizable)")


class QrPreviewResponse(BaseModel):
    data: QrPreviewData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "QrPayAuthMethod",
    "QrPayAuthorization",
    "QrPayData",
    "QrPayMerchant",
    "QrPayRequest",
    "QrPayResponse",
    "QrPayStatus",
    "QrPreviewData",
    "QrPreviewMerchant",
    "QrPreviewRequest",
    "QrPreviewResponse",
]
