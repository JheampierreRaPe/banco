"""Corridas de conciliacion (E5-T18 corte 2, HU19).

Revision ID: 0051_reconciliation_runs
Revises: 0050_reconciliation
Create Date: 2026-11-14

Crea SOLO la tabla propia `reconciliation.reconciliation_runs` (el schema ya
existe desde `0001`; no toca `accounts`/`transactions`/`ledger`/`risk`/
`fx`), segun `docs/03b-diccionario-de-datos.md#12.2`:

- `reconciliation.reconciliation_runs(id UUID PK, run_date DATE NOT NULL,
  currency CHAR(3) NOT NULL, status VARCHAR(15) NOT NULL default 'RUNNING'
  CHECK `ck_reconciliation_runs_status` (`RUNNING`/`COMPLETED`/`FAILED`),
  matched_count INTEGER NOT NULL default 0 CHECK >= 0,
  unmatched_count INTEGER NOT NULL default 0 CHECK >= 0,
  total_minor BIGINT NOT NULL default 0 CHECK >= 0 (centimos, nunca float),
  executed_by UUID NULL (REF logica a `identity.users`, sin FK fisica),
  created_at TIMESTAMPTZ NOT NULL default now(),
  finished_at TIMESTAMPTZ NULL)`;
  UQ natural `uq_reconciliation_runs_date_currency` (`run_date`,
  `currency`): una fila por fecha/moneda (idempotencia de la corrida);
  indice `ix_reconciliation_runs_status` (`status`).

Siembra idempotente (`ON CONFLICT (key) DO NOTHING`, respeta valores
personalizados) de las claves que lee `run_reconciliation`
(`docs/03b#12.2`, regla de oro 6; dueno del esquema: `shared/config`):

- `reconciliation.amount_tolerance_minor = 0` (centimos);
- `reconciliation.date_tolerance_days = 0` (dias);
- `reconciliation.max_candidate_pairs = 250000` (cota por grupo);
- `reconciliation.max_candidate_pairs_per_run = 1000000` (cota por corrida).

Reversible: `downgrade` elimina indice y tabla (las semillas de
`config.parameters` se conservan: son configuracion, no esquema).

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
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
        sa.UniqueConstraint(
            "run_date",
            "currency",
            name="uq_reconciliation_runs_date_currency",
        ),
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
        "ix_reconciliation_runs_status",
        table_name="reconciliation_runs",
        schema="reconciliation",
    )
    op.drop_table("reconciliation_runs", schema="reconciliation")
