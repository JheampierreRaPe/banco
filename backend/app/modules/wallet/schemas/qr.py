"""Esquemas Pydantic del QR de comercio (E4-T08, HU14).

Contrato fijado (`docs/05#6.5`):

- `POST /merchants/qr {amount_minor, currency, concept?, mode?}` ->
  `201 {"data": {charge_id, qr_content, payload, payload_hash,
  signature, amount_minor, currency, concept, mode, status,
  expires_at, created_at}, "meta": {request_id, replayed}}`.
- `GET /merchants/qr/{id}` ->
  `200 {"data": {charge_id, qr_content, status, amount_minor,
  currency, expires_at, paid_at?}, "meta": {request_id}}`.

`qr_content` (`str`) es el contenido escaneable/interoperable: el
token/sobre canonico `BO-QR1` que produce el propio backend
(`wallet.domain.qr.to_interop_token`, adaptador `E4-T03`); un lector
conforme (HU15) lo decodifica (`decode_payload`) y lo verifica contra
`payload`/`signature`. El `payload` estructurado sigue siendo objeto
(dict) y no sustituye al string.

El secreto HMAC y el RUC jamas viajan en respuestas (solo la firma
verificable, el `payload` canonico y el sobre `BO-QR1`). Dinero
entero en centimos.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

QrMode = Literal["DYNAMIC", "STATIC"]


class QrCreate(BaseModel):
    """Cuerpo de generacion del QR de cobro (monto en centimos)."""

    amount_minor: int = Field(description="Monto en centimos, entero > 0")
    currency: str = Field(default="PEN", max_length=3, description="ISO-4217 (3 letras)")
    concept: str | None = Field(default=None, max_length=120)
    mode: QrMode = Field(default="DYNAMIC")


class QrCreateData(BaseModel):
    """Cargo QR firmado recien generado (respuesta del `POST`)."""

    charge_id: str
    qr_content: str = Field(
        description="Sobre canonico BO-QR1 listo para render/escaneo "
        "(lo decodifica y verifica un lector conforme HU15)"
    )
    payload: dict[str, Any]
    payload_hash: str
    signature: str
    amount_minor: int
    currency: str
    concept: str | None = None
    mode: str
    status: str
    expires_at: datetime | None = None
    created_at: datetime | None = None


class QrCreateResponse(BaseModel):
    data: QrCreateData
    meta: dict = Field(default_factory=dict)


class QrStatusData(BaseModel):
    """Estado del cargo (respuesta del `GET`; `status` efectivo on-read)."""

    charge_id: str
    qr_content: str = Field(
        description="Sobre canonico BO-QR1 listo para render/escaneo "
        "(mismo contenido que el del POST)"
    )
    status: str
    amount_minor: int
    currency: str
    expires_at: datetime | None = None
    paid_at: datetime | None = None


class QrStatusResponse(BaseModel):
    data: QrStatusData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "QrCreate",
    "QrCreateData",
    "QrCreateResponse",
    "QrMode",
    "QrStatusData",
    "QrStatusResponse",
]
