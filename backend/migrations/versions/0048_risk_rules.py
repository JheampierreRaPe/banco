"""Reglas antifraude `risk.risk_rules` + parametros globales (E6-T01, HU20).

Revision ID: 0048_risk_rules
Revises: 0047_wallet_personal_qr
Create Date: 2026-11-10

Crea SOLO `risk.risk_rules` (dueno: `risk`; no toca
`transactions`/`ledger`/`accounts`/`identity`/`fx`/`reconciliation`):

- `risk.risk_rules(id UUID PK, code VARCHAR(40) UQ, name VARCHAR(120),
  enabled BOOLEAN default true, priority SMALLINT default 100,
  params_json JSONB default '{}', action VARCHAR(15) default 'ALERT'
  CHECK IN ('ALERT','HOLD','BLOCK'), created_at, updated_at,
  updated_by UUID NULL)` segun `docs/03b#11.1`.
- Indices: `ix_risk_rules_enabled` por `enabled`, `ix_risk_rules_priority`
  por `priority` (la evaluacion de `E6-T02` ordena `priority, code`).

Siembra idempotente (`ON CONFLICT DO NOTHING`: respeta valores ya
personalizados y es re-ejecutable) de las 5 reglas iniciales, todas
`enabled=true`, `action='ALERT'` y umbrales conservadores (la
calibracion fina es de `E6-T06`; NO se usa `HOLD`/`BLOCK` en la
semilla):

- `FACIAL_SPOOFING` (prioridad 10): fallos biometricos reiterados.
- `ATYPICAL_GEO` (prioridad 20): pais no habitual.
- `UNUSUAL_AMOUNT` (prioridad 30): monto > `k * promedio`.
- `VELOCITY` (prioridad 40): N operaciones en la ventana.
- `NEW_DEVICE` (prioridad 50): dispositivo no visto.

Ademas siembra (idempotente, `ON CONFLICT DO NOTHING`) las 2 claves
globales que las reglas leen por la fachada `shared/config` con
fallback (`E6-T01`): `risk.velocity_window_seconds` (`600`) y
`risk.unusual_amount_factor` (`3.0`).

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0048_risk_rules` (15) lo respeta.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048_risk_rules"
down_revision = "0047_wallet_personal_qr"
branch_labels = None
depends_on = None

RULES: tuple[tuple[str, str, bool, int, str, str], ...] = (
    (
        "FACIAL_SPOOFING",
        "Suplantacion / spoofing facial",
        True,
        10,
        '{"failed_attempts_threshold": 3, "block_on_lock": true}',
        "ALERT",
    ),
    (
        "ATYPICAL_GEO",
        "Geolocalizacion atipica",
        True,
        20,
        '{"mode": "country_mismatch", "score_bump": 10}',
        "ALERT",
    ),
    (
        "UNUSUAL_AMOUNT",
        "Monto inusual",
        True,
        30,
        '{"factor": 3.0, "min_amount_minor": 10000}',
        "ALERT",
    ),
    (
        "VELOCITY",
        "Velocidad de operaciones",
        True,
        40,
        '{"max_operations": 10, "window_seconds": 600}',
        "ALERT",
    ),
    (
        "NEW_DEVICE",
        "Dispositivo nuevo",
        True,
        50,
        '{"score_bump": 5}',
        "ALERT",
    ),
)

GLOBAL_PARAMS: tuple[tuple[str, str, str, str], ...] = (
    (
        "risk.velocity_window_seconds",
        "600",
        "risk",
        "Ventana global de velocidad antifraude en segundos (E6-T01)",
    ),
    (
        "risk.unusual_amount_factor",
        "3.0",
        "risk",
        "Factor global de monto inusual vs promedio (E6-T01)",
    ),
)


def upgrade() -> None:
    op.create_table(
        "risk_rules",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default="100"),
        sa.Column(
            "params_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("action", sa.String(15), nullable=False, server_default="ALERT"),
        sa.CheckConstraint(
            "action IN ('ALERT', 'HOLD', 'BLOCK')",
            name="ck_risk_rules_action",
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
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        schema="risk",
    )
    op.create_index("ix_risk_rules_enabled", "risk_rules", ["enabled"], schema="risk")
    op.create_index("ix_risk_rules_priority", "risk_rules", ["priority"], schema="risk")
    for code, name, enabled, priority, params_json, action in RULES:
        op.execute(
            sa.text(
                "INSERT INTO risk.risk_rules "
                "(id, code, name, enabled, priority, params_json, action) "
                "VALUES (gen_random_uuid(), :code, :name, :enabled, :priority, "
                "CAST(:params_json AS jsonb), :action) "
                "ON CONFLICT (code) DO NOTHING"
            ).bindparams(
                code=code,
                name=name,
                enabled=enabled,
                priority=priority,
                params_json=params_json,
                action=action,
            )
        )
    for key, value, module, description in GLOBAL_PARAMS:
        op.execute(
            sa.text(
                "INSERT INTO config.parameters (id, key, value_json, module, description) "
                "VALUES (gen_random_uuid(), :key, CAST(:value AS jsonb), :module, :description) "
                "ON CONFLICT (key) DO NOTHING"
            ).bindparams(key=key, value=value, module=module, description=description)
        )


def downgrade() -> None:
    for key, _, _, _ in GLOBAL_PARAMS:
        op.execute(sa.text("DELETE FROM config.parameters WHERE key = :key").bindparams(key=key))
    op.drop_index("ix_risk_rules_priority", table_name="risk_rules", schema="risk")
    op.drop_index("ix_risk_rules_enabled", table_name="risk_rules", schema="risk")
    op.drop_table("risk_rules", schema="risk")
