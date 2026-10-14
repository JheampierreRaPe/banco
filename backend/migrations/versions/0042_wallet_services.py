"""Catalogo de servicios + `service_payments`/`topups`/`service_subscriptions` (E4-T16, HU16).

Revision ID: 0042_wallet_services
Revises: 0041_wallet_qr_payment_guard
Create Date: 2026-10-13

Fuente: `docs/03b-diccionario-de-datos.md#95-billers-service_payments-topups-service_subscriptions`,
`docs/tasks/E4-T16.md`. Crea SOLO tablas propias del schema `wallet`
(sin tocar schemas de otros modulos):

- `wallet.billers` (catalogo extensible: `category` IDX, `name`,
  `required_fields` JSONB, `enabled`; agregar billers no requiere
  migracion, regla de HU16).
- `wallet.service_payments` (`user_id`/`transaction_id` REF logicas a
  `users`/`transactions` sin FK fisica entre schemas, regla de oro 4;
  `biller_id` FK fisica mismo schema; `supply_ref` IDX;
  `amount_minor CK>0`; `status` default `PENDING`; las filas las
  escribira el pago de `E4-T17`, aqui quedan vacias).
- `wallet.topups` (`user_id` REF logica; `operator`/`phone`;
  `amount_minor CK>0`; `currency` default `PEN`; `transaction_id` REF
  logica; `status` default `PENDING`; las escribe `E4-T17`).
- `wallet.service_subscriptions` (`user_id` REF logica; `biller_id` FK
  mismo schema; `supply_ref`; `notify_days_before` default `3`
  coherente con HU16; `status` default `ACTIVE`).

Sin `CHECK` sobre `status` en las tres tablas operativas: los estados
finales los fija el pago (`E4-T17`); aqui solo el default. Dinero
entero en centimos, nunca `float` (regla 3). **No mueve dinero.**

Siembra (idempotente, `ON CONFLICT (id) DO NOTHING`): 6 `billers`
demo con UUIDs fijos (espejo en
`wallet.repository.services.DEMO_BILLERS` + `ensure_demo_billers`
para tests/dev sin migracion) y `required_fields` por categoria
(`supply_ref` en servicios, `phone` en operadores de recarga).

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0042_wallet_services` (20) lo respeta.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0042_wallet_services"
down_revision = "0041_wallet_qr_payment_guard"
branch_labels = None
depends_on = None

# Billers demo (decision 3 del brief E4-T16): categorias de servicios +
# operadores de recarga; `required_fields` por categoria. UUIDs fijos,
# espejo de `wallet.repository.services.DEMO_BILLERS`.
DEMO_BILLERS = (
    (
        "c1111111-1111-4111-8111-111111111111",
        "ELECTRICIDAD",
        "Luz del Hogar Demo",
        '["supply_ref"]',
    ),
    (
        "c2222222-2222-4222-8222-222222222222",
        "AGUA",
        "Agua Potable Demo",
        '["supply_ref"]',
    ),
    (
        "c3333333-3333-4333-8333-333333333333",
        "GAS",
        "Gas Domiciliario Demo",
        '["supply_ref"]',
    ),
    (
        "c4444444-4444-4444-8444-444444444444",
        "INTERNET",
        "Internet Fibra Demo",
        '["supply_ref"]',
    ),
    (
        "c5555555-5555-4555-8555-555555555555",
        "TELEFONIA",
        "Recarga Movil Sol Demo",
        '["phone"]',
    ),
    (
        "c6666666-6666-4666-8666-666666666666",
        "TELEFONIA",
        "Recarga Movil Luna Demo",
        '["phone"]',
    ),
)


def upgrade() -> None:
    op.create_table(
        "billers",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column(
            "required_fields",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        schema="wallet",
    )
    op.create_index(
        "ix_billers_category",
        "billers",
        ["category"],
        schema="wallet",
    )
    op.create_table(
        "service_payments",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("biller_id", sa.Uuid(), nullable=False),
        sa.Column("supply_ref", sa.String(80), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=True),
        sa.Column("receipt_data", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(15), server_default="PENDING", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["biller_id"],
            ["wallet.billers.id"],
            name="fk_service_payments_biller",
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_service_payments_amount_minor"),
        schema="wallet",
    )
    op.create_index(
        "ix_service_payments_supply_ref",
        "service_payments",
        ["supply_ref"],
        schema="wallet",
    )
    op.create_table(
        "topups",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("operator", sa.String(40), nullable=False),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), server_default="PEN", nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(15), server_default="PENDING", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_topups_amount_minor"),
        schema="wallet",
    )
    op.create_table(
        "service_subscriptions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("biller_id", sa.Uuid(), nullable=False),
        sa.Column("supply_ref", sa.String(80), nullable=False),
        sa.Column(
            "notify_days_before",
            sa.SmallInteger(),
            server_default="3",
            nullable=False,
        ),
        sa.Column("status", sa.String(15), server_default="ACTIVE", nullable=False),
        sa.ForeignKeyConstraint(
            ["biller_id"],
            ["wallet.billers.id"],
            name="fk_service_subscriptions_biller",
        ),
        sa.CheckConstraint(
            "notify_days_before BETWEEN 1 AND 30",
            name="ck_service_subscriptions_notify_days",
        ),
        schema="wallet",
    )
    for biller_id, category, name, required_fields in DEMO_BILLERS:
        op.execute(
            sa.text(
                "INSERT INTO wallet.billers (id, category, name, required_fields, enabled) "
                "VALUES (CAST(:id AS uuid), :category, :name, "
                "CAST(:required_fields AS jsonb), true) "
                "ON CONFLICT (id) DO NOTHING"
            ).bindparams(
                id=biller_id,
                category=category,
                name=name,
                required_fields=required_fields,
            )
        )


def downgrade() -> None:
    # Las suscripciones/pagos referencian a los billers
    # (`fk_service_*_biller`): borrar primero las filas demo que los
    # referencien (solo las demo; el resto es dato operativo que el
    # downgrade no toca por diseno) y despues las filas demo.
    for biller_id, _category, _name, _required_fields in DEMO_BILLERS:
        op.execute(
            sa.text(
                "DELETE FROM wallet.service_subscriptions " "WHERE biller_id = CAST(:id AS uuid)"
            ).bindparams(id=biller_id)
        )
        op.execute(
            sa.text(
                "DELETE FROM wallet.service_payments WHERE biller_id = CAST(:id AS uuid)"
            ).bindparams(id=biller_id)
        )
    for biller_id, _category, _name, _required_fields in DEMO_BILLERS:
        op.execute(
            sa.text("DELETE FROM wallet.billers WHERE id = CAST(:id AS uuid)").bindparams(
                id=biller_id
            )
        )
    op.drop_table("service_subscriptions", schema="wallet")
    op.drop_table("topups", schema="wallet")
    op.drop_index(
        "ix_service_payments_supply_ref",
        table_name="service_payments",
        schema="wallet",
    )
    op.drop_table("service_payments", schema="wallet")
    op.drop_index(
        "ix_billers_category",
        table_name="billers",
        schema="wallet",
    )
    op.drop_table("billers", schema="wallet")
