"""Eventos de dominio del expediente de credito (E3-T08, HU10).

Nombres registrados en `docs/18` (cierra G25): se emiten por `outbox`
(regla de oro 8), nunca dentro de la transaccion de negocio de forma
directa. Payloads sin PII: solo `application_id`, `user_id`,
`product_id` y `status` (sin montos, score, ingresos ni razones con
cifras).
"""

CREDIT_APPLICATION_SUBMITTED = "credit.application.submitted"
CREDIT_APPROVED = "credit.approved"
CREDIT_REJECTED = "credit.rejected"
CREDIT_REVIEW_REQUIRED = "credit.review.required"
# Juego contractual generado (E3-T12, HU11; `docs/18`): se emite por
# `outbox` una vez por expediente (payload sin PII: solo
# `contract_ids`, `application_id`, `user_id` y `status`).
CREDIT_CONTRACT_GENERATED = "credit.contract.generated"
# Juego contractual firmado (E3-T13, HU11; `docs/18`): se emite por
# `outbox` al sellar (payload sin PII: `contract_ids`,
# `application_id`, `user_id`, `status=SIGNED` y `signed_at`).
CREDIT_CONTRACT_SIGNED = "credit.contract.signed"
# Desembolso del prestamo (E3-T17, HU12; `docs/18`): se emite por
# `outbox` al liquidar (payload sin PII: `loan_id`,
# `application_id`, `user_id`, `transaction_id`, `amount_minor`,
# `currency`).
CREDIT_DISBURSED = "credit.disbursed"
# Cuota pagada (E3-T20, HU12; `docs/18`): se emite por `outbox` al
# liquidar (payload sin PII: `loan_id`, `schedule_id`, `user_id`,
# `transaction_id`, `amount_minor`, `currency`).
LOAN_INSTALLMENT_PAID = "loan.installment.paid"

__all__ = [
    "CREDIT_APPLICATION_SUBMITTED",
    "CREDIT_APPROVED",
    "CREDIT_CONTRACT_GENERATED",
    "CREDIT_CONTRACT_SIGNED",
    "CREDIT_DISBURSED",
    "CREDIT_REJECTED",
    "CREDIT_REVIEW_REQUIRED",
    "LOAN_INSTALLMENT_PAID",
]
