"""Jobs programados de cuotas del prestamo (E3-T19, HU12 CA-03/CA-04).

- `auto_debit.run_auto_debit_once`: cobra las cuotas vencidas/hoy de
  los prestamos con `auto_debit_enabled` reutilizando el caso de uso
  de `E3-T20` (el dinero viaja por el motor; el job no debita por su
  cuenta).
- `installment_reminders.run_installment_reminders_once`: avisa por
  push las cuotas que vencen en `loan.reminder_days_before` dias.

Ambos son idempotentes y re-ejecutables (`commit` por item,
idempotencia por cuota/dia via el motor y `processed_events`) y se
registran en el worker via `app.composition.run_credit_loan_jobs_once`
(env `CREDIT_LOAN_JOBS_ENABLED`, periodicidad
`CREDIT_LOANS_JOBS_INTERVAL_SECONDS`).
"""

from app.modules.credits.jobs.auto_debit import (
    AUTO_DEBIT_CONSUMER,
    AUTO_DEBIT_KEY_PREFIX,
    FAILED_TEMPLATE_CODE,
    run_auto_debit_once,
)
from app.modules.credits.jobs.installment_reminders import (
    REMINDER_TEMPLATE_CODE,
    REMINDERS_CONSUMER,
    run_installment_reminders_once,
)
from app.modules.credits.jobs.notify import send_push_best_effort

__all__ = [
    "AUTO_DEBIT_CONSUMER",
    "AUTO_DEBIT_KEY_PREFIX",
    "FAILED_TEMPLATE_CODE",
    "REMINDERS_CONSUMER",
    "REMINDER_TEMPLATE_CODE",
    "run_auto_debit_once",
    "run_installment_reminders_once",
    "send_push_best_effort",
]
