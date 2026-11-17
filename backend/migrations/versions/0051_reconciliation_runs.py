"""Corridas de conciliacion + emparejamiento persistido (E5-T18, HU19).

Revision ID: 0051_reconciliation_runs
Revises: 0050_reconciliation
Create Date: 2026-11-14

Crea SOLO objetos propios del schema `reconciliation` (el schema ya existe
desde `0001`; no toca `accounts`/`transactions`/`ledger`/`risk`/`fx`):

- `reconciliation.reconciliation_runs(id UUID PK, run_date DATE NOT NULL,
  currency CHAR(3) NOT NULL, status VARCHAR(15) NOT NULL default 'RUNNING'
  CHECK `ck_reconciliation_runs_status` (`RUNNING`/`COMPLETED`/`FAILED`),
  matched_count INTEGER NOT NULL default 0 CHECK >= 0,
  unmatched_count INTEGER NOT NULL default 0 CHECK >= 0,
  total_minor BIGINT NOT NULL default 0 CHECK >= 0 (centimos, nunca float),
  executed_by UUID NULL (REF logica a `identity.users`, sin FK fisica),
  created_at TIMESTAMPTZ NOT NULL default now(),
  finished_at TIMESTAMPTZ NULL)`;
  indice `ix_reconciliation_runs_status` (`status`), indice
  `ix_reconciliation_runs_date_currency` (`run_date`, `currency`) e indice
  unico parcial `uq_reconciliation_runs_single_running` (`run_date`,
  `currency`, `WHERE status = 'RUNNING'`: una sola corrida en curso por
  fecha/moneda; la BD decide la carrera).
  Sin UQ natural: las corridas `COMPLETED` son inmutables y una fecha/moneda
  puede tener varias corridas (lotes tardios crean una corrida nueva; ver
  `docs/03b#12.2`).
- `reconciliation.clearing_items.run_id UUID NULL` (REF logica a
  `reconciliation_runs.id`, sin FK fisica: que corrida proceso el item) +
  indice `ix_clearing_items_run_id`.
- `reconciliation.reconciliation_matches(id UUID PK, run_id UUID NOT NULL
  FK `fk_reconciliation_matches_run` -> `reconciliation_runs.id`,
  clearing_item_id UUID NOT NULL UQ `uq_reconciliation_matches_item`
  FK `fk_reconciliation_matches_item` -> `clearing_items.id`,
  transaction_id UUID NOT NULL UQ `uq_reconciliation_matches_transaction`
  (REF logica a `transactions`, sin FK fisica: una operacion interna se
  concilia como maximo una vez),
  match_type VARCHAR(15) NOT NULL CHECK `ck_reconciliation_matches_type`
  (`EXACT`/`AMOUNT_DIFF`/`DATE_DIFF`), amount_minor BIGINT NOT NULL
  CHECK `ck_reconciliation_matches_amount_nonneg` (`>= 0`, centimos),
  currency CHAR(3) NOT NULL, created_at TIMESTAMPTZ NOT NULL default now())`;
  indices `ix_reconciliation_matches_run_id` (`run_id`) e
  `ix_reconciliation_matches_transaction_id` (`transaction_id`).

Siembra idempotente (`ON CONFLICT (key) DO NOTHING`, respeta valores
personalizados) de las claves que lee `run_reconciliation`
(`docs/03b#12.2`, regla de oro 6; dueno del esquema: `shared/config`):

- `reconciliation.amount_tolerance_minor = 0` (centimos);
- `reconciliation.date_tolerance_days = 0` (dias);
- `reconciliation.max_candidate_pairs = 250000` (cota por grupo);
- `reconciliation.max_candidate_pairs_per_run = 1000000` (cota por corrida).

Reversible: `downgrade` elimina la tabla de matches, la columna `run_id`
(con su indice), el indice unico parcial de `RUNNING` y la tabla de runs
con sus indices (las semillas de `config.parameters` se conservan: son
configuracion, no esquema).

NOTA: el `revision` tiene maximo 32 caracteres (`alembic.version.
version_num VARCHAR(32)`): `0051_reconciliation_runs` (24) lo respeta.
"""

import sqlalchemy as sa
from alembic import op

revision = "0051_reconciliation_runs"
down_revision = "0050_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("status", sa.String(15), nullable=False, server_default="RUNNING"),
        sa.Column("matched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unmatched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("executed_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('RUNNING','COMPLETED','FAILED')",
            name="ck_reconciliation_runs_status",
        ),
        sa.CheckConstraint(
            "matched_count >= 0",
            name="ck_reconciliation_runs_matched_nonneg",
        ),
        sa.CheckConstraint(
            "unmatched_count >= 0",
            name="ck_reconciliation_runs_unmatched_nonneg",
        ),
        sa.CheckConstraint(
            "total_minor >= 0",
            name="ck_reconciliation_runs_total_nonneg",
        ),
        schema="reconciliation",
    )
    op.create_index(
        "ix_reconciliation_runs_status",
        "reconciliation_runs",
        ["status"],
        schema="reconciliation",
    )
    op.create_index(
        "ix_reconciliation_runs_date_currency",
        "reconciliation_runs",
        ["run_date", "currency"],
        schema="reconciliation",
    )
    # Guarda de BD (MEDIO-2): una sola `RUNNING` por `(run_date,
    # currency)`; parcial para permitir varias corridas por fecha/moneda
    # (`COMPLETED` inmutables, lotes tardios) y reintentos de `FAILED`.
    op.create_index(
        "uq_reconciliation_runs_single_running",
        "reconciliation_runs",
        ["run_date", "currency"],
        schema="reconciliation",
        unique=True,
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.add_column(
        "clearing_items",
        sa.Column("run_id", sa.Uuid(), nullable=True),
        schema="reconciliation",
    )
    op.create_index(
        "ix_clearing_items_run_id",
        "clearing_items",
        ["run_id"],
        schema="reconciliation",
    )
    op.create_table(
        "reconciliation_matches",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("clearing_item_id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("match_type", sa.String(15), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["reconciliation.reconciliation_runs.id"],
            name="fk_reconciliation_matches_run",
        ),
        sa.ForeignKeyConstraint(
            ["clearing_item_id"],
            ["reconciliation.clearing_items.id"],
            name="fk_reconciliation_matches_item",
        ),
        sa.UniqueConstraint(
            "clearing_item_id",
            name="uq_reconciliation_matches_item",
        ),
        sa.UniqueConstraint(
            "transaction_id",
            name="uq_reconciliation_matches_transaction",
        ),
        sa.CheckConstraint(
            "match_type IN ('EXACT','AMOUNT_DIFF','DATE_DIFF')",
            name="ck_reconciliation_matches_type",
        ),
        sa.CheckConstraint(
            "amount_minor >= 0",
            name="ck_reconciliation_matches_amount_nonneg",
        ),
        schema="reconciliation",
    )
    op.create_index(
        "ix_reconciliation_matches_run_id",
        "reconciliation_matches",
        ["run_id"],
        schema="reconciliation",
    )
    op.create_index(
        "ix_reconciliation_matches_transaction_id",
        "reconciliation_matches",
        ["transaction_id"],
        schema="reconciliation",
    )
    for key, value, description in (
        (
            "reconciliation.amount_tolerance_minor",
            "0",
            "Tolerancia de monto en centimos para el cruce de conciliacion (E5-T18)",
        ),
        (
            "reconciliation.date_tolerance_days",
            "0",
            "Tolerancia de fecha valor en dias para el cruce de conciliacion (E5-T18)",
        ),
        (
            "reconciliation.max_candidate_pairs",
            "250000",
            "Cota de pares evaluables por grupo de referencia en el cruce (E5-T18)",
        ),
        (
            "reconciliation.max_candidate_pairs_per_run",
            "1000000",
            "Presupuesto total de pares evaluables por corrida de conciliacion (E5-T18)",
        ),
    ):
        op.execute(
            sa.text(
                "INSERT INTO config.parameters (id, key, value_json, module, description) "
                "VALUES (gen_random_uuid(), :key, CAST(:value AS jsonb), :module, "
                ":description) "
                "ON CONFLICT (key) DO NOTHING"
            ).bindparams(
                key=key,
                value=value,
                module="reconciliation",
                description=description,
            )
        )


def downgrade() -> None:
    op.drop_index(
        "ix_reconciliation_matches_transaction_id",
        table_name="reconciliation_matches",
        schema="reconciliation",
    )
    op.drop_index(
        "ix_reconciliation_matches_run_id",
        table_name="reconciliation_matches",
        schema="reconciliation",
    )
    op.drop_table("reconciliation_matches", schema="reconciliation")
    op.drop_index(
        "ix_clearing_items_run_id",
        table_name="clearing_items",
        schema="reconciliation",
    )
    op.drop_column("clearing_items", "run_id", schema="reconciliation")
    op.drop_index(
        "uq_reconciliation_runs_single_running",
        table_name="reconciliation_runs",
        schema="reconciliation",
    )
    op.drop_index(
        "ix_reconciliation_runs_date_currency",
        table_name="reconciliation_runs",
        schema="reconciliation",
    )
    op.drop_index(
        "ix_reconciliation_runs_status",
        table_name="reconciliation_runs",
        schema="reconciliation",
    )
    op.drop_table("reconciliation_runs", schema="reconciliation")
