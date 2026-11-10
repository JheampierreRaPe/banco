"""Concepto contable en la proyeccion de movimientos (E2-T35, HU05).

Revision ID: 0027_movements_entry_type
Revises: 0026_drop_preapproved_limit
Create Date: 2026-10-09

Fuente: `docs/03b-diccionario-de-datos.md#5.4-movements_view` (columna
`entry_type VARCHAR(30)` nullable) y `docs/03b#7.2-journal_entries`
(`entry_type`, concepto contable del asiento).

`upgrade()`: agrega `accounts.movements_view.entry_type` (`VARCHAR(30)`,
nullable; `NULL` en filas legacy ya proyectadas, que el cliente resuelve
por fallback sin mostrar texto crudo). `downgrade()`: la retira.
Solo schema `accounts`; sin tocar otros schemas ni reescribir migraciones
ya aplicadas.

Nota de nombre: `alembic_version.version_num` es `VARCHAR(32)` (ver
`0026_drop_preapproved_limit`); `0027_movements_entry_type` (25 chars)
respeta el limite.
"""

import sqlalchemy as sa
from alembic import op

revision = "0027_movements_entry_type"
down_revision = "0026_drop_preapproved_limit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "movements_view",
        sa.Column("entry_type", sa.String(30), nullable=True),
        schema="accounts",
    )


def downgrade() -> None:
    op.drop_column("movements_view", "entry_type", schema="accounts")
