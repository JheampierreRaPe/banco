"""Proyeccion de saldos: ledger_balances (E5-T12, HU18 CA-04).

Revision ID: 0006_ledger_balances
Revises: 0005_shared_outbox
Create Date: 2026-09-17

Fuente: `docs/03b-diccionario-de-datos.md#74-ledger_balances`,
`docs/03c-modelo-er.md#14-fuente-de-verdad-y-proyecciones`.
Solo schema `ledger`; sin tocar otros schemas.
PK `ledger_account_id` con FK autocontenida a `ledger.ledger_accounts`
(1:1). Sin FK entre schemas. Dinero entero (`BIGINT`); nunca `float`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_ledger_balances"
down_revision = "0005_shared_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ledger_balances",
        sa.Column("ledger_account_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column(
            "balance_minor",
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
        sa.ForeignKeyConstraint(
            ["ledger_account_id"],
            ["ledger.ledger_accounts.id"],
            name="fk_ledger_balances_account",
        ),
        schema="ledger",
    )


def downgrade() -> None:
    op.drop_table("ledger_balances", schema="ledger")
