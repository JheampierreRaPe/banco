"""Juego contractual `credits.contracts` (E3-T12, HU11).

Revision ID: 0033_credits_contracts
Revises: 0032_credits_dti_wider
Create Date: 2026-10-11

Fuente: `docs/03b-diccionario-de-datos.md#86-contracts` (columnas
`id`/`doc_ref`/`doc_hash`/`signature_token_hash`/`status`/`signed_at`/
`created_at`; se adopta **`doc_hash`**, 03b es la fuente ejecutable)
y `docs/tasks/E3-T12.md` con la decision del dueno: **tres documentos
separados** (contrato + pagare + hoja resumen), cada uno con su
`doc_ref`/`doc_hash`.

Desvios registrados en `docs/18` (gap E3-T12):
- `application_id` (FK fisica a `credits.loan_applications.id`, mismo
  schema) + `doc_type` (`CONTRATO`/`PAGARE`/`RESUMEN`) con UQ por
  `(application_id, doc_type)`: un juego por expediente.
- `loan_id` UUID **logico sin FK fisica** (`NULL` en HU11; lo puebla
  HU12 al desembolsar, pues `loans` aun no existe): sin `UQ` propia
  porque las tres filas del juego comparten el prestamo logico
  (`docs/03b#86` pedia `loan_id NOT NULL FK loans UQ`, inviable en
  HU11).
- El enum `contract_status` (`docs/03b#1`) se aplica como `CHECK`
  sobre `VARCHAR(15)` (no como enum Postgres: portable y sin drift
  en `alembic check`, mismo criterio que `loan_applications`).

Crea SOLO `credits.contracts` (el schema `credits` ya existe; aqui
solo `CREATE SCHEMA IF NOT EXISTS` defensivo, sin tocar otras
tablas).

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0033_credits_contracts` (22) lo respeta.
"""

import sqlalchemy as sa
from alembic import op

revision = "0033_credits_contracts"
down_revision = "0032_credits_dti_wider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('CREATE SCHEMA IF NOT EXISTS "credits"')
    op.create_table(
        "contracts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("loan_id", sa.Uuid(), nullable=True),
        sa.Column("doc_type", sa.String(20), nullable=False),
        sa.Column("doc_ref", sa.Text(), nullable=False),
        sa.Column("doc_hash", sa.String(64), nullable=False),
        sa.Column("signature_token_hash", sa.String(128), nullable=True),
        sa.Column("status", sa.String(15), server_default="GENERATED", nullable=False),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["credits.loan_applications.id"],
            name="fk_contracts_application",
        ),
        sa.CheckConstraint(
            "doc_type IN ('CONTRATO','PAGARE','RESUMEN')",
            name="ck_contracts_doc_type",
        ),
        sa.CheckConstraint(
            "status IN ('GENERATED','SIGNED','VOID')",
            name="ck_contracts_status",
        ),
        sa.UniqueConstraint(
            "application_id",
            "doc_type",
            name="uq_contracts_application_doc_type",
        ),
        schema="credits",
    )
    op.create_index(
        "ix_contracts_application_id",
        "contracts",
        ["application_id"],
        schema="credits",
    )
    op.create_index(
        "ix_contracts_status",
        "contracts",
        ["status"],
        schema="credits",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_contracts_status",
        table_name="contracts",
        schema="credits",
    )
    op.drop_index(
        "ix_contracts_application_id",
        table_name="contracts",
        schema="credits",
    )
    op.drop_table("contracts", schema="credits")
