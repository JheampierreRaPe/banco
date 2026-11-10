"""Job de recordatorios de cuota proxima a vencer (E3-T19, HU12 CA-04).

Avisa por push las cuotas `PENDING`/`PARTIAL` de prestamos `ACTIVE`
con `due_date == hoy + loan.reminder_days_before` (parametro
configurable en `config.parameters` via la fachada `shared/config`,
default `3`; regla de oro 6). La notificacion viaja por la fachada
`notifications.send` (plantilla `loan_installment_reminder`, sin PII
ni cifras); este job **no** mueve dinero ni escribe tablas ajenas.

Gestion de transaccion (decision documentada):
- Un `commit` por item (el llamante pasa la sesion): cada aviso se
  confirma de forma independiente y el lote continua ante fallos.
- Idempotencia por `(schedule_id, dia)`: la marca vive en
  `processed_events` (consumidor `credits-installment-reminders`,
  `event_id` determinista UUID5, patron E2-T20) —sin migracion ni
  columnas nuevas (decision 2 del brief, opcion recomendada)—:
  re-ejecutar el mismo dia no repite el aviso. Si el envio falla, se
  revierte el item completo (marca incluida): el siguiente ciclo lo
  reintenta.
- Solo la ventana exacta (`due_date == hoy + N`): una cuota fuera de
  la ventana no genera aviso; al dia siguiente ya no cae en la
  ventana (aviso unico por cuota y ventana).

Sin PII ni secretos en logs (solo conteos); sin `float`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.modules.credits.repository.loans import LoanValidationError

logger = logging.getLogger(__name__)

#: Consumidor `processed_events` de los recordatorios (uno por cuota y dia).
REMINDERS_CONSUMER = "credits-installment-reminders"

#: Plantilla push del recordatorio (aditiva E3-T19, sin PII ni cifras).
REMINDER_TEMPLATE_CODE = "loan_installment_reminder"

#: Tope de cuotas por pasada (acota el lote; el siguiente ciclo retoma).
DEFAULT_LIMIT = 100


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _reminder_event_id(schedule_id: uuid.UUID | str, day: date) -> uuid.UUID:
    """`event_id` determinista del recordatorio (una vez por cuota y dia)."""
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"loan-installment-reminder:{schedule_id}:{day.isoformat()}"
    )


def run_installment_reminders_once(
    session: Session,
    *,
    today: date | datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    sender: Any | None = None,
) -> dict[str, Any]:
    """Una pasada de recordatorios: avisa las cuotas que vencen en la ventana.

    Retorna `{"notified": [...], "skipped": [...], "failed": [...],
    "days_before": N, "target": "<iso>", "as_of": "<iso>"}` (ids en
    texto). Cada item confirma con su propio `commit`; ante fallo se
    revierte solo ese item y el lote continua. `sender` inyecta el
    adaptador de notificaciones (tests sin envios reales).
    """
    from app.modules.credits.jobs.notify import send_push_best_effort
    from app.modules.credits.repository import loans as loans_repo
    from app.modules.shared.config import get_loan_reminder_days_before
    from app.modules.shared.repository.outbox import try_mark_processed

    day = today.date() if isinstance(today, datetime) else today
    if day is None:
        day = _utc_today()
    if isinstance(day, bool) or not isinstance(day, date):
        raise LoanValidationError("today debe ser date o None")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise LoanValidationError(f"limit debe ser entero > 0, recibido: {limit!r}")

    # Lectura best-effort con fallback (nunca lanza): default `3`.
    days = get_loan_reminder_days_before(session)
    target = day + timedelta(days=days)

    upcoming = loans_repo.list_upcoming_installments(session, target=target, limit=limit)
    notified: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []
    for loan_id, schedule_id, owner, due in upcoming:
        sid = str(schedule_id)
        try:
            claimed = try_mark_processed(
                session,
                event_id=_reminder_event_id(schedule_id, day),
                consumer=REMINDERS_CONSUMER,
            )
        except Exception as exc:  # noqa: BLE001 - sin marca no se avisa (reintenta manana)
            logger.warning("reminder mark_failed error=%s", type(exc).__name__)
            try:
                session.rollback()
            except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                pass
            failed.append({"schedule_id": sid, "error": type(exc).__name__})
            continue
        if not claimed:
            skipped.append(sid)
            continue
        try:
            sent = send_push_best_effort(
                session,
                owner=owner,
                template_code=REMINDER_TEMPLATE_CODE,
                data={"loan_ref": str(loan_id)[:8], "due_on": due.isoformat()},
                sender=sender,
            )
            if sent:
                session.commit()
            else:
                session.rollback()
        except Exception as exc:  # noqa: BLE001 - el aviso nunca rompe el lote
            logger.warning("reminder item fallo error=%s", type(exc).__name__)
            try:
                session.rollback()
            except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                pass
            failed.append({"schedule_id": sid, "error": type(exc).__name__})
            continue
        if sent:
            notified.append(sid)
        else:
            failed.append({"schedule_id": sid, "error": "NOTIFICATION_FAILED"})
    logger.info(
        "reminder pasada notified=%s skipped=%s failed=%s days_before=%s",
        len(notified),
        len(skipped),
        len(failed),
        days,
    )
    return {
        "notified": notified,
        "skipped": skipped,
        "failed": failed,
        "days_before": days,
        "target": target.isoformat(),
        "as_of": day.isoformat(),
    }


__all__ = [
    "DEFAULT_LIMIT",
    "REMINDERS_CONSUMER",
    "REMINDER_TEMPLATE_CODE",
    "run_installment_reminders_once",
]
