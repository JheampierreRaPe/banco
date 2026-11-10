"""Parametro de la ventana del recordatorio de servicios (E4-T18, HU16 CA-03).

Revision ID: 0043_wallet_service_reminder
Revises: 0042_wallet_services
Create Date: 2026-10-13

Fuente: `docs/tasks/E4-T18.md` (ventana
`wallet.service_reminder_days_before`, default `3` dias segun HU16;
dueno del esquema `shared/config`, regla de oro 6). No crea tablas;
solo una fila de `config.parameters` (patron `0036_loan_reminder_param`).

Desviacion documentada del brief (decision del worker, ver `docs/18`):
el brief fija `revision="0043_wallet_service_reminder_param"` (34
caracteres), pero `alembic_version.version_num` es `VARCHAR(32)` y
Postgres rechaza el sello (`StringDataRightTruncation` verificado con
probe contra `banca_test`): `upgrade head` fallaria y los tests pg se
omitirian. El ARCHIVO conserva el nombre mandado
(`0043_wallet_service_reminder_param.py`); el `revision` se acorta a
`0043_wallet_service_reminder` (30 caracteres, dentro del limite de
`0036`). Cadena intacta: un solo head, reversible.

Siembra (idempotente, `ON CONFLICT (key) DO NOTHING`: respeta un valor
ya personalizado y es re-ejecutable):

- `wallet.service_reminder_days_before = 3` (dias antes del
  vencimiento derivado para el aviso push `service_reminder`; el job
  lee best-effort con fallback a `3`, asi que la fila solo fija el
  valor configurable via `PUT /admin/parameters`).
"""

import sqlalchemy as sa
from alembic import op

revision = "0043_wallet_service_reminder"
down_revision = "0042_wallet_services"
branch_labels = None
depends_on = None

REMINDER_KEY = "wallet.service_reminder_days_before"


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module, description) "
            "VALUES (gen_random_uuid(), :key, CAST(:value AS jsonb), :module, :description) "
            "ON CONFLICT (key) DO NOTHING"
        ).bindparams(
            key=REMINDER_KEY,
            value="3",
            module="wallet",
            description=(
                "Dias antes del vencimiento para el recordatorio push de servicios (job E4-T18)"
            ),
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM config.parameters WHERE key = :key").bindparams(key=REMINDER_KEY)
    )
