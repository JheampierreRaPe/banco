"""Repositorio del schema `notifications` (E1-T09, HU02).

Capa de datos sin endpoints ni logica de negocio: solo SQLAlchemy sobre el
schema propio. Convencion (patron `shared/repository/outbox.py`): las
funciones hacen `flush` y no `commit`; quien llama decide la transaccion.
Asi el registro `QUEUED` se persiste junto al cambio de negocio que lo origina
y un rollback lo revierte tambien (no hay notificaciones huerfanas).

Sin PII en logs: solo IDs y codigos de plantilla.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.notifications.models import Notification, NotificationTemplate

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_user_id(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"user_id debe ser UUID, recibido: {value!r}") from exc


def save_notification(
    session: Session,
    *,
    channel: str,
    template_code: str | None,
    payload: dict | None,
    user_id: uuid.UUID | str | None = None,
) -> Notification:
    """Registra una notificacion en `QUEUED` con `flush` sin `commit`."""
    if channel not in ("push", "email", "sms"):
        raise ValueError(f"channel debe ser push/email/sms, recibido: {channel!r}")
    if payload is not None and not isinstance(payload, dict):
        raise TypeError("payload debe ser dict o None")
    row = Notification(
        user_id=_coerce_user_id(user_id),
        channel=channel,
        template_code=template_code,
        payload_json=dict(payload) if payload is not None else None,
        status="QUEUED",
    )
    session.add(row)
    session.flush()
    logger.info(
        "notifications.queued id=%s channel=%s template=%s",
        row.id,
        channel,
        template_code,
    )
    return row


def get_notification(session: Session, notification_id: uuid.UUID) -> Notification | None:
    """Devuelve la notificacion por id (`None` si no existe)."""
    return session.get(Notification, notification_id)


def mark_sent(
    session: Session,
    notification_id: uuid.UUID,
    provider_ref: str,
    *,
    now: datetime | None = None,
) -> Notification | None:
    """Marca `SENT` con `provider_ref` y `sent_at`. `flush` sin `commit`."""
    row = session.get(Notification, notification_id)
    if row is None:
        return None
    row.status = "SENT"
    row.provider_ref = provider_ref
    row.error = None
    row.sent_at = now or _utcnow()
    session.flush()
    logger.info(
        "notifications.sent id=%s channel=%s template=%s",
        row.id,
        row.channel,
        row.template_code,
    )
    return row


def mark_failed(
    session: Session,
    notification_id: uuid.UUID,
    error: str,
    *,
    now: datetime | None = None,
) -> Notification | None:
    """Marca `FAILED` con `error` (sin PII: solo motivo del proveedor).

    `flush` sin `commit`. `None` si no existe.
    """
    _ = now  # `FAILED` no fija `sent_at`; se acepta `now` por simetria.
    row = session.get(Notification, notification_id)
    if row is None:
        return None
    row.status = "FAILED"
    row.error = str(error)[:500]
    session.flush()
    logger.warning(
        "notifications.failed id=%s channel=%s template=%s",
        row.id,
        row.channel,
        row.template_code,
    )
    return row


def list_pending(session: Session, *, limit: int = 100) -> list[Notification]:
    """Notificaciones `QUEUED` (y `FAILED` reimpulsables) en orden de llegada.

    Orden: (`created_at`, `id`). El worker las reclama con `retry_notification`.
    """
    if limit < 1:
        raise ValueError("limit debe ser >= 1")
    stmt = (
        sa.select(Notification)
        .where(Notification.status == "QUEUED")
        .order_by(Notification.created_at, Notification.id)
        .limit(limit)
    )
    return list(session.scalars(stmt).all())


def upsert_template(
    session: Session,
    *,
    code: str,
    channel: str,
    subject: str | None,
    body_template: str,
    enabled: bool = True,
) -> NotificationTemplate:
    """Crea o actualiza una plantilla por `code`. `flush` sin `commit`."""
    if not code or not str(code).strip():
        raise ValueError("code es obligatorio")
    if channel not in ("push", "email", "sms"):
        raise ValueError(f"channel debe ser push/email/sms, recibido: {channel!r}")
    if not body_template or not str(body_template).strip():
        raise ValueError("body_template es obligatorio")
    existing = session.scalar(
        sa.select(NotificationTemplate).where(NotificationTemplate.code == code)
    )
    if existing is None:
        row = NotificationTemplate(
            code=code,
            channel=channel,
            subject=subject,
            body_template=body_template,
            enabled=enabled,
        )
        session.add(row)
    else:
        existing.channel = channel
        existing.subject = subject
        existing.body_template = body_template
        existing.enabled = enabled
        row = existing
    session.flush()
    return row


def get_template(session: Session, code: str) -> NotificationTemplate | None:
    """Devuelve la plantilla `code` (`None` si no existe)."""
    return session.scalar(sa.select(NotificationTemplate).where(NotificationTemplate.code == code))
