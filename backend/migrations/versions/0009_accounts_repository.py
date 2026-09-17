"""Repositorio de cuentas y proyeccion de saldos: schema `accounts` (E2-T01, HU05).

Revision ID: 0009_accounts_repository
Revises: 0008_ledger_daily_closing
Create Date: 2026-09-17

Fuente: `docs/03b-diccionario-de-datos.md#5-schema-accounts`
(`5.1-accounts`, `5.2-account_balances`, `5.5-daily_balance_snapshots`),
`docs/03c-modelo-er.md#3-modulo-accounts`.
Solo schema `accounts`; sin tocar otros schemas ni otras tablas.
Sin FK entre schemas: `user_id`, `ledger_account_id` y
`ledger_hold_account_id` son UUID logicos (sin constraint fisico);
las unicas FK son autocontenidas (`account_balances`/`snapshots` ->
`accounts`). Dinero entero (`BIGINT`); nunca `float`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_accounts_repository"
down_revision = "0008_ledger_daily_closing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_number", sa.String(20), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("currency", sa.String(3), server_default="PEN", nullable=False),
        sa.Column("status", sa.String(20), server_default="ACTIVE", nullable=False),
        sa.Column("ledger_account_id", sa.Uuid(), nullable=True),
        sa.Column("ledger_hold_account_id", sa.Uuid(), nullable=True),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("account_number", name="uq_accounts_account_number"),
        sa.CheckConstraint(
            "type IN ('AHORRO', 'CORRIENTE', 'WALLET', 'POCKET', 'MULTICURRENCY')",
            name="ck_accounts_type",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'BLOCKED', 'CLOSED')",
            name="ck_accounts_status",
        ),
        schema="accounts",
    )
    op.create_index("ix_accounts_user_id", "accounts", ["user_id"], schema="accounts")
    op.create_index("ix_accounts_status", "accounts", ["status"], schema="accounts")

    op.create_table(
        "account_balances",
        sa.Column("account_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column(
            "available_minor",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "held_minor",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("available_minor >= 0", name="ck_account_balances_available_min"),
        sa.CheckConstraint("held_minor >= 0", name="ck_account_balances_held_min"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.accounts.id"],
            name="fk_account_balances_account",
        ),
        schema="accounts",
    )

    op.create_table(
        "daily_balance_snapshots",
        sa.Column("account_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("snapshot_date", sa.Date(), primary_key=True, nullable=False),
        sa.Column("available_minor", sa.BigInteger(), nullable=False),
        sa.Column("held_minor", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.accounts.id"],
            name="fk_daily_snapshots_account",
        ),
        schema="accounts",
    )


def downgrade() -> None:
    op.drop_table("daily_balance_snapshots", schema="accounts")
    op.drop_table("account_balances", schema="accounts")
    op.drop_index("ix_accounts_status", table_name="accounts", schema="accounts")
    op.drop_index("ix_accounts_user_id", table_name="accounts", schema="accounts")
    op.drop_table("accounts", schema="accounts")
