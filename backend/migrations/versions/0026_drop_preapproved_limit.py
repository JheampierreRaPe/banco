"""Retiro real del tope pre-aprobado de beneficiarios (E2-T33, HU07).

Revision ID: 0026_drop_preapproved_limit
Revises: 0025_biometric_coherence
Create Date: 2026-10-09

Nota de nombre: el brief proponia `0026_beneficiaries_drop_preapproved_limit`
(43 caracteres), pero `alembic_version.version_num` es `VARCHAR(32)` (ver
`0018`/`0019`/`0020`/`0022`/`0023`: "por eso el nombre corto") y el
`UPDATE alembic_version` falla con el nombre largo. Se usa el nombre corto
equivalente (27 caracteres, misma semantica).

Decision del dueno (2026-10-08, `8-10-2026-2.txt` D1): el tope pre-aprobado
por beneficiario se elimina; la unica regla es el umbral biometrico global
(`transfer.biometric_threshold_minor = 100000`, migracion `0025`).

`upgrade()`: neutraliza primero (`UPDATE ... SET preapproved_limit_minor =
NULL`) y luego elimina el `CHECK` (`ck_beneficiaries_preapproved_limit_min`)
y la columna (`accounts.beneficiaries.preapproved_limit_minor`). Los datos
con tope quedan efectivamente neutralizados al desaparecer la columna.

`downgrade()`: re-agrega la columna nulable y el `CHECK` con el mismo
nombre. Solo restaura el esquema: los datos de tope NO se restauran
(irreversibilidad de datos documentada).

Solo schema `accounts`; sin tocar otros schemas ni parametros
(`transfer.biometric_threshold_minor` y `transfer.daily_limit_*`
conservan sus valores).
"""

import sqlalchemy as sa
from alembic import op

revision = "0026_drop_preapproved_limit"
down_revision = "0025_biometric_coherence"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_beneficiaries_preapproved_limit_min"


def upgrade() -> None:
    op.execute(sa.text("UPDATE accounts.beneficiaries SET preapproved_limit_minor = NULL"))
    op.drop_constraint(_CONSTRAINT, "beneficiaries", schema="accounts", type_="check")
    op.drop_column("beneficiaries", "preapproved_limit_minor", schema="accounts")


def downgrade() -> None:
    op.add_column(
        "beneficiaries",
        sa.Column("preapproved_limit_minor", sa.BigInteger(), nullable=True),
        schema="accounts",
    )
    op.create_check_constraint(
        _CONSTRAINT,
        "beneficiaries",
        sa.text("preapproved_limit_minor IS NULL OR preapproved_limit_minor >= 0"),
        schema="accounts",
    )
