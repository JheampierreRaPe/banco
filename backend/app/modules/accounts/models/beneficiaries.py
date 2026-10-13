"""Tabla `accounts.beneficiaries` + indice ciego de busqueda (E2-T13, HU07 CA-01/CA-02).

Fuente: `docs/03b-diccionario-de-datos.md#53-beneficiaries`,
`docs/03b#15-indices-recomendados-resumen` (`beneficiaries (owner_user_id, alias)`),
`docs/02#10` (AES-256 en reposo), `docs/16` reglas 7/9 (sin PII en logs,
enmascarar por defecto).

Diseno:

- `owner_user_id` es REF logica a `identity.users.id` SIN FK fisica
  (regla de oro 4, `docs/03c#13`).
- Datos sensibles cifrados en reposo: `alias_enc`, `account_enc`,
  `holder_name_enc` (sobres de `service/beneficiaries_crypto`); en BD solo
  viaja cifrado, jamas en claro.
- `holder_doc_hash` es SOLO hash (HMAC ciego del documento): el numero en
  claro nunca se persiste ni se retorna.
- Busqueda indexada sin descifrar todo: `alias_search_hash` /
  `name_search_hash` / `account_hash` (igualdad exacta + dedup) e indice
  invertido `beneficiary_search_tokens` (palabra + prefijos, AND entre
  terminos de `q`). Ver `service/beneficiaries_crypto.py`.
- `account_last4` en claro (4 digitos): es el dato enmascarado estandar
  (`****1234`, igual que `mask_account_number` de E2-T02); permite
  responder listas sin descifrar.
- Soft-delete: `status ACTIVE/DELETED`; jamas borrado fisico.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.modules.accounts.models import SCHEMA

BENEFICIARY_STATUSES = ("ACTIVE", "DELETED")


class Beneficiary(Base):
    """Beneficiario frecuente (`03b#53`) con sensibles cifrados."""

    __tablename__ = "beneficiaries"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DELETED')",
            name="ck_beneficiaries_status",
        ),
        sa.Index("ix_beneficiaries_owner_alias", "owner_user_id", "alias_search_hash"),
        sa.Index("ix_beneficiaries_owner_account", "owner_user_id", "account_hash"),
        sa.Index("ix_beneficiaries_owner_status", "owner_user_id", "status"),
        # Unicidad de duplicados ACTIVE (`0022`, cierra la carrera entre el
        # chequeo por SELECT y el INSERT): parcial `WHERE status='ACTIVE'`
        # para permitir reutilizar alias/cuenta tras soft-delete. El
        # `sqlite_where` replica la semantica en SQLite (tests); en Postgres
        # manda `postgresql_where`.
        sa.Index(
            "uq_beneficiaries_owner_alias_active",
            "owner_user_id",
            "alias_search_hash",
            unique=True,
            postgresql_where=sa.text("status = 'ACTIVE'"),
            sqlite_where=sa.text("status = 'ACTIVE'"),
        ),
        sa.Index(
            "uq_beneficiaries_owner_account_active",
            "owner_user_id",
            "account_hash",
            unique=True,
            postgresql_where=sa.text("status = 'ACTIVE'"),
            sqlite_where=sa.text("status = 'ACTIVE'"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    # REF logica a `identity.users` (sin FK fisica).
    owner_user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    # Sensibles cifrados (sobres `B1`/`B0`; nunca en claro en BD).
    alias_enc: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    alias_search_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    photo_url: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    bank_code: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    account_enc: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    account_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    account_last4: Mapped[str] = mapped_column(sa.String(4), nullable=False)
    holder_name_enc: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    name_search_hash: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    # SOLO hash del documento (el numero en claro nunca se persiste).
    holder_doc_hash: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    currency: Mapped[str | None] = mapped_column(sa.String(3), nullable=True)
    status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )


class BeneficiarySearchToken(Base):
    """Indice invertido ciego (`owner_user_id`, `token_hash`) -> beneficiario.

    Cada fila es el HMAC de una palabra/prefijo del alias o nombre
    normalizados (ver `token_hashes_for`). La busqueda `q` se resuelve por
    igualdad indexada (AND entre terminos), sin LIKE sobre claro ni
    descifrado masivo. PK compuesta: sin duplicados por (beneficiario,
    token). FK autocontenida al propio schema (regla de oro 4).
    """

    __tablename__ = "beneficiary_search_tokens"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["beneficiary_id"],
            [f"{SCHEMA}.beneficiaries.id"],
            name="fk_beneficiary_tokens_beneficiary",
            ondelete="CASCADE",
        ),
        sa.Index("ix_beneficiary_tokens_owner_token", "owner_user_id", "token_hash"),
        {"schema": SCHEMA},
    )

    beneficiary_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    token_hash: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)


__all__ = ["BENEFICIARY_STATUSES", "Beneficiary", "BeneficiarySearchToken"]
