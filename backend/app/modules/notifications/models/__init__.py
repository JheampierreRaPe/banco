"""Modelos ORM del schema `notifications` (E1-T09, HU02).

Fuente: `docs/03b-diccionario-de-datos.md#13-schema-notifications` (columnas
exactas) e indices `docs/03b#15` (`(user_id, created_at DESC)`, `(status)`).
Frontera: solo `notifications` + `notification_templates` +
`user_channel_preferences` (E6-T12, HU22: la fachada de seguridad lee las
preferencias por canal; el respaldo `email` de seguridad se garantiza aunque
el usuario lo desactive, decision del dueno documentada en el servicio).

- `payload_json` usa `sa.JSON()` (generico) para que los tests corran en
  SQLite; la migracion Postgres usa `JSONB` segun `03b`.
- Sin FK a otros schemas: `user_id` es UUID logico (REF `identity.users`
  solo a nivel aplicacion, regla de oro 4). `template_code` es codigo
  logico sin FK fisica para no acoplar el registro al ciclo de plantillas.
- Estados: `QUEUED` / `SENT` / `FAILED` (CK). Canales: `push` / `email` /
  `sms` (CK).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

SCHEMA = "notifications"

CHANNELS = ("push", "email", "sms")
STATUSES = ("QUEUED", "SENT", "FAILED")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NotificationTemplate(Base):
    """Plantilla versionable por codigo (`03b#13`)."""

    __tablename__ = "notification_templates"
    __table_args__ = (
        sa.UniqueConstraint("code", name="uq_notification_templates_code"),
        sa.CheckConstraint(
            "channel IN ('push', 'email', 'sms')",
            name="ck_notification_templates_channel",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    channel: Mapped[str] = mapped_column(sa.String(10), nullable=False)
    subject: Mapped[str | None] = mapped_column(sa.String(150), nullable=True)
    body_template: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=True, server_default="true"
    )


class Notification(Base):
    """Registro de entrega (`03b#13`): la fila es la garantia de no perdida.

    Ciclo: `QUEUED` (registrado) -> `SENT` (proveedor acepto, con
    `provider_ref` y `sent_at`) o `FAILED` (agotados los reintentos, con
    `error`). Un worker posterior puede reimpulsar `QUEUED`/`FAILED` via
    `retry_notification` (estrategia documentada en `service/`).
    """

    __tablename__ = "notifications"
    __table_args__ = (
        sa.CheckConstraint(
            "channel IN ('push', 'email', 'sms')",
            name="ck_notifications_channel",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'SENT', 'FAILED')",
            name="ck_notifications_status",
        ),
        sa.Index("ix_notifications_status", "status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    channel: Mapped[str] = mapped_column(sa.String(10), nullable=False)
    template_code: Mapped[str | None] = mapped_column(sa.String(50), nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(sa.JSON(), nullable=True)
    status: Mapped[str] = mapped_column(
        sa.String(12), nullable=False, default="QUEUED", server_default="QUEUED"
    )
    provider_ref: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


# Indice `03b#15` exacto: `(user_id, created_at DESC)` (se declara fuera de
# `__table_args__` porque necesita la columna ordenada descendente).
sa.Index(
    "ix_notifications_user_created",
    Notification.user_id,
    Notification.created_at.desc(),
)


class UserChannelPreference(Base):
    """Preferencias de canal por titular (`03b#13`, E6-T12, HU22).

    `user_id` es PK logica (REF `identity.users` solo a nivel aplicacion,
    regla de oro 4: sin FK fisica entre schemas). Las alertas de seguridad
    respetan `push`, garantizan `email` como respaldo (decision del dueno)
    y nunca usan `sms` en la ruta de seguridad.
    """

    __tablename__ = "user_channel_preferences"
    __table_args__ = ({"schema": SCHEMA},)

    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    push: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=True, server_default="true"
    )
    email: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=True, server_default="true"
    )
    sms: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default="false"
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )
