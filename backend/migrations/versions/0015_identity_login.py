"""Dispositivos confiables y sesiones para login con nonce (E1-T13, HU03 CA-01).

Revision ID: 0015_identity_login
Revises: 0014_identity_otp
Create Date: 2026-09-17

Fuente: `docs/03b-diccionario-de-datos.md#43-device_bindings` (columnas
exactas + UQ(`user_id`, `device_id`)) y `#46-sessions` (columnas exactas +
UQ(`refresh_token_hash`); indices `device_id`, `expires_at`). Solo schema
`identity`; sin tocar otras tablas ni otros schemas. Sin FK entre schemas:
ambas `user_id` son FK contenidas en `identity.users` (mismo schema,
permitido por la regla de oro 4).

Desviacion documentada (igual que en el modelo `UserSession`):

- `sessions.ip VARCHAR(45)` en vez de `INET`: el `INET` de Postgres no
  existe en el SQLite de las pruebas (`TestClient` + ATTACH); se guarda la
  forma textual (IPv4/IPv6 caben en 45). Nulabilidad e indices segun `03b`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_identity_login"
down_revision = "0014_identity_otp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_bindings",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.String(128), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("platform", sa.String(20), nullable=True),
        sa.Column("biometric_type", sa.String(20), nullable=True),
        sa.Column(
            "status",
            sa.String(15),
            server_default="ACTIVE",
            nullable=False,
        ),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["identity.users.id"],
            name="fk_device_bindings_user",
        ),
        sa.UniqueConstraint("user_id", "device_id", name="uq_device_bindings_user_device"),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')",
            name="ck_device_bindings_status",
        ),
        schema="identity",
    )
    op.create_index(
        "ix_device_bindings_user",
        "device_bindings",
        ["user_id"],
        schema="identity",
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("refresh_token_hash", sa.String(128), nullable=False),
        sa.Column("device_id", sa.String(128), nullable=True),
        sa.Column("device_info", sa.JSON(), nullable=True),
        sa.Column("ip", sa.String(45), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["identity.users.id"],
            name="fk_sessions_user",
        ),
        sa.UniqueConstraint("refresh_token_hash", name="uq_sessions_refresh_token_hash"),
        schema="identity",
    )
    op.create_index(
        "ix_sessions_user",
        "sessions",
        ["user_id"],
        schema="identity",
    )
    op.create_index(
        "ix_sessions_device",
        "sessions",
        ["device_id"],
        schema="identity",
    )
    op.create_index(
        "ix_sessions_expires_at",
        "sessions",
        ["expires_at"],
        schema="identity",
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_expires_at", table_name="sessions", schema="identity")
    op.drop_index("ix_sessions_device", table_name="sessions", schema="identity")
    op.drop_index("ix_sessions_user", table_name="sessions", schema="identity")
    op.drop_table("sessions", schema="identity")
    op.drop_index("ix_device_bindings_user", table_name="device_bindings", schema="identity")
    op.drop_table("device_bindings", schema="identity")
