"""Cierre contable diario: ledger.daily_closings (E5-T13, HU18 CA-04).

Revision ID: 0008_ledger_daily_closing
Revises: 0007_shared_idempotency
Create Date: 2026-09-17

Fuente: `docs/03b-diccionario-de-datos.md#75-daily_closings`,
`docs/04-motor-transaccional-y-ledger.md#9-cierre-contable-diario`.
Solo schema `ledger`; sin tocar otros schemas ni otras tablas.
Idempotencia por UQ (`closing_date`, `currency`). `closed_by` es UUID
logico sin FK (regla: sin FK entre schemas). Dinero entero (`BIGINT`);
nunca `float`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_ledger_daily_closing"
down_revision = "0007_shared_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_closings",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("closing_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column(
            "total_debits_minor",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_credits_minor",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "balanced",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
        sa.Column(
            "postings_count",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("closing_date", "currency", name="uq_daily_closings_date_currency"),
        schema="ledger",
    )


def downgrade() -> None:
    op.drop_table("daily_closings", schema="ledger")
