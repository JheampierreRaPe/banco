"""Amplia `credits.loan_applications.dti_ratio` a `NUMERIC(12,4)` (fix DTI).

Revision ID: 0032_credits_dti_wider
Revises: 0031_credits_loan_applications
Create Date: 2026-10-09

Causa: `dti_ratio NUMERIC(6,4)` (max 99.9999) desbordaba con entradas
validas por esquema (`total_income_minor=1` + `total_debt_minor>=100`):
`psycopg.errors.NumericValueOutOfRange` no capturado -> HTTP 500 en
`POST /credits/applications`. Fuente: `docs/03b#82`.

Cambio: `NUMERIC(6,4)` -> `NUMERIC(12,4)` (max 99999999.9999; el DTI de
los casos reportados, `>= 100`, persiste exacto). No basta solo: la
deuda efectiva es declarada + externa del bureau (no acotada), asi que
cualquier precision fija podria desbordar; el repositorio ademas acota
el valor antes de persistir (defensa en profundidad, sin cambiar el
dictamen: decide el motor E3-T07 con el DTI exacto).

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0032_credits_dti_wider` (22) lo respeta.
"""

import sqlalchemy as sa
from alembic import op

revision = "0032_credits_dti_wider"
down_revision = "0031_credits_loan_applications"
branch_labels = None
depends_on = None

#: Tope informativo persistible (`NUMERIC(12,4)`).
_DTI_CAP = "99999999.9999"


def upgrade() -> None:
    op.alter_column(
        "loan_applications",
        "dti_ratio",
        existing_type=sa.Numeric(6, 4),
        type_=sa.Numeric(12, 4),
        schema="credits",
        existing_nullable=True,
    )


def downgrade() -> None:
    # Las filas acotadas por encima del rango viejo se recortan al volver
    # a `NUMERIC(6,4)` (solo dato informativo; el dictamen ya quedo).
    op.execute(
        "UPDATE credits.loan_applications SET dti_ratio = "
        "LEAST(GREATEST(dti_ratio, -99.9999), 99.9999) "
        "WHERE dti_ratio IS NOT NULL"
    )
    op.alter_column(
        "loan_applications",
        "dti_ratio",
        existing_type=sa.Numeric(12, 4),
        type_=sa.Numeric(6, 4),
        schema="credits",
        existing_nullable=True,
    )
