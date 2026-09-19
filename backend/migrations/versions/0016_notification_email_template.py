"""Plantilla de email del OTP (E1-T25, HU02 P-S2-01).

Revision ID: 0016_notification_email_template
Revises: 0015_identity_login
Create Date: 2026-09-18

Migracion DATA-ONLY: no cambia esquema. Inserta la plantilla base
`otp_code_email` (channel `email`, subject no nulo, body con `{code}` y
`{ttl_minutes}`) en `notifications.notification_templates`. `code` es UQ
global, por eso el codigo difiere del `otp_code` (sms); sin FK ni cambios en
`notifications`. Idempotente por `code` y reversible (el `downgrade` borra
solo esa fila).

Fuente: `docs/03b-diccionario-de-datos.md#13-schema-notifications`.
"""

from alembic import op

revision = "0016_notification_email_template"
down_revision = "0015_identity_login"
branch_labels = None
depends_on = None

EMAIL_OTP_CODE = "otp_code_email"
EMAIL_OTP_SUBJECT = "Tu codigo de verificacion"
EMAIL_OTP_BODY = "Tu codigo de verificacion es {code}. Vence en {ttl_minutes} minutos."


def upgrade() -> None:
    op.execute(
        "INSERT INTO notifications.notification_templates "
        "(id, code, channel, subject, body_template, enabled) VALUES "
        "(gen_random_uuid(), "
        f"'{EMAIL_OTP_CODE}', 'email', '{EMAIL_OTP_SUBJECT}', "
        f"'{EMAIL_OTP_BODY}', true) "
        "ON CONFLICT (code) DO NOTHING"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM notifications.notification_templates " f"WHERE code = '{EMAIL_OTP_CODE}'"
    )
