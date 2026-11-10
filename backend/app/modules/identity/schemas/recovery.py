"""Esquemas de recuperacion de acceso por email (E1-T33, HU04; E1-T41 retira `verify`).

`POST /auth/recovery/request {email}` -> 200 identico exista o no el email
(sin enumeracion) + OTP `RECOVERY` por email si el usuario es elegible.
El OTP emitido lo consume `POST /auth/pin-reset` (E1-T34). Envoltorio
`docs/05#4` (`{"data", "meta"}`). Los errores de negocio usan problem+json
via `AppError` en el router (`RATE_LIMITED` para la ventana por `email+IP`);
los errores de esquema usan el 422 estandar de FastAPI (`{"detail": [...]}`).

Sin auth: endpoint pre-sesion (el usuario aun no tiene tokens). El
email viaja como texto y se normaliza (`strip().lower()`) en el servicio.
El codigo OTP jamas se refleja en respuestas ni logs.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: Patron basico de email (422 ante malformado/vacio, sin filtrar existencia).
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class RecoveryRequest(BaseModel):
    """Entrada de `POST /auth/recovery/request` (email registrado)."""

    email: str = Field(
        min_length=1,
        max_length=320,
        pattern=EMAIL_PATTERN,
        description="Email registrado del usuario.",
    )


class RecoveryRequestData(BaseModel):
    accepted: bool
    ttl_seconds: int = Field(ge=0, description="Vigencia del OTP en segundos.")
    resend_wait_seconds: int = Field(ge=0, description="Espera minima entre envios.")


class RecoveryRequestResponse(BaseModel):
    data: RecoveryRequestData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "RecoveryRequest",
    "RecoveryRequestData",
    "RecoveryRequestResponse",
]
