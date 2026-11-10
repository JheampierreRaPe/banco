"""Esquema `wallet`: `wallets` + `wallet_links` (E4-T01, HU13).

Revision ID: 0037_wallet
Revises: 0036_loan_reminder_param
Create Date: 2026-10-13

Fuente: `docs/03b-diccionario-de-datos.md#91-wallets-y-wallet_links` y
`docs/tasks/E4-T01.md`. Crea SOLO el schema `wallet` con sus dos tablas
propias (sin tocar schemas de otros modulos).

Reglas aplicadas:
- Una billetera por titular (`wallets.user_id` UQ; REF logica a
  `identity.users`, sin FK fisica entre schemas, regla de oro 4).
- `wallet_links.wallet_id` FK fisica a `wallet.wallets` (mismo schema);
  `account_id` REF logica a `accounts` (sin FK fisica); `token_ref` UQ.
- Sin columnas de PAN/numero completo: solo `token_ref` + `last4` +
  `brand` (tokenizacion obligatoria, HU13).
- Enums como `CHECK` sobre `VARCHAR` (portable, sin drift en
  `alembic check`, mismo criterio que `credits.contracts`).

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0037_wallet` (12) lo respeta.
"""

import sqlalchemy as sa
from alembic import op

revision = "0037_wallet"
down_revision = "0036_loan_reminder_param"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('CREATE SCHEMA IF NOT EXISTS "wallet"')
    op.create_table(
        "wallets",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(15), server_default="ACTIVE", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", name="uq_wallets_user_id"),
        sa.CheckConstraint(
            "status IN ('ACTIVE','BLOCKED','CLOSED')",
            name="ck_wallets_status",
        ),
        schema="wallet",
    )
    op.create_index(
        "ix_wallets_user_id",
        "wallets",
        ["user_id"],
        schema="wallet",
    )
    op.create_table(
        "wallet_links",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("wallet_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("token_ref", sa.String(120), nullable=False),
        sa.Column("last4", sa.String(4), nullable=True),
        sa.Column("brand", sa.String(20), nullable=True),
        sa.Column("status", sa.String(15), server_default="ACTIVE", nullable=False),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["wallet.wallets.id"],
            name="fk_wallet_links_wallet",
        ),
        sa.UniqueConstraint("token_ref", name="uq_wallet_links_token_ref"),
        sa.CheckConstraint(
            "status IN ('ACTIVE','INACTIVE')",
            name="ck_wallet_links_status",
        ),
        schema="wallet",
    )
    op.create_index(
        "ix_wallet_links_wallet_id",
        "wallet_links",
        ["wallet_id"],
        schema="wallet",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wallet_links_wallet_id",
        table_name="wallet_links",
        schema="wallet",
    )
    op.drop_table("wallet_links", schema="wallet")
    op.drop_index(
        "ix_wallets_user_id",
        table_name="wallets",
        schema="wallet",
    )
    op.drop_table("wallets", schema="wallet")
