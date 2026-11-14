"""Modelos ORM del schema `reconciliation` (E5-T17, HU19).

Fuente: `docs/03b-diccionario-de-datos.md#12.1` y la migracion
`0050_reconciliation`. Frontera: solo `reconciliation`; sin imports de
`tablas` de `transactions`/`ledger`/`accounts`/`risk`/`fx` (regla de oro
4: el cruce con el motor se hace por IDs en `E5-T18`). Dinero entero en
centimos (`BIGINT`); nunca `float` (regla de oro 3). Sin PII: solo
referencias opacas, montos, monedas y fechas (regla 7).

Los `CHECK` replican 1:1 los de la migracion (sin drift en
`alembic check`): `status` del archivo (`RECEIVED`/`PROCESSED`/
`FAILED`), `status` del item (`PENDING`/`MATCHED`/`EXCEPTION`) y
`direction` (`INBOUND`/`OUTBOUND`) como `CHECK` sobre `VARCHAR`
(portable, mismo criterio que `wallet.qr_charges` y `fx`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

SCHEMA = "reconciliation"

#: Estados de `clearing_files.status` (`03b#12.1`, E5-T17).
CLEARING_FILE_STATUSES = ("RECEIVED", "PROCESSED", "FAILED")

#: Estados de `clearing_items.status` (`03b#12.1`, E5-T17).
CLEARING_ITEM_STATUSES = ("PENDING", "MATCHED", "EXCEPTION")

#: Estados de `reconciliation_runs.status` (`03b#12.2`, E5-T18 corte 2).
RUN_STATUSES = ("RUNNING", "COMPLETED", "FAILED")

#: Direcciones de `clearing_items.direction` (`03b#12.1`, E5-T17).
CLEARING_DIRECTIONS = ("INBOUND", "OUTBOUND")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ClearingFile(Base):
    """Lote de compensacion recibido de la red/equipo par (`03b#12.1`).

    `file_ref` es la clave natural (UQ): reingresar el mismo lote no
    duplica items (replay). `checksum` SHA-256 hex del contenido
    canonico: un `file_ref` conocido con checksum distinto es
    `VALIDATION_ERROR` y el archivo queda `FAILED`. Los items nacen
    `PENDING`; solo el matching (`E5-T18`) los mueve. Nada se borra.
    """

    __tablename__ = "clearing_files"
    __table_args__ = (
        sa.UniqueConstraint("file_ref", name="uq_clearing_files_file_ref"),
        sa.CheckConstraint(
            "status IN ('RECEIVED','PROCESSED','FAILED')",
            name="ck_clearing_files_status",
        ),
        sa.CheckConstraint(
            "item_count >= 0",
            name="ck_clearing_files_item_count_nonneg",
        ),
        sa.Index("ix_clearing_files_source", "source"),
        sa.Index("ix_clearing_files_status", "status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    file_ref: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    storage_ref: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    checksum: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    item_count: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default="RECEIVED", server_default="RECEIVED"
    )
    received_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )

    items: Mapped[list[ClearingItem]] = relationship(
        back_populates="file",
        lazy="selectin",
        # Sin cascade de borrado: los lotes son inmutables (nada se
        # borra, ni siquiera un archivo `FAILED`); la FK sin `ON
        # DELETE` los protege en Postgres y la fila huerfana sobrevive
        # en SQLite (mismo criterio que `fx.savings_pockets`).
        cascade="save-update, merge",
        passive_deletes="all",
        order_by="ClearingItem.external_ref",
    )


class ClearingItem(Base):
    """Operacion individual de un lote (`03b#12.1`, E5-T17).

    `amount_minor` entero en centimos `> 0`; `currency` ISO-4217;
    `value_date` fecha de valor; `direction` efecto del flujo
    (`INBOUND` suma entrante, `OUTBOUND` saliente). `raw` opaco con el
    contenido original del proveedor (sin PII en claro). Nace
    `PENDING` y solo el matching (`E5-T18`) lo mueve a
    `MATCHED`/`EXCEPTION`.
    """

    __tablename__ = "clearing_items"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["reconciliation.clearing_files.id"],
            name="fk_clearing_items_file",
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_clearing_items_amount_pos"),
        sa.CheckConstraint(
            "length(trim(currency)) = 3",
            name="ck_clearing_items_currency_iso",
        ),
        sa.CheckConstraint(
            "direction IN ('INBOUND','OUTBOUND')",
            name="ck_clearing_items_direction",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','MATCHED','EXCEPTION')",
            name="ck_clearing_items_status",
        ),
        sa.Index("ix_clearing_items_file_id", "file_id"),
        sa.Index("ix_clearing_items_external_ref", "external_ref"),
        sa.Index("ix_clearing_items_status", "status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    external_ref: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    # Monto entero en centimos `> 0`; nunca `float` (regla 3).
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    value_date: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    direction: Mapped[str] = mapped_column(sa.String(10), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default="PENDING", server_default="PENDING"
    )
    raw: Mapped[dict[str, Any] | None] = mapped_column(
        postgresql.JSONB(astext_type=sa.Text()), nullable=True
    )

    file: Mapped[ClearingFile] = relationship(back_populates="items")


class ReconciliationRun(Base):
    """Corrida de conciliacion (`03b#12.2`, E5-T18 corte 2).

    Una fila por (``run_date``, ``currency``) (UQ natural): reejecutar la
    misma fecha/moneda es idempotente (``COMPLETED`` se reutiliza,
    ``RUNNING`` concurrente es ``CONFLICT``, ``FAILED`` se reintenta en la
    misma fila). ``matched_count`` = pares conciliados; ``unmatched_count``
    = items de clearing no conciliados (lado clearing de las excepciones);
    ``total_minor`` = suma en centimos de los items conciliados (nunca
    ``float``). Sin PII: solo fecha, moneda y conteos. Los items los mueve
    solo el servicio ``run_reconciliation`` (``PENDING`` -> ``MATCHED`` /
    ``EXCEPTION``); este modelo no toca dinero ni asientos.
    """

    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        sa.UniqueConstraint(
            "run_date",
            "currency",
            name="uq_reconciliation_runs_date_currency",
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING','COMPLETED','FAILED')",
            name="ck_reconciliation_runs_status",
        ),
        sa.CheckConstraint(
            "matched_count >= 0",
            name="ck_reconciliation_runs_matched_nonneg",
        ),
        sa.CheckConstraint(
            "unmatched_count >= 0",
            name="ck_reconciliation_runs_unmatched_nonneg",
        ),
        sa.CheckConstraint(
            "total_minor >= 0",
            name="ck_reconciliation_runs_total_nonneg",
        ),
        sa.Index("ix_reconciliation_runs_status", "status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    run_date: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default="RUNNING", server_default="RUNNING"
    )
    matched_count: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default="0"
    )
    unmatched_count: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default="0"
    )
    # Suma en centimos de los items conciliados; nunca `float` (regla 3).
    total_minor: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, default=0, server_default="0"
    )
    executed_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


__all__ = [
    "CLEARING_DIRECTIONS",
    "CLEARING_FILE_STATUSES",
    "CLEARING_ITEM_STATUSES",
    "RUN_STATUSES",
    "SCHEMA",
    "ClearingFile",
    "ClearingItem",
    "ReconciliationRun",
]
