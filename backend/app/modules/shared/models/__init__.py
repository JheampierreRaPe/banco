"""Modelos ORM del schema `shared` (E5-T05 + E5-T04, HU17).

Fuente: `docs/03b-diccionario-de-datos.md#22-outbox`, `#23-processed_events`
y `#21-idempotency_keys`.
Frontera: solo `shared.outbox` + `shared.processed_events` + `shared.idempotency_keys`.
Sin FK a otros schemas (las referencias cruzadas son UUID logicos, sin constraint).

`payload` / `response_snapshot` usan `sa.JSON()` (generico) para que los tests
corran en SQLite; las migraciones Postgres usan `JSONB` segun `03b`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

SCHEMA = "shared"

# `docs/03b-diccionario-de-datos.md#1`: enum `outbox_status`.
OUTBOX_STATUSES = ("PENDING", "PUBLISHED", "FAILED")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OutboxEntry(Base):
    """Evento pendiente de publicacion (`03b#2.2`).

    Se inserta en la misma transaccion del cambio de negocio (`record`)
    y un worker lo publica despues (regla de oro 8: nunca publicar dentro
    de la transaccion de negocio). `attempts` + `available_at` implementan
    reintentos con backoff; `FAILED` es terminal tras N intentos.
    """

    __tablename__ = "outbox"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('PENDING', 'PUBLISHED', 'FAILED')",
            name="ck_outbox_status",
        ),
        sa.Index("ix_outbox_event_type", "event_type"),
        sa.Index("ix_outbox_aggregate_id", "aggregate_id"),
        sa.Index("ix_outbox_status", "status"),
        sa.Index("ix_outbox_available_at", "available_at"),
        sa.Index("ix_outbox_status_available_at", "status", "available_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(sa.String(60), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    payload: Mapped[dict] = mapped_column(sa.JSON(), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(12), nullable=False, default="PENDING", server_default="PENDING"
    )
    attempts: Mapped[int] = mapped_column(
        sa.SmallInteger(), nullable=False, default=0, server_default="0"
    )
    available_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class ProcessedEvent(Base):
    """Registro de consumo idempotente (`03b#2.3`, inbox).

    PK compuesta (`event_id`, `consumer`): el segundo consumo del mismo
    evento por el mismo consumidor se ignora (insert idempotente).
    """

    __tablename__ = "processed_events"
    __table_args__ = ({"schema": SCHEMA},)

    event_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    consumer: Mapped[str] = mapped_column(sa.String(60), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )


class IdempotencyKey(Base):
    """Clave de idempotencia (`03b#2.1`, E5-T04, HU17 CA-03).

    Aditivo: no toca `OutboxEntry` ni `ProcessedEvent` (E5-T05 intacto).
    Una fila por (`key`, `user_id`): la repeticion exacta (misma clave +
    mismo `request_hash` SHA-256 del cuerpo canonico) devuelve
    `response_snapshot` sin re-ejecutar; misma clave con distinto cuerpo
    es `409`; la fila vencida (`expires_at` pasado) se trata como nueva.

    `user_id` / `transaction_id` son UUID logicos sin FK fisica a otros
    schemas (regla de modularidad de `03b`). `user_id` es nulo para
    servicios tecnicos.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        sa.UniqueConstraint("key", "user_id", name="uq_idempotency_key_user"),
        sa.Index("ix_idempotency_created_at", "created_at"),
        sa.Index("ix_idempotency_expires_at", "expires_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    endpoint: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    method: Mapped[str] = mapped_column(sa.String(10), nullable=False)
    request_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    response_snapshot: Mapped[dict | None] = mapped_column(sa.JSON(), nullable=True)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
