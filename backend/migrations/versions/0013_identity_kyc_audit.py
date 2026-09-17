"""Persistencia KYC + auditoria (E1-T04, HU01 CA-02/CA-04).

Revision ID: 0013_identity_kyc_audit
Revises: 0012_identity_core
Create Date: 2026-09-17

Fuente: `docs/03b-diccionario-de-datos.md#44-kyc_verifications` (columnas
exactas) y `docs/03b#141-audit_log-append-only` (columnas exactas) e
indices `docs/03b#15` (`kyc`: `(user_id)`, `(created_at)`; `audit_log`:
`(seq)` UQ, `(entity_type, entity_id)`, `(created_at)`, `(action)`).
Solo schemas `identity` (una tabla nueva) y `audit` (una tabla nueva); sin
tocar otras tablas ni otros schemas. Sin FK entre schemas:
`kyc_verifications.user_id` es FK contenida en `identity.users` (mismo
schema, permitido por la regla de oro 4); `audit_log` no lleva FK fisicas
(`actor_id`/`entity_id` son UUID logicos).

Desviaciones documentadas (igual que en los modelos):
- `kyc_verifications.failure_reason TEXT` nulable: E1-T04 exige motivo por
  intento (CA-04) y `03b#4.4` no trae columna dedicada.
- `audit_log.seq` es `BIGSERIAL` segun `03b`; SQLAlchemy 2.0 no expone
  tipo `BIGSERIAL`/`SERIAL` (ni generico ni en el dialecto `postgresql`),
  asi que se declara `sa.BigInteger()` con `UQ` —mismo patron que las
  demas migraciones del repo y mismo tipo del modelo—; la fachada
  `audit.record` siempre fija su valor (`max(seq)+1`), insercion
  explicita compatible.
- `audit_log.ip` es `INET` segun `03b` (el modelo usa texto portable).
- `ck_audit_log_actor_type` (`USER`/`SYSTEM`/`SERVICE`) refuerza `03b#14.1`.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_identity_kyc_audit"
down_revision = "0012_identity_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kyc_verifications",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("overall_result", sa.Boolean(), nullable=False),
        sa.Column("document_json", postgresql.JSONB(), nullable=True),
        sa.Column("liveness_json", postgresql.JSONB(), nullable=True),
        sa.Column("face_match_json", postgresql.JSONB(), nullable=True),
        sa.Column("challenge_token_hash", sa.String(128), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["identity.users.id"],
            name="fk_kyc_verifications_user",
        ),
        schema="identity",
    )
    op.create_index(
        "ix_kyc_verifications_user",
        "kyc_verifications",
        ["user_id"],
        schema="identity",
    )
    op.create_index(
        "ix_kyc_verifications_created",
        "kyc_verifications",
        ["created_at"],
        schema="identity",
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("entity_type", sa.String(60), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("before_json", postgresql.JSONB(), nullable=True),
        sa.Column("after_json", postgresql.JSONB(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("device_id", sa.String(128), nullable=True),
        sa.Column("request_id", sa.String(60), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("prev_hash", sa.String(64), nullable=True),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("seq", name="uq_audit_log_seq"),
        sa.CheckConstraint(
            "actor_type IN ('USER', 'SYSTEM', 'SERVICE')",
            name="ck_audit_log_actor_type",
        ),
        schema="audit",
    )
    op.create_index(
        "ix_audit_log_entity",
        "audit_log",
        ["entity_type", "entity_id"],
        schema="audit",
    )
    op.create_index(
        "ix_audit_log_created",
        "audit_log",
        ["created_at"],
        schema="audit",
    )
    op.create_index(
        "ix_audit_log_action",
        "audit_log",
        ["action"],
        schema="audit",
    )
    op.create_index(
        "ix_audit_log_actor",
        "audit_log",
        ["actor_id"],
        schema="audit",
    )
    op.create_index(
        "ix_audit_log_request",
        "audit_log",
        ["request_id"],
        schema="audit",
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_request", table_name="audit_log", schema="audit")
    op.drop_index("ix_audit_log_actor", table_name="audit_log", schema="audit")
    op.drop_index("ix_audit_log_action", table_name="audit_log", schema="audit")
    op.drop_index("ix_audit_log_created", table_name="audit_log", schema="audit")
    op.drop_index("ix_audit_log_entity", table_name="audit_log", schema="audit")
    op.drop_table("audit_log", schema="audit")
    op.drop_index(
        "ix_kyc_verifications_created", table_name="kyc_verifications", schema="identity"
    )
    op.drop_index(
        "ix_kyc_verifications_user", table_name="kyc_verifications", schema="identity"
    )
    op.drop_table("kyc_verifications", schema="identity")
