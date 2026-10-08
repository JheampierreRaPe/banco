"""Coherencia umbral biometrico < limite diario (OBS-2).

Revision ID: 0025_biometric_coherence
Revises: 0024_accounts_numeric_backfill
Create Date: 2026-10-08

Causa (OBS-2): `transfer.biometric_threshold_minor = 1000000` (S/ 10,000,
`0001`) > `transfer.daily_limit_minor` /
`transfer.daily_limit_STANDARD_minor = 500000` (S/ 5,000, `0001`/`0023`).
En `transactions/service/biometric.py::evaluate_gate`,
`requires_biometry = (not exceeded) and (effective > threshold)`: con
`threshold > limit` la banda `(threshold, limit]` es vacia y todo monto
sobre el umbral se rechaza antes con `DAILY_LIMIT_EXCEEDED`. El reto
biometrico (`PENDING_AUTHORIZATION`) es inalcanzable.

Fix: baja el umbral a `100000` (S/ 1,000), dejando los limites diarios en
`500000`. La banda `(100000, 500000]` vuelve a exigir biometria y
`> 500000` sigue rechazando por limite diario. `UPDATE` directo (la clave
la sembro `0001`): idempotente y re-ejecutable; respeta personalizaciones
distintas de `1000000`? No: fija el valor coherente (el inconsistente es
el defecto a corregir). Reversible: el downgrade restaura `1000000`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0025_biometric_coherence"
down_revision = "0024_accounts_numeric_backfill"
branch_labels = None
depends_on = None

THRESHOLD_KEY = "transfer.biometric_threshold_minor"
FIXED_VALUE = "100000"
PREVIOUS_VALUE = "1000000"


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE config.parameters SET value_json = CAST(:value AS jsonb) " "WHERE key = :key"
        ).bindparams(key=THRESHOLD_KEY, value=FIXED_VALUE)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE config.parameters SET value_json = CAST(:value AS jsonb) " "WHERE key = :key"
        ).bindparams(key=THRESHOLD_KEY, value=PREVIOUS_VALUE)
    )
