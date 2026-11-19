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

#: Estados de `reconciliation_runs.status` (`03b#12.2`, E5-T18).
RUN_STATUSES = ("RUNNING", "COMPLETED", "FAILED")

#: Calificadores de `reconciliation_matches.match_type` (`03b#12.2`, E5-T18
#: review PR #27): solo pares conciliados (`EXACT`/`AMOUNT_DIFF`/`DATE_DIFF`).
MATCH_TYPES = ("EXACT", "AMOUNT_DIFF", "DATE_DIFF")

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
        sa.Index("ix_clearing_items_run_id", "run_id"),
        # M6 (review PR #27): alcance de la corrida (items `PENDING` de
        # archivos `PROCESSED` por fecha/moneda) en un solo indice.
        sa.Index(
            "ix_clearing_items_status_value_currency",
            "status",
            "value_date",
            "currency",
        ),
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
    # Corrida que proceso el item (`NULL` = aun no procesado). REF logica a
    # `reconciliation_runs.id` sin FK fisica (el item nace sin corrida y la
    # FK fisica impediria el `flush` por orden en SQLite); la fija solo el
    # servicio `run_reconciliation` (H2/H3 del review PR #27).
    run_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    raw: Mapped[dict[str, Any] | None] = mapped_column(
        postgresql.JSONB(astext_type=sa.Text()), nullable=True
    )

    file: Mapped[ClearingFile] = relationship(back_populates="items")


class ReconciliationRun(Base):
    """Corrida de conciliacion (`03b#12.2`, E5-T18 + review PR #27).

    Una fecha/moneda puede tener VARIAS corridas (sin UQ natural): las
    `COMPLETED` son inmutables y nunca se reabren; si llegan items `PENDING`
    nuevos (lotes tardios de archivos `PROCESSED`) se crea una corrida nueva
    que procesa solo esos items; reejecutar sin pendientes devuelve la ultima
    `COMPLETED`. Solo una `RUNNING` por `(run_date, currency)` (indice unico
    parcial `uq_reconciliation_runs_single_running` `WHERE status='RUNNING'`;
    segunda `RUNNING` concurrente = `CONFLICT` por la BD); `FAILED` se
    reintenta en la misma fila. `matched_count` = pares conciliados;
    `unmatched_count` = TOTAL de excepciones del dominio (BAJO PR #27 B1,
    opcion total: incluye `MISSING_INTERNAL` de items huerfanos y
    `MISSING_EXTERNAL` de operaciones internas sin item) y se mantiene por
    compatibilidad como `unmatched_item_count + unmatched_operation_count`;
    el desglose vive en `unmatched_item_count` (excepciones del lado
    clearing: items sin conciliar) y `unmatched_operation_count`
    (excepciones del lado interno: `MISSING_EXTERNAL`, operaciones sin
    item); `total_minor` = suma con signo en centimos de los items
    conciliados
    (M5 del review PR #27: `INBOUND` +, `OUTBOUND` -; puede ser negativo,
    sin CHECK `>= 0`; nunca `float`). Sin PII: solo fecha, moneda y conteos. Los items los mueve
    solo el servicio ``run_reconciliation`` (``PENDING`` -> ``MATCHED`` /
    ``EXCEPTION``); este modelo no toca dinero ni asientos.
    """

    __tablename__ = "reconciliation_runs"
    __table_args__ = (
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
            "unmatched_item_count >= 0",
            name="ck_reconciliation_runs_unmatched_item_nonneg",
        ),
        sa.CheckConstraint(
            "unmatched_operation_count >= 0",
            name="ck_reconciliation_runs_unmatched_operation_nonneg",
        ),
        sa.CheckConstraint(
            "unmatched_count = unmatched_item_count + unmatched_operation_count",
            name="ck_reconciliation_runs_unmatched_total",
        ),
        # Sin CHECK `>= 0` en `total_minor` (M5 del review PR #27): suma con
        # signo (`INBOUND` +, `OUTBOUND` -), puede ser negativa.
        sa.Index("ix_reconciliation_runs_status", "status"),
        sa.Index("ix_reconciliation_runs_date_currency", "run_date", "currency"),
        # Guarda de BD (MEDIO-2): una sola `RUNNING` por `(run_date,
        # currency)`. Parcial `WHERE status='RUNNING'` para permitir varias
        # corridas por fecha/moneda (`COMPLETED` inmutables, lotes tardios) y
        # reintentos de `FAILED` en su fila. El `sqlite_where` replica la
        # semantica en SQLite (tests); en Postgres manda `postgresql_where`
        # (mismo criterio que `beneficiaries`).
        sa.Index(
            "uq_reconciliation_runs_single_running",
            "run_date",
            "currency",
            unique=True,
            postgresql_where=sa.text("status = 'RUNNING'"),
            sqlite_where=sa.text("status = 'RUNNING'"),
        ),
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
    # Desglose del total (3.3 del comentario PR #27): excepciones del lado
    # clearing (items sin conciliar) y del lado interno (`MISSING_EXTERNAL`,
    # operaciones sin item). `unmatched_count` se mantiene como la suma por
    # compatibilidad (`E5-T18` CA-01 y lectores de `docs/03b#12.2`).
    unmatched_item_count: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default="0"
    )
    unmatched_operation_count: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default="0"
    )
    # Suma con signo en centimos de los items conciliados (`INBOUND` +,
    # `OUTBOUND` -; M5); nunca `float` (regla 3).
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


class ReconciliationMatch(Base):
    """Emparejamiento persistido de la corrida (`03b#12.2`, review PR #27).

    Una fila por par conciliado: que clearing_item cruzo con que operacion
    interna (`transaction_id` logico, sin FK fisica fuera del schema),
    con que calificador (`EXACT`/`AMOUNT_DIFF`/`DATE_DIFF`), monto y moneda.
    `amount_diff_minor` (`clearing - internal`, centimos) y `date_diff_days`
    (`clearing_date - internal_date`, dias) conservan la diferencia que
    califico el par (solo para `AMOUNT_DIFF`/`DATE_DIFF`; `EXACT` deja
    `NULL`): la misma convencion del dominio (`matching._assess_pair`).
    `UQ(clearing_item_id)`: un item se concilia como maximo una vez (doble
    match = conflicto). `UQ(transaction_id)`: una operacion interna se
    concilia como maximo una vez en todo el historial (doble uso =
    conflicto; la corrida excluye los ya usados antes de cruzar).
    Auditoria item -> `transaction_id` sin PII.
    """

    __tablename__ = "reconciliation_matches"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["reconciliation.reconciliation_runs.id"],
            name="fk_reconciliation_matches_run",
        ),
        sa.ForeignKeyConstraint(
            ["clearing_item_id"],
            ["reconciliation.clearing_items.id"],
            name="fk_reconciliation_matches_item",
        ),
        sa.UniqueConstraint(
            "clearing_item_id",
            name="uq_reconciliation_matches_item",
        ),
        sa.UniqueConstraint(
            "transaction_id",
            name="uq_reconciliation_matches_transaction",
        ),
        sa.CheckConstraint(
            "match_type IN ('EXACT','AMOUNT_DIFF','DATE_DIFF')",
            name="ck_reconciliation_matches_type",
        ),
        sa.CheckConstraint(
            "amount_minor >= 0",
            name="ck_reconciliation_matches_amount_nonneg",
        ),
        sa.Index("ix_reconciliation_matches_run_id", "run_id"),
        sa.Index("ix_reconciliation_matches_transaction_id", "transaction_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    clearing_item_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    # REF logica a `transactions` (sin FK fisica entre modulos, regla 4).
    transaction_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    match_type: Mapped[str] = mapped_column(sa.String(15), nullable=False)
    # Monto en centimos `>= 0` del item conciliado; nunca `float` (regla 3).
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    # Diferencias que calificaron el par (nullable; `EXACT` deja `NULL`).
    # Convencion del dominio: `clearing - internal` y dias
    # `clearing_date - internal_date` (pueden ser negativas).
    amount_diff_minor: Mapped[int | None] = mapped_column(sa.BigInteger(), nullable=True)
    date_diff_days: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )


__all__ = [
    "CLEARING_DIRECTIONS",
    "CLEARING_FILE_STATUSES",
    "CLEARING_ITEM_STATUSES",
    "MATCH_TYPES",
    "RUN_STATUSES",
    "SCHEMA",
    "ClearingFile",
    "ClearingItem",
    "ReconciliationMatch",
    "ReconciliationRun",
]
