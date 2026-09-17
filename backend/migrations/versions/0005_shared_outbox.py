"""Outbox transversal + inbox idempotente (E5-T05, HU17).

Revision ID: 0005_shared_outbox
Revises: 0004_ledger_double_entry
Create Date: 2026-09-17

Fuente: `docs/03b-diccionario-de-datos.md#22-outbox` y
`#23-processed_events-inbox`, `docs/modules/README.md#shared--config`.
Solo schema `shared`; sin tocar otros schemas ni otras tablas.
Sin FK entre schemas: `aggregate_id` es UUID logico (sin FK fisica).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_shared_outbox"
down_revision = "0004_ledger_double_entry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("aggregate_type", sa.String(60), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.String(12),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column(
            "attempts",
            sa.SmallInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "available_at",
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
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PUBLISHED', 'FAILED')",
            name="ck_outbox_status",
        ),
        schema="shared",
    )
    op.create_index(
        "ix_outbox_event_type",
        "outbox",
        ["event_type"],
        schema="shared",
    )
    op.create_index(
        "ix_outbox_aggregate_id",
        "outbox",
        ["aggregate_id"],
        schema="shared",
    )
    op.create_index(
        "ix_outbox_status",
        "outbox",
        ["status"],
        schema="shared",
    )
    op.create_index(
        "ix_outbox_available_at",
        "outbox",
        ["available_at"],
        schema="shared",
    )
    op.create_index(
        "ix_outbox_status_available_at",
        "outbox",
        ["status", "available_at"],
        schema="shared",
    )

    op.create_table(
        "processed_events",
        sa.Column("event_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("consumer", sa.String(60), primary_key=True, nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema="shared",
    )


def downgrade() -> None:
    op.drop_table("processed_events", schema="shared")
    op.drop_index("ix_outbox_status_available_at", table_name="outbox", schema="shared")
    op.drop_index("ix_outbox_available_at", table_name="outbox", schema="shared")
    op.drop_index("ix_outbox_status", table_name="outbox", schema="shared")
    op.drop_index("ix_outbox_aggregate_id", table_name="outbox", schema="shared")
    op.drop_index("ix_outbox_event_type", table_name="outbox", schema="shared")
    op.drop_table("outbox", schema="shared")
