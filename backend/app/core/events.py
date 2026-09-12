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
CREDIT_DISBURSED = "credit.disbursed"
QR_PAYMENT_CONFIRMED = "qr.payment.confirmed"
RECONCILIATION_EXCEPTION = "reconciliation.exception"
