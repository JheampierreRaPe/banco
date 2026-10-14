"""Bus de eventos de dominio (patron outbox).

Solo constantes y la interfaz; la implementacion vive en la tarea E5-T05.
Nunca publicar eventos directamente dentro de la transaccion de negocio.
"""

# Eventos de dominio iniciales. Ver docs/02-arquitectura.md seccion 7.
KYC_COMPLETED = "kyc.completed"
USER_ACTIVATED = "user.activated"
ACCOUNT_CREATED = "account.created"
TRANSACTION_INITIATED = "transaction.initiated"
FUNDS_HELD = "funds.held"
FUNDS_RELEASED = "funds.released"
LEDGER_ENTRY_POSTED = "ledger.entry.posted"
TRANSFER_SETTLED = "transfer.settled"
FRAUD_ALERT_RAISED = "fraud.alert.raised"
# Intento fallido de autorizacion biometrica (E2-T17 emisor via outbox,
# E2-T20 consumidor en `risk` + notificacion; mismo nombre que
# `identity.service.device_login.AUDIT_FAILED_ATTEMPT`).
AUTH_FAILED_ATTEMPT = "auth.failed_attempt"
# Reporte "no reconozco esta operacion" (E6-T14, HU22): lo emite
# `notifications` por `outbox` al reportar el titular (payload sin PII:
# `user_id`, `notification_id`, `reference`, `at`); lo consume `risk`
# (severidad + auditoria + `fraud.alert.raised`).
SECURITY_OPERATION_REPORTED = "security.operation_reported"
CREDIT_DISBURSED = "credit.disbursed"
# Cuota de prestamo pagada (E3-T20, HU12; `docs/18`): se emite por
# `outbox` al liquidar (payload sin PII: `loan_id`, `schedule_id`,
# `user_id`, `transaction_id`, `amount_minor`, `currency`).
LOAN_INSTALLMENT_PAID = "loan.installment.paid"
# Expediente de credito (E3-T08, HU10; cierra `docs/18` G25): el
# dictamen se emite por `outbox` con payload sin PII
# (`application_id`, `user_id`, `product_id`, `status`).
CREDIT_APPLICATION_SUBMITTED = "credit.application.submitted"
CREDIT_APPROVED = "credit.approved"
CREDIT_REJECTED = "credit.rejected"
CREDIT_REVIEW_REQUIRED = "credit.review.required"
# Juego contractual generado (E3-T12, HU11; `docs/18`): un evento por
# expediente via `outbox` (payload sin PII: `contract_ids`,
# `application_id`, `user_id`, `status`).
CREDIT_CONTRACT_GENERATED = "credit.contract.generated"
# Juego contractual firmado (E3-T13, HU11; `docs/18`): al sellar via
# `outbox` (payload sin PII: `contract_ids`, `application_id`,
# `user_id`, `status=SIGNED`, `signed_at`).
CREDIT_CONTRACT_SIGNED = "credit.contract.signed"
QR_PAYMENT_CONFIRMED = "qr.payment.confirmed"
# Pago de servicio liquidado (E4-T17, HU16 CA-04; `docs/18` G73): se
# emite por `outbox` al liquidar (payload sin PII: `service_payment_id`,
# `biller_id`, `transaction_id`, `amount_minor`, `currency`, `status`).
SERVICE_PAID = "service.paid"
# Recarga de celular liquidada (E4-T17, HU16 CA-02/CA-04): se emite por
# `outbox` al liquidar (payload sin PII: `topup_id`, `operator`,
# `transaction_id`, `amount_minor`, `currency`, `status`; sin telefono).
TOPUP_COMPLETED = "topup.completed"
# Vinculo de billetera creado/reactivado (E4-T01, HU13 CA-01): se emite
# por `outbox` al vincular (payload sin PII: `wallet_id`, `link_id`,
# `type` = ACCOUNT|CARD).
WALLET_LINK_CREATED = "wallet.link.created"
# Cargo QR generado (E4-T06, HU14; `docs/18` G68/G69): se emite por
# `outbox` al generar (payload sin PII: `charge_id`, `merchant_id`,
# `amount_minor`, `currency`, `mode`, `expires_at`).
QR_CHARGE_CREATED = "qr.charge.created"
RECONCILIATION_EXCEPTION = "reconciliation.exception"
