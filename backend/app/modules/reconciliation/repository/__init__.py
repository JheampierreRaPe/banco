"""Repositorio del schema `reconciliation` (E5-T17, HU19).

Capa de datos sin endpoints ni logica de negocio: solo SQLAlchemy sobre
las tablas propias (`clearing_files`, `clearing_items`). Convencion del
repo: `flush` sin `commit` (decide el llamante). No mueve dinero ni crea
asientos (eso es `E5-T18`..`E5-T20` via el motor). Sin PII en logs
(regla 7): aqui solo `file_ref`/`external_ref` hasheados y conteos
(nunca valores en claro).

Validaciones (el brief exige `ValueError` por datos invalidos):
`file_ref` opaco (`1..64`, sin eco en errores), `source` normalizada a
mayusculas, `checksum` SHA-256 hex de 64, `item_count` entero `>= 0`,
`external_ref` opaco (`1..128`), `amount_minor` entero en centimos
`> 0` (sin `bool`), `currency` ISO-4217 (3 letras ASCII, se normaliza a
mayusculas), `value_date` fecha valida, `direction` solo
`INBOUND`/`OUTBOUND`, `raw` mapeo o `None`.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.reconciliation.models import (
    CLEARING_DIRECTIONS,
    CLEARING_FILE_STATUSES,
    ClearingFile,
    ClearingItem,
)

logger = logging.getLogger(__name__)

_FILE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,63}$")
_EXTERNAL_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,127}$")
_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class ReconciliationError(ValueError):
    """Error base de `reconciliation` (mapeable a 4xx)."""


class ReconciliationValidationError(ReconciliationError):
    """Dato de `reconciliation` invalido (`VALIDATION_ERROR`)."""


class ReconciliationNotFoundError(ReconciliationError):
    """Fila de `reconciliation` inexistente (`NOT_FOUND`)."""


class ReconciliationConflictError(ReconciliationError):
    """Conflicto de negocio (`DUPLICATE_FILE`, UQ de `file_ref`)."""


def hash_file_ref(value: object) -> str:
    """Hash corto de `file_ref` para correlacion en logs (sin PII)."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def hash_external_ref(value: object) -> str:
    """Hash corto de `external_ref` para correlacion en logs (sin PII)."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def coerce_file_ref(value: object) -> str:
    """Normaliza y valida `file_ref` (opaco, sin eco en errores)."""
    text = str(value or "").strip()
    if not text or len(text) > 64 or not _FILE_REF_RE.match(text):
        raise ReconciliationValidationError("referencia de archivo invalida")
    return text


def coerce_source(value: object | None) -> str:
    """Normaliza la fuente/red (`NETWORK` por defecto)."""
    text = str(value if value is not None else "NETWORK").strip().upper()
    if not text or len(text) > 40 or not _FILE_REF_RE.match(text):
        raise ReconciliationValidationError("fuente de clearing invalida")
    return text


def coerce_checksum(value: object) -> str:
    """Valida `checksum` SHA-256 hex (sin eco en errores)."""
    text = str(value or "").strip().lower()
    if not _CHECKSUM_RE.match(text):
        raise ReconciliationValidationError("checksum de clearing invalido")
    return text


def coerce_item_count(value: object) -> int:
    """Valida `item_count` entero `>= 0` (sin `bool`)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReconciliationValidationError("conteo de items invalido")
    return value


def coerce_external_ref(value: object) -> str:
    """Normaliza y valida `external_ref` (opaco, sin eco en errores)."""
    text = str(value or "").strip()
    if not text or len(text) > 128 or not _EXTERNAL_REF_RE.match(text):
        raise ReconciliationValidationError("referencia externa invalida")
    return text


def coerce_amount_minor(value: object) -> int:
    """Monto entero en centimos `> 0` (nunca `float`, regla 3)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReconciliationValidationError("monto de clearing invalido")
    return value


def coerce_currency(value: object) -> str:
    """Codigo ISO-4217: 3 letras ASCII (se normaliza a mayusculas)."""
    if not isinstance(value, str):
        raise ReconciliationValidationError("moneda de clearing invalida")
    code = value.strip().upper()
    if not _CURRENCY_RE.match(code) or not code.isascii():
        raise ReconciliationValidationError("moneda de clearing invalida")
    return code


def coerce_value_date(value: object) -> date:
    """Normaliza `value_date` a fecha (exige fecha valida)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        raise ReconciliationValidationError("fecha de valor invalida") from None


def coerce_direction(value: object) -> str:
    """Valida `direction` (`INBOUND`/`OUTBOUND`)."""
    text = str(value or "").strip().upper()
    if text not in CLEARING_DIRECTIONS:
        raise ReconciliationValidationError("direccion de clearing invalida")
    return text


def coerce_raw(value: object) -> dict[str, Any] | None:
    """Valida `raw` opaco (mapeo o `None`; sin PII en logs)."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ReconciliationValidationError("contenido de clearing invalido")
    return dict(value)


def coerce_file_status(value: object) -> str:
    """Valida `status` del archivo (`RECEIVED`/`PROCESSED`/`FAILED`)."""
    text = str(value or "").strip().upper()
    if text not in CLEARING_FILE_STATUSES:
        raise ReconciliationValidationError("estado de archivo invalido")
    return text


def get_file_by_ref(session: Session, file_ref: object) -> ClearingFile | None:
    """Archivo por `file_ref` o `None` (`None` si la ref no es valida)."""
    try:
        code = coerce_file_ref(file_ref)
    except ReconciliationValidationError:
        return None
    return session.scalars(
        select(ClearingFile).where(ClearingFile.file_ref == code).limit(1)
    ).first()


def create_file_with_items(
    session: Session,
    *,
    source: object | None,
    file_ref: object,
    checksum: object,
    item_count: object,
    items: list[Mapping[str, Any]],
    storage_ref: object | None = None,
    status: str = "RECEIVED",
) -> ClearingFile:
    """Persiste un archivo con sus items `PENDING` (transaccional).

    Valida TODO antes de mutar (si algo falla, la sesion queda intacta
    y el llamante revierte sin parciales). Hace `flush`, no `commit`.
    `file_ref` duplicado (carrera) -> `ReconciliationConflictError`
    (la UQ decide; la perdedora hace replay).
    """
    code = coerce_file_ref(file_ref)
    origin = coerce_source(source)
    digest = coerce_checksum(checksum)
    count = coerce_item_count(item_count)
    state = coerce_file_status(status)
    if storage_ref is not None and (
        not isinstance(storage_ref, str) or not storage_ref.strip() or len(storage_ref) > 500
    ):
        raise ReconciliationValidationError("referencia de almacenamiento invalida")
    if not isinstance(items, list):
        raise ReconciliationValidationError("items de clearing invalidos")
    if count != len(items):
        raise ReconciliationValidationError("conteo de items inconsistente")
    # H-2 (`fx`): toda la validacion ANTES de tocar la sesion; si algo
    # falla aqui, no hay filas dirty para el llamante.
    prepared: list[dict[str, Any]] = []
    for entry in items:
        if not isinstance(entry, Mapping):
            raise ReconciliationValidationError("item de clearing invalido")
        prepared.append(
            {
                "external_ref": coerce_external_ref(entry.get("external_ref")),
                "amount_minor": coerce_amount_minor(entry.get("amount_minor", entry.get("amount"))),
                "currency": coerce_currency(entry.get("currency")),
                "value_date": coerce_value_date(entry.get("value_date", entry.get("date"))),
                "direction": coerce_direction(entry.get("direction", entry.get("type"))),
                "raw": coerce_raw(entry.get("raw")),
            }
        )
    row = ClearingFile(
        source=origin,
        file_ref=code,
        storage_ref=(storage_ref.strip() if isinstance(storage_ref, str) else None),
        checksum=digest,
        item_count=count,
        status=state,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
            for data in prepared:
                session.add(
                    ClearingItem(
                        file_id=row.id,
                        external_ref=data["external_ref"],
                        amount_minor=data["amount_minor"],
                        currency=data["currency"],
                        value_date=data["value_date"],
                        direction=data["direction"],
                        status="PENDING",
                        raw=data["raw"],
                    )
                )
            session.flush()
    except IntegrityError as exc:
        raise ReconciliationConflictError("archivo de clearing duplicado") from exc
    logger.debug(
        "clearing file creado file_hash=%s items=%d",
        hash_file_ref(code),
        len(prepared),
    )
    return row


def record_failed_file(
    session: Session,
    *,
    source: object | None = None,
    file_ref: object = None,
    checksum: object = None,
    item_count: object = 0,
    storage_ref: object | None = None,
) -> ClearingFile:
    """Conserva un archivo fallido (`FAILED`, sin items) como evidencia.

    Tolerante a proposito: si algun campo secundario es invalido se
    normaliza (`source` -> `NETWORK`, `item_count` -> `0`,
    `checksum`/`storage_ref` se guardan tal cual truncados) porque lo
    que importa es la clave (`file_ref`) y el estado. `file_ref`
    invalido -> `ReconciliationValidationError` (no hay clave que
    evidenciar). `file_ref` duplicado (carrera) ->
    `ReconciliationConflictError` (la UQ decide). Hace `flush`, no
    `commit`.
    """
    code = coerce_file_ref(file_ref)
    try:
        origin = coerce_source(source)
    except ReconciliationValidationError:
        origin = "NETWORK"
    try:
        count = coerce_item_count(item_count)
    except ReconciliationValidationError:
        count = 0
    digest = str(checksum or "").strip().lower()[:64]
    storage: str | None = None
    if isinstance(storage_ref, str) and storage_ref.strip():
        storage = storage_ref.strip()[:500]
    row = ClearingFile(
        source=origin,
        file_ref=code,
        storage_ref=storage,
        checksum=digest,
        item_count=count,
        status="FAILED",
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        raise ReconciliationConflictError("archivo de clearing duplicado") from exc
    logger.debug("clearing file fallido file_hash=%s", hash_file_ref(code))
    return row


def list_pending_items(
    session: Session,
    *,
    file_id: uuid.UUID | str | None = None,
    limit: int = 100,
) -> list[ClearingItem]:
    """Items `PENDING` (del archivo si se indica; orden estable)."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ReconciliationValidationError("limite invalido")
    stmt = select(ClearingItem).where(ClearingItem.status == "PENDING")
    if file_id is not None:
        try:
            fid = file_id if isinstance(file_id, uuid.UUID) else uuid.UUID(str(file_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ReconciliationValidationError("archivo invalido") from exc
        stmt = stmt.where(ClearingItem.file_id == fid)
    stmt = stmt.order_by(ClearingItem.value_date, ClearingItem.external_ref, ClearingItem.id)
    return list(session.scalars(stmt.limit(limit)).all())


def mark_file_status(session: Session, file: ClearingFile, status: object) -> ClearingFile:
    """Avanza el estado del archivo (`RECEIVED`/`PROCESSED`/`FAILED`).

    Idempotente si ya esta en el estado. Hace `flush`, no `commit`.
    """
    state = coerce_file_status(status)
    if file.status == state:
        return file
    file.status = state
    session.flush()
    logger.debug(
        "clearing file marcado file_hash=%s status=%s",
        hash_file_ref(file.file_ref),
        state,
    )
    return file


__all__ = [
    "ReconciliationConflictError",
    "ReconciliationError",
    "ReconciliationNotFoundError",
    "ReconciliationValidationError",
    "coerce_amount_minor",
    "coerce_checksum",
    "coerce_currency",
    "coerce_direction",
    "coerce_external_ref",
    "coerce_file_ref",
    "coerce_file_status",
    "coerce_item_count",
    "coerce_raw",
    "coerce_source",
    "coerce_value_date",
    "create_file_with_items",
    "get_file_by_ref",
    "hash_external_ref",
    "hash_file_ref",
    "list_pending_items",
    "mark_file_status",
    "record_failed_file",
]
