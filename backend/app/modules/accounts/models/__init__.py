"""Modelos ORM del schema `accounts` (E2-T01, HU05 CA-01/CA-02).

Fuente: `docs/03b-diccionario-de-datos.md#5-schema-accounts`,
`docs/03c-modelo-er.md#3-modulo-accounts`.
Frontera: solo tablas propias (`accounts`, `account_balances`,
`daily_balance_snapshots`). Sin FK a otros schemas: `user_id`,
`ledger_account_id` y `ledger_hold_account_id` son UUID logicos (REF),
sin constraint fisico (regla de oro 4 y `02#5.2`).

Reglas aplicadas:
- Dinero en centimos (`BIGINT`), nunca `float`.
- `available_minor >= 0` y `held_minor >= 0` (CK, `03b#16.1`).
- `account_number` unico (UQ).
- Invariante documentada (`03c#15.1`): `available_minor + held_minor`
  coincide con la suma de `postings` del ledger; esta tabla es
  proyeccion (refleja, no inventa) y el ledger es fuente de verdad.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

SCHEMA = "accounts"

ACCOUNT_TYPES = ("AHORRO", "CORRIENTE", "WALLET", "POCKET", "MULTICURRENCY")
ACCOUNT_STATUSES = ("ACTIVE", "BLOCKED", "CLOSED")


class Account(Base):
    """Cuenta de cliente (`03b#5.1`). Agregado raiz de `AccountBalance`."""

    __tablename__ = "accounts"
    __table_args__ = (
        sa.UniqueConstraint("account_number", name="uq_accounts_account_number"),
        sa.CheckConstraint(
            "type IN ('AHORRO', 'CORRIENTE', 'WALLET', 'POCKET', 'MULTICURRENCY')",
            name="ck_accounts_type",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'BLOCKED', 'CLOSED')",
            name="ck_accounts_status",
        ),
        sa.Index("ix_accounts_user_id", "user_id"),
        sa.Index("ix_accounts_status", "status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    # REF logica a `identity.users` (sin FK fisica).
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    account_number: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    type: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    currency: Mapped[str] = mapped_column(
        sa.String(3), nullable=False, default="PEN", server_default="PEN"
    )
    status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    # REFs logicas a `ledger.ledger_accounts` (sin FK fisica): disponible y retenido.
    ledger_account_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    ledger_hold_account_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )

    balance: Mapped[AccountBalance] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        lazy="selectin",
        uselist=False,
    )


class AccountBalance(Base):
    """Proyeccion disponible/retenido (`03b#5.2`, 1:1 con `accounts`).

    Optimistic locking con `version`: se lee, se verifica
    `expected_version` y se incrementa en cada `apply_delta`.
    """

    __tablename__ = "account_balances"
    __table_args__ = (
        sa.CheckConstraint("available_minor >= 0", name="ck_account_balances_available_min"),
        sa.CheckConstraint("held_minor >= 0", name="ck_account_balances_held_min"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{SCHEMA}.accounts.id"],
            name="fk_account_balances_account",
        ),
        {"schema": SCHEMA},
    )

    account_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    available_minor: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, default=0, server_default="0"
    )
    held_minor: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, default=0, server_default="0"
    )
    version: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )

    account: Mapped[Account] = relationship(back_populates="balance")


class DailyBalanceSnapshot(Base):
    """Corte diario por cuenta (`03b#5.5`). PK compuesta (`account_id`, `snapshot_date`)."""

    __tablename__ = "daily_balance_snapshots"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{SCHEMA}.accounts.id"],
            name="fk_daily_snapshots_account",
        ),
        {"schema": SCHEMA},
    )

    account_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(sa.Date(), primary_key=True)
    available_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    held_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)


__all__ = [
    "ACCOUNT_STATUSES",
    "ACCOUNT_TYPES",
    "SCHEMA",
    "Account",
    "AccountBalance",
    "DailyBalanceSnapshot",
]
