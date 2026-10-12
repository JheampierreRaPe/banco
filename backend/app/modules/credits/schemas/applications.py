"""Esquemas Pydantic de la solicitud de credito (E3-T08, HU10).

`ApplicationCreateRequest`: lo que envia el cliente para abrir un
expediente (`POST /credits/applications`). El consentimiento de bureau
es explicito (`bureau_authorized=true` obligatorio); el instante del
consentimiento (`bureau_authorized_at`) lo estampa el servidor al
evaluar (no se acepta del cliente: evita consentimientos fraguados).

Montos en centimos `int` (regla de oro 3: jamas `float`); `StrictInt`
y `StrictBool` rechazan `float`/`"1"` en el borde.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

__all__ = [
    "ApplicationCreateRequest",
    "ApplicationData",
    "ApplicationResponse",
    "ApplicationsListResponse",
    "OwnApplicationData",
    "ReviewRequest",
]


class ApplicationCreateRequest(BaseModel):
    """Solicitud de credito + autorizacion de bureau (HU10 CA-01/CA-02)."""

    model_config = ConfigDict(extra="forbid", strict=False)

    product_id: uuid.UUID = Field(description="Producto del catalogo")
    requested_amount_minor: StrictInt = Field(
        gt=0, le=1_000_000_000_000, description="Monto pedido en centimos"
    )
    term_months: StrictInt = Field(gt=0, le=360, description="Plazo en meses")
    total_income_minor: StrictInt = Field(
        gt=0, le=1_000_000_000_000, description="Ingreso total declarado en centimos"
    )
    total_debt_minor: StrictInt = Field(
        ge=0, le=1_000_000_000_000, description="Deuda total declarada en centimos"
    )
    bureau_authorized: StrictBool = Field(
        description="Consentimiento explicito de consulta al bureau (debe ser true)"
    )


class ReviewRequest(BaseModel):
    """Dictamen manual del analista sobre un expediente `IN_REVIEW`."""

    model_config = ConfigDict(extra="forbid", strict=False)

    decision: str = Field(
        pattern="^(APPROVED|REJECTED)$",
        description="Dictamen manual (solo APROBADO o RECHAZADO)",
    )
    reason: str = Field(min_length=1, max_length=2000, description="Justificacion del analista")


class ApplicationData(BaseModel):
    """Vista del expediente (estado visible, HU10 CA-04).

    `contract_signed` se deriva de `credits.contracts` (`True` solo si
    el expediente tiene filas y todas estan `SIGNED`): el estado del
    expediente no cambia con la firma, asi la pantalla de Estado
    distingue el aprobado-por-firmar del ya-firmado sin migracion.
    """

    model_config = ConfigDict(from_attributes=False)

    id: str
    user_id: str
    product_id: str
    requested_amount_minor: int
    term_months: int
    status: str
    contract_signed: bool = False
    bureau_authorized: bool
    bureau_authorized_at: str | None = None
    score: int | None = None
    dti_ratio: str | None = None
    total_income_minor: int | None = None
    total_debt_minor: int | None = None
    decision_reason: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    decided_at: str | None = None


class ApplicationResponse(BaseModel):
    """Envoltorio `docs/05#4` (`{"data", "meta"}`)."""

    data: ApplicationData
    meta: dict = Field(default_factory=dict)


class OwnApplicationData(BaseModel):
    """Vista minima del expediente para "Mis creditos" (HU10/HU11, E3-T23).

    Solo lo que la UI necesita: id, producto (`product_id` +
    `product_name`), monto en centimos (`int`, regla de oro 3), plazo,
    estado, `contract_signed` (derivado de `credits.contracts`: `True`
    solo si todas las filas del juego estan `SIGNED`; el estado del
    expediente no cambia con la firma), motivo y fechas. Sin
    ingresos/deuda/score/consentimiento (regla de oro 7).
    `extra="allow"` para que la misma envoltorio sirva a la bandeja
    del analista (expediente completo intacto).
    """

    model_config = ConfigDict(extra="allow")

    id: str
    product_id: str
    product_name: str | None = None
    requested_amount_minor: int
    term_months: int
    status: str
    contract_signed: bool = False
    decision_reason: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    decided_at: str | None = None


class ApplicationsListResponse(BaseModel):
    """Listado de expedientes (`{"data": [...], "meta": {total}}`, E3-T23).

    Doble modo sobre `GET /credits/applications`: sin `status` lista
    los expedientes **del titular** (vista minima `OwnApplicationData`:
    sin ingresos/deuda/score/documento); con `?status=IN_REVIEW` es la
    bandeja del analista (expediente completo: los extras se permiten
    y se serializan tal cual).
    """

    data: list[OwnApplicationData]
    meta: dict = Field(default_factory=dict)
