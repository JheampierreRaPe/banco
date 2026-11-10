"""Cobro QR + `qr_payments` (E4-T11, HU15; cierra `docs/18` G72 lado backend).

Revision ID: 0040_wallet_qr_payments
Revises: 0039_wallet_qr
Create Date: 2026-10-13

Fuente: `docs/03b-diccionario-de-datos.md#94-qr_payments`,
`docs/tasks/E4-T11.md`. Crea SOLO la tabla propia del schema
`wallet` (sin tocar schemas de otros modulos):

- `wallet.qr_payments` (`transaction_id` REF logica a
  `transactions` con UQ, sin FK fisica entre schemas, regla de oro 4;
  `charge_id` FK fisica mismo schema con IDX; `payer_user_id` REF
  logica a `identity.users`; `merchant_id` FK fisica mismo schema;
  dinero entero en centimos, nunca `float`).

Estados (`status` como `CHECK` sobre `VARCHAR`, portable sin drift
en `alembic check`, mismo criterio que `0037_wallet`/`0039_wallet_qr`):
`PENDING` (reservado, default segun `03b`), `PENDING_AUTHORIZATION`
(sobre el limite base: espera biometria/PIN del motor, `E2-T19`/
`E2-T31`), `PAID` (liquidado `SETTLED`) y `REJECTED` (sobre el
diario o sin fondos: sin tocar saldos, `docs/04#10`). El asiento
vive en el motor (`docs/04#7.4` via `pay_express`, sin comision);
`wallet` no escribe `transactions`/`ledger`.

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0040_wallet_qr_payments` (23) lo respeta.
"""

import sqlalchemy as sa
from alembic import op

revision = "0040_wallet_qr_payments"
down_revision = "0039_wallet_qr"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "qr_payments",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("charge_id", sa.Uuid(), nullable=False),
        sa.Column("payer_user_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(15), server_default="PENDING", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["charge_id"],
            ["wallet.qr_charges.id"],
            name="fk_qr_payments_charge",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["wallet.merchant_accounts.id"],
            name="fk_qr_payments_merchant",
        ),
        sa.UniqueConstraint("transaction_id", name="uq_qr_payments_transaction_id"),
        sa.CheckConstraint("amount_minor > 0", name="ck_qr_payments_amount_minor"),
        sa.CheckConstraint(
            "status IN ('PENDING','PENDING_AUTHORIZATION','PAID','REJECTED')",
            name="ck_qr_payments_status",
        ),
        schema="wallet",
    )
    op.create_index(
        "ix_qr_payments_charge_id",
        "qr_payments",
        ["charge_id"],
        schema="wallet",
    )
    op.create_index(
        "ix_qr_payments_payer_user_id",
        "qr_payments",
        ["payer_user_id"],
        schema="wallet",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_qr_payments_payer_user_id",
        table_name="qr_payments",
        schema="wallet",
    )
    op.drop_index(
        "ix_qr_payments_charge_id",
        table_name="qr_payments",
        schema="wallet",
    )
    op.drop_table("qr_payments", schema="wallet")
