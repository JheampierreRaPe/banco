"""`identity.users.email` pasa a NOT NULL (E1-T34, HU02/HU04, SCR-005).

Revision ID: 0018_users_email_not_null
Revises: 0017_identity_access_recovery
Create Date: 2026-09-22

Fuente: `docs/tasks/archive/E1-T34.md` (decision del dueno SCR-005, `docs/18#4`:
"OTP solo email"). `identity.users.email` hoy es nullable (migracion
`0012_identity_core` y modelo `User.email`); tras este cambio el modelo ORM
es `nullable=False` (sin drift: `alembic check` limpio).

Estrategia determinista sin backfill: la BD demo tiene **0 filas con email
NULL** (4 usuarios, verificado el 2026-09-22), asi que el upgrade no
necesita limpiar ni inventar datos. Como defensa, el upgrade incluye un
pre-check que CUENTA las filas con email NULL y falla con un mensaje claro
(abortando la migracion) si apareciera alguna: nunca se inventa un email
(no-PII: no hay backfill con datos falsos) ni se borran usuarios.

Reversible: el downgrade vuelve `email` a nullable.

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): por eso el nombre corto.
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_users_email_not_null"
down_revision = "0017_identity_access_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    nulls = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM identity.users WHERE email IS NULL"))
        .scalar()
    )
    if int(nulls or 0) > 0:
        raise RuntimeError(
            f"0018_users_email_not_null abortada: hay {int(nulls)} fila(s) en "
            "identity.users con email NULL. Depurarlas (asignar el email real del "
            "titular o dar de baja al usuario) y reintentar; esta migracion no "
            "inventa emails ni borra usuarios."
        )
    op.alter_column(
        "users",
        "email",
        existing_type=sa.String(320),
        nullable=False,
        schema="identity",
    )


def downgrade() -> None:
    op.alter_column(
        "users",
        "email",
        existing_type=sa.String(320),
        nullable=True,
        schema="identity",
    )
