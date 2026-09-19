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

E1-T27: `device_public_key` (`"hmac:<hex>"`), `platform` (`android|ios`) y
`biometric_type` (`FACE|FINGERPRINT`) son opcionales. No se valida el enum de
`platform`/`biometric_type` en el esquema: el servicio los ignora si son
invalidos (best-effort, sin romper el login ni delatar el PIN con un 422).
`device_public_key` jamas se refleja en logs ni en la respuesta.
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
    device_public_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description=(
            "Clave publica del dispositivo para firmar el nonce, formato 'hmac:<hex>' "
            "(o PEM Ed25519/EC). Opcional: sin ella el login no registra binding "
            "(compatibilidad hacia atras). Nunca se refleja en logs."
        ),
    )
    platform: str | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description=(
            "Plataforma del dispositivo (`android`/`ios`). Un valor invalido se ignora "
            "al registrar el binding: el login continua (sin oraculo 422-vs-401 sobre el PIN)."
        ),
    )
    biometric_type: str | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description=(
            "Biometria local (`FACE`/`FINGERPRINT`). Un valor invalido se ignora al "
            "registrar el binding: el login continua."
        ),
    )


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
