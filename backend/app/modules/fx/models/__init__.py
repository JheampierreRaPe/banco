"""Modelos ORM del schema `fx` (E7-T01, HU24/HU25).

Fuente: `docs/03b-diccionario-de-datos.md#10` (columnas exactas) y
`docs/tasks/E7-T01.md`. Frontera: solo `fx`; `user_id`/`account_id`/
`transaction_id`/`journal_entry_id` son REF logicas (UUID sin FK fisica
entre schemas, regla de oro 4). Dinero entero en centimos (`BIGINT`);
tasas/spread/yield `NUMERIC(18,8)`; nunca `float` (regla de oro 3).
Sin PII: `user_id` es UUID (regla 7).

Los `CHECK` replican 1:1 los de la migracion `0049_fx` (sin drift en
`alembic check`): moneda ISO-4217 de 3 letras (`length(trim()) = 3`,
portable Postgres/SQLite), `rate > 0`, montos enteros, `balance >= 0`,
`amount > 0` y estados como `CHECK` sobre `VARCHAR` (portable, mismo
criterio que `wallet.qr_charges`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

SCHEMA = "fx"

#: Estados de `fx_quotes.status` (`03b#10.2`, enum `fx_quote_status`).
FX_QUOTE_STATUSES = ("ACTIVE", "EXPIRED", "CONSUMED", "CANCELLED")

#: Estados de `fx_operations.status` (`03b#10.3`, enum `fx_operation_status`).
FX_OPERATION_STATUSES = ("EXECUTED", "REVERSED")

#: Estados de `savings_pockets.status` (`03b#10.3`, enum `pocket_status`).
POCKET_STATUSES = ("ACTIVE", "CLOSED")

#: Tipos de `pocket_movements.movement_type` (`03b#10.3`).
POCKET_MOVEMENT_TYPES = ("DEPOSIT", "WITHDRAWAL", "ROUNDDUP", "YIELD", "TRANSFER")

#: Direcciones de `pocket_movements.direction` (`03b#10.3`).
POCKET_MOVEMENT_DIRECTIONS = ("DEBIT", "CREDIT")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class FxRate(Base):
    """Tasa de cambio origen (`03b#10.1`, E7-T01).

    Una fila por par (`base`, `quote`) y momento (`fetched_at`): la
    tasa vigente es la de `fetched_at` mas reciente (`get_latest_rate`).
    `ttl_seconds` indica cuanto puede reusarse sin refrescar (regla
    configurable fuera del codigo, regla de oro 6; la lee `E7-T08`).
    """

    __tablename__ = "fx_rates"
    __table_args__ = (
        sa.CheckConstraint("length(trim(base)) = 3", name="ck_fx_rates_base_iso"),
        sa.CheckConstraint("length(trim(quote)) = 3", name="ck_fx_rates_quote_iso"),
        sa.CheckConstraint("base <> quote", name="ck_fx_rates_distinct_pair"),
        sa.CheckConstraint("rate > 0", name="ck_fx_rates_rate_pos"),
        sa.CheckConstraint("ttl_seconds > 0", name="ck_fx_rates_ttl_pos"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    base: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    quote: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    # Tipo de cambio (`NUMERIC(18,8)`); nunca `float` (regla 3).
    rate: Mapped[Decimal] = mapped_column(sa.Numeric(18, 8), nullable=False)
    source: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )
    ttl_seconds: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=60, server_default="60"
    )


sa.Index(
    "ix_fx_rates_base_quote_fetched",
    FxRate.base,
    FxRate.quote,
    FxRate.fetched_at.desc(),
)


class FxQuote(Base):
    """Cotizacion congelada para un usuario (`03b#10.2`, E7-T01).

    Congela `rate` (+ `spread`) con vigencia corta (`expires_at`;
    `03b#16.5`: `expires_at > created_at`). `status` avanza
    `ACTIVE -> CONSUMED` (al ejecutar, `E7-T11`), `ACTIVE -> EXPIRED`
    (al vencer, `expire_quote`) o `-> CANCELLED`; nada se borra.
    `source_amount_minor`/`target_amount_minor` son enteros en centimos.
    """

    __tablename__ = "fx_quotes"
    __table_args__ = (
        sa.CheckConstraint("length(trim(base)) = 3", name="ck_fx_quotes_base_iso"),
        sa.CheckConstraint("length(trim(quote)) = 3", name="ck_fx_quotes_quote_iso"),
        sa.CheckConstraint("base <> quote", name="ck_fx_quotes_distinct_pair"),
        sa.CheckConstraint("rate > 0", name="ck_fx_quotes_rate_pos"),
        sa.CheckConstraint("spread >= 0", name="ck_fx_quotes_spread_nonneg"),
        sa.CheckConstraint(
            "source_amount_minor > 0",
            name="ck_fx_quotes_source_amount_pos",
        ),
        sa.CheckConstraint(
            "target_amount_minor > 0",
            name="ck_fx_quotes_target_amount_pos",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','EXPIRED','CONSUMED','CANCELLED')",
            name="ck_fx_quotes_status",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_fx_quotes_expiry_after_creation",
        ),
        sa.Index("ix_fx_quotes_user_status", "user_id", "status"),
        sa.Index("ix_fx_quotes_expires_at", "expires_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    # REF logica a `identity.users` (sin FK fisica).
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    base: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    quote: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    # Tasa congelada (`NUMERIC(18,8)`); nunca `float` (regla 3).
    rate: Mapped[Decimal] = mapped_column(sa.Numeric(18, 8), nullable=False)
    spread: Mapped[Decimal] = mapped_column(
        sa.Numeric(18, 8), nullable=False, default=Decimal(0), server_default="0"
    )
    # Montos enteros en centimos; nunca `float` (regla 3).
    source_amount_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    target_amount_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(12), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )

    operation: Mapped[FxOperation | None] = relationship(
        back_populates="quote",
        lazy="selectin",
        uselist=False,
    )


class FxOperation(Base):
    """Ejecucion de una cotizacion (`03b#10.3`, E7-T01).

    `quote_id` es UQ + FK mismo schema: una cotizacion se ejecuta una
    sola vez. `transaction_id` es REF logica a
    `transactions.transactions` (sin FK fisica; lo puebla `E7-T11` al
    liquidar por el motor, `docs/04#7.7`: dos asientos, uno por moneda).
    No mueve dinero por si misma.
    """

    __tablename__ = "fx_operations"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["quote_id"],
            ["fx.fx_quotes.id"],
            name="fk_fx_operations_quote",
        ),
        sa.UniqueConstraint("quote_id", name="uq_fx_operations_quote_id"),
        sa.CheckConstraint(
            "status IN ('EXECUTED','REVERSED')",
            name="ck_fx_operations_status",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    quote_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    # REF logica a `transactions.transactions` (sin FK fisica).
    transaction_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default="EXECUTED", server_default="EXECUTED"
    )
    executed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )

    quote: Mapped[FxQuote] = relationship(back_populates="operation")


class SavingsPocket(Base):
    """Bolsillo / meta de ahorro (`03b#10.3`, E7-T01, HU24).

    `account_id` es REF logica a `accounts.accounts` (sin FK fisica;
    `NULL` = sin cuenta vinculada). `balance_minor` nunca negativo
    (los movimientos que lo alimentan los crea `E7-T05`); `target_minor`
    `NULL` = sin meta. `yield_rate` (`NUMERIC(18,8)`, lo consume `E7-T04`).
    `status` avanza `ACTIVE -> CLOSED`; nada se borra.
    """

    __tablename__ = "savings_pockets"
    __table_args__ = (
        sa.CheckConstraint("length(trim(currency)) = 3", name="ck_pockets_currency_iso"),
        sa.CheckConstraint(
            "target_minor IS NULL OR target_minor >= 0",
            name="ck_pockets_target_nonneg",
        ),
        sa.CheckConstraint("balance_minor >= 0", name="ck_pockets_balance_nonneg"),
        sa.CheckConstraint("yield_rate >= 0", name="ck_pockets_yield_nonneg"),
        sa.CheckConstraint(
            "status IN ('ACTIVE','CLOSED')",
            name="ck_pockets_status",
        ),
        sa.Index("ix_savings_pockets_user_status", "user_id", "status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    # REF logica a `identity.users` (sin FK fisica).
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    # REF logica a `accounts.accounts` (sin FK fisica; NULL = sin vinculo).
    account_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    name: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    # Meta en centimos (`NULL` = sin meta); nunca negativo.
    target_minor: Mapped[int | None] = mapped_column(sa.BigInteger(), nullable=True)
    # Saldo en centimos; nunca negativo ni `float` (regla 3).
    balance_minor: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, default=0, server_default="0"
    )
    roundup_enabled: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default=sa.text("false")
    )
    # Tasa anual (`NUMERIC(18,8)`); nunca `float` (regla 3).
    yield_rate: Mapped[Decimal] = mapped_column(
        sa.Numeric(18, 8), nullable=False, default=Decimal(0), server_default="0"
    )
    status: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )

    movements: Mapped[list[PocketMovement]] = relationship(
        back_populates="pocket",
        lazy="selectin",
        # Sin cascade de borrado: los movimientos son inmutables, solo
        # insercion (H-3). Borrar un bolsillo jamas borra sus movimientos
        # (`passive_deletes="all"`: el ORM no toca las filas hijas ni para
        # anular su FK; la FK sin `ON DELETE` las protege en Postgres y en
        # SQLite la fila huerfana sobrevive).
        cascade="save-update, merge",
        passive_deletes="all",
    )


class PocketMovement(Base):
    """Movimiento inmutable de un bolsillo (`03b#10.3`, E7-T01).

    Solo insercion (los crea `E7-T05` junto al asiento del motor,
    `docs/04#7.8`); `journal_entry_id` es REF logica a
    `ledger.journal_entries` (sin FK fisica; `NULL` hasta liquidar).
    `amount_minor` entero en centimos `> 0`; `direction` indica el
    efecto sobre `balance_minor` (`CREDIT` suma, `DEBIT` resta).
    """

    __tablename__ = "pocket_movements"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["pocket_id"],
            ["fx.savings_pockets.id"],
            name="fk_pocket_movements_pocket",
        ),
        sa.CheckConstraint(
            "movement_type IN ('DEPOSIT','WITHDRAWAL','ROUNDDUP','YIELD','TRANSFER')",
            name="ck_pocket_movements_type",
        ),
        sa.CheckConstraint(
            "direction IN ('DEBIT','CREDIT')",
            name="ck_pocket_movements_direction",
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_pocket_movements_amount_pos"),
        sa.CheckConstraint(
            "length(trim(currency)) = 3",
            name="ck_pocket_movements_currency_iso",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    pocket_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    # REF logica a `ledger.journal_entries` (sin FK fisica).
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    movement_type: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    direction: Mapped[str] = mapped_column(sa.String(6), nullable=False)
    # Monto entero en centimos `> 0`; nunca `float` (regla 3).
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )

    pocket: Mapped[SavingsPocket] = relationship(back_populates="movements")


sa.Index(
    "ix_pocket_movements_pocket_created",
    PocketMovement.pocket_id,
    PocketMovement.created_at.desc(),
)
