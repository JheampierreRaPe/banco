"""Parametros del motor de reglas de credito (E3-T07, HU10).

Revision ID: 0030_credits_loan_rule_params
Revises: 0029_credits_loan_products
Create Date: 2026-10-09

Fuente: `docs/tasks/E3-T07.md` (cierra `docs/18` G26; dueno del esquema
`shared/config`, regla de oro 6). No crea tablas; solo filas de
`config.parameters` (como `0023`).

Siembra (idempotente, `ON CONFLICT (key) DO NOTHING`: respeta valores ya
personalizados y es re-ejecutable) las 4 claves que faltaban:

- `loan.max_amount_minor = 5000000` (tope generico, S/ 50,000).
- `loan.min_score = 650` (score minimo del bureau para aprobar).
- `loan.review_margin = 50` (puntos de score sobre el minimo que mandan
  a revision manual: banda `[650, 700)` con los defaults).
- `loan.max_debt_minor = 10000000` (deuda total maxima, S/ 100,000).

Normaliza `loan.max_dti_ratio` (sembrado por `0001` como texto `"0.4"`,
peligro de comparar texto): si esta guardado como string numerico lo
convierte al numero JSON `0.4`; si ya es numero o es un texto no
numerico personalizado, no lo toca. Idempotente y re-ejecutable.

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0030_credits_loan_rule_params` (29) lo respeta.
"""

import sqlalchemy as sa
from alembic import op

revision = "0030_credits_loan_rule_params"
down_revision = "0029_credits_loan_products"
branch_labels = None
depends_on = None

LOAN_KEYS: tuple[tuple[str, str, str, str], ...] = (
    (
        "loan.max_amount_minor",
        "5000000",
        "credits",
        "Tope generico de monto solicitado del motor de credito (centimos)",
    ),
    (
        "loan.min_score",
        "650",
        "credits",
        "Score minimo del bureau para aprobar (motor de credito)",
    ),
    (
        "loan.review_margin",
        "50",
        "credits",
        "Margen sobre el minimo que manda a revision manual (puntos de score)",
    ),
    (
        "loan.max_debt_minor",
        "10000000",
        "credits",
        "Deuda total maxima del solicitante del motor de credito (centimos)",
    ),
)

DTI_KEY = "loan.max_dti_ratio"


def upgrade() -> None:
    for key, value, module, description in LOAN_KEYS:
        op.execute(
            sa.text(
                "INSERT INTO config.parameters (id, key, value_json, module, description) "
                "VALUES (gen_random_uuid(), :key, CAST(:value AS jsonb), :module, :description) "
                "ON CONFLICT (key) DO NOTHING"
            ).bindparams(key=key, value=value, module=module, description=description)
        )
    # Normaliza `"0.4"` (string) -> `0.4` (numero): solo strings
    # numericos; un numero o un texto personalizado no numerico quedan
    # intactos (la fachada acepta ambas formas).
    op.execute(
        sa.text(
            "UPDATE config.parameters "
            "SET value_json = CAST(value_json #>> '{}' AS jsonb) "
            "WHERE key = :key "
            "AND jsonb_typeof(value_json) = 'string' "
            "AND (value_json #>> '{}') ~ '^[0-9]+(\\.[0-9]+)?$'"
        ).bindparams(key=DTI_KEY)
    )


def downgrade() -> None:
    for key, _, _, _ in LOAN_KEYS:
        op.execute(sa.text("DELETE FROM config.parameters WHERE key = :key").bindparams(key=key))
    # La normalizacion del DTI no se revierte: es un cambio de
    # representacion sin cambio semantico (texto `"0.4"` y numero `0.4`
    # los acepta la fachada por igual) y revertirla podria reintroducir
    # el peligro de comparar texto o pisar un valor personalizado.
