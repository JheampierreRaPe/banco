"""Esquemas del login con dispositivo (E1-T13, HU03 CA-01).

`POST /auth/login/challenge` -> `nonce` de un solo uso con TTL corto;
`POST /auth/login/facial` -> verifica la firma del `nonce` y devuelve JWT
corto + refresh opaco. Envoltorio `docs/05#4` (`{"data", "meta"}`).
Los errores de negocio usan problem+json via `AppError` en el router
(`INVALID_LOGIN` generico sin filtrar, `EXPIRED_NONCE` por TTL); los
errores de esquema usan el 422 estandar de FastAPI (`{"detail": [...]}`).

Sin auth: son endpoints pre-sesion (el usuario aun no tiene tokens). El
`user_ref` viaja como texto y se interpreta como UUID en el servicio: un
formato invalido se mapea a `INVALID_LOGIN` generico (sin filtrar
existencia). La firma y el refresh jamas se reflejan en logs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChallengeRequest(BaseModel):
    """Entrada de `POST /auth/login/challenge` (a quien va el desafio)."""

    user_ref: str = Field(min_length=1, max_length=64, description="UUID del usuario en texto.")
    device_id: str | None = Field(
        default=None, min_length=1, max_length=128, description="Dispositivo que firmara."
    )


class ChallengeData(BaseModel):
    nonce: str = Field(description="Desafio aleatorio de un solo uso.")
    expires_in: int = Field(ge=0, description="Vigencia restante del nonce en segundos.")


class ChallengeResponse(BaseModel):
    data: ChallengeData
    meta: dict = Field(default_factory=dict)


class FacialRequest(BaseModel):
    """Entrada de `POST /auth/login/facial` (nonce firmado por el dispositivo)."""

    nonce: str = Field(min_length=1, max_length=256, description="Desafio emitido por /challenge.")
    device_id: str = Field(min_length=1, max_length=128, description="Dispositivo firmante.")
    signature: str = Field(
        min_length=1, max_length=4096, description="Firma del nonce (HMAC hex o base64 asimetrica)."
    )
    user_ref: str | None = Field(
        default=None, min_length=1, max_length=64, description="UUID esperado (chequeo opcional)."
    )
    platform: Literal["android", "ios"] | None = Field(default=None)
    biometric_type: Literal["FACE", "FINGERPRINT"] | None = Field(default=None)
    device_info: dict | None = Field(default=None, description="Modelo/SO (se guarda en la sesion).")
    ip: str | None = Field(default=None, min_length=1, max_length=45)


class FacialData(BaseModel):
    access_token: str
    refresh_token: str = Field(description="Opaco, de un solo despliegue: solo su hash se persiste.")
    token_type: str = Field(default="Bearer")
    session_id: str
    expires_in: int = Field(ge=0, description="Vigencia del refresh en segundos.")


class FacialResponse(BaseModel):
    data: FacialData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "ChallengeData",
    "ChallengeRequest",
    "ChallengeResponse",
    "FacialData",
    "FacialRequest",
    "FacialResponse",
]
