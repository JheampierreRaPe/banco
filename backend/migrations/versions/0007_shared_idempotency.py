"""Claves de idempotencia: shared.idempotency_keys (E5-T04, HU17 CA-03).

Revision ID: 0007_shared_idempotency
Revises: 0006_ledger_balances
Create Date: 2026-09-17

Fuente: `docs/03b#21-idempotency_keys`, `docs/04#5-idempotencia`.
Solo schema `shared`; sin tocar otros schemas ni otras tablas.
Sin FK entre schemas: `user_id` y `transaction_id` son UUID logicos
(sin FK fisica). Dinero entero en snapshots JSON; nunca `float`.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_shared_idempotency"
down_revision = "0006_ledger_balances"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("endpoint", sa.String(150), nullable=False),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("transaction_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("key", "user_id", name="uq_idempotency_key_user"),
        schema="shared",
    )
    op.create_index(
        "ix_idempotency_created_at",
        "idempotency_keys",
        ["created_at"],
        schema="shared",
    )
    op.create_index(
        "ix_idempotency_expires_at",
        "idempotency_keys",
        ["expires_at"],
        schema="shared",
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_expires_at", table_name="idempotency_keys", schema="shared")
    op.drop_index("ix_idempotency_created_at", table_name="idempotency_keys", schema="shared")
    op.drop_table("idempotency_keys", schema="shared")
