"""Guarda de doble cobro de `qr_payments` (fix ALTO-2/MEDIO-2, HU15).

Revision ID: 0041_wallet_qr_payment_guard
Revises: 0040_wallet_qr_payments
Create Date: 2026-10-13

Fuente: fix del validador (doble cobro por `charge_id` no atomico bajo
concurrencia + reintento legitimo tras `REJECTED`). Cambia SOLO el
indice propio del schema `wallet` (sin tocar otros schemas):

- Elimina `wallet.ix_qr_payments_charge_id` (no unico: dos pagos
  concurrentes del mismo `qr_charge` con `Idempotency-Key` distintas
  pasaban la guarda Python (`service/qr_payment.py:328-330`, lectura
  sin lock) y generaban dos asientos).
- Crea `wallet.uq_qr_payments_charge_active`: indice UNICO PARCIAL
  sobre `charge_id` para estados con cobro real/pendiente
  (`status <> 'REJECTED'`). La BD decide la carrera: un solo cobro
  por cargo; el perdedor recibe violacion de unicidad y el servicio
  la mapea a `CONFLICT` (409).
- Los `REJECTED` (sin mover dinero: sin fondos/diario) quedan fuera
  del indice: un cargo cuyo intento fue rechazado admite reintento
  con nueva clave (MEDIO-2).

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0041_wallet_qr_payment_guard` (28) lo respeta.
"""

import sqlalchemy as sa
from alembic import op

revision = "0041_wallet_qr_payment_guard"
down_revision = "0040_wallet_qr_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_qr_payments_charge_id",
        table_name="qr_payments",
        schema="wallet",
    )
    op.create_index(
        "uq_qr_payments_charge_active",
        "qr_payments",
        ["charge_id"],
        schema="wallet",
        unique=True,
        postgresql_where=sa.text("status <> 'REJECTED'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_qr_payments_charge_active",
        table_name="qr_payments",
        schema="wallet",
    )
    op.create_index(
        "ix_qr_payments_charge_id",
        "qr_payments",
        ["charge_id"],
        schema="wallet",
    )
