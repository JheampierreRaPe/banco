"""Esquema `fx`: tasas, cotizaciones, operaciones, bolsillos y movimientos (E7-T01, HU24/HU25).

Revision ID: 0049_fx
Revises: 0048_risk_rules
Create Date: 2026-11-11

Crea SOLO las cinco tablas propias del schema `fx` (el schema ya existe
desde `0001`; no toca `accounts`/`transactions`/`ledger`/`risk`/
`reconciliation`), segun `docs/03b-diccionario-de-datos.md#10`:

- `fx.fx_rates(id UUID PK, base CHAR(3), quote CHAR(3),
  rate NUMERIC(18,8) CHECK >0, source VARCHAR(40),
  fetched_at TIMESTAMPTZ, ttl_seconds INTEGER default 60)`;
  CHECKs de moneda ISO-4217 (`length(trim()) = 3`, portable
  Postgres/SQLite), `base <> quote`; indice `(base, quote, fetched_at DESC)`.
- `fx.fx_quotes(id UUID PK, user_id UUID REF, base/quote CHAR(3),
  rate NUMERIC(18,8), spread NUMERIC(18,8) default 0,
  source_amount_minor/target_amount_minor BIGINT, expires_at TIMESTAMPTZ,
  status VARCHAR(12) default 'ACTIVE' CHECK `fx_quote_status`
  (`ACTIVE`/`EXPIRED`/`CONSUMED`/`CANCELLED`), created_at)`;
  CHECKs de moneda, `base <> quote`, `rate > 0`, `spread >= 0`,
  `expires_at > created_at`; indices `(user_id, status)`, `(expires_at)`.
- `fx.fx_operations(id UUID PK, quote_id UUID UQ FK fx_quotes
  (una cotizacion se ejecuta una sola vez), transaction_id UUID REF
  `transactions` (logica, sin FK fisica, regla de oro 4),
  status VARCHAR(15) default 'EXECUTED' CHECK `fx_operation_status`
  (`EXECUTED`/`REVERSED`), executed_at)`.
- `fx.savings_pockets(id UUID PK, user_id UUID REF, account_id UUID NULL
  REF `accounts` (logica, sin FK fisica), name VARCHAR(80),
  currency CHAR(3), target_minor BIGINT NULL CHECK >=0,
  balance_minor BIGINT default 0 CHECK >=0, roundup_enabled BOOLEAN
  default false, yield_rate NUMERIC(18,8) default 0 CHECK >=0,
  status VARCHAR(15) default 'ACTIVE' CHECK `pocket_status`
  (`ACTIVE`/`CLOSED`), created_at)`;
  indice `(user_id, status)`.
- `fx.pocket_movements(id UUID PK, pocket_id UUID FK savings_pockets,
  journal_entry_id UUID NULL REF `ledger` (logica, sin FK fisica),
  movement_type VARCHAR(20) CHECK, direction VARCHAR(6) CHECK
  (`DEBIT`/`CREDIT`), amount_minor BIGINT CHECK >0, currency CHAR(3),
  created_at)`; los movimientos son inmutables (solo insercion);
  indice `(pocket_id, created_at)`.

Dinero entero en centimos (`BIGINT`); tasas/spread/yield
`NUMERIC(18,8)`; nunca `float` (regla de oro 3). Sin PII: `user_id`
es UUID (regla 7).

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0049_fx` (7) lo respeta.
"""

import sqlalchemy as sa
from alembic import op

revision = "0049_fx"
down_revision = "0048_risk_rules"
branch_labels = None
depends_on = None

# CHECK portable de moneda ISO-4217 (3 letras): `length`+`trim` existen
# en Postgres y SQLite (sin regex, sin drift en `alembic check`).
_ISO3 = "length(trim({col})) = 3"


def upgrade() -> None:
    op.execute('CREATE SCHEMA IF NOT EXISTS "fx"')

    op.create_table(
        "fx_rates",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("base", sa.CHAR(3), nullable=False),
        sa.Column("quote", sa.CHAR(3), nullable=False),
        sa.Column("rate", sa.Numeric(18, 8), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.CheckConstraint(_ISO3.format(col="base"), name="ck_fx_rates_base_iso"),
        sa.CheckConstraint(_ISO3.format(col="quote"), name="ck_fx_rates_quote_iso"),
        sa.CheckConstraint("base <> quote", name="ck_fx_rates_distinct_pair"),
        sa.CheckConstraint("rate > 0", name="ck_fx_rates_rate_pos"),
        sa.CheckConstraint("ttl_seconds > 0", name="ck_fx_rates_ttl_pos"),
        schema="fx",
    )
    op.create_index(
        "ix_fx_rates_base_quote_fetched",
        "fx_rates",
        ["base", "quote", sa.text("fetched_at DESC")],
        schema="fx",
    )

    op.create_table(
        "fx_quotes",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        # REF logica a `identity.users` (sin FK fisica, regla de oro 4).
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("base", sa.CHAR(3), nullable=False),
        sa.Column("quote", sa.CHAR(3), nullable=False),
        sa.Column("rate", sa.Numeric(18, 8), nullable=False),
        sa.Column("spread", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("source_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("target_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="ACTIVE"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(_ISO3.format(col="base"), name="ck_fx_quotes_base_iso"),
        sa.CheckConstraint(_ISO3.format(col="quote"), name="ck_fx_quotes_quote_iso"),
        sa.CheckConstraint("base <> quote", name="ck_fx_quotes_distinct_pair"),
        sa.CheckConstraint("rate > 0", name="ck_fx_quotes_rate_pos"),
        sa.CheckConstraint("spread >= 0", name="ck_fx_quotes_spread_nonneg"),
        sa.CheckConstraint(
            "source_amount_minor > 0",
            name="ck_fx_quotes_source_amount_pos",
        ),
        sa.CheckConstraint(
            "target_amount_minor > 0",
            name="ck_fx_quotes_target_amount_pos",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','EXPIRED','CONSUMED','CANCELLED')",
            name="ck_fx_quotes_status",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_fx_quotes_expiry_after_creation",
        ),
        schema="fx",
    )
    op.create_index(
        "ix_fx_quotes_user_status",
        "fx_quotes",
        ["user_id", "status"],
        schema="fx",
    )
    op.create_index(
        "ix_fx_quotes_expires_at",
        "fx_quotes",
        ["expires_at"],
        schema="fx",
    )

    op.create_table(
        "fx_operations",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        # FK mismo schema: una cotizacion se ejecuta una sola vez (UQ).
        sa.Column("quote_id", sa.Uuid(), nullable=False),
        # REF logica a `transactions.transactions` (sin FK fisica).
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(15), nullable=False, server_default="EXECUTED"),
        sa.Column(
            "executed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["quote_id"],
            ["fx.fx_quotes.id"],
            name="fk_fx_operations_quote",
        ),
        sa.UniqueConstraint("quote_id", name="uq_fx_operations_quote_id"),
        sa.CheckConstraint(
            "status IN ('EXECUTED','REVERSED')",
            name="ck_fx_operations_status",
        ),
        schema="fx",
    )

    op.create_table(
        "savings_pockets",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        # REF logica a `identity.users` (sin FK fisica).
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # REF logica a `accounts.accounts` (sin FK fisica; NULL = sin
        # cuenta vinculada).
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("target_minor", sa.BigInteger(), nullable=True),
        sa.Column("balance_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "roundup_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "yield_rate",
            sa.Numeric(18, 8),
            nullable=False,
            server_default="0",
        ),
        sa.Column("status", sa.String(15), nullable=False, server_default="ACTIVE"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(_ISO3.format(col="currency"), name="ck_pockets_currency_iso"),
        sa.CheckConstraint(
            "target_minor IS NULL OR target_minor >= 0",
            name="ck_pockets_target_nonneg",
        ),
        sa.CheckConstraint("balance_minor >= 0", name="ck_pockets_balance_nonneg"),
        sa.CheckConstraint("yield_rate >= 0", name="ck_pockets_yield_nonneg"),
        sa.CheckConstraint(
            "status IN ('ACTIVE','CLOSED')",
            name="ck_pockets_status",
        ),
        schema="fx",
    )
    op.create_index(
        "ix_savings_pockets_user_status",
        "savings_pockets",
        ["user_id", "status"],
        schema="fx",
    )

    op.create_table(
        "pocket_movements",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("pocket_id", sa.Uuid(), nullable=False),
        # REF logica a `ledger.journal_entries` (sin FK fisica; NULL
        # hasta que el servicio de `E7-T05` liquida).
        sa.Column("journal_entry_id", sa.Uuid(), nullable=True),
        sa.Column("movement_type", sa.String(20), nullable=False),
        sa.Column("direction", sa.String(6), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["pocket_id"],
            ["fx.savings_pockets.id"],
            name="fk_pocket_movements_pocket",
        ),
        sa.CheckConstraint(
            "movement_type IN ('DEPOSIT','WITHDRAWAL','ROUNDDUP','YIELD','TRANSFER')",
            name="ck_pocket_movements_type",
        ),
        sa.CheckConstraint(
            "direction IN ('DEBIT','CREDIT')",
            name="ck_pocket_movements_direction",
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_pocket_movements_amount_pos"),
        sa.CheckConstraint(
            _ISO3.format(col="currency"),
            name="ck_pocket_movements_currency_iso",
        ),
        schema="fx",
    )
    op.create_index(
        "ix_pocket_movements_pocket_created",
        "pocket_movements",
        ["pocket_id", sa.text("created_at DESC")],
        schema="fx",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pocket_movements_pocket_created",
        table_name="pocket_movements",
        schema="fx",
    )
    op.drop_table("pocket_movements", schema="fx")
    op.drop_index(
        "ix_savings_pockets_user_status",
        table_name="savings_pockets",
        schema="fx",
    )
    op.drop_table("savings_pockets", schema="fx")
    op.drop_table("fx_operations", schema="fx")
    op.drop_index(
        "ix_fx_quotes_expires_at",
        table_name="fx_quotes",
        schema="fx",
    )
    op.drop_index(
        "ix_fx_quotes_user_status",
        table_name="fx_quotes",
        schema="fx",
    )
    op.drop_table("fx_quotes", schema="fx")
    op.drop_index(
        "ix_fx_rates_base_quote_fetched",
        table_name="fx_rates",
        schema="fx",
    )
    op.drop_table("fx_rates", schema="fx")
