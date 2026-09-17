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
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

SCHEMA = "ledger"

LEDGER_ACCOUNT_TYPES = ("asset", "liability", "equity", "income", "expense")
LEDGER_OWNER_TYPES = ("CUSTOMER", "SYSTEM", "MERCHANT", "POOL")


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
    owner_type: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, server_default="SYSTEM"
    )
    owner_ref: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    parent_account_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid(), sa.ForeignKey(f"{SCHEMA}.ledger_accounts.id"), nullable=True
    )
    is_system: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
