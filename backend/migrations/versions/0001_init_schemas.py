"""Esquemas base y tabla config.parameters

Revision ID: 0001_init_schemas
Revises:
Create Date: 2026-09-12

Crea los 13 esquemas del sistema (uno por modulo) y la tabla transversal de parametros
configurables con su semilla. NO crea tablas de negocio: cada modulo las agrega en su tarea.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_init_schemas"
down_revision = None
branch_labels = None
depends_on = None

SCHEMAS = (
    "shared",
    "config",
    "identity",
    "accounts",
    "transactions",
    "ledger",
    "credits",
    "wallet",
    "fx",
    "risk",
    "reconciliation",
    "notifications",
    "audit",
)

PARAMETERS = (
    (
        "transfer.biometric_threshold_minor",
        "1000000",
        "transactions",
        "Monto desde el cual se exige biometria (centimos)",
    ),
    (
        "transfer.daily_limit_minor",
        "500000",
        "transactions",
        "Limite diario de transferencias (centimos)",
    ),
    (
        "qr.express_limit_minor",
        "20000",
        "wallet",
        "Limite de pago QR express sin factor adicional (centimos)",
    ),
    ("otp.ttl_seconds", "600", "identity", "Vigencia del OTP en segundos"),
    ("otp.max_resends", "3", "identity", "Numero maximo de reenvios de OTP"),
    ("auth.max_failed_attempts", "5", "identity", "Intentos fallidos antes del bloqueo temporal"),
    (
        "session.inactivity_seconds",
        "180",
        "identity",
        "Segundos de inactividad antes de cerrar sesion",
    ),
    ("kyc.match_threshold", "0.68", "identity", "Umbral de coincidencia facial del KYC"),
    ("kyc.max_attempts", "3", "identity", "Reintentos maximos del KYC"),
    ("loan.max_dti_ratio", "0.4", "credits", "Relacion deuda/ingreso maxima"),
    ("fx.quote_ttl_seconds", "30", "fx", "Segundos que se congela una cotizacion"),
    ("fx.spread_base", "0.005", "fx", "Spread base de cambio de divisas"),
    ("pocket.yield_rate", "0.03", "fx", "Tasa anual de rendimiento de bolsillos"),
)


def upgrade() -> None:
    for schema in SCHEMAS:
        op.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

    op.create_table(
        "parameters",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value_json", postgresql.JSONB(), nullable=False),
        sa.Column("module", sa.String(length=40), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
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
        sa.UniqueConstraint("key", name="uq_config_parameters_key"),
        schema="config",
    )
    op.create_index("ix_config_parameters_module", "parameters", ["module"], schema="config")

    for key, value, module, description in PARAMETERS:
        op.execute(
            "INSERT INTO config.parameters "
            "(id, key, value_json, module, description) VALUES "
            f"(gen_random_uuid(), '{key}', '{value}'::jsonb, '{module}', '{description}')"
        )


def downgrade() -> None:
    op.drop_index("ix_config_parameters_module", table_name="parameters", schema="config")
    op.drop_table("parameters", schema="config")
    for schema in SCHEMAS:
        op.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
