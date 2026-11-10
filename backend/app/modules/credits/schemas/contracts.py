"""Esquemas Pydantic del juego contractual (E3-T12, HU11).

`POST /credits/applications/{id}/contract` no lleva cuerpo (las
condiciones se copian del expediente aprobado + producto + motor);
la respuesta describe los tres documentos (`CONTRATO` + `PAGARE` +
`RESUMEN`, cada uno con su `doc_ref`/`doc_hash`).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ContractDocument",
    "ContractSetData",
    "ContractSetResponse",
]


class ContractDocument(BaseModel):
    """Metadatos de un documento del juego (sin contenido)."""

    model_config = ConfigDict(from_attributes=False)

    contract_id: str = Field(description="Fila `credits.contracts`")
    doc_type: str = Field(description="CONTRATO, PAGARE o RESUMEN")
    doc: str = Field(description="Nombre publico: contrato, pagare o resumen")
    doc_ref: str = Field(description="Ruta/almacen logica del documento")
    doc_hash: str = Field(description="SHA-256 del contenido canonico")
    status: str = Field(description="GENERATED (la firma es E3-T13; tras firmar, SIGNED)")


class ContractSetData(BaseModel):
    """Juego contractual de un expediente aprobado."""

    model_config = ConfigDict(from_attributes=False)

    application_id: str
    status: str = Field(description="GENERATED, SIGNED (todo el juego firmado) o PARTIAL")
    documents: list[ContractDocument] = Field(min_length=3, max_length=3)


class ContractSetResponse(BaseModel):
    """Envoltorio `docs/05#4` (`{"data", "meta"}`)."""

    data: ContractSetData
    meta: dict = Field(default_factory=dict)
