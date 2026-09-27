"""Esquemas Pydantic de beneficiarios frecuentes (E2-T13, HU07).

Fuente: `docs/05-contratos-api.md#62` (GET/POST/PUT/DELETE `/beneficiaries`)
y formato de respuesta `05#4` (`{"data", "meta"}`); columnas segun
`docs/03b-diccionario-de-datos.md#53`.

Reglas:

- Dinero entero en centimos (`preapproved_limit_minor: int >= 0`);
  nunca `float` (regla de oro 3).
- La cuenta/CCI y el documento jamas viajan en claro en respuestas: solo
  `account_masked` (`****` + ultimos 4). El documento (`holder_doc`) solo
  se acepta en escritura (se persiste unicamente su hash).
- El `owner_user_id` NO viene en el cuerpo: lo pone el servicio desde el
  JWT (regla del brief).
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class BeneficiaryCreate(BaseModel):
    """Alta `POST /beneficiaries` (CA-01)."""

    alias: str = Field(..., min_length=1, max_length=80, description="Nombre corto")
    account_or_cci: str = Field(..., min_length=1, max_length=30, description="Cuenta o CCI")
    photo_url: str | None = Field(default=None, max_length=2000)
    bank_code: str | None = Field(default=None, max_length=20)
    holder_name: str | None = Field(default=None, max_length=150)
    holder_doc: str | None = Field(
        default=None, max_length=30, description="Solo transito: se persiste su hash"
    )
    preapproved_limit_minor: int | None = Field(
        default=None, ge=0, description="Tope sin factor extra (centimos)"
    )
    currency: str | None = Field(default=None, max_length=3)


class BeneficiaryUpdate(BaseModel):
    """Edicion parcial `PUT /beneficiaries/{id}` (CA-03/CA-04)."""

    alias: str | None = Field(default=None, min_length=1, max_length=80)
    account_or_cci: str | None = Field(default=None, min_length=1, max_length=30)
    photo_url: str | None = Field(default=None, max_length=2000)
    bank_code: str | None = Field(default=None, max_length=20)
    holder_name: str | None = Field(default=None, max_length=150)
    holder_doc: str | None = Field(default=None, max_length=30)
    preapproved_limit_minor: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=3)


class BeneficiaryOut(BaseModel):
    """Beneficiario en respuestas: cuenta enmascarada, sin documento."""

    id: uuid.UUID
    alias: str
    photo_url: str | None = None
    bank_code: str | None = None
    account_masked: str = Field(..., pattern=r"^\*{4}.{4}$")
    holder_name: str | None = None
    preapproved_limit_minor: int | None = None
    currency: str | None = None
    status: str
    created_at: str | None = None
    updated_at: str | None = None


class BeneficiariesListResponse(BaseModel):
    """Envoltorio de lista/busqueda segun 05#4 (`data` + `meta`)."""

    data: list[BeneficiaryOut]
    meta: dict = Field(default_factory=dict)


class BeneficiaryResponse(BaseModel):
    """Envoltorio de recurso segun 05#4 (`data` + `meta`)."""

    data: BeneficiaryOut
    meta: dict = Field(default_factory=dict)


__all__ = [
    "BeneficiariesListResponse",
    "BeneficiaryCreate",
    "BeneficiaryOut",
    "BeneficiaryResponse",
    "BeneficiaryUpdate",
]
