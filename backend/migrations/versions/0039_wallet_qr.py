"""QR firmado + `qr_charges` (E4-T06, HU14; cierra `docs/18` G68/G69/G70 en QR).

Revision ID: 0039_wallet_qr
Revises: 0038_wallet_express_params
Create Date: 2026-10-13

Fuente: `docs/03b-diccionario-de-datos.md#92-merchant_accounts` y
`#93-qr_charges`, `docs/tasks/E4-T06.md`. Crea SOLO tablas propias del
schema `wallet` (sin tocar schemas de otros modulos):

- `wallet.merchant_accounts` (`user_id` UQ + REF logica a
  `identity.users`, sin FK fisica entre schemas, regla de oro 4;
  `tax_id_hash` hasheado, nunca el RUC en claro).
- `wallet.qr_charges` (`merchant_id` FK fisica mismo schema;
  `amount_minor CK>0`; `mode`/`status` como `CHECK` sobre `VARCHAR`,
  portable sin drift en `alembic check`, mismo criterio que
  `credits.contracts`/`0037_wallet`).

Indices (`docs/03b#15`): `(merchant_id, status)`, `(expires_at)`. No
crea `qr_payments` (es de `HU15`).

Siembra (idempotente, `ON CONFLICT DO NOTHING`: respeta valores ya
personalizados y es re-ejecutable):

- `wallet.qr_ttl_seconds = 300` (5 min, G70; dueno del esquema
  `shared/config`, regla de oro 6, patron `0030`/`0036`/`0038`).
- 2 comercios demo (G69, decision del dueno opcion (a): asumir
  comercios existentes + sembrar demo para el MVP): filas fijas con
  UUIDs deterministicos (ver `wallet.repository.qr.DEMO_MERCHANTS`);
  `tax_id_hash` = SHA-256 de RUC ficticios (no reales, nunca en
  claro). El `user_id` es REF logica (sin FK fisica): no exige filas
  en `identity`.

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0039_wallet_qr` (14) lo respeta.
"""

import sqlalchemy as sa
from alembic import op

revision = "0039_wallet_qr"
down_revision = "0038_wallet_express_params"
branch_labels = None
depends_on = None

PARAMS = (
    (
        "wallet.qr_ttl_seconds",
        "300",
        "wallet",
        "Vigencia del QR de cobro en segundos (E4-T06, default 5 min)",
    ),
)

# Comercios demo (G69): UUIDs fijos + `tax_id_hash` SHA-256 de RUC
# ficticios (no reales). Espejo de `wallet.repository.qr.DEMO_MERCHANTS`.
DEMO_MERCHANTS = (
    (
        "a1111111-1111-4111-8111-111111111111",
        "b1111111-1111-4111-8111-111111111111",
        "Bodega Demo San Juan",
        "5881c234c3f26f841c8ad0a38df95a3eb9847c2ef24897d47b77e5151a62949d",
    ),
    (
        "a2222222-2222-4222-8222-222222222222",
        "b2222222-2222-4222-8222-222222222222",
        "Cafe Demo Miraflores",
        "728eed2fc699fa4d11b8fc3301e02e912a81a2ff7ba8681f77caef14d38cb155",
    ),
)


def upgrade() -> None:
    op.create_table(
        "merchant_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("business_name", sa.String(150), nullable=False),
        sa.Column("tax_id_hash", sa.String(128), nullable=True),
        sa.Column("status", sa.String(15), server_default="ACTIVE", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", name="uq_merchant_accounts_user_id"),
        sa.CheckConstraint(
            "status IN ('ACTIVE','BLOCKED','CLOSED')",
            name="ck_merchant_accounts_status",
        ),
        schema="wallet",
    )
    op.create_index(
        "ix_merchant_accounts_user_id",
        "merchant_accounts",
        ["user_id"],
        schema="wallet",
    )
    op.create_table(
        "qr_charges",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("concept", sa.String(120), nullable=True),
        sa.Column("mode", sa.String(10), server_default="DYNAMIC", nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(256), nullable=False),
        sa.Column("status", sa.String(12), server_default="ACTIVE", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["wallet.merchant_accounts.id"],
            name="fk_qr_charges_merchant",
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_qr_charges_amount_minor"),
        sa.CheckConstraint(
            "mode IN ('DYNAMIC','STATIC')",
            name="ck_qr_charges_mode",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','PAID','EXPIRED','CANCELLED')",
            name="ck_qr_charges_status",
        ),
        schema="wallet",
    )
    op.create_index(
        "ix_qr_charges_merchant_status",
        "qr_charges",
        ["merchant_id", "status"],
        schema="wallet",
    )
    op.create_index(
        "ix_qr_charges_expires_at",
        "qr_charges",
        ["expires_at"],
        schema="wallet",
    )
    for key, value, module, description in PARAMS:
        op.execute(
            sa.text(
                "INSERT INTO config.parameters (id, key, value_json, module, description) "
                "VALUES (gen_random_uuid(), :key, CAST(:value AS jsonb), :module, :description) "
                "ON CONFLICT (key) DO NOTHING"
            ).bindparams(
                key=key,
                value=value,
                module=module,
                description=description,
            )
        )
    for merchant_id, user_id, business_name, tax_id_hash in DEMO_MERCHANTS:
        op.execute(
            sa.text(
                "INSERT INTO wallet.merchant_accounts "
                "(id, user_id, business_name, tax_id_hash, status) "
                "VALUES (CAST(:id AS uuid), CAST(:user_id AS uuid), "
                ":business_name, :tax_id_hash, 'ACTIVE') "
                "ON CONFLICT (id) DO NOTHING"
            ).bindparams(
                id=merchant_id,
                user_id=user_id,
                business_name=business_name,
                tax_id_hash=tax_id_hash,
            )
        )


def downgrade() -> None:
    # Los cargos referencian a los comercios (`fk_qr_charges_merchant`):
    # borrar primero los cargos de los comercios demo y solo despues las
    # filas demo; en el orden inverso el `DELETE` viola la FK si hay
    # cargos (el ciclo upgrade -> cargo -> downgrade -> upgrade debe ser
    # reversible sin error de FK).
    for merchant_id, _user_id, _business_name, _tax_id_hash in DEMO_MERCHANTS:
        op.execute(
            sa.text(
                "DELETE FROM wallet.qr_charges WHERE merchant_id = CAST(:id AS uuid)"
            ).bindparams(id=merchant_id)
        )
    for merchant_id, _user_id, _business_name, _tax_id_hash in DEMO_MERCHANTS:
        op.execute(
            sa.text("DELETE FROM wallet.merchant_accounts WHERE id = CAST(:id AS uuid)").bindparams(
                id=merchant_id
            )
        )
    for key, _value, _module, _description in PARAMS:
        op.execute(sa.text("DELETE FROM config.parameters WHERE key = :key").bindparams(key=key))
    op.drop_index(
        "ix_qr_charges_expires_at",
        table_name="qr_charges",
        schema="wallet",
    )
    op.drop_index(
        "ix_qr_charges_merchant_status",
        table_name="qr_charges",
        schema="wallet",
    )
    op.drop_table("qr_charges", schema="wallet")
    op.drop_index(
        "ix_merchant_accounts_user_id",
        table_name="merchant_accounts",
        schema="wallet",
    )
    op.drop_table("merchant_accounts", schema="wallet")
