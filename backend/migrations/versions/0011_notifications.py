"""Tablas del modulo notifications (E1-T09, HU02).

Revision ID: 0011_notifications
Revises: 0010_accounts_movements
Create Date: 2026-09-17

Fuente: `docs/03b-diccionario-de-datos.md#13-schema-notifications` (columnas
exactas) e indices `docs/03b#15` (`(user_id, created_at DESC)`, `(status)`).
Solo schema `notifications`; sin tocar otros schemas ni otras tablas.

Solo `notification_templates` + `notifications` (la fachada `send` no necesita
`user_channel_preferences`: se difiere a su tarea). Sin FK entre schemas:
`user_id` es UUID logico (referencia a usuarios solo a nivel aplicacion,
regla de oro 4) y `template_code` es codigo logico sin FK fisica.

Semilla: las 3 plantillas base (`otp_code`/sms, `account_activated`/email,
`login_alert`/push) segun el contrato del modulo; idempotente por UQ `code`.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_notifications"
down_revision = "0010_accounts_movements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_templates",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("subject", sa.String(150), nullable=True),
        sa.Column("body_template", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.UniqueConstraint("code", name="uq_notification_templates_code"),
        sa.CheckConstraint(
            "channel IN ('push', 'email', 'sms')",
            name="ck_notification_templates_channel",
        ),
        schema="notifications",
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("template_code", sa.String(50), nullable=True),
        sa.Column("payload_json", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(12), server_default="QUEUED", nullable=False),
        sa.Column("provider_ref", sa.String(120), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "channel IN ('push', 'email', 'sms')",
            name="ck_notifications_channel",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'SENT', 'FAILED')",
            name="ck_notifications_status",
        ),
        schema="notifications",
    )
    op.execute(
        "CREATE INDEX ix_notifications_user_created "
        "ON notifications.notifications (user_id, created_at DESC)"
    )
    op.create_index(
        "ix_notifications_status",
        "notifications",
        ["status"],
        schema="notifications",
    )
    for code, channel, subject, body in (
        (
            "otp_code",
            "sms",
            None,
            "Tu codigo de verificacion es {code}. Vence en {ttl_minutes} minutos.",
        ),
        (
            "account_activated",
            "email",
            "Tu cuenta esta activa",
            "Tu cuenta {account_masked} fue activada. Bienvenido/a.",
        ),
        (
            "login_alert",
            "push",
            None,
            "Nuevo inicio de sesion desde {device} el {at}.",
        ),
    ):
        subject_sql = "NULL" if subject is None else f"'{subject}'"
        op.execute(
            "INSERT INTO notifications.notification_templates "
            "(id, code, channel, subject, body_template, enabled) VALUES "
            f"(gen_random_uuid(), '{code}', '{channel}', {subject_sql}, '{body}', true) "
            "ON CONFLICT (code) DO NOTHING"
        )


def downgrade() -> None:
    op.drop_index("ix_notifications_status", table_name="notifications", schema="notifications")
    op.execute("DROP INDEX IF EXISTS notifications.ix_notifications_user_created")
    op.drop_table("notifications", schema="notifications")
    op.drop_table("notification_templates", schema="notifications")
