"""Esquemas Pydantic de la firma del contrato (E3-T14, HU11).

- `POST .../sign/challenge` (reto): cuerpo opcional `{device_id?}`
  para ligar el `nonce` al dispositivo; responde el `nonce` a firmar.
- `POST .../sign` (firma): dos vias mutuamente excluyentes (decision
  del dueno, patron E2-T19/E2-T31):
  - biometria del dispositivo: `{device_id, signature[, nonce]}`;
  - fallback PIN: `{pin}` (el `nonce` no aplica a esta via).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ChallengeData",
    "ChallengeRequest",
    "ChallengeResponse",
    "SignContractData",
    "SignContractRequest",
    "SignContractResponse",
    "SignedDocumentOut",
]


class SignContractRequest(BaseModel):
    """`POST .../sign`: firma del `nonce` o PIN (exactamente un factor).

    Regla de esquema (patron E2-T31): exactamente uno de
    `signature`/`pin` (`422` si ambos o ninguno); `device_id`
    obligatorio solo con `signature`; `nonce` solo aplica a la firma
    (eco opcional del reto vigente). El `pin` viaja como texto libre
    (`1..128`, sin patron para no crear un oraculo `422` distinto del
    `403` generico). `extra="forbid"` (sin campos ajenos).
    """

    model_config = ConfigDict(extra="forbid")

    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    signature: str | None = Field(default=None, min_length=1, max_length=4096)
    nonce: str | None = Field(default=None, max_length=256)
    pin: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _exactly_one_factor(self) -> SignContractRequest:
        """Exige un solo factor (`signature` o `pin`, nunca ambos ni ninguno)."""
        has_signature = self.signature is not None
        has_pin = self.pin is not None
        if has_signature == has_pin:
            raise ValueError("se exige exactamente un factor: `signature` o `pin`")
        if has_signature and self.device_id is None:
            raise ValueError("`device_id` es obligatorio con `signature`")
        return self


class ChallengeRequest(BaseModel):
    """`POST .../sign/challenge`: reto opcional ligado al dispositivo.

    Sin cuerpo (o `{}`) el `nonce` queda ligado solo al titular; con
    `device_id` la firma posterior debe venir de ese dispositivo
    (desajuste -> generico, sin oraculo). Solo rotacion: no mueve
    dinero, sin `Idempotency-Key`.
    """

    model_config = ConfigDict(extra="forbid")

    device_id: str | None = Field(default=None, min_length=1, max_length=128)


class SignedDocumentOut(BaseModel):
    """Un documento firmado (metadatos + sello derivado, sin contenido)."""

    model_config = ConfigDict(from_attributes=False)

    contract_id: str = Field(description="Fila `credits.contracts`")
    doc_type: str = Field(description="CONTRATO, PAGARE o RESUMEN")
    doc: str = Field(description="Nombre publico: contrato, pagare o resumen")
    doc_ref: str = Field(description="Ruta/almacen logica del documento")
    doc_hash: str = Field(description="SHA-256 del contenido canonico")
    status: str = Field(description="SIGNED")
    signed_at: str = Field(description="Instante del servidor (ISO-8601)")
    seal: str = Field(description="Sello de no repudio (derivado, verificable)")


class SignContractData(BaseModel):
    """Juego firmado (una firma sella los tres documentos)."""

    model_config = ConfigDict(from_attributes=False)

    application_id: str
    contract_id: str = Field(description="Primer documento (compat `05#64`)")
    status: str = Field(description="SIGNED")
    signed_at: str = Field(description="Instante del servidor (ISO-8601)")
    via: str = Field(description="device (biometria) o pin (fallback)")
    documents: list[SignedDocumentOut] = Field(min_length=3, max_length=3)


class SignContractResponse(BaseModel):
    """Envoltorio `docs/05#4` (`{"data", "meta"}`)."""

    data: SignContractData
    meta: dict = Field(default_factory=dict)


class ChallengeData(BaseModel):
    """Reto vigente (`nonce` de un solo uso, TTL 120 s)."""

    application_id: str
    nonce: str = Field(description="`nonce` a firmar con el dispositivo")
    expires_in: int = Field(description="Vigencia restante en segundos")
    status: str = Field(description="GENERATED")


class ChallengeResponse(BaseModel):
    """Envoltorio `docs/05#4` (`{"data", "meta"}`)."""

    data: ChallengeData
    meta: dict = Field(default_factory=dict)
