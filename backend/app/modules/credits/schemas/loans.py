"""Esquemas Pydantic del desembolso y la lectura de prestamos (E3-T17, HU12).

Dinero entero en centimos (`int`), tasas como strings decimales
exactos; sin `float` (regla de oro 3). La cuenta solo viaja
enmascarada (`****dddd`, regla de oro 9); el `account_id` es UUID
logico (sin numero en claro en respuestas).
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class DisburseRequest(BaseModel):
    """Cuenta elegida para el desembolso (omitida = principal del titular)."""

    model_config = ConfigDict(extra="forbid")

    account_id: uuid.UUID | None = Field(default=None)


class DisburseData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loan_id: uuid.UUID
    application_id: uuid.UUID
    transaction_id: uuid.UUID
    status: str
    disbursed_at: str | None = None
    account_masked: str
    amount_minor: int
    currency: str


class DisburseMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    replayed: bool = False


class DisburseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: DisburseData
    meta: DisburseMeta


class LoanOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    application_id: uuid.UUID
    account_id: uuid.UUID
    principal_minor: int
    outstanding_principal_minor: int
    tcea: str
    term_months: int
    status: str
    disbursed_at: str | None = None
    auto_debit_enabled: bool
    created_at: str | None = None


class LoansListMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    request_id: str


class LoansListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[LoanOut]
    meta: LoansListMeta


class LoanScheduleRowOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    installment_no: int
    due_date: str
    principal_minor: int
    interest_minor: int
    insurance_minor: int
    total_minor: int
    status: str
    paid_at: str | None = None


class LoanScheduleData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loan: LoanOut
    schedule: list[LoanScheduleRowOut]


class LoanScheduleMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str


class LoanScheduleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: LoanScheduleData
    meta: LoanScheduleMeta


class PayInstallmentRequest(BaseModel):
    """Cuota a cobrar (omitida = proxima pendiente/vencida del prestamo)."""

    model_config = ConfigDict(extra="forbid")

    schedule_id: uuid.UUID | None = Field(default=None)


class PayInstallmentData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loan_id: uuid.UUID
    schedule_id: uuid.UUID
    transaction_id: uuid.UUID
    status: str
    amount_minor: int
    outstanding_principal_minor: int
    paid_at: str | None = None
    failure_code: str | None = None


class PayInstallmentMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    replayed: bool = False


class PayInstallmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: PayInstallmentData
    meta: PayInstallmentMeta


class AutoDebitRequest(BaseModel):
    """Interruptor del debito automatico (E3-T19, HU12 CA-03).

    `enabled` es booleano estricto (ni `1`/`0` ni texto: lo no
    booleano es `422`); no mueve dinero, por eso no exige
    `Idempotency-Key`.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


class AutoDebitData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loan_id: uuid.UUID
    auto_debit_enabled: bool


class AutoDebitMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str


class AutoDebitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: AutoDebitData
    meta: AutoDebitMeta


__all__ = [
    "AutoDebitData",
    "AutoDebitMeta",
    "AutoDebitRequest",
    "AutoDebitResponse",
    "DisburseData",
    "DisburseMeta",
    "DisburseRequest",
    "DisburseResponse",
    "LoanOut",
    "LoanScheduleData",
    "LoanScheduleMeta",
    "LoanScheduleResponse",
    "LoanScheduleRowOut",
    "LoansListMeta",
    "LoansListResponse",
    "PayInstallmentData",
    "PayInstallmentMeta",
    "PayInstallmentRequest",
    "PayInstallmentResponse",
]
