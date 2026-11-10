"""Fachada interna del catalogo `credits.loan_products` (E3-T02, HU09).

Punto de entrada estable para los consumidores (E3-T01 motor de calculo,
E3-T03 endpoint de simulacion): expone los productos habilitados con sus
tasas y topes como diccionarios planos (tasas `Decimal` exactas, dinero
en centimos enteros; sin `float`), sin exponer la sesion ni los modelos
ORM. Solo lectura del schema propio; sin `JOIN` entre schemas ni
imports de modelos ajenos (regla de oro 4).

Un cambio de tasa/tope en datos se refleja de inmediato (lectura
directa, sin cache en proceso): no requiere despliegue (regla de oro 6).

Errores: `NOT_FOUND` (producto inexistente), `VALIDATION_ERROR`
(selector ausente/duplicado o `product_id` malformado). Re-exporta los
errores del repositorio para los consumidores.

Evaluacion crediticia (E3-T07, HU10): `evaluate_application` une el motor
puro (`credits.domain.rules`) con los parametros vigentes (`loan.*` via
`shared/config`, sin cache). Solo lectura: no persiste ni emite eventos.

Expediente (E3-T08, HU10): `applications` (`submit_application`,
`review_application`, ...) orquesta catalogo + bureau + reglas,
persiste el dictamen, emite por `outbox`, audita y notifica.

Juego contractual (E3-T12, HU11): `documents` (`generate_contracts`,
`get_contract_set`, `download_contract_pdf`) copia las condiciones del
expediente aprobado + producto + motor, persiste las tres filas
`GENERATED` y emite `credit.contract.generated` por `outbox`.

Firma del juego (E3-T13, HU11): `signing` (`request_signature_challenge`,
`sign_contract` por biometria del dispositivo, `sign_contract_with_pin`
como fallback) verifica el factor por fachada de `identity` (D-03),
sella las filas (`SIGNED` + `signed_at` + `signature_token_hash`) y
emite `credit.contract.signed` por `outbox`. Sin router propio.

Firma idempotente + copia por correo (E3-T14, HU11): `contract_signing`
(`sign_contract_idempotent`) orquesta `signing` + `notifications.send`
(`channel="email"`, best-effort) con `Idempotency-Key` obligatoria.

Prestamo y cronograma definitivo (E3-T18, HU12): `loans`
(`create_loan`, `create_loan_with_schedule`, `create_schedules`,
`get_loan`, `list_loans_by_user`, `list_schedule`,
`next_due_installment`, `mark_installment_paid[_partial]`,
`record_payment`, `flag_overdue_installments`, `set_auto_debit`,
`set_own_auto_debit` (titular, lo expone `E3-T19`),
`update_outstanding_principal`, `link_contracts_to_loan`,
`build_definitive_schedule`) une el dominio puro con el repositorio
del mismo schema. Sin endpoints ni eventos: **no mueve dinero**.

Desembolso transaccional (E3-T17, HU12): `disbursement`
(`disburse_application_idempotent`, `list_own_loans`,
`get_own_schedule`) orquesta `loans` + el motor
`transactions.service.disbursement` en una sola transaccion, emite
`credit.disbursed` por `outbox` y audita best-effort.

Pago de cuota (E3-T20, HU12): `loan_payments`
(`pay_installment_idempotent`) cobra la cuota indicada (o la proxima
exigible) por el motor `transactions.service.loan_payment` en la
misma transaccion, registra `loan_payments`, marca la cuota `PAID`,
baja el saldo de capital (y cierra el prestamo a `PAID` si no quedan
cuotas) y emite `loan.installment.paid` por `outbox`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.modules.credits import repository as credits_repo
from app.modules.credits.models import LoanProduct
from app.modules.credits.repository import (
    LoanProductConflictError,
    LoanProductError,
    LoanProductNotFoundError,
    LoanProductValidationError,
)
from app.modules.credits.service.applications import (
    get_own_application,
    list_review_queue,
    review_application,
    submit_application,
    submit_application_idempotent,
)
from app.modules.credits.service.contract_signing import (
    EMAIL_TEMPLATE_CODE as SIGN_EMAIL_TEMPLATE_CODE,
)
from app.modules.credits.service.contract_signing import (
    IDEMPOTENCY_ENDPOINT as SIGN_IDEMPOTENCY_ENDPOINT,
)
from app.modules.credits.service.contract_signing import sign_contract_idempotent
from app.modules.credits.service.disbursement import (
    IDEMPOTENCY_ENDPOINT as DISBURSE_IDEMPOTENCY_ENDPOINT,
)
from app.modules.credits.service.disbursement import (
    disburse_application_idempotent,
    get_own_schedule,
    list_own_loans,
)
from app.modules.credits.service.documents import (
    download_contract_pdf,
    generate_contracts,
    generate_contracts_idempotent,
    get_contract_set,
)
from app.modules.credits.service.evaluation import evaluate_application
from app.modules.credits.service.loan_payments import (
    IDEMPOTENCY_ENDPOINT as PAY_IDEMPOTENCY_ENDPOINT,
)
from app.modules.credits.service.loan_payments import pay_installment_idempotent
from app.modules.credits.service.loans import (
    DefinitiveInstallment,
    Loan,
    LoanConflictError,
    LoanError,
    LoanNotFoundError,
    LoanPayment,
    LoanSchedule,
    LoanValidationError,
    ScheduleValidationError,
    build_definitive_schedule,
    create_loan,
    create_loan_with_schedule,
    create_schedules,
    first_due_date_for,
    flag_overdue_installments,
    get_loan,
    get_schedule_installment,
    link_contracts_to_loan,
    list_loans_by_user,
    list_schedule,
    mark_installment_paid,
    mark_installment_partial,
    next_due_installment,
    record_payment,
    set_auto_debit,
    set_own_auto_debit,
    update_outstanding_principal,
)
from app.modules.credits.service.signing import (
    ContractNonceExpiredError,
    ContractNotAuthorizedError,
    request_signature_challenge,
    sign_contract,
    sign_contract_with_pin,
)

__all__ = [
    "DISBURSE_IDEMPOTENCY_ENDPOINT",
    "PAY_IDEMPOTENCY_ENDPOINT",
    "SIGN_EMAIL_TEMPLATE_CODE",
    "SIGN_IDEMPOTENCY_ENDPOINT",
    "ContractNonceExpiredError",
    "ContractNotAuthorizedError",
    "DefinitiveInstallment",
    "Loan",
    "LoanConflictError",
    "LoanError",
    "LoanNotFoundError",
    "LoanPayment",
    "LoanProductConflictError",
    "LoanProductError",
    "LoanProductNotFoundError",
    "LoanProductValidationError",
    "LoanSchedule",
    "LoanValidationError",
    "ScheduleValidationError",
    "build_definitive_schedule",
    "create_loan",
    "create_loan_with_schedule",
    "create_schedules",
    "disburse_application_idempotent",
    "download_contract_pdf",
    "evaluate_application",
    "first_due_date_for",
    "flag_overdue_installments",
    "generate_contracts",
    "generate_contracts_idempotent",
    "get_contract_set",
    "get_loan",
    "get_own_application",
    "get_own_schedule",
    "get_product",
    "get_schedule_installment",
    "link_contracts_to_loan",
    "list_loans_by_user",
    "list_own_loans",
    "list_products",
    "list_review_queue",
    "list_schedule",
    "mark_installment_paid",
    "mark_installment_partial",
    "next_due_installment",
    "pay_installment_idempotent",
    "record_payment",
    "request_signature_challenge",
    "review_application",
    "set_auto_debit",
    "set_own_auto_debit",
    "sign_contract",
    "sign_contract_idempotent",
    "sign_contract_with_pin",
    "submit_application",
    "submit_application_idempotent",
    "to_dict",
    "update_outstanding_principal",
]


def to_dict(product: LoanProduct) -> dict[str, Any]:
    """Vista plana del producto (tasas `Decimal`, montos en centimos)."""
    return {
        "id": product.id,
        "code": product.code,
        "name": product.name,
        "tcea": product.tcea,
        "tea": product.tea,
        "insurance_rate": product.insurance_rate,
        "min_amount_minor": product.min_amount_minor,
        "max_amount_minor": product.max_amount_minor,
        "min_term_months": product.min_term_months,
        "max_term_months": product.max_term_months,
        "allowed_terms": (
            list(product.allowed_terms) if product.allowed_terms is not None else None
        ),
        "enabled": product.enabled,
    }


def list_products(session: Session, *, only_enabled: bool = True) -> list[dict[str, Any]]:
    """Catalogo ordenado por `code` (por defecto solo habilitados).

    Los deshabilitados no se listan a clientes; el admin puede pedirlos
    con `only_enabled=False`.
    """
    return [to_dict(row) for row in credits_repo.list_products(session, only_enabled=only_enabled)]


def get_product(
    session: Session,
    *,
    code: str | None = None,
    product_id: uuid.UUID | str | None = None,
) -> dict[str, Any]:
    """Un producto por `code` o por `product_id` (exactamente uno).

    Lanza `LoanProductValidationError` si faltan o sobran selectores (o
    si `product_id` no es UUID) y `LoanProductNotFoundError` si no existe.
    """
    if (code is None) == (product_id is None):
        raise LoanProductValidationError("get_product exige exactamente uno de code / product_id")
    row: LoanProduct | None
    if code is not None:
        if not isinstance(code, str) or not code.strip():
            raise LoanProductValidationError(f"code debe ser texto no vacio, recibido: {code!r}")
        row = credits_repo.get_by_code(session, code)
        label = f"code={code.strip()!r}"
    else:
        row = credits_repo.get_by_id(session, product_id)
        label = f"product_id={product_id!r}"
    if row is None:
        raise LoanProductNotFoundError(f"producto inexistente ({label})")
    if not isinstance(row.tcea, Decimal):
        raise LoanProductValidationError(f"tcea corrupta en {label}")  # pragma: no cover
    return to_dict(row)
