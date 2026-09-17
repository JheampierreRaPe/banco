"""Nucleo del schema `identity` (E1-T03, HU01).

Revision ID: 0012_identity_core
Revises: 0011_notifications
Create Date: 2026-09-17

Fuente: `docs/03b-diccionario-de-datos.md#4-schema-identity` (columnas
exactas de `4.1 users` y `4.2 credentials`) e indices `docs/03b#15`
(`(doc_number_hash)` UQ, `(email)` UQ, `(phone)`, `(status)`).
Solo schema `identity`; sin tocar otros schemas ni otras tablas.
Sin FK entre schemas: `credentials.user_id` es FK contenida en
`identity.users` (mismo schema, permitido por la regla de oro 4).

Desviacion documentada (igual que en los modelos): `email` es
`VARCHAR(320)` en vez de `CITEXT` porque el repo aun no habilita la
extension `citext`; UQ y nulabilidad se mantienen.
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_identity_core"
down_revision = "0011_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("doc_type", sa.String(10), nullable=False),
        sa.Column("doc_number_hash", sa.String(128), nullable=False),
        sa.Column("doc_number_masked", sa.String(20), nullable=True),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            server_default="PENDING_ACTIVATION",
            nullable=False,
        ),
        sa.Column("kyc_status", sa.String(20), server_default="PENDING", nullable=False),
        sa.Column("risk_profile", sa.String(20), server_default="STANDARD", nullable=False),
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
        sa.UniqueConstraint("doc_number_hash", name="uq_users_doc_number_hash"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint(
            "doc_type IN ('DNI', 'CE', 'PASSPORT')",
            name="ck_users_doc_type",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING_ACTIVATION', 'ACTIVE', 'BLOCKED', 'CLOSED')",
            name="ck_users_status",
        ),
        schema="identity",
    )
    op.create_index(
        "ix_users_phone",
        "users",
        ["phone"],
        schema="identity",
    )
    op.create_index(
        "ix_users_status",
        "users",
        ["status"],
        schema="identity",
    )
    op.create_index(
        "ix_users_kyc_status",
        "users",
        ["kyc_status"],
        schema="identity",
    )
    op.create_table(
        "credentials",
        sa.Column("user_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("pin_hash", sa.Text(), nullable=True),
        sa.Column(
            "biometric_enabled", sa.Boolean(), server_default="false", nullable=False
        ),
        sa.Column("failed_attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "failed_attempts >= 0",
            name="ck_credentials_failed_attempts_min",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["identity.users.id"],
            name="fk_credentials_user",
        ),
        schema="identity",
    )


def downgrade() -> None:
    op.drop_table("credentials", schema="identity")
    op.drop_index("ix_users_kyc_status", table_name="users", schema="identity")
    op.drop_index("ix_users_status", table_name="users", schema="identity")
    op.drop_index("ix_users_phone", table_name="users", schema="identity")
    op.drop_table("users", schema="identity")
