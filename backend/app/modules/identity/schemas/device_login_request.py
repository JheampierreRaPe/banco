"""Esquemas del paso 1 del login en dispositivo nuevo (E1-T45, HU03/HU04).

`POST /auth/login/device/request {email, doc_type, document_number}` ->
siempre 200 con `{accepted, ttl_seconds, resend_wait_seconds}` (constantes
globales, identico exista o no la cuenta) + OTP `LOGIN` solo por email
cuando `email` y documento pertenecen a la MISMA cuenta `ACTIVE`. El OTP
emitido lo consume el paso 2 (`POST /auth/login/device/complete`, E1-T46).
Envoltorio `docs/05#4` (`{"data", "meta"}`). Los errores de negocio usan
problem+json via `AppError` en el router (`RATE_LIMITED` para la ventana
por `email+IP`); los errores de esquema usan el 422 estandar de FastAPI
(`{"detail": [...]}`).

`doc_type` (`DNI`/`RUC`, default `DNI`) gobierna SOLO la validacion de
formato/longitud (`DNI` 8 / `RUC` 11 digitos, paridad con `pin_reset`
E1-T34/E1-T40 y el lookup E1-T35); la resolucion sigue por `email` +
`doc_number_hash` HMAC (no se cruza con `users.doc_type`, sin oraculo).

Sin auth: es un endpoint pre-sesion. El email se normaliza
(`strip().lower()`) en el servicio; el documento jamas se persiste ni se
refleja en logs/respuestas (solo se compara su hash HMAC server-side en
tiempo constante). El codigo OTP jamas se refleja en respuestas ni logs.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, model_validator

#: Patron basico de email (422 ante malformado/vacio, sin filtrar existencia).
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

#: Tipos de documento aceptados (E1-T45, paridad con `pin_reset` E1-T40 y el
#: lookup E1-T35; gobiernan SOLO la validacion de formato/longitud).
DEVICE_LOGIN_DOC_TYPES: tuple[str, ...] = ("DNI", "RUC")

#: Longitudes exactas por tipo (solo digitos).
DEVICE_LOGIN_DOC_LENGTHS: dict[str, int] = {"DNI": 8, "RUC": 11}


class DeviceLoginRequestRequest(BaseModel):
    """Entrada de `POST /auth/login/device/request` (email + DNI/RUC -> OTP LOGIN)."""

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
    document_number: str = Field(
        min_length=1,
        max_length=32,
        description=(
            "Numero de documento del titular (solo digitos: DNI 8 / RUC 11 "
            "segun `doc_type`; solo transito: se compara su hash HMAC "
            "server-side, nunca se persiste ni se refleja en logs)."
        ),
    )

    @model_validator(mode="after")
    def _check_doc_type_length(self) -> DeviceLoginRequestRequest:
        """Exige `doc_type in {DNI,RUC}` y `document_number` solo digitos exactos.

        E1-T45 (paridad con `pin_reset` E1-T40 y el lookup E1-T35): `DNI` 8 /
        `RUC` 11 digitos. Cualquier combinacion invalida -> `ValueError`
        (422 estandar de FastAPI, sin codigo de negocio nuevo y sin tocar
        OTP ni estado). Normaliza `doc_type` (trim + mayusculas) y
        `document_number` (trim) para que el servicio reciba la forma
        canonica.
        """
        kind = self.doc_type.strip().upper() if isinstance(self.doc_type, str) else ""
        if kind not in DEVICE_LOGIN_DOC_TYPES:
            raise ValueError(f"doc_type debe ser uno de {DEVICE_LOGIN_DOC_TYPES}")
        digits = self.document_number.strip() if isinstance(self.document_number, str) else ""
        if not digits or re.fullmatch(r"[0-9]+", digits) is None:
            raise ValueError("document_number debe contener solo digitos")
        expected = DEVICE_LOGIN_DOC_LENGTHS[kind]
        if len(digits) != expected:
            raise ValueError(f"document_number: {kind} debe tener {expected} digitos")
        self.doc_type = kind
        self.document_number = digits
        return self


#: Alias de compatibilidad (mismo modelo, nombre corto).
DeviceLoginRequest = DeviceLoginRequestRequest


class DeviceLoginRequestRequestData(BaseModel):
    """Cuerpo 200 de `POST /auth/login/device/request` (constantes globales)."""

    accepted: bool
    ttl_seconds: int = Field(ge=0, description="Vigencia del OTP en segundos.")
    resend_wait_seconds: int = Field(ge=0, description="Espera minima entre envios.")


#: Alias de compatibilidad (mismo modelo, nombre corto).
DeviceLoginRequestData = DeviceLoginRequestRequestData


class DeviceLoginRequestResponse(BaseModel):
    data: DeviceLoginRequestRequestData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "DEVICE_LOGIN_DOC_LENGTHS",
    "DEVICE_LOGIN_DOC_TYPES",
    "EMAIL_PATTERN",
    "DeviceLoginRequest",
    "DeviceLoginRequestData",
    "DeviceLoginRequestRequest",
    "DeviceLoginRequestRequestData",
    "DeviceLoginRequestResponse",
]
