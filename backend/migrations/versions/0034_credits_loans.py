"""Prestamos y cronograma definitivo `credits.loans` + `loan_schedules` + `loan_payments` (E3-T18, HU12).

Revision ID: 0034_credits_loans
Revises: 0033_credits_contracts
Create Date: 2026-10-13

Fuente: `docs/03b-diccionario-de-datos.md#83-loans` / `#84-loan_schedules` /
`#85-loan_payments` (columnas y tipos exactos) y `docs/tasks/E3-T18.md`.
Crea SOLO esas tres tablas (el schema `credits` ya existe; aqui solo
`CREATE SCHEMA IF NOT EXISTS` defensivo, sin tocar otras tablas ni
crear `transactions`/`journal_entries`/`postings`: el dinero es E3-T17).

Desvios registrados en `docs/18` (gap E3-T18, G61 aplicado):
- Columna aditiva `loans.auto_debit_enabled BOOLEAN NOT NULL DEFAULT
  false` (no prevista en `docs/03b#83`; necesaria para CA-03 de HU12;
  la consume `E3-T19`).
- Los enums `loan_status` / `installment_status` (`docs/03b#1`) se
  aplican como `CHECK` sobre `VARCHAR` (no como enum Postgres:
  portable y sin drift en `alembic check`, mismo criterio que
  `0031_credits_loan_applications` / `0033_credits_contracts`).
- `account_id` es REF logica a `accounts.accounts` (sin FK fisica) y
  `loan_payments.transaction_id` es REF logica a
  `transactions.transactions` (sin FK fisica): sin FK entre schemas
  (regla de oro 4). `application_id` / `loan_id` / `schedule_id` son
  FK fisicas dentro de `credits`.

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0034_credits_loans` (18) lo respeta.
"""

import sqlalchemy as sa
from alembic import op

revision = "0034_credits_loans"
down_revision = "0033_credits_contracts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('CREATE SCHEMA IF NOT EXISTS "credits"')
    op.create_table(
        "loans",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("principal_minor", sa.BigInteger(), nullable=False),
        sa.Column("tcea", sa.Numeric(18, 8), nullable=False),
        sa.Column("term_months", sa.SmallInteger(), nullable=False),
        sa.Column(
            "insurance_rate",
            sa.Numeric(18, 8),
            server_default="0",
            nullable=False,
        ),
        sa.Column("outstanding_principal_minor", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(20), server_default="ACTIVE", nullable=False),
        sa.Column("disbursed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "auto_debit_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["credits.loan_applications.id"],
            name="fk_loans_application",
        ),
        sa.UniqueConstraint(
            "application_id",
            name="uq_loans_application_id",
        ),
        sa.CheckConstraint(
            "principal_minor > 0",
            name="ck_loans_principal_pos",
        ),
        sa.CheckConstraint(
            "outstanding_principal_minor >= 0",
            name="ck_loans_outstanding_nonneg",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','PAID','DEFAULTED','WRITTEN_OFF')",
            name="ck_loans_status",
        ),
        schema="credits",
    )
    op.create_index(
        "ix_loans_status",
        "loans",
        ["status"],
        schema="credits",
    )
    op.create_table(
        "loan_schedules",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("loan_id", sa.Uuid(), nullable=False),
        sa.Column("installment_no", sa.SmallInteger(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("principal_minor", sa.BigInteger(), nullable=False),
        sa.Column("interest_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "insurance_minor",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("total_minor", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(15), server_default="PENDING", nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["loan_id"],
            ["credits.loans.id"],
            name="fk_loan_schedules_loan",
        ),
        sa.UniqueConstraint(
            "loan_id",
            "installment_no",
            name="uq_loan_schedules_loan_installment",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','PARTIAL','PAID','OVERDUE')",
            name="ck_loan_schedules_status",
        ),
        schema="credits",
    )
    op.create_index(
        "ix_loan_schedules_loan_due_status",
        "loan_schedules",
        ["loan_id", "due_date", "status"],
        schema="credits",
    )
    op.create_table(
        "loan_payments",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("loan_id", sa.Uuid(), nullable=False),
        sa.Column("schedule_id", sa.Uuid(), nullable=True),
        sa.Column("transaction_id", sa.Uuid(), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "principal_applied_minor",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "interest_applied_minor",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "insurance_applied_minor",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "paid_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["loan_id"],
            ["credits.loans.id"],
            name="fk_loan_payments_loan",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["credits.loan_schedules.id"],
            name="fk_loan_payments_schedule",
        ),
        sa.CheckConstraint(
            "amount_minor > 0",
            name="ck_loan_payments_amount_pos",
        ),
        schema="credits",
    )
    op.create_index(
        "ix_loan_payments_loan_id",
        "loan_payments",
        ["loan_id"],
        schema="credits",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_loan_payments_loan_id",
        table_name="loan_payments",
        schema="credits",
    )
    op.drop_table("loan_payments", schema="credits")
    op.drop_index(
        "ix_loan_schedules_loan_due_status",
        table_name="loan_schedules",
        schema="credits",
    )
    op.drop_table("loan_schedules", schema="credits")
    op.drop_index(
        "ix_loans_status",
        table_name="loans",
        schema="credits",
    )
    op.drop_table("loans", schema="credits")
