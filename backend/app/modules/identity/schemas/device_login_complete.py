"""Esquemas del paso 2 del login en dispositivo nuevo (E1-T46, HU03/HU04).

`POST /auth/login/device/complete {email, doc_type, document_number, code, pin,
device_id, device_public_key[, platform, biometric_type, device_info]}` -> valida el OTP
`LOGIN` (emitido por el paso 1, `POST /auth/login/device/request`, E1-T45) +
el PIN de la MISMA cuenta `ACTIVE` (resuelta por `email + documento`), registra
o actualiza el `device_binding` y RECIEN entonces abre sesion (JWT corto +
refresh opaco + `user_ref` para que el cliente persista el `userRef`).
Envoltorio `docs/05#4` (`{"data", "meta"}`). Los errores de negocio usan
problem+json via `AppError` en el router (`INVALID_LOGIN`: un unico 401
generico para cuenta inexistente/no elegible, documento que no coincide, OTP
invalido/vencido/bloqueado y PIN erroneo —sin oraculo—; `ACCOUNT_LOCKED` 423;
`RATE_LIMITED` 429); los errores de esquema usan el 422 estandar de FastAPI.

Decision de formato del PIN (documentada, patron `pin_reset` E1-T34): el PIN se
valida aqui (`4-6` digitos numericos -> 422 generico) y en el servicio (defensivo,
antes de tocar estado). NO crea oraculo 422-vs-401 sobre la cuenta: el 422 solo
habla del formato del propio input del llamante (igual que `pin_reset` y
`pin_setup`), nunca del estado de la cuenta; cualquier combinacion valida pero
incorrecta colapsa al mismo 401.

`doc_type` (`DNI`/`RUC`, default `DNI`) gobierna SOLO la validacion de
formato/longitud (`DNI` 8 / `RUC` 11 digitos, paridad con el paso 1 E1-T45 y
`pin_reset` E1-T40); la resolucion sigue por `email` + `doc_number_hash` HMAC
(no se cruza con `users.doc_type`, sin oraculo).

`device_id`/`device_public_key` solo exigen no vacios (sin validar el formato de
la clave, como `PinLoginRequest`: se persiste tal cual, `hmac:`/PEM, y la
verifica `device_login.verify_signature` cuando toque firmar). `platform`/
`biometric_type` no validan enum en el esquema: el servicio los ignora si son
invalidos (best-effort, sin romper el login ni delatar nada con un 422).

Sin auth: es un endpoint pre-sesion (el OTP `LOGIN` + PIN validos SON la
autorizacion). El email se normaliza (`strip().lower()`) en el servicio; el
documento, el OTP, el PIN y la clave publica jamas se persisten en claro ni se
reflejan en logs/respuestas (el documento solo se compara hasheado HMAC
server-side en tiempo constante).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, model_validator

#: Patron basico de email (422 ante malformado/vacio, sin filtrar existencia).
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

#: Tipos de documento aceptados (E1-T46, paridad con el paso 1 E1-T45 y
#: `pin_reset` E1-T40; gobiernan SOLO la validacion de formato/longitud).
DEVICE_LOGIN_COMPLETE_DOC_TYPES: tuple[str, ...] = ("DNI", "RUC")

#: Longitudes exactas por tipo (solo digitos).
DEVICE_LOGIN_COMPLETE_DOC_LENGTHS: dict[str, int] = {"DNI": 8, "RUC": 11}


class DeviceLoginCompleteRequest(BaseModel):
    """Entrada de `POST /auth/login/device/complete` (OTP LOGIN + PIN -> sesion)."""

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
    code: str = Field(
        min_length=6,
        max_length=6,
        pattern=r"^\d{6}$",
        description="OTP LOGIN de 6 digitos (emitido por el paso 1, un solo uso).",
    )
    pin: str = Field(
        min_length=4,
        max_length=6,
        pattern=r"^\d{4,6}$",
        description="PIN en claro, 4-6 digitos (solo transito, nunca se persiste).",
    )
    device_id: str = Field(
        min_length=1,
        max_length=128,
        description="Dispositivo a asociar (se guarda en la sesion y el binding).",
    )
    device_public_key: str = Field(
        min_length=1,
        max_length=4096,
        description=(
            "Clave publica del dispositivo, formato 'hmac:<hex>' (o PEM "
            "Ed25519/EC). Se persiste tal cual (sin validar formato); nunca se "
            "refleja en logs ni en la respuesta."
        ),
    )
    platform: str | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description=(
            "Plataforma del dispositivo (`android`/`ios`). Un valor invalido se ignora "
            "al registrar el binding: el login continua."
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
    device_info: dict | None = Field(
        default=None,
        description="Modelo/SO (se guarda en la sesion; solo metadatos no sensibles).",
    )

    @model_validator(mode="after")
    def _check_doc_type_length(self) -> DeviceLoginCompleteRequest:
        """Exige `doc_type in {DNI,RUC}` y `document_number` solo digitos exactos.

        Paridad con el paso 1 (E1-T45) y `pin_reset` (E1-T40): `DNI` 8 /
        `RUC` 11 digitos. Cualquier combinacion invalida -> `ValueError`
        (422 estandar de FastAPI, sin codigo de negocio nuevo y sin tocar
        OTP ni estado). Normaliza `doc_type` (trim + mayusculas) y
        `document_number` (trim) para que el servicio reciba la forma
        canonica.
        """
        kind = self.doc_type.strip().upper() if isinstance(self.doc_type, str) else ""
        if kind not in DEVICE_LOGIN_COMPLETE_DOC_TYPES:
            raise ValueError(f"doc_type debe ser uno de {DEVICE_LOGIN_COMPLETE_DOC_TYPES}")
        digits = self.document_number.strip() if isinstance(self.document_number, str) else ""
        if not digits or re.fullmatch(r"[0-9]+", digits) is None:
            raise ValueError("document_number debe contener solo digitos")
        expected = DEVICE_LOGIN_COMPLETE_DOC_LENGTHS[kind]
        if len(digits) != expected:
            raise ValueError(f"document_number: {kind} debe tener {expected} digitos")
        self.doc_type = kind
        self.document_number = digits
        return self


class DeviceLoginCompleteData(BaseModel):
    """Exito de `/auth/login/device/complete` (sesion abierta en el dispositivo)."""

    access_token: str
    refresh_token: str = Field(
        description="Opaco, de un solo despliegue: solo su hash se persiste."
    )
    token_type: str = Field(default="Bearer")
    session_id: str
    expires_in: int = Field(ge=0, description="Vigencia del refresh en segundos.")
    user_ref: str = Field(description="UUID del usuario en texto (el `userRef` a persistir).")
    biometric_enabled: bool = Field(
        default=False, description="Consentimiento vigente del usuario (E1-T39)."
    )


class DeviceLoginCompleteResponse(BaseModel):
    data: DeviceLoginCompleteData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "DEVICE_LOGIN_COMPLETE_DOC_LENGTHS",
    "DEVICE_LOGIN_COMPLETE_DOC_TYPES",
    "EMAIL_PATTERN",
    "DeviceLoginCompleteData",
    "DeviceLoginCompleteRequest",
    "DeviceLoginCompleteResponse",
]
