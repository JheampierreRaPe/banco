"""Esquemas del reseteo de PIN con email+DNI+OTP (E1-T34, HU02/HU04, SCR-005).

`POST /auth/pin-reset {email, doc_number, code, pin}` -> fija/actualiza
`credentials.pin_hash` (previo OTP `RECOVERY` valido, que lo consume) para
el usuario que recupera el acceso pero no recuerda su PIN; responde
`{user_ref, pin_set: true}` SIN abrir sesion (la unica sesion la abre
`POST /auth/login/pin`). Envoltorio `docs/05#4` (`{"data", "meta"}`).
Los errores de negocio usan problem+json via `AppError` en el router
(`INVALID_PIN_RESET`: un solo 401 generico para email no registrado, DNI
que no coincide, sin OTP, codigo incorrecto, OTP vencido o bloqueado por
intentos —sin oraculo—; `RATE_LIMITED` solo para la ventana por
`email+IP`); los errores de esquema usan el 422 estandar de FastAPI
(`{"detail": [...]}`).

Sin auth: es un endpoint pre-sesion (el OTP `RECOVERY` valido ES la
autorizacion). El email se normaliza (`strip().lower()`) en el servicio;
el DNI jamas se persiste ni se refleja en logs/respuestas (solo se compara
su hash HMAC server-side). El codigo OTP y el PIN jamas se reflejan en
respuestas ni logs. El PIN se valida aqui (`4-6` digitos numericos -> 422
generico, sin oraculo sobre el estado de la cuenta) y solo viaja en
transito (se persiste unicamente su hash PBKDF2).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: Patron basico de email (422 ante malformado/vacio, sin filtrar existencia).
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class PinResetRequest(BaseModel):
    """Entrada de `POST /auth/pin-reset` (email + DNI + OTP -> PIN nuevo)."""

    email: str = Field(
        min_length=1,
        max_length=320,
        pattern=EMAIL_PATTERN,
        description="Email registrado del usuario.",
    )
    doc_number: str = Field(
        min_length=1,
        max_length=32,
        description=(
            "Numero de documento del titular (DNI/CE/pasaporte, con o sin "
            "separadores; solo transito: se compara su hash HMAC server-side, "
            "nunca se persiste ni se refleja en logs)."
        ),
    )
    code: str = Field(
        min_length=6, max_length=6, pattern=r"^\d{6}$", description="OTP RECOVERY de 6 digitos."
    )
    pin: str = Field(
        min_length=4,
        max_length=6,
        pattern=r"^\d{4,6}$",
        description="PIN nuevo en claro, 4-6 digitos (solo transito, nunca se persiste).",
    )


class PinResetData(BaseModel):
    """Exito de `/pin-reset` (E1-T34): PIN fijado, sin sesion ni tokens."""

    user_ref: str = Field(description="UUID del usuario en texto.")
    pin_set: bool = Field(description="True cuando el PIN quedo fijado.")


class PinResetResponse(BaseModel):
    data: PinResetData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "PinResetData",
    "PinResetRequest",
    "PinResetResponse",
]
