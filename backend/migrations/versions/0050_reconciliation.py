"""Esquema `reconciliation`: ingesta de lotes de compensacion (E5-T17, HU19).

Revision ID: 0050_reconciliation
Revises: 0049_fx
Create Date: 2026-11-12

Crea SOLO las dos tablas propias del schema `reconciliation` (el schema ya
existe desde `0001`; no toca `accounts`/`transactions`/`ledger`/`risk`/
`fx`), segun `docs/03b-diccionario-de-datos.md#12.1`:

- `reconciliation.clearing_files(id UUID PK, source VARCHAR(40),
  file_ref VARCHAR(80) UQ, storage_ref TEXT NULL, checksum VARCHAR(64),
  item_count INTEGER, status VARCHAR(15) default 'RECEIVED'
  CHECK `ck_clearing_files_status` (`RECEIVED`/`PROCESSED`/`FAILED`),
  received_at TIMESTAMPTZ)`;
  indices: `ix_clearing_files_source` (`source`), `ix_clearing_files_status`
  (`status`) (el `file_ref` ya indexa por su UQ).
- `reconciliation.clearing_items(id UUID PK, file_id UUID FK
  clearing_files (misma schema), external_ref VARCHAR(80),
  amount_minor BIGINT CHECK > 0, currency CHAR(3) CHECK ISO-4217,
  value_date DATE, direction VARCHAR(10)
  CHECK `ck_clearing_items_direction` (`INBOUND`/`OUTBOUND`),
  status VARCHAR(15) default 'PENDING'
  CHECK `ck_clearing_items_status` (`PENDING`/`MATCHED`/`EXCEPTION`),
  raw JSONB NULL)`;
  indices: `ix_clearing_items_file_id` (`file_id`),
  `ix_clearing_items_external_ref` (`external_ref`),
  `ix_clearing_items_status` (`status`).

Dinero entero en centimos (`BIGINT CHECK > 0`); nunca `float` (regla de
oro 3). Sin PII: solo referencias opacas, montos, monedas y fechas
(regla 7). Los items nacen `PENDING` (los escribe la ingesta de `E5-T17`;
el matching de `E5-T18` los mueve a `MATCHED`/`EXCEPTION`). Nada se
borra: un archivo fallido conserva su fila con `FAILED`.

Reversible: `downgrade` elimina indices y tablas en orden inverso.

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0050_reconciliation` (20) lo respeta.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0050_reconciliation"
down_revision = "0049_fx"
branch_labels = None
depends_on = None

# CHECK portable de moneda ISO-4217 (3 letras): `length`+`trim` existen
# en Postgres y SQLite (sin regex, sin drift en `alembic check`).
_ISO3 = "length(trim({col})) = 3"


def upgrade() -> None:
    op.execute('CREATE SCHEMA IF NOT EXISTS "reconciliation"')

    op.create_table(
        "clearing_files",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("file_ref", sa.String(80), nullable=False),
        sa.Column("storage_ref", sa.Text(), nullable=True),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(15), nullable=False, server_default="RECEIVED"),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("file_ref", name="uq_clearing_files_file_ref"),
        sa.CheckConstraint(
            "status IN ('RECEIVED','PROCESSED','FAILED')",
            name="ck_clearing_files_status",
        ),
        sa.CheckConstraint("item_count >= 0", name="ck_clearing_files_item_count_nonneg"),
        schema="reconciliation",
    )
    op.create_index(
        "ix_clearing_files_source",
        "clearing_files",
        ["source"],
        schema="reconciliation",
    )
    op.create_index(
        "ix_clearing_files_status",
        "clearing_files",
        ["status"],
        schema="reconciliation",
    )

    op.create_table(
        "clearing_items",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("external_ref", sa.String(80), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("value_date", sa.Date(), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("status", sa.String(15), nullable=False, server_default="PENDING"),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["reconciliation.clearing_files.id"],
            name="fk_clearing_items_file",
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_clearing_items_amount_pos"),
        sa.CheckConstraint(
            _ISO3.format(col="currency"),
            name="ck_clearing_items_currency_iso",
        ),
        sa.CheckConstraint(
            "direction IN ('INBOUND','OUTBOUND')",
            name="ck_clearing_items_direction",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','MATCHED','EXCEPTION')",
            name="ck_clearing_items_status",
        ),
        schema="reconciliation",
    )
    op.create_index(
        "ix_clearing_items_file_id",
        "clearing_items",
        ["file_id"],
        schema="reconciliation",
    )
    op.create_index(
        "ix_clearing_items_external_ref",
        "clearing_items",
        ["external_ref"],
        schema="reconciliation",
    )
    op.create_index(
        "ix_clearing_items_status",
        "clearing_items",
        ["status"],
        schema="reconciliation",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_clearing_items_status",
        table_name="clearing_items",
        schema="reconciliation",
    )
    op.drop_index(
        "ix_clearing_items_external_ref",
        table_name="clearing_items",
        schema="reconciliation",
    )
    op.drop_index(
        "ix_clearing_items_file_id",
        table_name="clearing_items",
        schema="reconciliation",
    )
    op.drop_table("clearing_items", schema="reconciliation")
    op.drop_index(
        "ix_clearing_files_status",
        table_name="clearing_files",
        schema="reconciliation",
    )
    op.drop_index(
        "ix_clearing_files_source",
        table_name="clearing_files",
        schema="reconciliation",
    )
    op.drop_table("clearing_files", schema="reconciliation")
