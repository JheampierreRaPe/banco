"""Unicidad de duplicados ACTIVE en beneficiarios (E2-T13 fix, MEDIUM).

Revision ID: 0022_beneficiaries_active_uq
Revises: 0021_accounts_beneficiaries
Create Date: 2026-09-27

Cierra la carrera entre el chequeo de duplicados por SELECT (`service/
beneficiaries` + `repository.find_active_duplicate_*`) y el INSERT: dos
altas concurrentes con mismo alias o misma cuenta/CCI del titular chocan
contra el UNIQUE en BD (el servicio traduce `IntegrityError` a
`DuplicateBeneficiaryError`, 409).

Parciales `WHERE status = 'ACTIVE'` (solo `accounts`, sin tocar otros
schemas): tras el soft-delete (`status=DELETED`) el alias/cuenta puede
reutilizarse (comportamiento ya cubierto por el servicio). Reflejan los
indices del modelo ORM (`uq_beneficiaries_owner_*_active`).

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): por eso el nombre corto.
"""

import sqlalchemy as sa
from alembic import op

revision = "0022_beneficiaries_active_uq"
down_revision = "0021_accounts_beneficiaries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_beneficiaries_owner_alias_active",
        "beneficiaries",
        ["owner_user_id", "alias_search_hash"],
        schema="accounts",
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "uq_beneficiaries_owner_account_active",
        "beneficiaries",
        ["owner_user_id", "account_hash"],
        schema="accounts",
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_beneficiaries_owner_account_active",
        table_name="beneficiaries",
        schema="accounts",
    )
    op.drop_index(
        "uq_beneficiaries_owner_alias_active",
        table_name="beneficiaries",
        schema="accounts",
    )
