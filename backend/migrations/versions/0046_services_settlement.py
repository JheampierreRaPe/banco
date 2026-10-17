"""Cuenta de liquidacion demo de servicios/recargas (fix HU16).

Revision ID: 0046_services_settlement
Revises: 0045_notification_email_content
Create Date: 2026-10-15

BUG (critico, HU16): `POST /services/payments` y `POST /topups` fallaban
con `422 VALIDATION_ERROR` ("algun campo es invalido") en la demo. Causa
raiz: `wallet.service.services.resolve_settlement_account` lee
`WALLET_SERVICES_SETTLEMENT_ACCOUNT_ID` (regla de oro 6, fuera del
codigo) y, si falta la env **o** no existe una cuenta `accounts`
`ACTIVE` con esa moneda, rechaza fail-closed antes de mover dinero. En el
entorno docker la env no se inyectaba y **no habia cuenta de liquidacion
sembrada** (la demo solo corre `alembic upgrade head`; los usuarios se
crean por el flujo real de la app).

Fix: esta migracion DATA-ONLY siembra la cuenta interna de liquidacion
del proveedor externo (sin usuario ni cuenta en el core) que el motor
acredita al liquidar (`docs/04#7.10`: `Debe 2100-Pagador / Haber
2000-Liquidacion`). UUID fijo y determinista (documentado en
`.env.example` de la raiz y `backend/.env.example`), moneda `PEN`
(MVP), estado `ACTIVE` y fila de saldo en cero. Idempotente
(`ON CONFLICT DO NOTHING`), reversible (el `downgrade` borra solo la
fila sembrada). Un solo head, `alembic check` limpio.

Espejo en `tests/factories.py::demo_settlement_account` (mismo UUID) y
en `docker-compose.yml` (env inyectada a `backend`/`worker`). **No mueve
dinero.**
"""

import sqlalchemy as sa
from alembic import op

revision = "0046_services_settlement"
down_revision = "0045_notification_email_content"
branch_labels = None
depends_on = None

#: UUID fijo de la cuenta de liquidacion demo (PEN). Debe coincidir con
#: `WALLET_SERVICES_SETTLEMENT_ACCOUNT_ID` de `.env.example` (raiz y
#: `backend/`) y con `tests/factories.SETTLEMENT_ACCOUNT_ID`.
SETTLEMENT_ACCOUNT_ID = "d0000000-0000-4000-8000-000000000001"
#: Titular sintetico interno del proveedor (REF logica sin FK fisica,
#: regla de oro 4; no es un usuario de `identity`).
SETTLEMENT_OWNER_ID = "d0000000-0000-4000-8000-0000000000ff"
SETTLEMENT_ACCOUNT_NUMBER = "00990000000001"


def upgrade() -> None:
    # Cuenta interna de liquidacion del proveedor (una por moneda; MVP PEN).
    op.execute(
        sa.text(
            "INSERT INTO accounts.accounts "
            "(id, user_id, account_number, type, currency, status) "
            "VALUES (CAST(:id AS uuid), CAST(:owner AS uuid), :number, "
            "'CORRIENTE', 'PEN', 'ACTIVE') "
            "ON CONFLICT DO NOTHING"
        ).bindparams(
            id=SETTLEMENT_ACCOUNT_ID,
            owner=SETTLEMENT_OWNER_ID,
            number=SETTLEMENT_ACCOUNT_NUMBER,
        )
    )
    # Proyeccion de saldo en cero (la exige el motor al acreditar).
    op.execute(
        sa.text(
            "INSERT INTO accounts.account_balances "
            "(account_id, currency, available_minor, held_minor, version) "
            "VALUES (CAST(:id AS uuid), 'PEN', 0, 0, 0) "
            "ON CONFLICT DO NOTHING"
        ).bindparams(id=SETTLEMENT_ACCOUNT_ID)
    )


def downgrade() -> None:
    # Borra solo la fila sembrada por esta migracion (si existe).
    op.execute(
        sa.text(
            "DELETE FROM accounts.account_balances WHERE account_id = CAST(:id AS uuid)"
        ).bindparams(id=SETTLEMENT_ACCOUNT_ID)
    )
    op.execute(
        sa.text("DELETE FROM accounts.accounts WHERE id = CAST(:id AS uuid)").bindparams(
            id=SETTLEMENT_ACCOUNT_ID
        )
    )
