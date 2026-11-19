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
`external_ref` opaco (`1..80`, igual que la columna `VARCHAR(80)` de la
migracion `0050`, M4), `amount_minor` entero en centimos
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
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.reconciliation.models import (
    CLEARING_DIRECTIONS,
    CLEARING_FILE_STATUSES,
    MATCH_TYPES,
    RUN_STATUSES,
    ClearingFile,
    ClearingItem,
    ReconciliationMatch,
    ReconciliationRun,
)

logger = logging.getLogger(__name__)

_FILE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,63}$")
# M4 (review PR #27): `clearing_items.external_ref` es `VARCHAR(80)` en la
# migracion `0050` (ver `docs/03b#12.1`); el validador acepta como maximo
# 80 (1 + 79), sin migracion. Una ref de 81+ es `VALIDATION_ERROR`.
_EXTERNAL_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,79}$")
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
    """Normaliza y valida `external_ref` (opaco, `1..80`, sin eco en errores)."""
    text = str(value or "").strip()
    if not text or len(text) > 80 or not _EXTERNAL_REF_RE.match(text):
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
    `storage_ref` se guarda tal cual truncado) porque lo que importa es
    la clave (`file_ref`) y el estado. `checksum` siempre queda en 64
    hex: si ya es SHA-256 valido se guarda tal cual; si es malformado o
    ausente se persiste el digest derivado
    `sha256(str(checksum).encode()).hexdigest()` (determinista por valor
    entrante: conserva la evidencia sin romper el contrato de 64 hex de
    `clearing_files.checksum`, F2 del dictamen PR #27). `file_ref`
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
    try:
        digest = coerce_checksum(checksum)
    except ReconciliationValidationError:
        raw_text = "" if checksum is None else str(checksum)
        digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
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

    Idempotente si ya esta en el estado. Si el archivo pasa a `FAILED`,
    sus items aun `PENDING` se marcan `EXCEPTION` (sin `run_id`: no los
    proceso ninguna corrida, pero ya no quedan en el limbo: el cruce
    `list_pending_items_for_run` solo lee `PENDING` de `PROCESSED`).
    Hace `flush`, no `commit`.
    """
    state = coerce_file_status(status)
    if file.status == state:
        # Ya en `FAILED`: los items ya se cerraron en la transicion
        # original; nada que hacer.
        return file
    file.status = state
    session.flush()
    if state == "FAILED":
        result = session.execute(
            update(ClearingItem)
            .where(ClearingItem.file_id == file.id, ClearingItem.status == "PENDING")
            .values(status="EXCEPTION")
        )
        closed = int(result.rowcount or 0)
        if closed:
            session.flush()
            logger.debug(
                "clearing file fallido items cerrados file_hash=%s cerrados=%d",
                hash_file_ref(file.file_ref),
                closed,
            )
    logger.debug(
        "clearing file marcado file_hash=%s status=%s",
        hash_file_ref(file.file_ref),
        state,
    )
    return file


# ------------------------------------------------- corridas (E5-T18 corte 2)


def hash_run_scope(run_date: object, currency: object) -> str:
    """Hash corto de `(run_date, currency)` para correlacion en logs (sin PII)."""
    return hashlib.sha256(f"{run_date}|{currency}".encode()).hexdigest()[:16]


def coerce_run_date(value: object) -> date:
    """Normaliza `run_date` a fecha (exige fecha valida, sin hora)."""
    if isinstance(value, datetime):
        raise ReconciliationValidationError("fecha de corrida invalida")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        raise ReconciliationValidationError("fecha de corrida invalida") from None


def coerce_run_currency(value: object) -> str:
    """Codigo ISO-4217 de la corrida (3 letras ASCII, se normaliza a mayusculas)."""
    if not isinstance(value, str):
        raise ReconciliationValidationError("moneda de corrida invalida")
    code = value.strip().upper()
    if not _CURRENCY_RE.match(code) or not code.isascii():
        raise ReconciliationValidationError("moneda de corrida invalida")
    return code


def coerce_run_status(value: object) -> str:
    """Valida `status` de la corrida (`RUNNING`/`COMPLETED`/`FAILED`)."""
    text = str(value or "").strip().upper()
    if text not in RUN_STATUSES:
        raise ReconciliationValidationError("estado de corrida invalido")
    return text


def get_run_by_date_currency(
    session: Session, run_date: date, currency: str
) -> ReconciliationRun | None:
    """Ultima corrida por `(run_date, currency)` o `None` (solo lectura).

    Sin UQ natural (review PR #27, H3): una fecha/moneda puede tener varias
    corridas (lotes tardios); se retorna la mas reciente por
    (`created_at`, `id`) como desempates estables y deterministas: mismos
    datos -> misma respuesta (BAJO-3: sin depender de los `NULL` de
    `finished_at`, cuyo orden varia por motor).
    """
    return session.scalars(
        select(ReconciliationRun)
        .where(
            ReconciliationRun.run_date == run_date,
            ReconciliationRun.currency == currency,
        )
        .order_by(
            ReconciliationRun.created_at.desc(),
            ReconciliationRun.id.desc(),
        )
        .limit(1)
    ).first()


def list_runs_by_date_currency(
    session: Session, run_date: date, currency: str
) -> list[ReconciliationRun]:
    """Todas las corridas de `(run_date, currency)` (antiguas primero)."""
    return list(
        session.scalars(
            select(ReconciliationRun)
            .where(
                ReconciliationRun.run_date == run_date,
                ReconciliationRun.currency == currency,
            )
            .order_by(ReconciliationRun.created_at, ReconciliationRun.id)
        ).all()
    )


def create_run(
    session: Session,
    *,
    run_date: date,
    currency: str,
    executed_by: uuid.UUID | None = None,
    status: str = "RUNNING",
) -> ReconciliationRun:
    """Crea la corrida en `status` (varias por fecha/moneda, H3).

    Hace `flush`, no `commit`. Sin UQ natural: una fecha/moneda admite
    varias corridas (la idempotencia la decide el servicio: sin pendientes
    nuevos se reutiliza la ultima `COMPLETED`; con pendientes se crea una
    nueva). Guarda de BD (MEDIO-2, indice parcial
    `uq_reconciliation_runs_single_running`): una segunda `RUNNING` para la
    misma fecha/moneda falla aqui con `ReconciliationConflictError` (la
    rama `except` del servicio vuelve a ser alcanzable: carrera real entre
    dos corridas concurrentes).
    """
    state = coerce_run_status(status)
    row = ReconciliationRun(
        run_date=run_date,
        currency=currency,
        status=state,
        matched_count=0,
        unmatched_count=0,
        unmatched_item_count=0,
        unmatched_operation_count=0,
        total_minor=0,
        executed_by=executed_by,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        raise ReconciliationConflictError("corrida de conciliacion duplicada") from exc
    logger.debug(
        "reconciliation run creado run_hash=%s status=%s",
        hash_run_scope(run_date, currency),
        state,
    )
    return row


def mark_run_running(session: Session, run: ReconciliationRun) -> ReconciliationRun:
    """Reabre una corrida `FAILED` como `RUNNING` (reintento en la misma fila).

    Resetea contadores y `finished_at`. Solo desde `FAILED`: `UPDATE ...
    WHERE id=:id AND status='FAILED'` y `rowcount == 0` =>
    `ReconciliationConflictError` (el llamante decide: `COMPLETED` se
    reutiliza, `RUNNING` concurrente es conflicto); no se confia en el
    estado en memoria (un objeto stale con `status='FAILED'` no gana si la
    fila ya se cerro: solo un reintento gana). Hace `flush`, no `commit`.
    """
    result = session.execute(
        update(ReconciliationRun)
        .where(ReconciliationRun.id == run.id, ReconciliationRun.status == "FAILED")
        .values(
            status="RUNNING",
            matched_count=0,
            unmatched_count=0,
            unmatched_item_count=0,
            unmatched_operation_count=0,
            total_minor=0,
            finished_at=None,
        )
    )
    if result.rowcount == 0:
        raise ReconciliationConflictError("la corrida no admite reintento en su estado actual")
    run.status = "RUNNING"
    run.matched_count = 0
    run.unmatched_count = 0
    run.unmatched_item_count = 0
    run.unmatched_operation_count = 0
    run.total_minor = 0
    run.finished_at = None
    session.flush()
    return run


def complete_run(
    session: Session,
    run: ReconciliationRun,
    *,
    matched_count: int,
    unmatched_count: int,
    total_minor: int,
    unmatched_item_count: int = 0,
    unmatched_operation_count: int = 0,
) -> ReconciliationRun:
    """Cierra la corrida como `COMPLETED` con sus contadores.

    Solo desde `RUNNING`: `UPDATE ... WHERE id=:id AND status='RUNNING'`
    y `rowcount == 0` => `ReconciliationConflictError`; no se confia en el
    estado en memoria (un objeto stale con `status='RUNNING'` no gana si
    la fila ya se cerro: solo un cierre gana). `matched_count`/
    `unmatched_count`/`unmatched_item_count`/`unmatched_operation_count`
    enteros `>= 0` (`unmatched_count` = TOTAL = suma del desglose;
    H-6 del review PR #27: se exige la igualdad
    `unmatched_count == unmatched_item_count +
    unmatched_operation_count`, `VALIDATION_ERROR` si no cuadra).
    Compatibilidad acotada (F4 del dictamen PR #27): los defaults `0`
    del desglose solo preservan a los llamantes con totales 0 (p. ej.
    `matched=1, unmatched=0` sin desglose sigue cerrando); si
    `unmatched_count > 0` el desglose es obligatorio y debe sumar el
    total (un llamante legacy con `unmatched_count=5` sin desglose
    recibe `VALIDATION_ERROR`, no un cierre silencioso).
    `total_minor` entero con signo
    (M5 del review PR #27: `INBOUND` suma, `OUTBOUND` resta; puede ser
    negativo). Se validan antes de mutar. Hace `flush`, no `commit`.
    """
    for name, value in (
        ("matched_count", matched_count),
        ("unmatched_count", unmatched_count),
        ("unmatched_item_count", unmatched_item_count),
        ("unmatched_operation_count", unmatched_operation_count),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReconciliationValidationError(f"contador de corrida invalido: {name}")
    if isinstance(total_minor, bool) or not isinstance(total_minor, int):
        raise ReconciliationValidationError("contador de corrida invalido: total_minor")
    if unmatched_count != unmatched_item_count + unmatched_operation_count:
        raise ReconciliationValidationError(
            "contador de corrida inconsistente: unmatched_count != desglose"
        )
    finished = datetime.now(UTC)
    result = session.execute(
        update(ReconciliationRun)
        .where(ReconciliationRun.id == run.id, ReconciliationRun.status == "RUNNING")
        .values(
            matched_count=matched_count,
            unmatched_count=unmatched_count,
            unmatched_item_count=unmatched_item_count,
            unmatched_operation_count=unmatched_operation_count,
            total_minor=total_minor,
            status="COMPLETED",
            finished_at=finished,
        )
    )
    if result.rowcount == 0:
        raise ReconciliationConflictError("la corrida no esta en curso")
    run.matched_count = matched_count
    run.unmatched_count = unmatched_count
    run.unmatched_item_count = unmatched_item_count
    run.unmatched_operation_count = unmatched_operation_count
    run.total_minor = total_minor
    run.status = "COMPLETED"
    run.finished_at = finished
    session.flush()
    logger.debug(
        "reconciliation run completado run_hash=%s matched=%d unmatched=%d",
        hash_run_scope(run.run_date, run.currency),
        matched_count,
        unmatched_count,
    )
    return run


def fail_run(session: Session, run: ReconciliationRun) -> ReconciliationRun:
    """Cierra la corrida como `FAILED` (sin parciales; el motivo va al log).

    Solo desde `RUNNING`: `UPDATE ... WHERE id=:id AND status='RUNNING'`
    y `rowcount == 0` => `ReconciliationConflictError`; no se confia en el
    estado en memoria (un objeto stale con `status='RUNNING'` no gana si
    la fila ya se cerro). Hace `flush`, no `commit`.
    """
    finished = datetime.now(UTC)
    result = session.execute(
        update(ReconciliationRun)
        .where(ReconciliationRun.id == run.id, ReconciliationRun.status == "RUNNING")
        .values(status="FAILED", finished_at=finished)
    )
    if result.rowcount == 0:
        raise ReconciliationConflictError("la corrida no esta en curso")
    run.status = "FAILED"
    run.finished_at = finished
    session.flush()
    return run


def reclaim_stale_running(
    session: Session,
    *,
    run_date: date,
    currency: str,
    older_than: datetime,
) -> int:
    """Marca `FAILED` las corridas `RUNNING` huerfanas del alcance (lease).

    MEDIA del review PR #27: una `RUNNING` con `created_at < older_than`
    se considera huerfana (el worker murio sin cerrarla) y se cierra como
    `FAILED` con `finished_at` para que la proxima `run_reconciliation`
    continue con una corrida nueva o un reintento; las `RUNNING` recientes
    se conservan (el llamante las trata como `CONFLICT`). `UPDATE` masivo
    por alcance (`run_date`, `currency`), sin columnas nuevas (usa el
    `created_at` existente). Hace `flush`, no `commit`. Retorna las filas
    recuperadas (`rowcount`).
    """

    day = coerce_run_date(run_date)
    code = coerce_run_currency(currency)
    if isinstance(older_than, bool) or not isinstance(older_than, datetime):
        raise ReconciliationValidationError("corte de antiguedad invalido")
    finished = datetime.now(UTC)
    result = session.execute(
        update(ReconciliationRun)
        .where(
            ReconciliationRun.run_date == day,
            ReconciliationRun.currency == code,
            ReconciliationRun.status == "RUNNING",
            ReconciliationRun.created_at < older_than,
        )
        .values(status="FAILED", finished_at=finished)
    )
    reclaimed = int(result.rowcount or 0)
    if reclaimed:
        session.flush()
        # El `UPDATE` masivo no toca el mapa de identidad: expirar para
        # que la siguiente lectura vea el `FAILED` real (no el `RUNNING`
        # stale en memoria).
        session.expire_all()
        logger.info(
            "reconciliation run huerfana recuperada run_hash=%s recuperadas=%d",
            hash_run_scope(day, code),
            reclaimed,
        )
    return reclaimed


def list_pending_items_for_run(
    session: Session,
    *,
    run_date: date,
    currency: str,
) -> list[ClearingItem]:
    """Items `PENDING` de archivos `PROCESSED` con `value_date` y moneda.

    H1 (review PR #27): el JOIN con `clearing_files` exige
    `status='PROCESSED'` (no basta `ClearingItem.status=PENDING`): los items
    de archivos `FAILED` (o `RECEIVED`) nunca se cruzan. Alcance exacto de
    la corrida: la tolerancia de fecha solo califica pares (dominio), no
    amplia el alcance. Solo lectura; orden estable.
    """
    stmt = (
        select(ClearingItem)
        .join(ClearingFile, ClearingItem.file_id == ClearingFile.id)
        .where(
            ClearingItem.status == "PENDING",
            ClearingItem.value_date == run_date,
            ClearingItem.currency == currency,
            ClearingFile.status == "PROCESSED",
        )
        .order_by(ClearingItem.value_date, ClearingItem.external_ref, ClearingItem.id)
    )
    return list(session.scalars(stmt).all())


def mark_item_status(
    session: Session,
    item: ClearingItem,
    status: object,
    *,
    run_id: uuid.UUID | str | None = None,
) -> ClearingItem:
    """Mueve un item `PENDING` a `MATCHED`/`EXCEPTION` (solo el matching).

    Atomico (MEDIO-2): `UPDATE ... WHERE id=... AND status='PENDING'` y
    `rowcount == 0` => `ReconciliationConflictError`; no se confia en el
    estado en memoria (un objeto stale con `status='PENDING'` no gana si
    la fila ya se concilio: solo un worker gana). Si se indica `run_id`,
    lo registra en el item (que corrida lo proceso; H2 del review PR #27:
    tambien las excepciones registran `run_id`). Hace `flush`, no `commit`.
    """
    text = str(status or "").strip().upper()
    if text not in ("MATCHED", "EXCEPTION"):
        raise ReconciliationValidationError("estado de item invalido")
    if run_id is not None:
        try:
            rid = run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ReconciliationValidationError("corrida de item invalida") from exc
        values: dict[str, Any] = {"status": text, "run_id": rid}
    else:
        values = {"status": text}
    result = session.execute(
        update(ClearingItem)
        .where(ClearingItem.id == item.id, ClearingItem.status == "PENDING")
        .values(**values)
    )
    if result.rowcount == 0:
        raise ReconciliationConflictError("el item ya fue conciliado")
    item.status = text
    if run_id is not None:
        item.run_id = rid
    session.flush()
    return item


# ------------------------------------------------- matches (H2 review PR #27)

#: Tamano de lote del `IN` de usados (H-1 del review PR #27): Postgres
#: limita los parametros por consulta a 65.535 y SQLite a 32.767
#: (`SQLITE_MAX_VARIABLE_NUMBER`); trocear en 5.000 deja margen amplio y
#: evita que una corrida con muchos candidatos explote. Los resultados de
#: cada lote se unen en un solo conjunto.
_USED_IDS_BATCH_SIZE = 5_000


def coerce_match_type(value: object) -> str:
    """Valida el calificador del emparejamiento (`EXACT`/`AMOUNT_DIFF`/`DATE_DIFF`)."""
    text = str(value or "").strip().upper()
    if text not in MATCH_TYPES:
        raise ReconciliationValidationError("tipo de emparejamiento invalido")
    return text


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Distingue UQ duplicada de otro fallo de persistencia (sin PII).

    Solo `uq_reconciliation_matches_item` / `uq_reconciliation_matches_transaction`
    (o el `SQLSTATE 23505` / `UNIQUE constraint failed` del motor) es
    duplicado (`CONFLICT`); cualquier otro `IntegrityError` (FK, CHECK) es
    error de persistencia. Sin eco de valores: solo nombres de constraint.
    """
    orig = getattr(exc, "orig", None)
    if orig is not None:
        diag = getattr(orig, "diag", None)
        if getattr(diag, "constraint_name", None) in (
            "uq_reconciliation_matches_item",
            "uq_reconciliation_matches_transaction",
        ):
            return True
        if getattr(orig, "pgcode", None) == "23505":
            return True
        text = str(orig).lower()
    else:
        text = str(exc).lower()
    if "uq_reconciliation_matches_item" in text or "uq_reconciliation_matches_transaction" in text:
        return True
    return "unique constraint failed" in text or "unique constraint" in text


def create_match(
    session: Session,
    *,
    run_id: uuid.UUID | str,
    clearing_item_id: uuid.UUID | str,
    transaction_id: uuid.UUID | str,
    match_type: object,
    amount_minor: object,
    currency: object,
    amount_diff_minor: object = None,
    date_diff_days: object = None,
) -> ReconciliationMatch:
    """Persiste el emparejamiento item -> operacion interna (H2).

    `amount_diff_minor` (`clearing - internal`, centimos) y `date_diff_days`
    (`clearing_date - internal_date`, dias) conservan la diferencia que
    califico el par (nullable; `EXACT` deja `NULL`): misma convencion que
    el dominio (`matching._assess_pair`). `UQ(clearing_item_id)`: un item
    se concilia como maximo una vez; `UQ(transaction_id)` (MEDIO-1): una
    operacion interna se concilia como maximo una vez en todo el historial
    (doble match/uso -> `ReconciliationConflictError`; la UQ decide).
    Otro `IntegrityError` (FK/CHECK) -> `ReconciliationError` (error de
    persistencia, no conflicto). Hace `flush`, no `commit`.
    """
    try:
        rid = run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ReconciliationValidationError("corrida de emparejamiento invalida") from exc
    try:
        iid = (
            clearing_item_id
            if isinstance(clearing_item_id, uuid.UUID)
            else uuid.UUID(str(clearing_item_id))
        )
    except (ValueError, AttributeError, TypeError) as exc:
        raise ReconciliationValidationError("item de emparejamiento invalido") from exc
    try:
        tid = (
            transaction_id
            if isinstance(transaction_id, uuid.UUID)
            else uuid.UUID(str(transaction_id))
        )
    except (ValueError, AttributeError, TypeError) as exc:
        raise ReconciliationValidationError("operacion de emparejamiento invalida") from exc
    qualifier = coerce_match_type(match_type)
    # `amount_minor` del match admite `0` (CHECK `>= 0`): el item siempre es
    # `> 0`, pero la validacion propia no reutiliza el `> 0` del clearing.
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor < 0:
        raise ReconciliationValidationError("monto de emparejamiento invalido")
    amount = int(amount_minor)
    code = coerce_currency(currency)
    for name, value in (
        ("amount_diff_minor", amount_diff_minor),
        ("date_diff_days", date_diff_days),
    ):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ReconciliationValidationError(f"diferencia de emparejamiento invalida: {name}")
    row = ReconciliationMatch(
        run_id=rid,
        clearing_item_id=iid,
        transaction_id=tid,
        match_type=qualifier,
        amount_minor=amount,
        amount_diff_minor=None if amount_diff_minor is None else int(amount_diff_minor),
        date_diff_days=None if date_diff_days is None else int(date_diff_days),
        currency=code,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        if _is_unique_violation(exc):
            raise ReconciliationConflictError("emparejamiento duplicado") from exc
        raise ReconciliationError("no se pudo persistir el emparejamiento") from exc
    return row


def get_match_by_item(
    session: Session, clearing_item_id: uuid.UUID | str
) -> ReconciliationMatch | None:
    """Emparejamiento del item o `None` (auditoria item -> operacion)."""
    try:
        iid = (
            clearing_item_id
            if isinstance(clearing_item_id, uuid.UUID)
            else uuid.UUID(str(clearing_item_id))
        )
    except (ValueError, AttributeError, TypeError) as exc:
        raise ReconciliationValidationError("item de emparejamiento invalido") from exc
    return session.scalars(
        select(ReconciliationMatch).where(ReconciliationMatch.clearing_item_id == iid).limit(1)
    ).first()


def list_matches_by_run(session: Session, run_id: uuid.UUID | str) -> list[ReconciliationMatch]:
    """Emparejamientos de la corrida (orden estable por item)."""
    try:
        rid = run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ReconciliationValidationError("corrida de emparejamiento invalida") from exc
    return list(
        session.scalars(
            select(ReconciliationMatch)
            .where(ReconciliationMatch.run_id == rid)
            .order_by(ReconciliationMatch.clearing_item_id, ReconciliationMatch.id)
        ).all()
    )


def list_used_transaction_ids(
    session: Session,
    *,
    currency: str | None = None,
    run_date: date | str | None = None,
    date_tolerance_days: int = 0,
    transaction_ids: object | None = None,
) -> set[str]:
    """IDs de operaciones internas ya emparejadas (evita reutilizarlas, H2).

    Con `transaction_ids` (3.1 del comentario PR #27): solo los ya usados
    dentro de ese conjunto de candidatos (`WHERE transaction_id IN (...)`,
    sin ventana de fechas: el dominio puede emparejar a ± tolerancia y una
    ventana por `run_date` reoferta operaciones ya conciliadas en corridas
    vecinas). El `IN` se trocea en lotes de `_USED_IDS_BATCH_SIZE`
    (H-1: Postgres admite 65.535 parametros y SQLite 32.767 por consulta;
    el troceo evita que muchos candidatos exploten) y los resultados se
    unen. Sin `transaction_ids` rige el alcance legacy
    (`currency`/`run_date` ± `date_tolerance_days`, BAJO-2) o, sin alcance,
    todos los emparejados (compatibilidad). Solo lectura.
    """
    if transaction_ids is not None:
        if isinstance(transaction_ids, (str, bytes)):
            raise ReconciliationValidationError("identificadores de operacion invalidos")
        try:
            wanted = list(transaction_ids)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ReconciliationValidationError("identificadores de operacion invalidos") from exc
        tids: list[uuid.UUID] = []
        for raw in wanted:
            try:
                tids.append(raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw)))
            except (ValueError, AttributeError, TypeError) as exc:
                raise ReconciliationValidationError("identificador de operacion invalido") from exc
        if not tids:
            return set()
        found: set[str] = set()
        for start in range(0, len(tids), _USED_IDS_BATCH_SIZE):
            rows = session.scalars(
                select(ReconciliationMatch.transaction_id).where(
                    ReconciliationMatch.transaction_id.in_(
                        tids[start : start + _USED_IDS_BATCH_SIZE]
                    )
                )
            ).all()
            found.update(str(tid) for tid in rows)
        return found
    stmt = select(ReconciliationMatch.transaction_id)
    if currency is not None or run_date is not None:
        stmt = stmt.join(ReconciliationRun, ReconciliationMatch.run_id == ReconciliationRun.id)
        if currency is not None:
            stmt = stmt.where(ReconciliationMatch.currency == coerce_currency(currency))
        if run_date is not None:
            day = coerce_run_date(run_date)
            if (
                isinstance(date_tolerance_days, bool)
                or not isinstance(date_tolerance_days, int)
                or date_tolerance_days < 0
            ):
                raise ReconciliationValidationError("tolerancia de fecha invalida")
            start = day - timedelta(days=date_tolerance_days)
            end = day + timedelta(days=date_tolerance_days)
            stmt = stmt.where(ReconciliationRun.run_date.between(start, end))
    rows = session.scalars(stmt).all()
    return {str(tid) for tid in rows}


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
    "coerce_match_type",
    "coerce_raw",
    "coerce_run_currency",
    "coerce_run_date",
    "coerce_run_status",
    "coerce_source",
    "coerce_value_date",
    "complete_run",
    "create_file_with_items",
    "create_match",
    "create_run",
    "fail_run",
    "get_file_by_ref",
    "get_match_by_item",
    "get_run_by_date_currency",
    "hash_external_ref",
    "hash_file_ref",
    "hash_run_scope",
    "list_matches_by_run",
    "list_pending_items",
    "list_pending_items_for_run",
    "list_runs_by_date_currency",
    "list_used_transaction_ids",
    "mark_file_status",
    "mark_item_status",
    "mark_run_running",
    "reclaim_stale_running",
    "record_failed_file",
]
