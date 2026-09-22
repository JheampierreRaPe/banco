"""Esquemas de recuperacion de acceso por email (E1-T33, HU04).

`POST /auth/recovery/request {email}` -> 200 identico exista o no el email
(sin enumeracion) + OTP `RECOVERY` por email si el usuario es elegible.
`POST /auth/recovery/verify` -> valida el OTP SIN abrir sesion y devuelve
solo `{user_ref, device_bound}` (SCR-005: la unica sesion la abre
`POST /auth/login/pin`; `user_ref` lo persiste `F-T29`). Envoltorio
`docs/05#4` (`{"data", "meta"}`). Los errores de negocio usan problem+json
via `AppError` en el router (`INVALID_RECOVERY_CODE` para email no
registrado, no elegible, sin OTP, codigo incorrecto, OTP vencido o
bloqueado por intentos —un solo generico sin oraculo— y `RATE_LIMITED`
solo para la ventana por `email+IP`); los errores de esquema usan el 422
estandar de FastAPI (`{"detail": [...]}`).

Sin auth: son endpoints pre-sesion (el usuario aun no tiene tokens). El
email viaja como texto y se normaliza (`strip().lower()`) en el servicio.
El codigo OTP jamas se refleja en respuestas ni logs. `platform` /
`biometric_type` no se validan como enum en el esquema: el servicio los
ignora si son invalidos (best-effort, sin oraculo 422-vs-401).
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


class RecoveryVerifyRequest(BaseModel):
    """Entrada de `POST /auth/recovery/verify` (OTP + dispositivo nuevo)."""

    email: str = Field(
        min_length=1,
        max_length=320,
        pattern=EMAIL_PATTERN,
        description="Email registrado del usuario.",
    )
    code: str = Field(
        min_length=6, max_length=6, pattern=r"^\d{6}$", description="OTP de 6 digitos."
    )
    device_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Dispositivo nuevo (solo binding del dispositivo, sin abrir sesion).",
    )
    device_public_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description=(
            "Clave publica del dispositivo nuevo, formato 'hmac:<hex>' "
            "(o PEM Ed25519/EC). Opcional: sin ella el acceso igual procede "
            "(`device_bound=false`). Nunca se refleja en logs."
        ),
    )
    platform: str | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description="Plataforma (`android`/`ios`); un valor invalido se ignora.",
    )
    biometric_type: str | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description="Biometria local (`FACE`/`FINGERPRINT`); un valor invalido se ignora.",
    )


class RecoveryVerifyData(BaseModel):
    """Exito de `verify` (E1-T33): sin sesion ni tokens (SCR-005)."""

    user_ref: str = Field(description="UUID del usuario en texto (lo persiste F-T29).")
    device_bound: bool = Field(
        description="True solo si se REGISTRO un binding nuevo en este verify."
    )


class RecoveryVerifyResponse(BaseModel):
    data: RecoveryVerifyData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "RecoveryRequest",
    "RecoveryRequestData",
    "RecoveryRequestResponse",
    "RecoveryVerifyData",
    "RecoveryVerifyRequest",
    "RecoveryVerifyResponse",
]
