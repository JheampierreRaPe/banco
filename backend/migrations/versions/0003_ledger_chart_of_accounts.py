"""Catalogo contable y ledger_accounts (E5-T09, HU18).

Revision ID: 0003_ledger_chart_of_accounts
Revises: 0002_transactions_persistence
Create Date: 2026-09-17

Fuente: `docs/03b-diccionario-de-datos.md#71-ledger_accounts` y
`docs/04-motor-transaccional-y-ledger.md#21-subcuentas-de-un-cliente`.
Solo schema `ledger`; sin tocar otros schemas.
Sin FK entre schemas: `owner_ref` es UUID logico; `parent_account_id`
es autocontenida (`ledger.ledger_accounts.id`).

Nota: `code` se crea como VARCHAR(50) (no 30) porque el formato exigido
`2000-<uuid>` (41 caracteres) no cabe en 30. Unicidad (`UQ`) intacta.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_ledger_chart_of_accounts"
down_revision = "0002_transactions_persistence"
branch_labels = None
depends_on = None

# (code, name, type, currency, owner_type, is_system)
CATALOG = (
    ("1000", "Caja y bancos", "asset", "PEN", "SYSTEM", True),
    ("2000", "Depositos de clientes - disponible", "liability", "PEN", "SYSTEM", True),
    ("2100", "Depositos de clientes - retenido", "liability", "PEN", "SYSTEM", True),
    ("2200", "Otras obligaciones", "liability", "PEN", "SYSTEM", True),
    ("3000", "Capital", "equity", "PEN", "SYSTEM", True),
    ("4000", "Ingresos por intereses", "income", "PEN", "SYSTEM", True),
    ("4100", "Ingresos por comisiones", "income", "PEN", "SYSTEM", True),
    ("4200", "Otros ingresos", "income", "PEN", "SYSTEM", True),
    ("5000", "Costos financieros", "expense", "PEN", "SYSTEM", True),
    ("6000", "Gastos operativos", "expense", "PEN", "SYSTEM", True),
    ("9100", "Cuentas de orden", "asset", "PEN", "SYSTEM", True),
)


def upgrade() -> None:
    op.create_table(
        "ledger_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("type", sa.String(12), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("owner_type", sa.String(20), server_default="SYSTEM", nullable=False),
        sa.Column("owner_ref", sa.Uuid(), nullable=True),
        sa.Column("parent_account_id", sa.Uuid(), nullable=True),
        sa.Column("is_system", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("code", name="uq_ledger_accounts_code"),
        sa.CheckConstraint(
            "type IN ('asset', 'liability', 'equity', 'income', 'expense')",
            name="ck_ledger_accounts_type",
        ),
        sa.ForeignKeyConstraint(
            ["parent_account_id"],
            ["ledger.ledger_accounts.id"],
            name="fk_ledger_accounts_parent",
        ),
        schema="ledger",
    )
    op.create_index(
        "ix_ledger_accounts_owner_ref",
        "ledger_accounts",
        ["owner_ref"],
        schema="ledger",
    )
    op.create_index(
        "ix_ledger_accounts_type",
        "ledger_accounts",
        ["type"],
        schema="ledger",
    )

    # Seed del catalogo minimo (idempotente por `code`).
    for code, name, type_, currency, owner_type, is_system in CATALOG:
        op.execute(
            sa.text(
                """
                INSERT INTO ledger.ledger_accounts
                    (id, code, name, type, currency, owner_type, is_system)
                VALUES
                    (gen_random_uuid(), :code, :name, :type, :currency,
                     :owner_type, :is_system)
                ON CONFLICT (code) DO NOTHING
                """
            ).bindparams(
                code=code,
                name=name,
                type=type_,
                currency=currency,
                owner_type=owner_type,
                is_system=is_system,
            )
        )


def downgrade() -> None:
    op.drop_index("ix_ledger_accounts_type", table_name="ledger_accounts", schema="ledger")
    op.drop_index(
        "ix_ledger_accounts_owner_ref",
        table_name="ledger_accounts",
        schema="ledger",
    )
    op.drop_table("ledger_accounts", schema="ledger")
