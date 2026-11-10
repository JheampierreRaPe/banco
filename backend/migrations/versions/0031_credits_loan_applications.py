"""Expediente `credits.loan_applications` (E3-T08, HU10).

Revision ID: 0031_credits_loan_applications
Revises: 0030_credits_loan_rule_params
Create Date: 2026-10-09

Fuente: `docs/03b-diccionario-de-datos.md#82-loan_applications`
(columnas y tipos exactos) y `docs/tasks/E3-T08.md`. Crea SOLO
`credits.loan_applications` (el schema `credits` ya existe desde
`0001_init_schemas`; aqui solo `CREATE SCHEMA IF NOT EXISTS`
defensivo, sin tocar otros schemas ni otras tablas).

- `product_id` es FK fisica a `credits.loan_products.id` (mismo schema).
- `user_id` es UUID logico (REF `identity.users` sin FK fisica,
  regla de oro 4).
- El enum `loan_application_status` (`docs/03b#1`) se aplica como
  `CHECK` sobre `VARCHAR(20)` (no como enum Postgres: portable y sin
  drift en `alembic check`).
- Dinero entero en centimos (`BIGINT`); `dti_ratio NUMERIC(6,4)`;
  sin `float` (regla de oro 3).

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0031_credits_loan_applications` (30) lo
respeta.
"""

import sqlalchemy as sa
from alembic import op

revision = "0031_credits_loan_applications"
down_revision = "0030_credits_loan_rule_params"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('CREATE SCHEMA IF NOT EXISTS "credits"')
    op.create_table(
        "loan_applications",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("requested_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("term_months", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(20), server_default="SUBMITTED", nullable=False),
        sa.Column(
            "bureau_authorized",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("bureau_authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("dti_ratio", sa.Numeric(6, 4), nullable=True),
        sa.Column("total_income_minor", sa.BigInteger(), nullable=True),
        sa.Column("total_debt_minor", sa.BigInteger(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["credits.loan_products.id"],
            name="fk_loan_applications_product",
        ),
        sa.CheckConstraint(
            "requested_amount_minor > 0",
            name="ck_loan_applications_amount_pos",
        ),
        sa.CheckConstraint("term_months > 0", name="ck_loan_applications_term_pos"),
        sa.CheckConstraint(
            "status IN "
            "('DRAFT','SUBMITTED','IN_EVALUATION','APPROVED','REJECTED','IN_REVIEW','EXPIRED')",
            name="ck_loan_applications_status",
        ),
        schema="credits",
    )
    op.create_index(
        "ix_loan_applications_user_id",
        "loan_applications",
        ["user_id"],
        schema="credits",
    )
    op.create_index(
        "ix_loan_applications_status",
        "loan_applications",
        ["status"],
        schema="credits",
    )
    op.create_index(
        "ix_loan_applications_created_at",
        "loan_applications",
        ["created_at"],
        schema="credits",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_loan_applications_created_at",
        table_name="loan_applications",
        schema="credits",
    )
    op.drop_index(
        "ix_loan_applications_status",
        table_name="loan_applications",
        schema="credits",
    )
    op.drop_index(
        "ix_loan_applications_user_id",
        table_name="loan_applications",
        schema="credits",
    )
    op.drop_table("loan_applications", schema="credits")
