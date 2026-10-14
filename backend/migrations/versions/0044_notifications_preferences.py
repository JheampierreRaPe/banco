"""Preferencias de canal + claves de la ruta de seguridad (E6-T12, HU22).

Revision ID: 0044_notifications_preferences
Revises: 0043_wallet_service_reminder
Create Date: 2026-10-13

Fuente: `docs/tasks/E6-T12.md` (`docs/03b#13`: la tabla
`user_channel_preferences` estaba DIFERIDA; esta tarea la implementa) y
regla de oro 6 (claves configurables fuera del codigo, leidas via
`shared/config` best-effort con fallback). Crea SOLO objetos propios
del schema `notifications` (sin tocar schemas de otros modulos):

- `notifications.user_channel_preferences` (`user_id` PK logica a
  `users`, sin FK fisica entre schemas, regla de oro 4; `push`/`email`
  default `true`, `sms` default `false`; `updated_at` default `now()`).
- Siembra idempotente (`ON CONFLICT (key) DO NOTHING`: respeta un valor
  ya personalizado y es re-ejecutable) en la tabla transversal
  `config.parameters` (patron `0043_wallet_service_reminder`, espacio de
  claves gobernado por migraciones):
  - `notifications.security_delivery_window_seconds = 5` (ventana
    objetivo de entrega del push de seguridad en segundos, CA-01).
  - `notifications.security_alerts_email_backup = true` (el correo es
    respaldo garantizado de eventos de seguridad aunque el usuario lo
    desactive; decision del dueno documentada en el servicio).

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0044_notifications_preferences` (29) lo
respeta. **No mueve dinero.**
"""

import sqlalchemy as sa
from alembic import op

revision = "0044_notifications_preferences"
down_revision = "0043_wallet_service_reminder"
branch_labels = None
depends_on = None

WINDOW_KEY = "notifications.security_delivery_window_seconds"
BACKUP_KEY = "notifications.security_alerts_email_backup"

_SEEDS = (
    (
        WINDOW_KEY,
        "5",
        "notifications",
        "Ventana objetivo de entrega del push de seguridad en segundos (E6-T12)",
    ),
    (
        BACKUP_KEY,
        "true",
        "notifications",
        "Email como respaldo garantizado de alertas de seguridad (E6-T12)",
    ),
)


def upgrade() -> None:
    op.create_table(
        "user_channel_preferences",
        sa.Column("user_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("push", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("email", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("sms", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema="notifications",
    )
    for key, value, module, description in _SEEDS:
        op.execute(
            sa.text(
                "INSERT INTO config.parameters (id, key, value_json, module, description) "
                "VALUES (gen_random_uuid(), :key, CAST(:value AS jsonb), :module, "
                ":description) "
                "ON CONFLICT (key) DO NOTHING"
            ).bindparams(
                key=key,
                value=value,
                module=module,
                description=description,
            )
        )


def downgrade() -> None:
    for key, _value, _module, _description in _SEEDS:
        op.execute(sa.text("DELETE FROM config.parameters WHERE key = :key").bindparams(key=key))
    op.drop_table("user_channel_preferences", schema="notifications")
