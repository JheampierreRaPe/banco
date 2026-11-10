"""Proyeccion de movimientos: `accounts.movements_view` (E2-T04, HU05 CA-03).

Revision ID: 0010_accounts_movements
Revises: 0009_accounts_repository
Create Date: 2026-09-17

Fuente: `docs/03b-diccionario-de-datos.md#5.4-movements_view-vista-materializada`
(columnas exactas) e indice `docs/03b#15` (`(account_id, created_at DESC)`).
Solo schema `accounts`; sin tocar otros schemas ni otras tablas.

Decision (ver `accounts/repository/movements.py`): tabla de proyeccion con el
nombre y las columnas exactas de `03b#5.4` en lugar de `MATERIALIZED VIEW`
nativa, porque esta exigiria `SELECT ... FROM ledger.postings` (JOIN entre
modulos, prohibido por la regla de oro 4). El "refresh" es re-derivacion por
evento (`rebuild_entry_movements`): la migracion deja la tabla con su UQ
natural (PK compuesta) e indice; el refresh vive en el repositorio.
Sin FK entre schemas: `journal_entry_id` y `transaction_id` son UUID logicos
(sin constraint); la unica FK es autocontenida (`account_id` -> `accounts`).
Dinero entero (`BIGINT`); nunca `float`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_accounts_movements"
down_revision = "0009_accounts_repository"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "movements_view",
        sa.Column("journal_entry_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("direction", sa.String(6), primary_key=True, nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("currency", sa.String(3), primary_key=True, nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("value_date", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "direction IN ('DEBIT', 'CREDIT')",
            name="ck_movements_direction",
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_movements_amount_positive"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.accounts.id"],
            name="fk_movements_account",
        ),
        schema="accounts",
    )
    op.execute(
        "CREATE INDEX ix_movements_account_created "
        "ON accounts.movements_view (account_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS accounts.ix_movements_account_created")
    op.drop_table("movements_view", schema="accounts")
