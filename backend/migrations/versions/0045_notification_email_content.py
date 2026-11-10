"""Contenido pulido de los correos de notificacion (feedback del dueno).

Revision ID: 0045_notification_email_content
Revises: 0044_notifications_preferences
Create Date: 2026-10-15

Migracion DATA-ONLY: no cambia esquema. Actualiza las filas semilla de
`notifications.notification_templates` (`otp_code`, `account_activated`,
`login_alert` de `0011` y `otp_code_email` de `0016`) a los textos pulidos
de `app.modules.notifications.domain.templates.BASE_TEMPLATES` (tildes,
claridad y tono consistente). Idempotente y re-ejecutable (`UPDATE ...
WHERE code = ...`: 0 filas si la semilla aun no existe; la fachada usa la
base en ese caso). Reversible: el `downgrade` restaura los textos previos.

Alcance: solo esas 4 filas (las plantillas de seguridad/creditos/wallet
viven solo en `BASE_TEMPLATES` y no tienen semilla en BD). **No mueve
dinero.**
"""

import sqlalchemy as sa
from alembic import op

revision = "0045_notification_email_content"
down_revision = "0044_notifications_preferences"
branch_labels = None
depends_on = None

_NEW = (
    # (code, subject, body_template)
    (
        "otp_code",
        None,
        "Tu código de verificación es {code}. Vence en {ttl_minutes} minutos.",
    ),
    (
        "otp_code_email",
        "Tu código de verificación",
        (
            "Tu código de verificación es {code}. Vence en {ttl_minutes} minutos. "
            "Si no solicitaste este código, ignora este mensaje."
        ),
    ),
    (
        "account_activated",
        "Tu cuenta está activa",
        (
            "Tu cuenta {account_masked} fue activada. "
            "Ya puedes ingresar a tu banca en línea. ¡Bienvenido/a!"
        ),
    ),
    (
        "login_alert",
        None,
        ("Nuevo inicio de sesión desde {device} el {at}. " "Si no fuiste tú, revisa tu actividad."),
    ),
)

_OLD = (
    (
        "otp_code",
        None,
        "Tu codigo de verificacion es {code}. Vence en {ttl_minutes} minutos.",
    ),
    (
        "otp_code_email",
        "Tu codigo de verificacion",
        "Tu codigo de verificacion es {code}. Vence en {ttl_minutes} minutos.",
    ),
    (
        "account_activated",
        "Tu cuenta esta activa",
        "Tu cuenta {account_masked} fue activada. Bienvenido/a.",
    ),
    (
        "login_alert",
        None,
        "Nuevo inicio de sesion desde {device} el {at}.",
    ),
)


def _apply(rows) -> None:
    for code, subject, body in rows:
        op.execute(
            sa.text(
                "UPDATE notifications.notification_templates "
                "SET subject = :subject, body_template = :body "
                "WHERE code = :code"
            ).bindparams(subject=subject, body=body, code=code)
        )


def upgrade() -> None:
    _apply(_NEW)


def downgrade() -> None:
    _apply(_OLD)
