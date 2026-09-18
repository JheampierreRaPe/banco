"""Esquemas de activacion y reenvio de OTP (E1-T10, HU02 CA-02/CA-03/CA-04).

`POST /auth/activate` -> cuenta `ACTIVE`; `POST /auth/otp/resend` -> nuevo
codigo con control de intentos. Envoltorio `docs/05#4` (`{"data", "meta"}`).
Los errores de negocio usan problem+json via `AppError` en el router
(`INVALID_OTP`, `EXPIRED_OTP`, `RESEND_LIMIT`, `RATE_LIMITED`); los errores
de esquema usan el 422 estandar de FastAPI (`{"detail": [...]}`).

Sin auth: son endpoints pre-activacion (el usuario aun no tiene sesion). El
`user_ref` viaja como texto y se interpreta como UUID en el servicio: un
formato invalido se mapea a `INVALID_OTP` generico (sin filtrar existencia).
El codigo OTP jamas se refleja en respuestas ni logs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: Canales aceptados en el reenvio (espejo de `notifications.CHANNELS`;
#: el servicio resuelve el defecto `sms`, canal de la plantilla `otp_code`).
OTP_CHANNELS: tuple[str, ...] = ("sms", "email", "push")


class ActivateRequest(BaseModel):
    """Entrada de `POST /auth/activate` (cuenta + codigo de un solo uso)."""

    user_ref: str = Field(min_length=1, max_length=64, description="UUID del usuario en texto.")
    code: str = Field(
        min_length=6, max_length=6, pattern=r"^\d{6}$", description="OTP de 6 digitos."
    )


class ActivateData(BaseModel):
    user_id: str
    status: str


class ActivateResponse(BaseModel):
    data: ActivateData
    meta: dict = Field(default_factory=dict)


class ResendRequest(BaseModel):
    """Entrada de `POST /auth/otp/resend` (nuevo codigo, invalida el anterior)."""

    user_ref: str = Field(min_length=1, max_length=64, description="UUID del usuario en texto.")
    channel: Literal["sms", "email", "push"] | None = Field(
        default=None, description="Canal de entrega (defecto: sms)."
    )


class ResendData(BaseModel):
    user_id: str
    resend_count: int = Field(ge=0, description="Reenvios consumidos en el ciclo.")
    expires_in: int = Field(ge=0, description="Vigencia restante del codigo en segundos.")


class ResendResponse(BaseModel):
    data: ResendData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "OTP_CHANNELS",
    "ActivateData",
    "ActivateRequest",
    "ActivateResponse",
    "ResendData",
    "ResendRequest",
    "ResendResponse",
]
