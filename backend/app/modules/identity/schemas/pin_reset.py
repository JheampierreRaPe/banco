"""Esquemas del reseteo de PIN con email+DNI/RUC+OTP (E1-T34, HU02/HU04, SCR-005).

`POST /auth/pin-reset {email, doc_number, [doc_type,] code, pin}` -> fija/actualiza
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

E1-T40: `doc_type` (`DNI`/`RUC`, default `DNI` por compatibilidad F-T43)
gobierna SOLO la validacion de formato/longitud del `doc_number` (DNI 8 /
RUC 11 digitos, en paridad con el lookup E1-T35); la resolucion sigue por
`email` + `doc_number_hash` HMAC (no se cruza con `users.doc_type`).

Sin auth: es un endpoint pre-sesion (el OTP `RECOVERY` valido ES la
autorizacion). El email se normaliza (`strip().lower()`) en el servicio;
el DNI/RUC jamas se persiste ni se refleja en logs/respuestas (solo se compara
su hash HMAC server-side). El codigo OTP y el PIN jamas se reflejan en
respuestas ni logs. El PIN se valida aqui (`4-6` digitos numericos -> 422
generico, sin oraculo sobre el estado de la cuenta) y solo viaja en
transito (se persiste unicamente su hash PBKDF2).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, model_validator

#: Patron basico de email (422 ante malformado/vacio, sin filtrar existencia).
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

#: Tipos de documento aceptados en `/pin-reset` (E1-T40, paridad con el
#: lookup E1-T35: `document_lookup.DOC_LOOKUP_TYPES`).
PIN_RESET_DOC_TYPES: tuple[str, ...] = ("DNI", "RUC")

#: Longitudes exactas por tipo (solo digitos; E1-T40, paridad E1-T35).
PIN_RESET_DOC_LENGTHS: dict[str, int] = {"DNI": 8, "RUC": 11}


class PinResetRequest(BaseModel):
    """Entrada de `POST /auth/pin-reset` (email + DNI/RUC + OTP -> PIN nuevo)."""

    email: str = Field(
        min_length=1,
        max_length=320,
        pattern=EMAIL_PATTERN,
        description="Email registrado del usuario.",
    )
    doc_type: str = Field(
        default="DNI",
        description="Tipo de documento: DNI|RUC (solo valida el formato/longitud).",
    )
    doc_number: str = Field(
        min_length=1,
        max_length=32,
        description=(
            "Numero de documento del titular (solo digitos: DNI 8 / RUC 11 "
            "segun `doc_type`; solo transito: se compara su hash HMAC "
            "server-side, nunca se persiste ni se refleja en logs)."
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

    @model_validator(mode="after")
    def _check_doc_type_length(self) -> PinResetRequest:
        """Exige `doc_type in {DNI,RUC}` y `doc_number` solo digitos exactos.

        E1-T40 (paridad con el lookup E1-T35): `DNI` 8 / `RUC` 11 digitos.
        Cualquier combinacion invalida -> `ValueError` (422 estandar de
        FastAPI, sin codigo de negocio nuevo y sin tocar OTP ni estado).
        Normaliza `doc_type` (trim + mayusculas) y `doc_number` (trim) para
        que el servicio reciba la forma canonica.
        """
        kind = self.doc_type.strip().upper() if isinstance(self.doc_type, str) else ""
        if kind not in PIN_RESET_DOC_TYPES:
            raise ValueError(f"doc_type debe ser uno de {PIN_RESET_DOC_TYPES}")
        digits = self.doc_number.strip() if isinstance(self.doc_number, str) else ""
        if not digits or re.fullmatch(r"[0-9]+", digits) is None:
            raise ValueError("doc_number debe contener solo digitos")
        expected = PIN_RESET_DOC_LENGTHS[kind]
        if len(digits) != expected:
            raise ValueError(f"doc_number: {kind} debe tener {expected} digitos")
        self.doc_type = kind
        self.doc_number = digits
        return self


class PinResetData(BaseModel):
    """Exito de `/pin-reset` (E1-T34): PIN fijado, sin sesion ni tokens."""

    user_ref: str = Field(description="UUID del usuario en texto.")
    pin_set: bool = Field(description="True cuando el PIN quedo fijado.")


class PinResetResponse(BaseModel):
    data: PinResetData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "EMAIL_PATTERN",
    "PIN_RESET_DOC_LENGTHS",
    "PIN_RESET_DOC_TYPES",
    "PinResetData",
    "PinResetRequest",
    "PinResetResponse",
]
