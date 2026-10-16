"""Esquemas Pydantic del QR personal estatico (E4-T21, HU14).

Contrato fijado (`docs/05#6.5`):

- `POST /wallet/qr {currency?, concept?}` (Bearer, solo el titular;
  `Idempotency-Key` opcional: no mueve dinero) ->
  `201 {"data": {charge_id, qr_content, payload, payload_hash,
  signature, amount_minor: null, currency, concept, mode: "STATIC",
  status, expires_at: null, created_at, payee_type: "PERSONAL",
  owner_user_id, payee_account_id, reusable: true},
  "meta": {request_id, replayed}}`.
- `GET /wallet/qr/{id}` (solo el titular dueno; ajeno/inexistente/de
  comercio -> `404` neutro) ->
  `200 {"data": {charge_id, qr_content, status, amount_minor: null,
  currency, expires_at: null, paid_at?, payee_type, reusable},
  "meta": {request_id}}`.

`qr_content` (`str`) es el sobre canonico `BO-QR1` con
`amount_minor = 0` ("lo define el pagador"; el pagador aporta el monto
al pagar). Dinero entero en centimos (el `None` es "sin monto", nunca
`float`). Sin PII en respuestas (solo `id`s).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PersonalQrCreate(BaseModel):
    """Cuerpo de generacion del QR personal (sin monto, sin caducidad)."""

    currency: str = Field(default="PEN", max_length=3, description="ISO-4217 (3 letras)")
    concept: str | None = Field(default=None, max_length=120)


class PersonalQrCreateData(BaseModel):
    """QR personal estatico recien generado (respuesta del `POST`)."""

    charge_id: str
    qr_content: str = Field(
        description="Sobre canonico BO-QR1 listo para render/escaneo "
        "(amount_minor = 0: el pagador digita el monto)"
    )
    payload: dict[str, Any]
    payload_hash: str
    signature: str
    amount_minor: None = Field(default=None, description="Siempre null: sin monto")
    currency: str
    concept: str | None = None
    mode: str = Field(default="STATIC")
    status: str
    expires_at: None = Field(default=None, description="Siempre null: sin caducidad")
    created_at: datetime | None = None
    payee_type: str = Field(default="PERSONAL")
    owner_user_id: str
    payee_account_id: str | None = None
    reusable: bool = Field(default=True)


class PersonalQrCreateResponse(BaseModel):
    data: PersonalQrCreateData
    meta: dict = Field(default_factory=dict)


class PersonalQrStatusData(BaseModel):
    """Estado del QR personal (respuesta del `GET`)."""

    charge_id: str
    qr_content: str = Field(description="Mismo sobre BO-QR1 del POST")
    status: str
    amount_minor: None = Field(default=None)
    currency: str
    expires_at: None = Field(default=None)
    paid_at: datetime | None = None
    payee_type: str = Field(default="PERSONAL")
    reusable: bool = Field(default=True)


class PersonalQrStatusResponse(BaseModel):
    data: PersonalQrStatusData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "PersonalQrCreate",
    "PersonalQrCreateData",
    "PersonalQrCreateResponse",
    "PersonalQrStatusData",
    "PersonalQrStatusResponse",
]
