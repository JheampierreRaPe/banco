"""Job de recordatorios de vencimiento de servicios (E4-T18, HU16 CA-03).

Recorre las `service_subscriptions` `ACTIVE` y, cuando el vencimiento
derivado cae en la ventana (`hoy + N`), avisa al titular por push
(plantilla `service_reminder`) de forma **idempotente** (una vez por
suscripcion y dia). **No mueve dinero**; la notificacion viaja por la
fachada `notifications.send` (best-effort, sin PII ni cifras).

Origen del vencimiento (decision 1 del brief, documentada):
`service_subscriptions` (`docs/03b#95`, migracion `0042`) no tiene
columna de fecha de vencimiento (solo `notify_days_before`) y el
adaptador `ServiceProviderGateway` (`E4-T16`) tampoco expone fecha de
vencimiento (solo `valid`/`amount_due_minor`/`holder_masked`). Agregar
una columna (`next_due_at`) excede la migracion `0043` (solo siembra
el parametro, patron `0036`) y el proveedor real aun no existe, asi
que el MVP deriva el vencimiento de forma pura y determinista:

- `subscription_due_day(subscription_id)`: dia del mes `1..28`
  derivado del `subscription_id` (entero del UUID mod 28 + 1,
  estable entre pasadas; `1..28` existe en todos los meses).
- `next_due_date(subscription_id, today)`: proxima ocurrencia de ese
  dia `>= today` (si el dia del mes en curso ya paso, el mismo dia
  del mes siguiente).

El proveedor real (fuera del MVP) reemplazara `next_due_date` por la
fecha del proveedor sin tocar el resto del job (ventana,
idempotencia, envio). La ventana efectiva por suscripcion es el
minimo entre el parametro global
`wallet.service_reminder_days_before` (default 3, `shared/config`,
semilla `0043`) y su `notify_days_before` (1..30, default 3
coherente con HU16): el titular puede adelantar su aviso y el global
acota la ventana maxima; cambiar el global cambia el resultado sin
despliegue (regla de oro 6). Solo la ventana exacta (`due == hoy +
N`) genera aviso: fuera de ventana no hay aviso y al dia siguiente
la misma suscripcion ya no cae en la ventana (aviso unico por
ventana).

Gestion de transaccion (patron `E3-T19`): el llamante pasa la sesion
y cada item confirma con su propio `commit`; el fallo de un item
revierte solo ese item y el lote continua. Idempotencia por
`(subscription_id, dia)` en `processed_events` (consumidor
`wallet-service-reminders`, `event_id` determinista UUID5, sin
migracion ni columnas nuevas, patron E2-T20/E3-T19): re-ejecutar el
mismo dia no repite el aviso. La marca se confirma al reclamarla
(semantica del helper `shared.try_mark_processed`, igual que en
`E3-T19`): si el envio falla despues, el item queda `failed` sin
romper la pasada y sin duplicar en reintentos (best-effort; el aviso
de esa ventana exacta se pierde, como en `E3-T19`).

Sin PII ni secretos en logs (solo conteos); sin `float`.
"""

from __future__ import annotations

import calendar
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Consumidor `processed_events` de los recordatorios (uno por suscripcion y dia).
REMINDERS_CONSUMER = "wallet-service-reminders"

#: Plantilla push del recordatorio (aditiva E4-T18, sin PII ni cifras).
REMINDER_TEMPLATE_CODE = "service_reminder"

#: Tope de suscripciones por pasada (acota el lote; el siguiente ciclo retoma).
DEFAULT_LIMIT = 100


def subscription_due_day(subscription_id: uuid.UUID | str) -> int:
    """Dia del mes `1..28` determinista de la suscripcion (puro)."""
    sid = (
        subscription_id
        if isinstance(subscription_id, uuid.UUID)
        else uuid.UUID(str(subscription_id))
    )
    return (sid.int % 28) + 1


def next_due_date(subscription_id: uuid.UUID | str, today: date) -> date:
    """Proxima ocurrencia del dia de vencimiento `>= today` (puro)."""
    day = subscription_due_day(subscription_id)
    last_this = calendar.monthrange(today.year, today.month)[1]
    candidate = date(today.year, today.month, min(day, last_this))
    if candidate >= today:
        return candidate
    year = today.year + (1 if today.month == 12 else 0)
    month = 1 if today.month == 12 else today.month + 1
    last_next = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_next))


def _reminder_event_id(subscription_id: uuid.UUID | str, day: date) -> uuid.UUID:
    """`event_id` determinista del recordatorio (una vez por suscripcion y dia)."""
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"wallet-service-reminder:{subscription_id}:{day.isoformat()}"
    )


def _coerce_day(now: Any) -> date:
    if now is None:
        return datetime.now(UTC).date()
    if isinstance(now, datetime):
        return now.date()
    if isinstance(now, date) and not isinstance(now, bool):
        return now
    raise ValueError("now debe ser date/datetime o None")


def _effective_days(global_days: int, row: Any) -> int:
    """Ventana efectiva de la suscripcion (dias, entero `>= 1`).

    Minimo entre el parametro global y el `notify_days_before` de la
    fila (si es valido): el titular puede adelantar su aviso y el
    global acota la ventana maxima. Fila sin valor valido -> global.
    """
    raw = getattr(row, "notify_days_before", None)
    if isinstance(raw, bool):
        return global_days
    if isinstance(raw, int) and 1 <= raw <= 30:
        return min(global_days, raw)
    return global_days


def run_service_reminders_once(
    session: Session,
    *,
    now: date | datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    sender: Any | None = None,
) -> dict[str, Any]:
    """Una pasada de recordatorios: avisa las suscripciones que vencen en la ventana.

    Retorna `{"notified": [...], "skipped": [...], "failed": [...],
    "days_before": N, "as_of": "<iso>"}` (ids en texto). Cada item
    confirma con su propio `commit`; ante fallo se revierte solo ese
    item y el lote continua. `sender` inyecta el adaptador de
    notificaciones (tests sin envios reales).
    """
    from app.modules.shared.config import get_wallet_service_reminder_days_before
    from app.modules.shared.repository.outbox import try_mark_processed
    from app.modules.wallet.repository import services as services_repo
    from app.modules.wallet.service.services import notify_service_reminder

    day = _coerce_day(now)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError(f"limit debe ser entero > 0, recibido: {limit!r}")

    # Lectura best-effort con fallback (nunca lanza): default `3`.
    days = get_wallet_service_reminder_days_before(session)

    candidates = services_repo.list_active_subscriptions(session, limit=limit)
    notified: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []
    for row in candidates:
        sid = str(row.id)
        try:
            window = _effective_days(days, row)
            due = next_due_date(row.id, day)
        except Exception as exc:  # noqa: BLE001 - fila ilegible: se omite sin romper el lote
            logger.warning("reminder skip error=%s", type(exc).__name__)
            try:
                session.rollback()
            except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                pass
            skipped.append(sid)
            continue
        if due != day + timedelta(days=window):
            skipped.append(sid)
            continue
        try:
            claimed = try_mark_processed(
                session,
                event_id=_reminder_event_id(row.id, day),
                consumer=REMINDERS_CONSUMER,
            )
        except Exception as exc:  # noqa: BLE001 - sin marca no se avisa (reintenta manana)
            logger.warning("reminder mark_failed error=%s", type(exc).__name__)
            try:
                session.rollback()
            except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                pass
            failed.append({"subscription_id": sid, "error": type(exc).__name__})
            continue
        if not claimed:
            skipped.append(sid)
            continue
        try:
            notify_service_reminder(
                session,
                owner=row.user_id,
                subscription_id=row.id,
                due_on=due.isoformat(),
                sender=sender,
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 - el aviso nunca rompe el lote
            logger.warning("reminder item fallo error=%s", type(exc).__name__)
            try:
                session.rollback()
            except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                pass
            failed.append({"subscription_id": sid, "error": type(exc).__name__})
            continue
        notified.append(sid)
    logger.info(
        "service-reminder pasada notified=%s skipped=%s failed=%s days_before=%s",
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
        "as_of": day.isoformat(),
    }


__all__ = [
    "DEFAULT_LIMIT",
    "REMINDERS_CONSUMER",
    "REMINDER_TEMPLATE_CODE",
    "next_due_date",
    "run_service_reminders_once",
    "subscription_due_day",
]
