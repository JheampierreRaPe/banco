"""Esquemas del precheck de email del paso "Continuar" del KYC (E1-T40, HU01).

`POST /auth/kyc/email/check {email}` -> `200 {"data": {"available": true}}`
si el correo no esta registrado, o `409 DUPLICATE_EMAIL` (neutro, via
`AppError` en el router) si ya existe. Envoltorio `docs/05#4`
(`{"data", "meta"}`). Los errores de esquema usan el 422 estandar de
FastAPI (`{"detail": [...]}`).

Sin auth: es un endpoint pre-registro (el email aun no tiene cuenta). El
email se normaliza (`strip().lower()`) en el servicio y jamas se refleja
en logs ni en el mensaje de error.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

#: Patron basico de email (422 ante malformado/vacio, sin filtrar existencia;
#: mismo patron que `schemas/recovery.py` y `schemas/pin_reset.py`).
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class EmailCheckRequest(BaseModel):
    """Entrada de `POST /auth/kyc/email/check` (email a prechequear)."""

    email: str = Field(
        min_length=1,
        max_length=320,
        pattern=EMAIL_PATTERN,
        description="Email a verificar (se normaliza: trim + minusculas).",
    )

    @field_validator("email", mode="before")
    @classmethod
    def _strip_email(cls, value: object) -> object:
        """Tolera espacios alrededor (E1-T40: `  CORREO@x.com  ` -> 409 si existe).

        El trim corre ANTES del patron, asi una variacion con espacios de un
        email registrado responde `409 DUPLICATE_EMAIL` (no 422); el servicio
        vuelve a normalizar (`strip().lower()`). Malformado/vacio -> 422.
        """
        return value.strip() if isinstance(value, str) else value


class EmailCheckData(BaseModel):
    """Disponibilidad del email (E1-T40): `True` si no esta registrado."""

    available: bool = Field(description="True cuando el email no esta registrado.")


class EmailCheckResponse(BaseModel):
    data: EmailCheckData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "EMAIL_PATTERN",
    "EmailCheckData",
    "EmailCheckRequest",
    "EmailCheckResponse",
]
