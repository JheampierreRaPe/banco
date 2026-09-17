"""Persistencia del motor: transactions, historial y holds (E5-T02, HU17).

Revision ID: 0002_transactions_persistence
Revises: 0001_init_schemas
Create Date: 2026-09-17

Fuente: `docs/03b-diccionario-de-datos.md#6-schema-transactions`.
Solo schema `transactions`; sin tocar `ledger` ni `accounts`.
Sin FK entre schemas: las referencias cruzadas son UUID logicos.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_transactions_persistence"
down_revision = "0001_init_schemas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transactions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("type", sa.String(30), nullable=False),
        sa.Column(
            "status",
            sa.String(25),
            server_default="INITIATED",
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(80), nullable=True),
        sa.Column("initiator_user_id", sa.Uuid(), nullable=True),
        sa.Column("source_account_id", sa.Uuid(), nullable=True),
        sa.Column("target_account_id", sa.Uuid(), nullable=True),
        sa.Column("external_ref", sa.String(80), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("fee_minor", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("risk_level", sa.String(15), server_default="LOW", nullable=False),
        sa.Column("risk_score", sa.SmallInteger(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
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
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount_minor > 0", name="ck_transactions_amount_positive"),
        sa.CheckConstraint("fee_minor >= 0", name="ck_transactions_fee_non_negative"),
        schema="transactions",
    )
    op.create_index(
        "ix_transactions_idempotency_key",
        "transactions",
        ["idempotency_key"],
        schema="transactions",
    )
    op.create_index(
        "ix_transactions_status_created",
        "transactions",
        ["status", "created_at"],
        schema="transactions",
    )
    op.create_index(
        "ix_transactions_source_account",
        "transactions",
        ["source_account_id"],
        schema="transactions",
    )
    op.create_index(
        "ix_transactions_external_ref",
        "transactions",
        ["external_ref"],
        schema="transactions",
    )

    op.create_table(
        "transaction_status_history",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.String(25), nullable=True),
        sa.Column("to_status", sa.String(25), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor_type", sa.String(20), server_default="SYSTEM", nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.transactions.id"],
            name="fk_tx_history_transaction",
        ),
        schema="transactions",
    )
    op.create_index(
        "ix_tx_history_tx_created",
        "transaction_status_history",
        ["transaction_id", "created_at"],
        schema="transactions",
    )

    op.create_table(
        "holds",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(15), server_default="ACTIVE", nullable=False),
        sa.Column(
            "held_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount_minor > 0", name="ck_holds_amount_positive"),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.transactions.id"],
            name="fk_holds_transaction",
        ),
        schema="transactions",
    )
    op.create_index(
        "ix_holds_status_expires",
        "holds",
        ["status", "expires_at"],
        schema="transactions",
    )
    op.create_index("ix_holds_account", "holds", ["account_id"], schema="transactions")
    op.create_index("ix_holds_transaction", "holds", ["transaction_id"], schema="transactions")


def downgrade() -> None:
    op.drop_index("ix_holds_transaction", table_name="holds", schema="transactions")
    op.drop_index("ix_holds_account", table_name="holds", schema="transactions")
    op.drop_index("ix_holds_status_expires", table_name="holds", schema="transactions")
    op.drop_table("holds", schema="transactions")
    op.drop_index(
        "ix_tx_history_tx_created",
        table_name="transaction_status_history",
        schema="transactions",
    )
    op.drop_table("transaction_status_history", schema="transactions")
    op.drop_index(
        "ix_transactions_external_ref",
        table_name="transactions",
        schema="transactions",
    )
    op.drop_index(
        "ix_transactions_source_account",
        table_name="transactions",
        schema="transactions",
    )
    op.drop_index(
        "ix_transactions_status_created",
        table_name="transactions",
        schema="transactions",
    )
    op.drop_index(
        "ix_transactions_idempotency_key",
        table_name="transactions",
        schema="transactions",
    )
    op.drop_table("transactions", schema="transactions")
