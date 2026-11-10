"""Perfiles de riesgo minimos + limite por perfil (E2-T18, HU08).

Revision ID: 0023_risk_profiles_params
Revises: 0022_beneficiaries_active_uq
Create Date: 2026-09-27

Fuente: `docs/03b-diccionario-de-datos.md#112-risk_profiles` (columnas ya
fijadas) y `docs/tasks/E2-T18.md#modelo-de-datos` (claves por perfil).

Crea SOLO `risk.risk_profiles` (minimo que el motor consulta): no toca
`alerts`/`risk_rules` (los crea HU20: E6-T03/E6-T04, Sprint 4, D-16) ni
ningun otro schema. Sin FK entre schemas: `user_id` es UUID logico (REF
a `identity.users`, sin constraint fisico). Sin `float`: montos en
`BIGINT` de centimos.

Siembra (idempotente, `ON CONFLICT DO NOTHING`: respeta valores ya
personalizados y es re-ejecutable) la clave de limite del perfil
`STANDARD` (`transfer.daily_limit_STANDARD_minor = 500000`, igual que el
generico de `0001` para migrar sin sorpresas). Perfiles nuevos los
siembran sus migraciones (HU20) con el mismo patron
`transfer.daily_limit_{PERFIL}_minor`.

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): por eso el nombre corto.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023_risk_profiles_params"
down_revision = "0022_beneficiaries_active_uq"
branch_labels = None
depends_on = None

PROFILE_KEYS: tuple[tuple[str, str, str, str], ...] = (
    (
        "transfer.daily_limit_STANDARD_minor",
        "500000",
        "transactions",
        "Limite diario de transferencias del perfil STANDARD (centimos)",
    ),
)


def upgrade() -> None:
    op.create_table(
        "risk_profiles",
        sa.Column("user_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "avg_amount_minor",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("usual_countries", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("usual_hours", postgresql.ARRAY(sa.SmallInteger()), nullable=True),
        sa.Column("last_device_id", sa.String(128), nullable=True),
        sa.Column(
            "score",
            sa.SmallInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema="risk",
    )
    for key, value, module, description in PROFILE_KEYS:
        op.execute(
            sa.text(
                "INSERT INTO config.parameters (id, key, value_json, module, description) "
                "VALUES (gen_random_uuid(), :key, CAST(:value AS jsonb), :module, :description) "
                "ON CONFLICT (key) DO NOTHING"
            ).bindparams(key=key, value=value, module=module, description=description)
        )


def downgrade() -> None:
    for key, _, _, _ in PROFILE_KEYS:
        op.execute(sa.text("DELETE FROM config.parameters WHERE key = :key").bindparams(key=key))
    op.drop_table("risk_profiles", schema="risk")
