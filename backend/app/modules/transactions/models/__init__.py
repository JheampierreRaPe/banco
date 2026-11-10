"""Modelos ORM del schema `transactions` (E5-T02, HU17).

Fuente: `docs/03b-diccionario-de-datos.md#6-schema-transactions`.
Frontera: solo tablas propias (`transactions`, `transaction_status_history`,
`holds`). Sin FK a otros schemas (referencias logicas por UUID); sin
acceso a `ledger` ni `accounts`.

Reglas de oro aplicadas:
- Dinero en centimos (`BIGINT`), nunca `float`.
- `amount_minor > 0`, `fee_minor >= 0`.
- Append-only en historial: sin UPDATE/DELETE desde el repositorio.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

SCHEMA = "transactions"

# JSONB portable: en Postgres usa JSONB; en SQLite (pruebas sin Docker) usa JSON.
JSONB = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


class HoldStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    CAPTURED = "CAPTURED"
    EXPIRED = "EXPIRED"


class Transaction(Base):
    """Cabecera de la operacion (`03b#6.1`)."""

    __tablename__ = "transactions"
    __table_args__ = (
        sa.CheckConstraint("amount_minor > 0", name="ck_transactions_amount_positive"),
        sa.CheckConstraint("fee_minor >= 0", name="ck_transactions_fee_non_negative"),
        sa.Index("ix_transactions_idempotency_key", "idempotency_key"),
        sa.Index("ix_transactions_status_created", "status", "created_at"),
        sa.Index("ix_transactions_source_account", "source_account_id"),
        sa.Index("ix_transactions_external_ref", "external_ref"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(sa.String(30), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(25), nullable=False, default="INITIATED")
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(80), nullable=True)
    # REF logicas (sin FK fisica): otro schema.
    initiator_user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    source_account_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    target_account_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(sa.String(80), nullable=True)
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    fee_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False, default=0)
    risk_level: Mapped[str] = mapped_column(sa.String(15), nullable=False, default="LOW")
    risk_score: Mapped[int | None] = mapped_column(sa.SmallInteger(), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    # Columna DB `metadata`; atributo `meta` (evita choque con Base.metadata).
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    version: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )
    settled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    history: Mapped[list[TransactionStatusHistory]] = relationship(
        back_populates="transaction",
        order_by="TransactionStatusHistory.created_at",
        lazy="selectin",
    )
    holds: Mapped[list[Hold]] = relationship(back_populates="transaction", lazy="selectin")


class TransactionStatusHistory(Base):
    """Historial append-only (`03b#6.2`). Sin UPDATE/DELETE."""

    __tablename__ = "transaction_status_history"
    __table_args__ = (
        sa.Index("ix_tx_history_tx_created", "transaction_id", "created_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(),
        sa.ForeignKey(f"{SCHEMA}.transactions.id"),
        nullable=False,
    )
    from_status: Mapped[str | None] = mapped_column(sa.String(25), nullable=True)
    to_status: Mapped[str] = mapped_column(sa.String(25), nullable=False)
    reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    actor_type: Mapped[str] = mapped_column(sa.String(20), nullable=False, default="SYSTEM")
    actor_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    transaction: Mapped[Transaction] = relationship(back_populates="history")


class Hold(Base):
    """Retencion de fondos (`03b#6.3`). `account_id` es REF logica."""

    __tablename__ = "holds"
    __table_args__ = (
        sa.CheckConstraint("amount_minor > 0", name="ck_holds_amount_positive"),
        sa.Index("ix_holds_status_expires", "status", "expires_at"),
        sa.Index("ix_holds_account", "account_id"),
        sa.Index("ix_holds_transaction", "transaction_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(),
        sa.ForeignKey(f"{SCHEMA}.transactions.id"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default=HoldStatus.ACTIVE.value
    )
    held_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    transaction: Mapped[Transaction] = relationship(back_populates="holds")


__all__ = [
    "SCHEMA",
    "Hold",
    "HoldStatus",
    "Transaction",
    "TransactionStatusHistory",
]
