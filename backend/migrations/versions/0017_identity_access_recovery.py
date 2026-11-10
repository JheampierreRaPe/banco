"""Tabla de recuperaciones de acceso concedidas (E1-T31, HU04).

Revision ID: 0017_identity_access_recovery
Revises: 0016_notification_email_template
Create Date: 2026-09-21

Fuente: `docs/03b-diccionario-de-datos.md#48-access_recovery` (columnas
exactas: `user_id`, `method`, `verification_result`, `device_id`,
`new_credential_set`, `notified_channels`, `created_at`) e indice
`docs/03b#15` (`access_recovery`: `(user_id)`). Solo schema `identity`;
sin tocar otras tablas ni otros schemas. Sin FK entre schemas:
`access_recovery.user_id` es FK contenida en `identity.users` (mismo
schema, permitido por la regla de oro 4).

Desviaciones documentadas (igual que en el modelo `AccessRecovery`):
- `verification_result`/`notified_channels` son `JSONB` en Postgres (el
  modelo usa `sa.JSON()` portable para el SQLite de las pruebas;
  `migrations/env.py::compare_type` los equipara: equivalencia
  intencional, no drift).
- `new_credential_set BOOLEAN NOT NULL DEFAULT false` (E1-T31 inserta
  `false`: aqui no hay cambio de credencial, a diferencia de la HU04
  canonica).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017_identity_access_recovery"
down_revision = "0016_notification_email_template"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_recovery",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("method", sa.String(30), nullable=False),
        sa.Column("verification_result", postgresql.JSONB(), nullable=True),
        sa.Column("device_id", sa.String(128), nullable=True),
        sa.Column(
            "new_credential_set",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
        sa.Column("notified_channels", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["identity.users.id"],
            name="fk_access_recovery_user",
        ),
        sa.CheckConstraint(
            "method IN ('DEVICE_BIOMETRIC', 'OTP')",
            name="ck_access_recovery_method",
        ),
        schema="identity",
    )
    op.create_index(
        "ix_access_recovery_user",
        "access_recovery",
        ["user_id"],
        schema="identity",
    )


def downgrade() -> None:
    op.drop_index("ix_access_recovery_user", table_name="access_recovery", schema="identity")
    op.drop_table("access_recovery", schema="identity")
