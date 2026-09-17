"""Modelos ORM del schema `ledger` (E5-T09, HU18).

Fuente: `docs/03b-diccionario-de-datos.md#71-ledger_accounts`.
Frontera: solo `ledger.ledger_accounts`. Sin FK a otros schemas: `owner_ref`
y `account_ref` son UUID logicos; `parent_account_id` es autocontenida.

Desviacion documentada de `03b`: `code` se define como `VARCHAR(50)` en vez
de `VARCHAR(30)` porque el formato exigido `2000-<uuid>` (41 caracteres)
no cabe en 30. El ejemplo de `03b#7.1` (`2000-<uuid>`) manda sobre la
longitud. Unicidad (`UQ`) e idempotencia se mantienen.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

SCHEMA = "ledger"

LEDGER_ACCOUNT_TYPES = ("asset", "liability", "equity", "income", "expense")
LEDGER_OWNER_TYPES = ("CUSTOMER", "SYSTEM", "MERCHANT", "POOL")

# `docs/03b-diccionario-de-datos.md#1`: posting_direction y journal_status.
POSTING_DIRECTIONS = ("DEBIT", "CREDIT")
JOURNAL_STATUSES = ("POSTED", "REVERSED")


class LedgerAccount(Base):
    """Cuenta contable (`03b#7.1`). Catalogo + subcuentas por cliente."""

    __tablename__ = "ledger_accounts"
    __table_args__ = (
        sa.UniqueConstraint("code", name="uq_ledger_accounts_code"),
        sa.CheckConstraint(
            "type IN ('asset', 'liability', 'equity', 'income', 'expense')",
            name="ck_ledger_accounts_type",
        ),
        sa.Index("ix_ledger_accounts_owner_ref", "owner_ref"),
        sa.Index("ix_ledger_accounts_type", "type"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    type: Mapped[str] = mapped_column(sa.String(12), nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    owner_type: Mapped[str] = mapped_column(sa.String(20), nullable=False, server_default="SYSTEM")
    owner_ref: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    parent_account_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid(), sa.ForeignKey(f"{SCHEMA}.ledger_accounts.id"), nullable=True
    )
    is_system: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class JournalEntry(Base):
    """Asiento de partida doble (`03b#7.2`). Append-only: se revierte, no se edita.

    `transaction_id` es UUID logico SIN FK fisica a otro schema (regla: ningun
    modulo accede a tablas de otro). `reverses_entry_id` es auto-FK contenida
    en `ledger.journal_entries`.
    """

    __tablename__ = "journal_entries"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('POSTED', 'REVERSED')",
            name="ck_journal_entries_status",
        ),
        sa.Index("ix_journal_entries_transaction_id", "transaction_id"),
        sa.Index("ix_journal_entries_value_date", "value_date"),
        sa.Index("ix_journal_entries_status", "status"),
        sa.Index("ix_journal_entries_created_at", "created_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    entry_type: Mapped[str] = mapped_column(sa.String(30), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    value_date: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(12), nullable=False, default="POSTED", server_default="POSTED"
    )
    reverses_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid(), sa.ForeignKey(f"{SCHEMA}.journal_entries.id"), nullable=True
    )
    prev_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=sa.func.now(),
        nullable=False,
    )

    postings: Mapped[list[Posting]] = relationship(back_populates="entry", passive_deletes=True)


class Posting(Base):
    """Movimiento de un asiento (`03b#7.3`). Append-only: sin UPDATE ni DELETE.

    Dinero entero en centimos (`amount_minor BIGINT CK > 0`); nunca `float`.
    """

    __tablename__ = "postings"
    __table_args__ = (
        sa.CheckConstraint(
            "direction IN ('DEBIT', 'CREDIT')",
            name="ck_postings_direction",
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_postings_amount_positive"),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"],
            [f"{SCHEMA}.journal_entries.id"],
            name="fk_postings_journal_entry",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_account_id"],
            [f"{SCHEMA}.ledger_accounts.id"],
            name="fk_postings_ledger_account",
        ),
        sa.Index("ix_postings_journal_entry_id", "journal_entry_id"),
        sa.Index("ix_postings_ledger_account_id", "ledger_account_id"),
        sa.Index("ix_postings_account_ref", "account_ref"),
        sa.Index("ix_postings_created_at", "created_at"),
        sa.Index("ix_postings_ledger_account_created_at", "ledger_account_id", "created_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    journal_entry_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    ledger_account_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    direction: Mapped[str] = mapped_column(sa.String(6), nullable=False)
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    account_ref: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=sa.func.now(),
        nullable=False,
    )

    entry: Mapped[JournalEntry] = relationship(back_populates="postings")


class LedgerBalance(Base):
    """Proyeccion de saldo contable (`03b#7.4`, E5-T12, HU18 CA-04).

    Aditivo: no modifica `LedgerAccount`/`JournalEntry`/`Posting`.
    Fuente de verdad: `postings`; esta tabla es proyeccion y se actualiza
    en la misma transaccion que el asiento con optimistic locking
    (`version`: se lee, se verifica y se incrementa).

    Dinero entero en `balance_minor` (firmado: DEBIT suma, CREDIT resta);
    nunca `float`.
    """

    __tablename__ = "ledger_balances"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["ledger_account_id"],
            [f"{SCHEMA}.ledger_accounts.id"],
            name="fk_ledger_balances_account",
        ),
        {"schema": SCHEMA},
    )

    ledger_account_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    balance_minor: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, default=0, server_default="0"
    )
    version: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=sa.func.now(),
        nullable=False,
    )


class DailyClosing(Base):
    """Cierre contable diario (`03b#7.5`, E5-T13, HU18 CA-04).

    Aditivo: no modifica `LedgerAccount`/`JournalEntry`/`Posting`/
    `LedgerBalance`. Una fila por (`closing_date`, `currency`) (UQ):
    la segunda corrida del mismo dia+moneda retorna la fila existente
    sin duplicar (el job `run_daily_closing` la re-lee antes de crear).

    `balanced` refleja el cuadre del dia (`total_debits == total_credits`
    en enteros); `closed_at` solo se fija cuando ademas la proyeccion
    `ledger_balances` es consistente con `postings`. Si descuadra, la fila
    queda con `balanced=false` y `closed_at=None`: nunca se editan
    postings ni balances para "cuadrar" (regla de oro 2).
    """

    __tablename__ = "daily_closings"
    __table_args__ = (
        sa.UniqueConstraint("closing_date", "currency", name="uq_daily_closings_date_currency"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    closing_date: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    total_debits_minor: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, default=0, server_default="0"
    )
    total_credits_minor: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, default=0, server_default="0"
    )
    balanced: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default="false"
    )
    postings_count: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, default=0, server_default="0"
    )
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    closed_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
