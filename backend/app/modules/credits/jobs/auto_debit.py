"""Job de debito automatico de cuotas (E3-T19, HU12 CA-03).

Para las cuotas `PENDING`/`PARTIAL`/`OVERDUE` con `due_date <= hoy` de
prestamos `ACTIVE` con `auto_debit_enabled`, cobra cada cuota
reutilizando el caso de uso de `E3-T20`
(`credits.service.loan_payments.pay_installment_idempotent`): el
dinero viaja por el motor (`Transaction LOAN_INSTALLMENT`, asiento
`docs/04#7.6`); este job **no** debita ni asienta por su cuenta
(regla de oro 1) y **no** escribe tablas de
`transactions`/`ledger`/`accounts` (regla de oro 4).

Gestion de transaccion (decision documentada):
- Un `commit` por item (el llamante pasa la sesion): cada cuota se
  cobra y confirma de forma independiente; el fallo de una no
  revierte las demas y el lote continua.
- Idempotencia por cuota: la clave de idempotencia HTTP es
  determinista por cuota (`auto-debit:<schedule_id>`) y la del motor
  va anclada a la cuota (`loan-installment:<schedule_id>`, E3-T20):
  re-ejecutar no duplica cobros (la cuota `PAID` se excluye de la
  seleccion y el replay devuelve el snapshot).
- Sin fondos (`REJECTED` + `INSUFFICIENT_FUNDS`): la cuota y los
  saldos quedan intactos y se avisa al titular con
  `loan_auto_debit_failed` (best-effort), como maximo una vez por
  cuota y dia (marca en `processed_events`, consumidor
  `credits-auto-debit`): re-ejecutar el mismo dia no repite el aviso.
- Cuota ya `PAID` por otro canal entre la seleccion y el cobro
  (`CONFLICT`) -> `skipped` sin aviso (nada que cobrar).

Sin PII ni secretos en logs (solo conteos y estados); dinero entero
en centimos, sin `float`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.modules.credits.repository.loans import (
    LoanConflictError,
    LoanError,
    LoanValidationError,
)

logger = logging.getLogger(__name__)

#: Prefijo de la clave de idempotencia HTTP del job (determinista por cuota).
AUTO_DEBIT_KEY_PREFIX = "auto-debit"

#: Consumidor `processed_events` de los avisos de fallo (uno por cuota y dia).
AUTO_DEBIT_CONSUMER = "credits-auto-debit"

#: Plantilla push del fallo de cobro (aditiva E3-T19, sin PII ni cifras).
FAILED_TEMPLATE_CODE = "loan_auto_debit_failed"

#: Tope de cuotas por pasada (acota el lote; el siguiente ciclo retoma).
DEFAULT_LIMIT = 100


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _job_key_for(schedule_id: uuid.UUID | str) -> str:
    """Clave de idempotencia HTTP del job para una cuota (estable y unica)."""
    return f"{AUTO_DEBIT_KEY_PREFIX}:{schedule_id}"


def _failure_event_id(schedule_id: uuid.UUID | str, day: date) -> uuid.UUID:
    """`event_id` determinista del aviso de fallo (una vez por cuota y dia)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"loan-auto-debit-failed:{schedule_id}:{day.isoformat()}")


def _notify_failure(
    session: Session,
    *,
    loan_id: uuid.UUID,
    schedule_id: uuid.UUID,
    owner: uuid.UUID,
    day: date,
    sender: Any | None = None,
) -> bool:
    """Avisa el fallo de cobro una sola vez por cuota y dia.

    Reclama la marca en `processed_events` (si ya estaba, otro ciclo
    aviso hoy: no se repite) y envia el push best-effort. Retorna
    `True` si el aviso quedo registrado (pendiente de `commit` del
    llamante); `False` si ya se aviso hoy o el envio fallo.
    """
    from app.modules.credits.jobs.notify import send_push_best_effort
    from app.modules.shared.repository.outbox import try_mark_processed

    try:
        claimed = try_mark_processed(
            session,
            event_id=_failure_event_id(schedule_id, day),
            consumer=AUTO_DEBIT_CONSUMER,
        )
    except Exception as exc:  # noqa: BLE001 - sin marca no se avisa (reintenta manana)
        logger.warning("auto-debit mark_failed error=%s", type(exc).__name__)
        return False
    if not claimed:
        return False
    return send_push_best_effort(
        session,
        owner=owner,
        template_code=FAILED_TEMPLATE_CODE,
        data={"loan_ref": str(loan_id)[:8], "at": day.isoformat()},
        sender=sender,
    )


def run_auto_debit_once(
    session: Session,
    *,
    today: date | datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    sender: Any | None = None,
) -> dict[str, Any]:
    """Una pasada del debito automatico: cobra las cuotas vencidas/hoy.

    Retorna `{"paid": [...], "failed": [...], "skipped": [...],
    "as_of": "<iso>"}` (ids en texto). Cada item confirma con su
    propio `commit`; ante fallo se revierte solo ese item y el lote
    continua. `sender` inyecta el adaptador de notificaciones (tests
    sin envios reales).
    """
    from app.modules.credits.repository import loans as loans_repo
    from app.modules.credits.repository.applications import (
        ApplicationConflictError,
        ApplicationNotFoundError,
        ApplicationValidationError,
        IdempotencyKeyConflictError,
    )
    from app.modules.credits.service import loan_payments as pay_service

    day = today.date() if isinstance(today, datetime) else today
    if day is None:
        day = _utc_today()
    if isinstance(day, bool) or not isinstance(day, date):
        raise LoanValidationError("today debe ser date o None")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise LoanValidationError(f"limit debe ser entero > 0, recibido: {limit!r}")

    dues = loans_repo.list_auto_debit_dues(session, as_of=day, limit=limit)
    paid: list[str] = []
    failed: list[dict[str, str]] = []
    skipped: list[str] = []
    for loan_id, schedule_id, owner in dues:
        sid = str(schedule_id)
        try:
            view, _replayed = pay_service.pay_installment_idempotent(
                session,
                owner,
                loan_id,
                schedule_id=schedule_id,
                idempotency_key=_job_key_for(schedule_id),
            )
        except LoanConflictError:
            # Carrera resuelta por otro canal (cuota `PAID` o prestamo
            # ya no `ACTIVE`): nada que cobrar, sin aviso.
            try:
                session.rollback()
            except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                pass
            skipped.append(sid)
            continue
        except (
            LoanError,
            ApplicationNotFoundError,
            ApplicationValidationError,
            ApplicationConflictError,
            IdempotencyKeyConflictError,
        ) as exc:
            # Fallo de negocio (cuenta inexistente/inactiva, cuota no
            # exigible...): sin dinero movido; avisa una vez por dia.
            try:
                session.rollback()
            except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                pass
            try:
                notified = _notify_failure(
                    session,
                    loan_id=loan_id,
                    schedule_id=schedule_id,
                    owner=owner,
                    day=day,
                    sender=sender,
                )
                if notified:
                    session.commit()
                else:
                    session.rollback()
            except Exception:  # noqa: BLE001 - el aviso nunca rompe el lote
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                    pass
                notified = False
            failed.append(
                {"schedule_id": sid, "error": type(exc).__name__, "notified": bool(notified)}
            )
            continue
        except Exception as exc:  # noqa: BLE001 - fallo tecnico: el lote continua
            logger.warning("auto-debit item fallo error=%s", type(exc).__name__)
            try:
                session.rollback()
            except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                pass
            failed.append({"schedule_id": sid, "error": type(exc).__name__, "notified": False})
            continue

        if str(view.get("status")) == "REJECTED":
            # Sin fondos: cuota y saldos intactos (E3-T20); avisa una
            # vez por dia. Si el aviso falla, se revierte el item
            # completo (incluido el snapshot del rechazo: el motor lo
            # reconstruye idempotente en el siguiente ciclo).
            failure = str(view.get("failure_code") or "INSUFFICIENT_FUNDS")
            try:
                notified = _notify_failure(
                    session,
                    loan_id=loan_id,
                    schedule_id=schedule_id,
                    owner=owner,
                    day=day,
                    sender=sender,
                )
                if notified:
                    session.commit()
                else:
                    session.rollback()
            except Exception:  # noqa: BLE001 - el aviso nunca rompe el lote
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                    pass
                notified = False
            failed.append({"schedule_id": sid, "error": failure, "notified": bool(notified)})
            continue

        try:
            session.commit()
        except Exception as exc:  # noqa: BLE001 - el cobro quedo en duda: se reporta
            logger.warning("auto-debit commit fallo error=%s", type(exc).__name__)
            try:
                session.rollback()
            except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                pass
            failed.append({"schedule_id": sid, "error": type(exc).__name__, "notified": False})
            continue
        paid.append(sid)
    logger.info(
        "auto-debit pasada paid=%s failed=%s skipped=%s", len(paid), len(failed), len(skipped)
    )
    return {"paid": paid, "failed": failed, "skipped": skipped, "as_of": day.isoformat()}


__all__ = [
    "AUTO_DEBIT_CONSUMER",
    "AUTO_DEBIT_KEY_PREFIX",
    "DEFAULT_LIMIT",
    "FAILED_TEMPLATE_CODE",
    "run_auto_debit_once",
]
