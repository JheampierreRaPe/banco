"""Esquemas del login con PIN (E1-T14, HU03 CA-02/CA-03).

`POST /auth/login/pin` -> verifica el PIN (hash PBKDF2, comparacion en
tiempo constante) y devuelve JWT corto + refresh opaco. Envoltorio
`docs/05#4` (`{"data", "meta"}`). Los errores de negocio usan problem+json
via `AppError` en el router (`INVALID_CREDENTIALS` generico sin filtrar
existencia, `ACCOUNT_LOCKED` sin precision del tiempo restante); los
errores de esquema usan el 422 estandar de FastAPI (`{"detail": [...]}`).

Sin auth: es un endpoint pre-sesion (el usuario aun no tiene tokens). El
`user_ref` viaja como texto y se interpreta como UUID en el servicio: un
formato invalido se mapea a `INVALID_CREDENTIALS` generico (sin filtrar
existencia). El PIN viaja como texto libre (`1..128`, sin validar patron
para no crear un oraculo 422-vs-401) y jamas se refleja en logs.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PinLoginRequest(BaseModel):
    """Entrada de `POST /auth/login/pin` (PIN de contingencia, HU03 CA-02)."""

    user_ref: str = Field(min_length=1, max_length=64, description="UUID del usuario en texto.")
    pin: str = Field(
        min_length=1, max_length=128, description="PIN en claro (solo transito, nunca se persiste)."
    )
    device_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Dispositivo origen (se guarda en la sesion).",
    )
    device_info: dict | None = Field(
        default=None, description="Modelo/SO (se guarda en la sesion)."
    )
    ip: str | None = Field(default=None, min_length=1, max_length=45)


class PinLoginData(BaseModel):
    access_token: str
    refresh_token: str = Field(
        description="Opaco, de un solo despliegue: solo su hash se persiste."
    )
    token_type: str = Field(default="Bearer")
    session_id: str
    expires_in: int = Field(ge=0, description="Vigencia del refresh en segundos.")


class PinLoginResponse(BaseModel):
    data: PinLoginData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "PinLoginData",
    "PinLoginRequest",
    "PinLoginResponse",
]
