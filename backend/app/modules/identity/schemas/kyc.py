"""Esquemas del proxy KYC pre-registro (E1-T02 + E1-T24, HU01/HU02).

`POST /auth/kyc/challenge` -> `{token, steps, expires_in}` y
`POST /auth/kyc/submit` -> `{overall_result, ..., user_id, status, account_id}`,
ambos con envoltorio `docs/05#4` (`{"data", "meta"}`).

E1-T24 amplia el submit con los datos del titular (`applicant`) y el numero
de documento (`document.number`): el backend hashea el numero server-side
(`doc_number_hash`, UQ en `identity.users`) y deriva `doc_number_masked`;
`email` es obligatorio. El numero en claro y los frames nunca salen del
proceso: las imagenes viajan como base64 en JSON (sin multipart) y el
`service/` las valida (tamano + magic bytes JPEG/PNG) ANTES de reenviar al
adaptador y las descarta despues (nunca se persisten ni se loguean). Sin
auth todavia: son endpoints pre-registro (el dispositivo aun no tiene cuenta).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: Tipos de documento aceptados en el submit (validacion temprana, 05#5/422;
#: E1-T36 suma `RUC`, en paridad con `models.DOC_TYPES`).
KYC_DOC_TYPES: tuple[str, ...] = ("DNI", "CE", "PASSPORT", "RUC")


class KycChallengeRequest(BaseModel):
    """Entrada de `POST /auth/kyc/challenge` (todo opcional, pre-registro)."""

    device_id: str | None = Field(
        default=None, max_length=128, description="ID logico del dispositivo (rate limit)."
    )


class KycChallengeData(BaseModel):
    token: str = Field(description="Token de desafio opaco (TTL corto).")
    steps: list[str] = Field(description="Secuencia de pasos de liveness.")
    expires_in: int = Field(ge=0, description="TTL del token en segundos.")


class KycChallengeResponse(BaseModel):
    data: KycChallengeData
    meta: dict = Field(default_factory=dict)


class KycDocumentPayload(BaseModel):
    type: str = Field(description="Tipo de documento: DNI|CE|PASSPORT|RUC.")
    number: str = Field(
        min_length=1,
        max_length=32,
        description="Numero de documento en claro; el backend lo hashea (nunca se persiste).",
    )
    image_b64: str = Field(min_length=1, description="Anverso/recorte del documento en base64.")


class KycApplicantPayload(BaseModel):
    """Datos del titular para el alta (`onboard_customer`, E1-T24).

    E1-T36: `first_name`/`last_name` aceptan vacio a nivel de esquema (el
    RUC de persona juridica trae nombres vacios y razon social); la regla
    por tipo la valida `kyc_proxy.validate_applicant` ANTES del proveedor
    (`RUC` exige `business_name` o nombres completos; `DNI`/`CE`/`PASSPORT`
    exigen nombres). `business_name` es la razon social (max 150).
    """

    first_name: str = Field(default="", max_length=100)
    last_name: str = Field(default="", max_length=100)
    business_name: str | None = Field(
        default=None, max_length=150, description="Razon social (RUC de persona juridica)."
    )
    email: str = Field(min_length=1, max_length=320, description="Correo obligatorio (UQ).")
    phone: str | None = Field(default=None, max_length=20)


class KycSegmentPayload(BaseModel):
    """Segmento de liveness: `frames_b64` (rafaga) o `image_b64` (compatibilidad).

    El tope de frames por tarea lo valida el `service/`
    (`KYC_MAX_FRAMES_PER_SEGMENT`, default 30: permite >=15 frames por tarea,
    minimo del microservicio = 5).
    """

    task: str = Field(min_length=1, max_length=64, description="Paso de liveness evaluado.")
    image_b64: str | None = Field(
        default=None,
        description="Frame unico del segmento en base64 (compatibilidad).",
    )
    frames_b64: list[str] | None = Field(
        default=None,
        description="Rafaga de frames del segmento en base64, en orden.",
    )


class KycSubmitRequest(BaseModel):
    """Entrada de `POST /auth/kyc/submit` (documento + liveness + titular)."""

    challenge_token: str = Field(min_length=1, max_length=512)
    document: KycDocumentPayload
    segments: list[KycSegmentPayload] = Field(min_length=1, max_length=10)
    applicant: KycApplicantPayload


class KycSubmitData(BaseModel):
    overall_result: bool
    detail_code: str = ""
    distance: float = 0.0
    steps_verified: list[str] = Field(
        default_factory=list,
        description="Nombres de los pasos de liveness verificados (list[str], no conteo).",
    )
    steps_total: list[str] = Field(
        default_factory=list,
        description="Nombres de todos los pasos de liveness del desafio (list[str]).",
    )
    failed_step: str | None = Field(default=None, description="Paso que fallo, o `null`.")
    step_results: dict = Field(
        default_factory=dict, description="Resultado por paso (`{paso: {passed, reason}}`)."
    )
    overall_reason: str = Field(default="", description="Motivo global del microservicio.")
    user_id: str | None = Field(default=None, description="Usuario creado si el KYC paso.")
    status: str = Field(default="REJECTED", description="`ONBOARDED` | `REJECTED`.")
    account_id: str | None = Field(
        default=None, description="Cuenta digital creada si el KYC paso."
    )


class KycSubmitResponse(BaseModel):
    data: KycSubmitData
    meta: dict = Field(default_factory=dict)


class KycEvaluateRequest(BaseModel):
    """Entrada de `POST /auth/kyc/evaluate` (un paso + rafaga de frames)."""

    challenge_token: str = Field(min_length=1, max_length=512)
    step: str = Field(min_length=1, max_length=64, description="Paso a evaluar.")
    frames_b64: list[str] = Field(min_length=1, description="Frames del paso, en orden.")


class KycEvaluateData(BaseModel):
    step: str
    passed: bool
    reason: str = ""
    frames_analyzed: int = 0
    details: dict = Field(default_factory=dict)


class KycEvaluateResponse(BaseModel):
    data: KycEvaluateData
    meta: dict = Field(default_factory=dict)


class KycDocumentValidateRequest(BaseModel):
    """Entrada de `POST /auth/kyc/document/validate` (E1-T30).

    Imagen del documento en base64 (coherente con el cliente Flutter, sin
    multipart en el cliente); el `service/` valida tamano + magic bytes y el
    adaptador la reenvia como `file` multipart al microservicio.
    """

    image_b64: str = Field(description="Recorte del documento en base64 (no vacio).")


class KycDocumentValidateData(BaseModel):
    """Resultado de legibilidad del documento (`is_valid=false` no es error)."""

    is_valid: bool
    issues: list[str] = Field(default_factory=list, description="Motivos de rechazo.")
    checks: dict = Field(default_factory=dict, description="Detalle por verificacion.")


class KycDocumentValidateResponse(BaseModel):
    data: KycDocumentValidateData
    meta: dict = Field(default_factory=dict)


class DocumentLookupRequest(BaseModel):
    """Entrada de `POST /auth/kyc/document/lookup` (E1-T35).

    El formato estricto (`type in {DNI,RUC}`, `number` solo digitos con
    longitud exacta DNI=8/RUC=11) lo valida el `service/` para devolver el
    envoltorio 05#4 (`422 VALIDATION_ERROR`) en lugar del 422 de FastAPI.
    """

    type: str = Field(description="Tipo de documento: DNI|RUC.")
    number: str = Field(description="Numero de documento (solo digitos, 8|11).")


class DocumentLookupData(BaseModel):
    """Titular normalizado (E1-T35, HU01).

    Persona natural: `first_name`/`last_name` con valores y `business_name`
    vacio. RUC de persona juridica: `business_name` con la razon social y
    `first_name`/`last_name` vacios. Campos no editables en el cliente.
    """

    document_type: str = Field(description="Tipo consultado: DNI|RUC.")
    first_name: str = Field(default="", description="Nombres (persona natural).")
    last_name: str = Field(default="", description="Apellidos (persona natural).")
    business_name: str = Field(default="", description="Razon social (RUC juridica).")


class DocumentLookupResponse(BaseModel):
    data: DocumentLookupData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "KYC_DOC_TYPES",
    "DocumentLookupData",
    "DocumentLookupRequest",
    "DocumentLookupResponse",
    "KycApplicantPayload",
    "KycChallengeData",
    "KycChallengeRequest",
    "KycChallengeResponse",
    "KycDocumentPayload",
    "KycDocumentValidateData",
    "KycDocumentValidateRequest",
    "KycDocumentValidateResponse",
    "KycEvaluateData",
    "KycEvaluateRequest",
    "KycEvaluateResponse",
    "KycSegmentPayload",
    "KycSubmitData",
    "KycSubmitRequest",
    "KycSubmitResponse",
]
