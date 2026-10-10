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
    """Vista del expediente (estado visible, HU10 CA-04)."""

    model_config = ConfigDict(from_attributes=False)

    id: str
    user_id: str
    product_id: str
    requested_amount_minor: int
    term_months: int
    status: str
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


class ApplicationsListResponse(BaseModel):
    """Bandeja del analista (`{"data": [...], "meta": {total}}`)."""

    data: list[ApplicationData]
    meta: dict = Field(default_factory=dict)
