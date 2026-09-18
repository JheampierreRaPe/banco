"""Esquemas del proxy KYC pre-registro (E1-T02, HU01).

`POST /auth/kyc/challenge` -> `{token, steps, expires_in}` y
`POST /auth/kyc/submit` -> `{overall_result, ...}`, ambos con envoltorio
`docs/05#4` (`{"data", "meta"}`).

Las imagenes viajan como base64 en JSON (sin multipart): el `service/`
las valida (tamano + magic bytes JPEG/PNG) ANTES de reenviar al adaptador
y las descarta despues (nunca se persisten ni se loguean). Sin auth todavia:
son endpoints pre-registro (el dispositivo aun no tiene cuenta).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: Tipos de documento aceptados en el submit (validacion temprana, 05#5/422).
KYC_DOC_TYPES: tuple[str, ...] = ("DNI", "CE", "PASSPORT")


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
    type: str = Field(description="Tipo de documento: DNI|CE|PASSPORT.")
    image_b64: str = Field(min_length=1, description="Anverso/recorte del documento en base64.")


class KycSegmentPayload(BaseModel):
    task: str = Field(min_length=1, max_length=64, description="Paso de liveness evaluado.")
    image_b64: str = Field(min_length=1, description="Frame del segmento en base64.")


class KycSubmitRequest(BaseModel):
    """Entrada de `POST /auth/kyc/submit` (documento + segmentos de liveness)."""

    challenge_token: str = Field(min_length=1, max_length=512)
    document: KycDocumentPayload
    segments: list[KycSegmentPayload] = Field(min_length=1, max_length=10)


class KycSubmitData(BaseModel):
    overall_result: bool
    detail_code: str = ""
    distance: float = 0.0


class KycSubmitResponse(BaseModel):
    data: KycSubmitData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "KYC_DOC_TYPES",
    "KycChallengeData",
    "KycChallengeRequest",
    "KycChallengeResponse",
    "KycDocumentPayload",
    "KycSegmentPayload",
    "KycSubmitData",
    "KycSubmitRequest",
    "KycSubmitResponse",
]
