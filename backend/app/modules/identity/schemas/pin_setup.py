"""Esquemas del fijado inicial del PIN (addendum E1-T14, HU02/HU03).

`POST /auth/pin/setup {user_ref, code, pin}` -> fija `pin_hash` una sola vez
(previo OTP `ACTIVATION` valido, que lo consume). Envoltorio `docs/05#4`
(`{"data", "meta"}`). Los errores de negocio usan problem+json via
`AppError` en el router (`INVALID_SETUP_CODE` generico sin filtrar
existencia, `PIN_ALREADY_SET` solo con OTP valido); los errores de esquema
usan el 422 estandar de FastAPI (`{"detail": [...]}`).

Sin auth: es un endpoint pre-sesion (el OTP valido ES la autorizacion). El
`user_ref` viaja como texto y se interpreta como UUID en el servicio: un
formato invalido se mapea a `INVALID_SETUP_CODE` generico. El PIN se valida
aqui (`4-6` digitos numericos -> 422 generico, sin oraculo sobre el estado
de la cuenta) y jamas se refleja en logs ni respuestas.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PinSetupRequest(BaseModel):
    """Entrada de `POST /auth/pin/setup` (fijado unico del PIN)."""

    user_ref: str = Field(min_length=1, max_length=64, description="UUID del usuario en texto.")
    code: str = Field(
        min_length=6, max_length=6, pattern=r"^\d{6}$", description="OTP ACTIVATION de 6 digitos."
    )
    pin: str = Field(
        min_length=4,
        max_length=6,
        pattern=r"^\d{4,6}$",
        description="PIN nuevo en claro, 4-6 digitos (solo transito, nunca se persiste).",
    )


class PinSetupData(BaseModel):
    user_id: str
    status: str


class PinSetupResponse(BaseModel):
    data: PinSetupData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "PinSetupData",
    "PinSetupRequest",
    "PinSetupResponse",
]
