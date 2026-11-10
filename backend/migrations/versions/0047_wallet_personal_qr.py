"""QR personal estatico (E4-T21, HU14).

Revision ID: 0047_wallet_personal_qr
Revises: 0046_services_settlement
Create Date: 2026-10-15

Cualquier titular `ACTIVE` puede generar un QR personal estatico que
recibe dinero en su propia cuenta: sin monto (`amount_minor = NULL`,
lo digita el pagador) y sin caducidad (`expires_at = NULL`,
reutilizable). Cambia SOLO el schema propio `wallet` (sin tocar
otros schemas):

- `wallet.qr_charges`:
  - `merchant_id`: `NOT NULL` -> **nullable** (un QR personal no
    tiene comercio).
  - `amount_minor`: `NOT NULL` -> **nullable**; el `CHECK` pasa a
    `amount_minor IS NULL OR amount_minor > 0`.
  - Nuevas columnas: `payee_type VARCHAR(10)` (`MERCHANT`|`PERSONAL`,
    `CHECK`, default `MERCHANT`), `owner_user_id UUID` (REF logica a
    `identity.users`, nullable), `payee_account_id UUID` (REF logica
    a `accounts.accounts`, nullable), `reusable BOOLEAN NOT NULL
    DEFAULT false`.
  - `CHECK` de destino: `(payee_type = 'MERCHANT' AND merchant_id IS
    NOT NULL) OR (payee_type = 'PERSONAL' AND owner_user_id IS NOT
    NULL)`.
  - `CHECK` personal-estatico: `payee_type <> 'PERSONAL' OR
    (amount_minor IS NULL AND expires_at IS NULL AND reusable =
    true)`.
- `wallet.qr_payments`:
  - `merchant_id`: `NOT NULL` -> **nullable** (pago de QR personal).
  - Nuevas columnas: `payee_user_id UUID` (REF logica a
    `identity.users`, nullable), `reusable BOOLEAN NOT NULL DEFAULT
    false` (denormalizado del cargo al insertar).
  - **Guarda de doble cobro acotada:** se dropea y recrea el indice
    unico parcial `uq_qr_payments_charge_active` con predicado
    `status <> 'REJECTED' AND reusable = false` (un QR de un solo uso
    sigue admitiendo **un** cobro; un QR reutilizable admite varios).
- `amount_minor` de `qr_payments` **sigue NOT NULL** (es el monto
  realmente cobrado).

`amount_minor` del sobre `BO-QR1` para el QR personal es `0` ("lo
define el pagador"; el adaptador `E4-T03` admite `>= 0`); la fila
guarda `NULL` y la verificacion compara contra la fila (nunca se
confia en el cliente).

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0047_wallet_personal_qr` (22) lo respeta.
"""

import sqlalchemy as sa
from alembic import op

revision = "0047_wallet_personal_qr"
down_revision = "0046_services_settlement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- `qr_charges`: destino polimorfico + monto nullable + reusable.
    op.alter_column(
        "qr_charges",
        "merchant_id",
        existing_type=sa.Uuid(),
        nullable=True,
        schema="wallet",
    )
    op.alter_column(
        "qr_charges",
        "amount_minor",
        existing_type=sa.BigInteger(),
        nullable=True,
        schema="wallet",
    )
    op.add_column(
        "qr_charges",
        sa.Column(
            "payee_type",
            sa.String(10),
            nullable=False,
            server_default="MERCHANT",
        ),
        schema="wallet",
    )
    op.add_column(
        "qr_charges",
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        schema="wallet",
    )
    op.add_column(
        "qr_charges",
        sa.Column("payee_account_id", sa.Uuid(), nullable=True),
        schema="wallet",
    )
    op.add_column(
        "qr_charges",
        sa.Column(
            "reusable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema="wallet",
    )
    # Las filas historicas son de comercio, de un solo uso.
    op.execute(
        sa.text(
            "UPDATE wallet.qr_charges SET payee_type = 'MERCHANT', reusable = false "
            "WHERE payee_type IS NULL OR reusable IS NULL"
        )
    )
    op.drop_constraint("ck_qr_charges_amount_minor", "qr_charges", schema="wallet", type_="check")
    op.create_check_constraint(
        "ck_qr_charges_amount_minor",
        "qr_charges",
        "(amount_minor IS NULL OR amount_minor > 0)",
        schema="wallet",
    )
    op.create_check_constraint(
        "ck_qr_charges_payee_type",
        "qr_charges",
        "payee_type IN ('MERCHANT','PERSONAL')",
        schema="wallet",
    )
    op.create_check_constraint(
        "ck_qr_charges_payee",
        "qr_charges",
        "((payee_type = 'MERCHANT' AND merchant_id IS NOT NULL) "
        "OR (payee_type = 'PERSONAL' AND owner_user_id IS NOT NULL))",
        schema="wallet",
    )
    op.create_check_constraint(
        "ck_qr_charges_personal_static",
        "qr_charges",
        "(payee_type <> 'PERSONAL' OR "
        "(amount_minor IS NULL AND expires_at IS NULL AND reusable = true))",
        schema="wallet",
    )
    op.create_index(
        "ix_qr_charges_owner_user_id",
        "qr_charges",
        ["owner_user_id"],
        schema="wallet",
    )

    # --- `qr_payments`: destino personal + guarda acotada a un solo uso.
    op.drop_index(
        "uq_qr_payments_charge_active",
        table_name="qr_payments",
        schema="wallet",
    )
    op.alter_column(
        "qr_payments",
        "merchant_id",
        existing_type=sa.Uuid(),
        nullable=True,
        schema="wallet",
    )
    op.add_column(
        "qr_payments",
        sa.Column("payee_user_id", sa.Uuid(), nullable=True),
        schema="wallet",
    )
    op.add_column(
        "qr_payments",
        sa.Column(
            "reusable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema="wallet",
    )
    op.execute(sa.text("UPDATE wallet.qr_payments SET reusable = false WHERE reusable IS NULL"))
    op.create_index(
        "uq_qr_payments_charge_active",
        "qr_payments",
        ["charge_id"],
        schema="wallet",
        unique=True,
        postgresql_where=sa.text("status <> 'REJECTED' AND reusable = false"),
    )
    op.create_index(
        "ix_qr_payments_payee_user_id",
        "qr_payments",
        ["payee_user_id"],
        schema="wallet",
    )


def downgrade() -> None:
    # --- `qr_payments`: vuelve la guarda sin `reusable`.
    op.drop_index(
        "ix_qr_payments_payee_user_id",
        table_name="qr_payments",
        schema="wallet",
    )
    op.drop_index(
        "uq_qr_payments_charge_active",
        table_name="qr_payments",
        schema="wallet",
    )
    op.drop_column("qr_payments", "reusable", schema="wallet")
    op.drop_column("qr_payments", "payee_user_id", schema="wallet")
    op.execute(sa.text("DELETE FROM wallet.qr_payments WHERE merchant_id IS NULL"))
    op.alter_column(
        "qr_payments",
        "merchant_id",
        existing_type=sa.Uuid(),
        nullable=False,
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

    # --- `qr_charges`: vuelve el esquema de solo-comercio.
    op.drop_index(
        "ix_qr_charges_owner_user_id",
        table_name="qr_charges",
        schema="wallet",
    )
    op.drop_constraint(
        "ck_qr_charges_personal_static", "qr_charges", schema="wallet", type_="check"
    )
    op.drop_constraint("ck_qr_charges_payee", "qr_charges", schema="wallet", type_="check")
    op.drop_constraint("ck_qr_charges_payee_type", "qr_charges", schema="wallet", type_="check")
    op.drop_constraint("ck_qr_charges_amount_minor", "qr_charges", schema="wallet", type_="check")
    op.execute(sa.text("DELETE FROM wallet.qr_charges WHERE payee_type = 'PERSONAL'"))
    op.drop_column("qr_charges", "reusable", schema="wallet")
    op.drop_column("qr_charges", "payee_account_id", schema="wallet")
    op.drop_column("qr_charges", "owner_user_id", schema="wallet")
    op.drop_column("qr_charges", "payee_type", schema="wallet")
    op.execute(sa.text("DELETE FROM wallet.qr_charges WHERE amount_minor IS NULL"))
    op.alter_column(
        "qr_charges",
        "amount_minor",
        existing_type=sa.BigInteger(),
        nullable=False,
        schema="wallet",
    )
    op.alter_column(
        "qr_charges",
        "merchant_id",
        existing_type=sa.Uuid(),
        nullable=False,
        schema="wallet",
    )
    op.create_check_constraint(
        "ck_qr_charges_amount_minor",
        "qr_charges",
        "amount_minor > 0",
        schema="wallet",
    )
