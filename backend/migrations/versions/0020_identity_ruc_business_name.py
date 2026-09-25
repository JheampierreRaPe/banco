"""Soporte real de RUC en `identity.users` (E1-T36, HU01).

Revision ID: 0020_identity_ruc_business_name
Revises: 0019_identity_parameters
Create Date: 2026-09-24

Fuente: `docs/tasks/E1-T36.md` (decision del dueno: RUC = opcion B = soporte
REAL de persona juridica, no solo-consulta). Cambios:

- Nueva columna `identity.users.business_name VARCHAR(150) NULL` (razon
  social; solo RUC juridica, el resto la deja `NULL`).
- CHECK `ck_users_doc_type` relajado a
  `doc_type IN ('DNI', 'CE', 'PASSPORT', 'RUC')`.

Sin backfill ni datos inventados: la columna nace nulable y el CHECK solo
amplia el dominio (ninguna fila existente viola el nuevo CHECK). No se toca
`doc_number_hash` (UQ, HMAC de E1-T24) ni `doc_number_masked`.

Reversible: el downgrade recrea el CHECK sin `RUC` y dropea la columna.
Documentado: el downgrade FALLA si ya existen filas con `doc_type='RUC'`
(hay que migrarlas o darlas de baja antes; esta migracion no inventa
datos ni borra usuarios).

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): por eso el nombre corto.
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_identity_ruc_business_name"
down_revision = "0019_identity_parameters"
branch_labels = None
depends_on = None

#: Dominio final del CHECK (espejo de `models.DOC_TYPES`, E1-T36).
DOC_TYPES_WITH_RUC = "doc_type IN ('DNI', 'CE', 'PASSPORT', 'RUC')"
#: Dominio previo al cambio (migracion `0012_identity_core`).
DOC_TYPES_LEGACY = "doc_type IN ('DNI', 'CE', 'PASSPORT')"


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("business_name", sa.String(150), nullable=True),
        schema="identity",
    )
    op.drop_constraint("ck_users_doc_type", "users", schema="identity", type_="check")
    op.create_check_constraint(
        "ck_users_doc_type",
        "users",
        DOC_TYPES_WITH_RUC,
        schema="identity",
    )


def downgrade() -> None:
    ruc_rows = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM identity.users WHERE doc_type = 'RUC'"))
        .scalar()
    )
    if int(ruc_rows or 0) > 0:
        raise RuntimeError(
            f"0020_identity_ruc_business_name abortada: hay {int(ruc_rows)} fila(s) en "
            "identity.users con doc_type='RUC'. Migrarlas o darlas de baja antes de "
            "revertir; este downgrade no inventa datos ni borra usuarios."
        )
    op.drop_constraint("ck_users_doc_type", "users", schema="identity", type_="check")
    op.create_check_constraint(
        "ck_users_doc_type",
        "users",
        DOC_TYPES_LEGACY,
        schema="identity",
    )
    op.drop_column("users", "business_name", schema="identity")
