"""Tabla OTP de un solo uso (E1-T08, HU02 CA-01/CA-02/CA-03).

Revision ID: 0014_identity_otp
Revises: 0013_identity_kyc_audit
Create Date: 2026-09-17

Fuente: `docs/03b-diccionario-de-datos.md#45-otp_codes` (columnas exactas)
e indices `docs/03b#15` (`otp_codes`: `(user_id, purpose, status)`,
`(expires_at)`). Solo schema `identity`; sin tocar otras tablas ni otros
schemas. Sin FK entre schemas: `otp_codes.user_id` es FK contenida en
`identity.users` (mismo schema, permitido por la regla de oro 4).

Desviaciones documentadas (igual que en el modelo `OtpCode`):
- `destination` nulable (en `03b` es NOT NULL): el OTP puede generarse
  antes de conocer el canal (E1-T10 lo completa al reenviar).
- `code_hash VARCHAR(128)` guarda `"salt_hex$sha256_hex"` (97 caracteres):
  nunca el codigo en claro; el salt viaja junto al digest porque `03b#4.5`
  no trae columna de salt.
- Extension aditiva `resend_count SMALLINT` (default 0): la regla HU02
  "max 3 reenvios" (`config.parameters: otp.max_resends`) exige contar
  reenvios por ciclo; sin esta columna el parametro seria inaplicable.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_identity_otp"
down_revision = "0013_identity_kyc_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "otp_codes",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("destination", sa.String(255), nullable=True),
        sa.Column("code_hash", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "attempts", sa.SmallInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "max_attempts", sa.SmallInteger(), server_default="3", nullable=False
        ),
        sa.Column(
            "resend_count", sa.SmallInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "status",
            sa.String(20),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["identity.users.id"],
            name="fk_otp_codes_user",
        ),
        sa.CheckConstraint(
            "purpose IN ('ACTIVATION', 'RECOVERY', 'PAYMENT', 'LOGIN')",
            name="ck_otp_codes_purpose",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'USED', 'EXPIRED')",
            name="ck_otp_codes_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_otp_codes_attempts_min",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name="ck_otp_codes_max_attempts_min",
        ),
        sa.CheckConstraint(
            "resend_count >= 0",
            name="ck_otp_codes_resend_count_min",
        ),
        schema="identity",
    )
    op.create_index(
        "ix_otp_codes_user_purpose_status",
        "otp_codes",
        ["user_id", "purpose", "status"],
        schema="identity",
    )
    op.create_index(
        "ix_otp_codes_expires_at",
        "otp_codes",
        ["expires_at"],
        schema="identity",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_otp_codes_expires_at", table_name="otp_codes", schema="identity"
    )
    op.drop_index(
        "ix_otp_codes_user_purpose_status",
        table_name="otp_codes",
        schema="identity",
    )
    op.drop_table("otp_codes", schema="identity")
