"""Fachada `send` del modulo `notifications` (E1-T09, HU02 CA-01).

Estrategia de envio (documentada):
- **Envio directo via sender inyectable + reintentos con backoff en la
  fachada** (no outbox perezoso): la fila `notifications` ES el registro
  durable. `send` registra `QUEUED` en la sesion del llamante (misma
  transaccion del negocio: si el negocio falla, no hay notificacion
  huerfana), intenta el envio inmediato y deja `SENT` o `FAILED`.
- **Tolerancia a caida de un canal sin perder el mensaje**: el fallo de un
  proveedor solo marca esa fila (`FAILED` con `error`, o `QUEUED` si aun
  quedan intentos en un reimpulso posterior); los demas canales no se ven
  afectados y `retry_notification` reintenta despues la fila pendiente. Un
  worker periodico puede barrer `list_pending` + reimpulsar `FAILED`.
- Backoff exponencial `base * 2**(intento-1)` (mismo patron que
  `shared/repository/outbox.py`), con `sleep_fn` inyectable (tests sin espera).
- Sin PII en logs: solo id de notificacion, canal y codigo de plantilla.
  El `payload_json` persistido contiene solo los datos de render (el
  llamante no debe incluir PII sensible: el render logea unicamente el
  codigo de plantilla).

Contrato: `send(canal, destinatario, plantilla, datos)` == `send(channel,
recipient, template_code, data, ...)` (nombres en ingles por convencion del
repo). El `sender` se inyecta (por defecto `MockNotificationSender`); el
proveedor real solo implementa el Protocol sin tocar este modulo.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.adapters.notification_sender import (
    CHANNELS,
    MockNotificationSender,
    NotificationProviderError,
    NotificationSender,
)
from app.modules.notifications import repository as repo
from app.modules.notifications.domain.templates import BASE_TEMPLATES, render_template
from app.modules.notifications.models import Notification

logger = logging.getLogger(__name__)

# Reintentos: tras N intentos la fila pasa a FAILED (terminal hasta reimpulso).
DEFAULT_MAX_ATTEMPTS = 3
# Backoff exponencial base en segundos (CA-01 HU02: envio < 5 s en el caso feliz).
DEFAULT_BACKOFF_BASE_SECONDS = 1


def compute_backoff_seconds(attempts: int, base_seconds: int) -> int:
    """Backoff exponencial tras `attempts` fallos (>= 1). Dominio puro."""
    if attempts < 1:
        raise ValueError("attempts debe ser >= 1")
    if base_seconds < 0:
        raise ValueError("base_seconds debe ser >= 0")
    return base_seconds * (2 ** (attempts - 1))


def _resolve_template(session: Session, template_code: str) -> dict:
    """Plantilla desde BD (`notification_templates`) con fallback a semilla.

    Si la BD no tiene la plantilla pero es un codigo base conocido, se usa la
    semilla `BASE_TEMPLATES` (permite operar sin seed fisico). Plantilla
    deshabilitada o codigo desconocido -> `ValueError`.
    """
    if not template_code or not str(template_code).strip():
        raise ValueError("template_code es obligatorio")
    stored = repo.get_template(session, template_code)
    if stored is not None:
        if not stored.enabled:
            raise ValueError(f"plantilla deshabilitada: {template_code!r}")
        return {
            "code": stored.code,
            "channel": stored.channel,
            "subject": stored.subject,
            "body_template": stored.body_template,
        }
    if template_code in BASE_TEMPLATES:
        return dict(BASE_TEMPLATES[template_code])
    raise ValueError(f"plantilla desconocida: {template_code!r}")


def send(
    session: Session,
    *,
    channel: str,
    recipient: str,
    template_code: str,
    data: dict,
    user_id: uuid.UUID | str | None = None,
    sender: NotificationSender | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_seconds: int = DEFAULT_BACKOFF_BASE_SECONDS,
    sleep_fn=time.sleep,
    now: datetime | None = None,
) -> Notification:
    """Fachada `send(canal, destinatario, plantilla, datos)`.

    1. Resuelve y renderiza la plantilla (fallo de render -> `ValueError`
       ANTES de persistir: no deja filas residuales).
    2. Registra `QUEUED` (flush, sin commit: misma transaccion del llamante).
    3. Intenta el envio con `sender` (inyectable; mock por defecto) con
       reintentos + backoff; deja `SENT` (`provider_ref`, `sent_at`) o
       `FAILED` (`error` del proveedor, sin PII).
    """
    if channel not in CHANNELS:
        raise ValueError(f"channel debe ser uno de {CHANNELS}, recibido: {channel!r}")
    if not recipient or not str(recipient).strip():
        raise ValueError("recipient es obligatorio")
    if not isinstance(data, dict):
        raise TypeError("data debe ser dict")
    if max_attempts < 1:
        raise ValueError("max_attempts debe ser >= 1")
    sender = sender if sender is not None else MockNotificationSender()

    template = _resolve_template(session, template_code)
    if template["channel"] != channel:
        raise ValueError(
            f"plantilla {template_code!r} es de canal {template['channel']!r}, "
            f"recibido: {channel!r}"
        )
    body = render_template(template["body_template"], data)
    logger.info("notifications.render channel=%s template=%s", channel, template_code)

    row = repo.save_notification(
        session,
        channel=channel,
        template_code=template_code,
        # `recipient` se persiste (dato de entrega necesario para el
        # reintento posterior); nunca se incluye en logs.
        payload={"template": template_code, "recipient": str(recipient), "data": dict(data)},
        user_id=user_id,
    )
    _attempt_delivery(
        session,
        row,
        sender=sender,
        subject=template["subject"],
        body=body,
        recipient=str(recipient),
        max_attempts=max_attempts,
        backoff_base_seconds=backoff_base_seconds,
        sleep_fn=sleep_fn,
        now=now,
    )
    return row


def _attempt_delivery(
    session: Session,
    row: Notification,
    *,
    sender: NotificationSender,
    subject: str | None,
    body: str,
    recipient: str,
    max_attempts: int,
    backoff_base_seconds: int,
    sleep_fn,
    now: datetime | None,
) -> Notification:
    """Bucle de envio con backoff; actualiza `SENT`/`FAILED` (flush, sin commit)."""
    moment = now or datetime.now(UTC)
    last_error = "sin intentos"
    for attempt in range(1, max_attempts + 1):
        try:
            result = sender.send(
                channel=row.channel, recipient=recipient, subject=subject, body=body
            )
        except NotificationProviderError as exc:
            last_error = str(exc) or "provider caido"
        except (ValueError, TypeError):
            raise
        except Exception as exc:  # noqa: BLE001 - cualquier caida cuenta como intento
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if result.ok:
                return repo.mark_sent(
                    session, row.id, result.provider_ref or "mock-sin-ref", now=moment
                )
            last_error = result.error or "provider rechazo el envio"
        if attempt < max_attempts:
            delay = compute_backoff_seconds(attempt, backoff_base_seconds)
            if delay > 0:
                sleep_fn(delay)
    return repo.mark_failed(session, row.id, last_error)


def retry_notification(
    session: Session,
    notification_id: uuid.UUID,
    sender: NotificationSender | None = None,
    *,
    recipient: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_seconds: int = DEFAULT_BACKOFF_BASE_SECONDS,
    sleep_fn=time.sleep,
    now: datetime | None = None,
) -> Notification:
    """Reimpulsa una notificacion `QUEUED`/`FAILED` (reintento posterior).

    La fila nunca se pierde: tras la caida de un canal, este reintento la
    lleva a `SENT` cuando el proveedor se recupera. `SENT` ya enviada no se
    reenvia (idempotente por estado). Re-renderiza con el `payload` guardado;
    el destinatario sale del `payload` salvo override explicito.
    """
    sender = sender if sender is not None else MockNotificationSender()
    row = repo.get_notification(session, notification_id)
    if row is None:
        raise ValueError(f"notificacion inexistente: {notification_id}")
    if row.status == "SENT":
        return row
    if row.template_code is None:
        raise ValueError("notificacion sin plantilla: no reintentable")
    template = _resolve_template(session, row.template_code)
    stored = row.payload_json or {}
    data = stored.get("data", {}) if isinstance(stored, dict) else {}
    body = render_template(template["body_template"], data if isinstance(data, dict) else {})
    to = recipient if recipient is not None else (stored.get("recipient", "") or "")
    if not str(to).strip():
        raise ValueError("reintento sin destinatario")
    row.status = "QUEUED"
    row.error = None
    session.flush()
    return _attempt_delivery(
        session,
        row,
        sender=sender,
        subject=template["subject"],
        body=body,
        recipient=str(to),
        max_attempts=max_attempts,
        backoff_base_seconds=backoff_base_seconds,
        sleep_fn=sleep_fn,
        now=now,
    )
