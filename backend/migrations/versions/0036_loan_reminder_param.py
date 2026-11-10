"""Parametro del recordatorio de cuota (E3-T19, HU12 CA-04).

Revision ID: 0036_loan_reminder_param
Revises: 0035_ledger_catalog_fix
Create Date: 2026-10-13

Fuente: `docs/tasks/E3-T19.md` (decision 1 del brief: ventana
`loan.reminder_days_before`, default `3` dias segun HU12 2-3 dias;
dueno del esquema `shared/config`, regla de oro 6). No crea tablas;
solo una fila de `config.parameters` (como `0030`).

Siembra (idempotente, `ON CONFLICT (key) DO NOTHING`: respeta un valor
ya personalizado y es re-ejecutable):

- `loan.reminder_days_before = 3` (dias antes del vencimiento para el
  aviso push `loan_installment_reminder`; el job lee best-effort con
  fallback a `3`, asi que la fila solo fija el valor configurable
  via `PUT /admin/parameters`).

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0036_loan_reminder_param` (24) lo respeta.
"""

import sqlalchemy as sa
from alembic import op

revision = "0036_loan_reminder_param"
down_revision = "0035_ledger_catalog_fix"
branch_labels = None
depends_on = None

REMINDER_KEY = "loan.reminder_days_before"


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module, description) "
            "VALUES (gen_random_uuid(), :key, CAST(:value AS jsonb), :module, :description) "
            "ON CONFLICT (key) DO NOTHING"
        ).bindparams(
            key=REMINDER_KEY,
            value="3",
            module="credits",
            description=(
                "Dias antes del vencimiento para el recordatorio push de cuota (job E3-T19)"
            ),
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM config.parameters WHERE key = :key").bindparams(key=REMINDER_KEY)
    )
