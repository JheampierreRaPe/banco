"""Catalogo configurable `credits.loan_products` (E3-T02, HU09).

Revision ID: 0029_credits_loan_products
Revises: 0028_backfill_entry_type
Create Date: 2026-10-09

Fuente: `docs/03b-diccionario-de-datos.md#81-loan_products` (columnas y
tipos exactos) y `docs/tasks/E3-T02.md`. Crea SOLO
`credits.loan_products` (el schema `credits` ya existe desde
`0001_init_schemas`; aqui solo `CREATE SCHEMA IF NOT EXISTS`
defensivo, sin tocar otros schemas ni otras tablas).

El enum `loan_application_status` (`docs/03b#1`) NO se declara: ninguna
tabla de este brief lo usa (lo creara E3-T08 con `loan_applications`).

Siembra (idempotente, `ON CONFLICT (code) DO NOTHING`: respeta tasas ya
personalizadas y es re-ejecutable) 3 productos demo con tasas `NUMERIC`
y montos en centimos (`BIGINT`); sin `float` (regla de oro 3). La copia
SQL esta congelada; la lista viva para tests/herramientas vive en
`credits.repository.DEMO_PRODUCTS`.

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0029_credits_loan_products` (26) lo respeta.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0029_credits_loan_products"
down_revision = "0028_backfill_entry_type"
branch_labels = None
depends_on = None

#: Productos demo (copia congelada de `credits.repository.DEMO_PRODUCTS`).
DEMO_PRODUCTS: tuple[dict, ...] = (
    {
        "code": "PERSONAL_LIBRE",
        "name": "Prestamo personal de libre disponibilidad",
        "tcea": "0.32500000",
        "tea": "0.28000000",
        "insurance_rate": "0.00350000",
        "min_amount_minor": 100_000,
        "max_amount_minor": 5_000_000,
        "min_term_months": 6,
        "max_term_months": 36,
        "allowed_terms": [6, 12, 18, 24, 36],
        "enabled": True,
    },
    {
        "code": "VEHICULAR",
        "name": "Credito vehicular",
        "tcea": "0.18500000",
        "tea": "0.16500000",
        "insurance_rate": "0.00280000",
        "min_amount_minor": 1_000_000,
        "max_amount_minor": 20_000_000,
        "min_term_months": 12,
        "max_term_months": 60,
        "allowed_terms": [12, 24, 36, 48, 60],
        "enabled": True,
    },
    {
        "code": "PYME_CAPITAL",
        "name": "Capital de trabajo PyME",
        "tcea": "0.24900000",
        "tea": "0.22000000",
        "insurance_rate": "0.00300000",
        "min_amount_minor": 500_000,
        "max_amount_minor": 15_000_000,
        "min_term_months": 6,
        "max_term_months": 48,
        "allowed_terms": [6, 12, 24, 36, 48],
        "enabled": True,
    },
)

_SEED_SQL = """INSERT INTO credits.loan_products
(id, code, name, tcea, tea, insurance_rate, min_amount_minor, max_amount_minor,
 min_term_months, max_term_months, allowed_terms, enabled)
VALUES (gen_random_uuid(), :code, :name, CAST(:tcea AS NUMERIC(18,8)),
 CAST(:tea AS NUMERIC(18,8)), CAST(:insurance_rate AS NUMERIC(18,8)),
 :min_amount_minor, :max_amount_minor, :min_term_months, :max_term_months,
 CAST(:allowed_terms AS SMALLINT[]), :enabled)
ON CONFLICT (code) DO NOTHING"""


def seed_demo_products(bind) -> int:
    """Siembra los productos demo; retorna filas insertadas (0 si ya existen).

    Idempotente por `ON CONFLICT (code) DO NOTHING`: re-ejecutable sin
    duplicar ni pisar tasas personalizadas.
    """
    total = 0
    for spec in DEMO_PRODUCTS:
        result = bind.execute(sa.text(_SEED_SQL).bindparams(**spec))
        total += int(result.rowcount or 0)
    return total


def upgrade() -> None:
    op.execute('CREATE SCHEMA IF NOT EXISTS "credits"')
    op.create_table(
        "loan_products",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("tcea", sa.Numeric(18, 8), nullable=False),
        sa.Column("tea", sa.Numeric(18, 8), nullable=True),
        sa.Column("insurance_rate", sa.Numeric(18, 8), server_default="0", nullable=False),
        sa.Column("min_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("max_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("min_term_months", sa.SmallInteger(), nullable=False),
        sa.Column("max_term_months", sa.SmallInteger(), nullable=False),
        sa.Column("allowed_terms", postgresql.ARRAY(sa.SmallInteger()), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
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
        sa.UniqueConstraint("code", name="uq_loan_products_code"),
        sa.CheckConstraint(
            "min_amount_minor <= max_amount_minor",
            name="ck_loan_products_amount_range",
        ),
        sa.CheckConstraint(
            "min_term_months <= max_term_months",
            name="ck_loan_products_term_range",
        ),
        sa.CheckConstraint("tcea >= 0", name="ck_loan_products_tcea_nonneg"),
        sa.CheckConstraint("tea IS NULL OR tea >= 0", name="ck_loan_products_tea_nonneg"),
        sa.CheckConstraint("insurance_rate >= 0", name="ck_loan_products_insurance_nonneg"),
        schema="credits",
    )
    op.create_index(
        "ix_credits_loan_products_enabled",
        "loan_products",
        ["enabled"],
        schema="credits",
    )
    seed_demo_products(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    for spec in DEMO_PRODUCTS:
        bind.execute(
            sa.text("DELETE FROM credits.loan_products WHERE code = :code").bindparams(
                code=spec["code"]
            )
        )
    op.drop_index("ix_credits_loan_products_enabled", table_name="loan_products", schema="credits")
    op.drop_table("loan_products", schema="credits")
