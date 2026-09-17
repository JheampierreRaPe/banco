"""Esquemas Pydantic de lectura de cuentas (E2-T02, HU05 CA-01/CA-02).

Fuente: `docs/05-contratos-api.md#62` (GET /accounts, GET /accounts/{id}) y
formato de respuesta `05#4` (`{"data", "meta"}`); columnas segun
`docs/03b-diccionario-de-datos.md#51` (`accounts`, `account_balances`).

Reglas:
- Dinero entero en centimos (`*_minor: int`); nunca `float` (regla de oro 3).
- El numero de cuenta jamas viaja completo: solo `account_number_masked`
  (`****` + ultimos 4). No existe campo con el numero en claro.
- `balance_minor` (contable) = `available_minor + held_minor` (lo calcula el
  `service/` leyendo la proyeccion; aqui solo se transporta).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


class AccountSummary(BaseModel):
    """Cuenta del consolidado GET /accounts (05#6.2)."""

    id: uuid.UUID
    account_number_masked: str = Field(
        ..., pattern=r"^\*{4}.{4}$", description="Enmascarado: **** + ultimos 4"
    )
    type: str
    currency: str
    available_minor: int = Field(..., ge=0)
    held_minor: int = Field(..., ge=0)
    balance_minor: int = Field(..., ge=0, description="Contable = disponible + retenido")


class AccountDetail(AccountSummary):
    """Detalle GET /accounts/{id}: consolidado + estado de la cuenta."""

    status: str


class AccountsListResponse(BaseModel):
    """Envoltorio de lista segun 05#4 (`data` + `meta` paginada simple)."""

    data: list[AccountSummary]
    meta: dict = Field(default_factory=dict)


class AccountDetailResponse(BaseModel):
    """Envoltorio de detalle segun 05#4 (`data` + `meta`)."""

    data: AccountDetail
    meta: dict = Field(default_factory=dict)


class Movement(BaseModel):
    """Movimiento de `movements_view` (E2-T03, HU05 CA-03/CA-04).

    Columnas exactas de `03b#5.4`. Dinero entero en centimos
    (`amount_minor > 0`); nunca `float`. Sin numeros de cuenta en claro
    (solo `account_id` UUID; el enmascaramiento de E2-T02 no aplica aqui
    porque no viaja ningun numero).
    """

    journal_entry_id: uuid.UUID
    transaction_id: uuid.UUID | None = None
    account_id: uuid.UUID
    direction: str = Field(..., pattern=r"^(DEBIT|CREDIT)$")
    amount_minor: int = Field(..., ge=1)
    currency: str
    description: str | None = None
    value_date: date | None = None
    created_at: datetime | None = None


class MovementsListResponse(BaseModel):
    """Envoltorio paginado segun 05#4 (`data` + `meta.page/page_size/total`)."""

    data: list[Movement]
    meta: dict = Field(default_factory=dict)


__all__ = [
    "AccountDetail",
    "AccountDetailResponse",
    "AccountSummary",
    "AccountsListResponse",
    "Movement",
    "MovementsListResponse",
]
