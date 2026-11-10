"""Bitacora append-only con hash encadenado (`audit.audit_log`, E1-T04).

Fuente: `docs/03b-diccionario-de-datos.md#141-audit_log-append-only` e
indices `docs/03b#15` (`(seq)` UQ, `(entity_type, entity_id)`,
`(created_at)`, `(action)`); `docs/modules/README.md#audit` (dueno de
`audit_log`; prohibido modificar/eliminar registros).

Frontera: solo tablas propias (schema `audit`). Sin FK fisicas hacia otros
schemas (regla de oro 4): `actor_id`/`entity_id` son UUID logicos. Dinero no
aplica; nunca `float`. Sin PII en logs mas alla de los IDs tecnicos.

Desviaciones documentadas de `03b#14.1` (solo portabilidad SQLite/Postgres,
sin cambio semantico):
- `ip`: `sa.String(45)` portable con variante `postgresql.INET()` en
  Postgres (DDL `INET` segun `03b` y binds tipados `INET`: la insercion
  funciona en ambos motores; en SQLite queda `VARCHAR(45)`). Sin la
  variante, el ORM enviaba `::VARCHAR` contra la columna `INET` y Postgres
  (psycopg) rechazaba todo `INSERT` en `audit_log` (`DatatypeMismatch`).
- `seq`: la fachada `audit.service.record` lo asigna como `max(seq)+1`
  (determinista en SQLite y Postgres); la migracion usa `BIGSERIAL` segun
  `03b` y acepta el valor explicito (la fachada siempre lo fija, nunca NULL).
- JSON: `sa.JSON()` generico en el modelo (SQLite en pruebas), `JSONB` en
  la migracion Postgres (igual que `notifications`/`shared`).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

SCHEMA = "audit"

ACTOR_TYPES = ("USER", "SYSTEM", "SERVICE")


class AuditLog(Base):
    """Evento de auditoria append-only con hash encadenado (`03b#14.1`).

    `prev_hash` es el `hash` del registro anterior (`None` en el genesis);
    `hash` es SHA-256 hex de los campos canonicos (lo calcula la fachada
    `record`, nunca el llamante). Sin `UPDATE`/`DELETE`: el repositorio solo
    expone lectura y la fachada solo inserta (regla de oro 2).
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        sa.UniqueConstraint("seq", name="uq_audit_log_seq"),
        sa.CheckConstraint(
            "actor_type IN ('USER', 'SYSTEM', 'SERVICE')",
            name="ck_audit_log_actor_type",
        ),
        sa.Index("ix_audit_log_entity", "entity_type", "entity_id"),
        sa.Index("ix_audit_log_created", "created_at"),
        sa.Index("ix_audit_log_action", "action"),
        sa.Index("ix_audit_log_actor", "actor_id"),
        sa.Index("ix_audit_log_request", "request_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    seq: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    actor_type: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    action: Mapped[str] = mapped_column(sa.String(60), nullable=False)
    entity_type: Mapped[str] = mapped_column(sa.String(60), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    before_json: Mapped[dict | None] = mapped_column(sa.JSON(), nullable=True)
    after_json: Mapped[dict | None] = mapped_column(sa.JSON(), nullable=True)
    ip: Mapped[str | None] = mapped_column(
        sa.String(45).with_variant(postgresql.INET(), "postgresql"), nullable=True
    )
    device_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    request_id: Mapped[str | None] = mapped_column(sa.String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    prev_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)


__all__ = [
    "ACTOR_TYPES",
    "SCHEMA",
    "AuditLog",
]
