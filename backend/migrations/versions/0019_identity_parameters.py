"""Siembra en `config.parameters` las constantes de identidad (E1-T34, SCR-005).

Revision ID: 0019_identity_parameters
Revises: 0018_users_email_not_null
Create Date: 2026-09-22

Fuente: `docs/tasks/E1-T34.md#modelo-de-datos` (regla de oro 6: toda regla
variable va en `config.parameters`). Valores = constantes actuales del
codigo, para migrar sin sorpresas (el comportamiento no cambia al aplicar):

| key | valor | origen |
|---|---|---|
| `auth.lockout_seconds` | `900` | `pin_login.LOCKOUT_SECONDS` |
| `auth.max_failed_attempts` | `5` | `pin_login.MAX_FAILED_ATTEMPTS` (ya sembrada en `0001`; se incluye idempotente) |
| `otp.resend_wait_seconds` | `30` | `otp_service.OTP_RESEND_WAIT_SECONDS` |
| `otp.max_attempts` | `3` | `otp_service.OTP_MAX_ATTEMPTS` |
| `auth.recovery_request_window_seconds` | `60` | `recovery.recovery_request_rate_limit_cfg` |
| `auth.recovery_request_max_requests` | `10` | idem |
| `auth.recovery_verify_window_seconds` | `60` | `recovery.recovery_verify_rate_limit_cfg` |
| `auth.recovery_verify_max_requests` | `10` | idem |

No se duplican `otp.ttl_seconds` ni `otp.max_resends` (ya sembradas en
`0001_init_schemas.py`).

Idempotencia: `INSERT ... ON CONFLICT (key) DO NOTHING` (re-ejecutable sin
duplicar; respeta valores ya personalizados: no pisa claves existentes).
Reversible: el downgrade elimina SOLO las 7 claves nuevas de esta revision
(`auth.max_failed_attempts` la conserva: la sembro `0001` y el downgrade de
`0001` la elimina con la tabla).

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): por eso el nombre corto.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019_identity_parameters"
down_revision = "0018_users_email_not_null"
branch_labels = None
depends_on = None

MODULE = "identity"

PARAMETERS: tuple[tuple[str, str, str], ...] = (
    ("auth.lockout_seconds", "900", "Segundos de bloqueo temporal tras agotar intentos del PIN"),
    ("auth.max_failed_attempts", "5", "Intentos fallidos de PIN antes del bloqueo temporal"),
    ("otp.resend_wait_seconds", "30", "Espera minima entre reenvios de OTP en segundos"),
    ("otp.max_attempts", "3", "Intentos fallidos por codigo OTP antes de bloquearlo"),
    (
        "auth.recovery_request_window_seconds",
        "60",
        "Ventana del rate-limit de POST /auth/recovery/request en segundos",
    ),
    (
        "auth.recovery_request_max_requests",
        "10",
        "Maximo de POST /auth/recovery/request por ventana (por email+IP)",
    ),
    (
        "auth.recovery_verify_window_seconds",
        "60",
        "Ventana del rate-limit de POST /auth/recovery/verify y /auth/pin-reset en segundos",
    ),
    (
        "auth.recovery_verify_max_requests",
        "10",
        "Maximo de POST /auth/recovery/verify y /auth/pin-reset por ventana (por email+IP)",
    ),
)

#: Claves nuevas de esta revision (las que elimina el downgrade).
NEW_KEYS: tuple[str, ...] = tuple(
    key for key, _, _ in PARAMETERS if key != "auth.max_failed_attempts"
)


def upgrade() -> None:
    for key, value, description in PARAMETERS:
        op.execute(
            sa.text(
                "INSERT INTO config.parameters (id, key, value_json, module, description) "
                "VALUES (gen_random_uuid(), :key, CAST(:value AS jsonb), :module, :description) "
                "ON CONFLICT (key) DO NOTHING"
            ).bindparams(key=key, value=value, module=MODULE, description=description)
        )


def downgrade() -> None:
    for key in NEW_KEYS:
        op.execute(sa.text("DELETE FROM config.parameters WHERE key = :key").bindparams(key=key))
