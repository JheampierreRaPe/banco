"""Asientos de partida doble: journal_entries + postings (E5-T10, HU18).

Revision ID: 0004_ledger_double_entry
Revises: 0003_ledger_chart_of_accounts
Create Date: 2026-09-17

Fuente: `docs/03b-diccionario-de-datos.md#72-journal_entries` y
`#73-postings-append-only`, `docs/04-motor-transaccional-y-ledger.md#2`.
Solo schema `ledger`; sin tocar otros schemas.
Sin FK entre schemas: `transaction_id` es UUID logico (sin FK fisica);
`reverses_entry_id` y `journal_entry_id`/`ledger_account_id` son FK
autocontenidas en `ledger`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_ledger_double_entry"
down_revision = "0003_ledger_chart_of_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=True),
        sa.Column("entry_type", sa.String(30), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("value_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.String(12),
            server_default="POSTED",
            nullable=False,
        ),
        sa.Column("reverses_entry_id", sa.Uuid(), nullable=True),
        sa.Column("prev_hash", sa.String(64), nullable=True),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('POSTED', 'REVERSED')",
            name="ck_journal_entries_status",
        ),
        sa.ForeignKeyConstraint(
            ["reverses_entry_id"],
            ["ledger.journal_entries.id"],
            name="fk_journal_entries_reverses",
        ),
        schema="ledger",
    )
    op.create_index(
        "ix_journal_entries_transaction_id",
        "journal_entries",
        ["transaction_id"],
        schema="ledger",
    )
    op.create_index(
        "ix_journal_entries_value_date",
        "journal_entries",
        ["value_date"],
        schema="ledger",
    )
    op.create_index(
        "ix_journal_entries_status",
        "journal_entries",
        ["status"],
        schema="ledger",
    )
    op.create_index(
        "ix_journal_entries_created_at",
        "journal_entries",
        ["created_at"],
        schema="ledger",
    )

    op.create_table(
        "postings",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("journal_entry_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_account_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(6), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("account_ref", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "direction IN ('DEBIT', 'CREDIT')",
            name="ck_postings_direction",
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_postings_amount_positive"),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"],
            ["ledger.journal_entries.id"],
            name="fk_postings_journal_entry",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_account_id"],
            ["ledger.ledger_accounts.id"],
            name="fk_postings_ledger_account",
        ),
        schema="ledger",
    )
    op.create_index(
        "ix_postings_journal_entry_id",
        "postings",
        ["journal_entry_id"],
        schema="ledger",
    )
    op.create_index(
        "ix_postings_ledger_account_id",
        "postings",
        ["ledger_account_id"],
        schema="ledger",
    )
    op.create_index(
        "ix_postings_account_ref",
        "postings",
        ["account_ref"],
        schema="ledger",
    )
    op.create_index(
        "ix_postings_created_at",
        "postings",
        ["created_at"],
        schema="ledger",
    )
    op.create_index(
        "ix_postings_ledger_account_created_at",
        "postings",
        ["ledger_account_id", "created_at"],
        schema="ledger",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_postings_ledger_account_created_at",
        table_name="postings",
        schema="ledger",
    )
    op.drop_index("ix_postings_created_at", table_name="postings", schema="ledger")
    op.drop_index("ix_postings_account_ref", table_name="postings", schema="ledger")
    op.drop_index(
        "ix_postings_ledger_account_id", table_name="postings", schema="ledger"
    )
    op.drop_index(
        "ix_postings_journal_entry_id", table_name="postings", schema="ledger"
    )
    op.drop_table("postings", schema="ledger")
    op.drop_index(
        "ix_journal_entries_created_at", table_name="journal_entries", schema="ledger"
    )
    op.drop_index(
        "ix_journal_entries_status", table_name="journal_entries", schema="ledger"
    )
    op.drop_index(
        "ix_journal_entries_value_date", table_name="journal_entries", schema="ledger"
    )
    op.drop_index(
        "ix_journal_entries_transaction_id",
        table_name="journal_entries",
        schema="ledger",
    )
    op.drop_table("journal_entries", schema="ledger")
