"""Consumidor `auth.failed_attempt` -> alerta de seguridad (E6-T12, HU22).

Contrato D-04 (mismo que `accounts.events` E2-T23 y `risk.events` E2-T20):
el bus `app.core.bus` entrega **solo el `entry`** (registro de
`shared.outbox`, sin sesion). `on_auth_failed_attempt(entry)` abre **su
propia sesion** con `commit` propio, deriva `event_id = entry.id`,
reclama idempotencia en `processed_events` (consumidor
`"notifications-security"`: duplicado -> `False` sin efectos) y delega
en la fachada `notify_security_event` (push + correo en paralelo,
respetando preferencias con respaldo `email` garantizado).

Mapeo del payload (el evento ya trae lo necesario): `user_id`,
`device` (`device_id`/`device`), `at` (`at`/`occurred_at`/`created_at`),
`location` (`location`/`city`/`region`, aproximada) y `reference`
(`reference`/`authorization_id`/`transaction_id`, o el `event_id`).

NOTA de cableado (A1, cierra `docs/18` G80): la suscripcion al bus vive
en el arranque (`composition.subscribe_security_alerts`, llamado desde
`main.lifespan`). `on_auth_failed_attempt` solo envia el correo de
respaldo (`include_push=False`) para no duplicar el push que ya emite
el consumidor de `E2-T20` sobre el mismo evento; `on_sensitive_business_event`
cubre las operaciones sensibles (push + correo).

Sin imports a modelos ajenos: solo dicts del payload por evento/fachada
(regla de oro 4). Sin PII ni secretos en logs. No mueve dinero (regla de
oro 1). Ante fallo se revierte y se re-lanza para que el worker aplique
reintento con backoff (diseno E5-T05).

Cableado A1 (HU22 CA-01 E2E): `app.composition.subscribe_security_alerts`
suscribe `on_auth_failed_attempt` (solo correo de respaldo: el push de
login lo emite `risk`/E2-T20, asi queda exactamente UN push por evento)
y `on_sensitive_business_event` a cada operacion sensible
(`transfer.settled`, `credit.disbursed`, `loan.installment.paid`,
`qr.payment.confirmed`, `service.paid`, `topup.completed`), que invoca
`notify_security_event` (push + correo con dispositivo/hora/ubicacion;
fallback aproximado cuando el evento no los trae). Excepcion E3-T24:
`credit.disbursed` se marca procesado SIN alerta generica (su
notificacion dedicada `credit_disbursed` push+email ya cubre el evento;
ver `DEDICATED_ONLY_EVENTS`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.core import events as domain_events

logger = logging.getLogger(__name__)

#: Consumidor idempotente en `shared.processed_events` (<= 60 caracteres).
SECURITY_CONSUMER = "notifications-security"

#: Operaciones sensibles que disparan push + correo (A1, HU22 CA-01).
#: Todas viajan por `outbox` con `user_id` en el payload (los emisores lo
#: aportan: `transactions`/`credits`/`wallet`; sin importar sus modelos
#: aqui, regla de oro 4).
SENSITIVE_BUSINESS_EVENTS = (
    "transfer.settled",
    "credit.disbursed",
    "loan.installment.paid",
    "qr.payment.confirmed",
    "service.paid",
    "topup.completed",
)

#: Eventos con notificacion dedicada que SUSTITUYE a la alerta generica
#: (E3-T24, HU12, decision del dueno): `credit.disbursed` ya genera la
#: notificacion transaccional `credit_disbursed` (push) +
#: `credit_disbursed_email` (email) en el propio caso de uso de
#: `credits` (monto + cuenta enmascarada + referencia, accionable). La
#: generica solo aportaria la etiqueta ("Desembolso de crédito"): enviar
#: ambas seria 2 push + 2 correos por el mismo evento, ruido que entierra
#: la transaccional. `on_sensitive_business_event` marca estos eventos
#: como procesados SIN alerta (el cableado A1 y `credit.disbursed` en el
#: `outbox` quedan intactos).
DEDICATED_ONLY_EVENTS = ("credit.disbursed",)


def _first_present(payload: dict, *keys: str) -> Any | None:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _email_candidate(payload: dict, *keys: str) -> str | None:
    """Email aportado por el emisor (`None` si ausente: rige el resolutor)."""
    found = _first_present(payload, *keys)
    if found is None:
        return None
    text = str(found).strip()
    return text if text else None


def on_auth_failed_attempt(
    entry: Any,
    *,
    session_factory: Callable[[], Any] | None = None,
    sender_push: Any | None = None,
    sender_email: Any | None = None,
) -> bool:
    """Handler del bus para `auth.failed_attempt` (D-04, E6-T12/A1).

    Solo el CORREO de respaldo (`include_push=False`): el push de login
    lo emite el consumidor de `risk` (E2-T20) sobre el mismo evento, y
    emitir otro push aqui duplicaria el aviso. Ambos consumen el mismo
    `entry.id` con consumidores distintos (`risk-auth-failures` y
    `notifications-security`): exactamente UN push + UN correo por
    evento de login. Resto identico al contrato D-04 (sesion/`commit`
    propios, idempotencia, rollback + re-lanza ante fallo).
    """
    from app.modules.notifications import service as notifications_service
    from app.modules.shared.repository import outbox as outbox_repo

    if session_factory is None:
        from app.core.db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    payload = dict(entry.payload or {})
    event_id = entry.id
    session = session_factory()
    try:
        if outbox_repo.is_processed(session, event_id=event_id, consumer=SECURITY_CONSUMER):
            logger.info(
                "notifications.security.duplicado event=%s", domain_events.AUTH_FAILED_ATTEMPT
            )
            return False
        user_id = _first_present(payload, "user_id")
        if user_id is None:
            raise ValueError("auth.failed_attempt sin user_id")
        notifications_service.notify_security_event(
            session,
            user_id=user_id,
            event_type=domain_events.AUTH_FAILED_ATTEMPT,
            reference=str(
                _first_present(payload, "reference", "authorization_id", "transaction_id")
                or event_id
            ),
            device=_first_present(payload, "device", "device_id", "device_label"),
            at=_first_present(payload, "at", "occurred_at", "created_at"),
            location=_first_present(payload, "location", "city", "region"),
            sender_push=sender_push,
            sender_email=sender_email,
            include_push=False,
        )
        outbox_repo.try_mark_processed(session, event_id=event_id, consumer=SECURITY_CONSUMER)
        session.commit()
        logger.info("notifications.security.consumido event=%s", domain_events.AUTH_FAILED_ATTEMPT)
        return True
    except Exception:
        try:
            session.rollback()
        except Exception:  # noqa: BLE001, S110 - limpieza best-effort
            pass
        raise
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001, S110 - limpieza best-effort
            pass


__all__ = [
    "SECURITY_CONSUMER",
    "SENSITIVE_BUSINESS_EVENTS",
    "on_auth_failed_attempt",
    "on_sensitive_business_event",
]


def on_sensitive_business_event(
    entry: Any,
    *,
    session_factory: Callable[[], Any] | None = None,
    sender_push: Any | None = None,
    sender_email: Any | None = None,
) -> bool:
    """Handler del bus para operaciones sensibles (D-04, A1/HU22 CA-01).

    Un handler para los seis eventos de `SENSITIVE_BUSINESS_EVENTS`:
    abre su propia sesion (`session_factory` o `SessionLocal`), reclama
    idempotencia (`processed_events`, consumidor `SECURITY_CONSUMER`:
    duplicado -> `False` sin efectos) y delega en
    `notify_security_event` (push + correo en paralelo con dispositivo,
    hora y ubicacion; el fallback aproximado de la fachada cubre los
    eventos sin dispositivo/ubicacion). `commit` propio si alerta;
    `rollback` y re-lanza ante fallo (el worker reintenta). Los senders
    se inyectan (tests sin red).

    Sin `user_id` en el payload no hay alerta (se marca procesado sin
    efectos, `True`: reintentarlo nunca lo conseguiria y envenenaria al
    worker). Sin PII ni secretos en logs. No mueve dinero.
    """
    from app.modules.notifications import service as notifications_service
    from app.modules.shared.repository import outbox as outbox_repo

    if session_factory is None:
        from app.core.db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    payload = dict(entry.payload or {})
    event_id = entry.id
    event_type = str(getattr(entry, "event_type", "") or "")
    session = session_factory()
    try:
        if outbox_repo.is_processed(session, event_id=event_id, consumer=SECURITY_CONSUMER):
            logger.info("notifications.security.duplicado event=%s", event_type)
            return False
        if event_type in DEDICATED_ONLY_EVENTS:
            # E3-T24: `credit.disbursed` ya notifico por su via dedicada
            # (`credit_disbursed` push+email, con monto + cuenta
            # enmascarada + referencia). Se marca procesado sin la alerta
            # generica para no duplicar (2 push + 2 correos por evento).
            outbox_repo.try_mark_processed(session, event_id=event_id, consumer=SECURITY_CONSUMER)
            session.commit()
            logger.info("notifications.security.dedicada event=%s: sin alerta generica", event_type)
            return True
        user_id = _first_present(payload, "user_id", "payer_user_id")
        if user_id is None:
            outbox_repo.try_mark_processed(session, event_id=event_id, consumer=SECURITY_CONSUMER)
            session.commit()
            logger.warning("notifications.security sin user_id event=%s: omitido", event_type)
            return True
        notifications_service.notify_security_event(
            session,
            user_id=user_id,
            event_type=event_type or "sensitive.operation",
            reference=str(
                _first_present(
                    payload,
                    "reference",
                    "transaction_id",
                    "qr_payment_id",
                    "service_payment_id",
                    "topup_id",
                    "loan_id",
                    "charge_id",
                )
                or event_id
            ),
            device=_first_present(payload, "device", "device_id", "device_label"),
            at=_first_present(payload, "at", "occurred_at", "created_at"),
            location=_first_present(payload, "location", "city", "region"),
            recipient_email=(_email_candidate(payload, "recipient_email", "email")),
            sender_push=sender_push,
            sender_email=sender_email,
        )
        outbox_repo.try_mark_processed(session, event_id=event_id, consumer=SECURITY_CONSUMER)
        session.commit()
        logger.info("notifications.security.consumido event=%s", event_type)
        return True
    except Exception:
        try:
            session.rollback()
        except Exception:  # noqa: BLE001, S110 - limpieza best-effort
            pass
        raise
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001, S110 - limpieza best-effort
            pass
