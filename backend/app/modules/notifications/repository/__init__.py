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

from app.modules.notifications.models import (
    Notification,
    NotificationTemplate,
    UserChannelPreference,
)

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


# --------------------------------------- preferencias por canal (E6-T12)


def get_preferences(session: Session, user_id: uuid.UUID | str) -> UserChannelPreference | None:
    """Preferencias del titular (`None` si aun no tiene fila)."""
    return session.get(UserChannelPreference, _coerce_user_id(user_id))


def upsert_preferences(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    push: bool,
    email: bool,
    sms: bool,
) -> UserChannelPreference:
    """Crea o reemplaza las preferencias del titular (`flush`, sin `commit`).

    Idempotente por `user_id` PK (repetir con los mismos valores no duplica;
    concurrencia = ultimo escritor gana). Los tres flags deben ser `bool`
    (`ValueError` en otro caso). Fija `updated_at`.
    """
    uid = _coerce_user_id(user_id)
    if uid is None:
        raise ValueError("user_id es obligatorio")
    for name, value in (("push", push), ("email", email), ("sms", sms)):
        if not isinstance(value, bool):
            raise TypeError(f"{name} debe ser bool, recibido: {value!r}")
    row = session.get(UserChannelPreference, uid)
    if row is None:
        row = UserChannelPreference(user_id=uid, push=push, email=email, sms=sms)
        session.add(row)
    else:
        row.push = push
        row.email = email
        row.sms = sms
    row.updated_at = _utcnow()
    session.flush()
    logger.info("notifications.preferences user_id=%s", uid)
    return row


# --------------------------------------- centro de notificaciones (E6-T12)


def list_user_notifications(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    channel: str | None = None,
    unread_only: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> list[Notification]:
    """Notificaciones del titular, mas recientes primero (solo lectura).

    Filtros: `channel` (`push`/`email`/`sms`; `ValueError` si es otro),
    `unread_only` (`read_at IS NULL`). Paginado con `limit` (1..100) y
    `offset` (>= 0). Jamas devuelve filas ajenas (el llamante pasa el
    `user_id` del JWT).
    """
    uid = _coerce_user_id(user_id)
    if uid is None:
        raise ValueError("user_id es obligatorio")
    if channel is not None and channel not in ("push", "email", "sms"):
        raise ValueError(f"channel debe ser push/email/sms, recibido: {channel!r}")
    if limit < 1 or limit > 100:
        raise ValueError("limit debe estar entre 1 y 100")
    if offset < 0:
        raise ValueError("offset debe ser >= 0")
    stmt = sa.select(Notification).where(Notification.user_id == uid)
    if channel is not None:
        stmt = stmt.where(Notification.channel == channel)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    stmt = stmt.order_by(Notification.created_at.desc(), Notification.id.desc())
    stmt = stmt.limit(limit).offset(offset)
    return list(session.scalars(stmt).all())


def count_user_notifications(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    channel: str | None = None,
    unread_only: bool = False,
) -> int:
    """Total del titular con los mismos filtros del listado (solo lectura)."""
    uid = _coerce_user_id(user_id)
    if uid is None:
        raise ValueError("user_id es obligatorio")
    if channel is not None and channel not in ("push", "email", "sms"):
        raise ValueError(f"channel debe ser push/email/sms, recibido: {channel!r}")
    stmt = sa.select(sa.func.count()).select_from(Notification).where(Notification.user_id == uid)
    if channel is not None:
        stmt = stmt.where(Notification.channel == channel)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    return int(session.scalar(stmt) or 0)


def mark_notification_read(
    session: Session,
    *,
    notification_id: uuid.UUID,
    user_id: uuid.UUID | str,
    now: datetime | None = None,
) -> Notification | None:
    """Fija `read_at` si la fila es del titular (`flush`, sin `commit`).

    Idempotente: si ya estaba leida retorna la fila sin cambios. `None` si
    no existe o es ajena (el llamante responde `404` neutro).
    """
    uid = _coerce_user_id(user_id)
    if uid is None:
        raise ValueError("user_id es obligatorio")
    row = session.get(Notification, notification_id)
    if row is None or row.user_id != uid:
        return None
    if row.read_at is None:
        row.read_at = now or _utcnow()
        session.flush()
    logger.info(
        "notifications.read id=%s channel=%s template=%s",
        row.id,
        row.channel,
        row.template_code,
    )
    return row
