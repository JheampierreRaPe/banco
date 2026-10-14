"""Correccion del catalogo contable semilla `4200`/`5000` (E5-T23, HU12).

Revision ID: 0035_ledger_catalog_fix
Revises: 0034_credits_loans
Create Date: 2026-10-13

Fuente: `docs/04-motor-transaccional-y-ledger.md#7.5-desembolso-de-prestamo-5000`
/`#7.6-pago-de-cuota-300--200-capital--80-interes--20-seguro` y
`docs/03-modelo-de-datos.md#5-schema-ledger` (catalogo contable semilla):
`5000` debe ser **asset** "Cartera de prestamos" (capital colocado) y
`4200` debe ser **income** "Ingresos por seguro". La migracion historica
`0003_ledger_chart_of_accounts` sembro `5000 = "Costos financieros"`
(**expense**) y `4200 = "Otros ingresos"`, y `_get_or_create` de
`ledger.repository` no actualiza filas existentes: cambiar solo
`ledger/domain/chart.py` no corrige bases ya sembradas.

Forward-only (no se edita `0003`): esta migracion es la fuente de la
correccion tanto para bases nuevas (ejecutan `0003` y luego `0035`) como
existentes. Solo `UPDATE` por `code` sobre `ledger.ledger_accounts`
(catalogo, no asientos: no toca `postings`/`journal_entries` ni el
invariante append-only). Idempotente por `code` y reversible exacta.

Nota de nombre: `alembic_version.version_num` es `VARCHAR(32)` (ver
`0026_drop_preapproved_limit`); `0035_ledger_catalog_fix` (23 chars)
respeta el limite.
"""

import sqlalchemy as sa
from alembic import op

revision = "0035_ledger_catalog_fix"
down_revision = "0034_credits_loans"
branch_labels = None
depends_on = None

# Valores correctos (`docs/04#7.5`/`#7.6`, `docs/03#5`).
_FIX_5000_NAME = "Cartera de prestamos"
_FIX_5000_TYPE = "asset"
_FIX_4200_NAME = "Ingresos por seguro"
_FIX_4200_TYPE = "income"

# Valores previos sembrados por `0003` (para el `downgrade` reversible).
_PREV_5000_NAME = "Costos financieros"
_PREV_5000_TYPE = "expense"
_PREV_4200_NAME = "Otros ingresos"

_UPGRADE_5000 = sa.text(
    "UPDATE ledger.ledger_accounts " "SET name = :name, type = :type WHERE code = '5000'"
).bindparams(name=_FIX_5000_NAME, type=_FIX_5000_TYPE)

_UPGRADE_4200 = sa.text(
    "UPDATE ledger.ledger_accounts " "SET name = :name WHERE code = '4200'"
).bindparams(name=_FIX_4200_NAME)

_DOWNGRADE_5000 = sa.text(
    "UPDATE ledger.ledger_accounts " "SET name = :name, type = :type WHERE code = '5000'"
).bindparams(name=_PREV_5000_NAME, type=_PREV_5000_TYPE)

_DOWNGRADE_4200 = sa.text(
    "UPDATE ledger.ledger_accounts " "SET name = :name WHERE code = '4200'"
).bindparams(name=_PREV_4200_NAME)


def apply_fix(bind) -> None:
    """Corrige las filas `5000`/`4200` (idempotente por `code`)."""
    bind.execute(_UPGRADE_5000)
    bind.execute(_UPGRADE_4200)


def revert_fix(bind) -> None:
    """Restaura los valores previos de `0003` (idempotente por `code`)."""
    bind.execute(_DOWNGRADE_5000)
    bind.execute(_DOWNGRADE_4200)


def upgrade() -> None:
    apply_fix(op.get_bind())


def downgrade() -> None:
    revert_fix(op.get_bind())
