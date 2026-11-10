"""Beneficiarios frecuentes con cifrado y busqueda indexada (E2-T13, HU07).

Revision ID: 0021_accounts_beneficiaries
Revises: 0020_identity_ruc_business_name
Create Date: 2026-09-27

Fuente: `docs/03b-diccionario-de-datos.md#53-beneficiaries`
(`beneficiaries`) y `docs/03b#15` (`beneficiaries (owner_user_id, alias)`),
`docs/02#10` (AES-256 en reposo), gap `G11` (`docs/18`).
Solo schema `accounts`; sin tocar otros schemas ni otras tablas.
Sin FK entre schemas: `owner_user_id` es UUID logico (REF a
`identity.users`, sin constraint fisico); la unica FK es autocontenida
(`beneficiary_search_tokens` -> `beneficiaries`, con `CASCADE`).
Sin `float`: el tope es `BIGINT` nulable con `CK >= 0`.

Columnas sensibles cifradas a nivel aplicacion (`service/
beneficiaries_crypto`, sobres `B1`/`B0`; en BD solo cifrado):
`alias_enc`, `account_enc`, `holder_name_enc`. `holder_doc_hash` guarda
SOLO el HMAC (el documento en claro nunca se persiste). `account_last4`
es el dato enmascarado estandar (`****` + ultimos 4, igual que E2-T02).
Indices ciegos: `(owner_user_id, alias_search_hash)` (exacto + dedup),
`(owner_user_id, account_hash)` (dedup CCI) y el invertido
`(owner_user_id, token_hash)` (busqueda por palabra/prefijo, AND).
"""

import sqlalchemy as sa
from alembic import op

revision = "0021_accounts_beneficiaries"
down_revision = "0020_identity_ruc_business_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "beneficiaries",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("alias_enc", sa.Text(), nullable=False),
        sa.Column("alias_search_hash", sa.String(128), nullable=False),
        sa.Column("photo_url", sa.Text(), nullable=True),
        sa.Column("bank_code", sa.String(20), nullable=True),
        sa.Column("account_enc", sa.Text(), nullable=False),
        sa.Column("account_hash", sa.String(128), nullable=False),
        sa.Column("account_last4", sa.String(4), nullable=False),
        sa.Column("holder_name_enc", sa.Text(), nullable=True),
        sa.Column("name_search_hash", sa.String(128), nullable=True),
        sa.Column("holder_doc_hash", sa.String(128), nullable=True),
        sa.Column("preapproved_limit_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("status", sa.String(20), server_default="ACTIVE", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DELETED')",
            name="ck_beneficiaries_status",
        ),
        sa.CheckConstraint(
            "preapproved_limit_minor IS NULL OR preapproved_limit_minor >= 0",
            name="ck_beneficiaries_preapproved_limit_min",
        ),
        schema="accounts",
    )
    op.create_index(
        "ix_beneficiaries_owner_alias",
        "beneficiaries",
        ["owner_user_id", "alias_search_hash"],
        schema="accounts",
    )
    op.create_index(
        "ix_beneficiaries_owner_account",
        "beneficiaries",
        ["owner_user_id", "account_hash"],
        schema="accounts",
    )
    op.create_index(
        "ix_beneficiaries_owner_status",
        "beneficiaries",
        ["owner_user_id", "status"],
        schema="accounts",
    )

    op.create_table(
        "beneficiary_search_tokens",
        sa.Column("beneficiary_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("token_hash", sa.String(128), primary_key=True, nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["beneficiary_id"],
            ["accounts.beneficiaries.id"],
            name="fk_beneficiary_tokens_beneficiary",
            ondelete="CASCADE",
        ),
        schema="accounts",
    )
    op.create_index(
        "ix_beneficiary_tokens_owner_token",
        "beneficiary_search_tokens",
        ["owner_user_id", "token_hash"],
        schema="accounts",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_beneficiary_tokens_owner_token",
        table_name="beneficiary_search_tokens",
        schema="accounts",
    )
    op.drop_table("beneficiary_search_tokens", schema="accounts")
    op.drop_index("ix_beneficiaries_owner_status", table_name="beneficiaries", schema="accounts")
    op.drop_index("ix_beneficiaries_owner_account", table_name="beneficiaries", schema="accounts")
    op.drop_index("ix_beneficiaries_owner_alias", table_name="beneficiaries", schema="accounts")
    op.drop_table("beneficiaries", schema="accounts")
