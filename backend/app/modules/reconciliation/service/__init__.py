"""Servicio de ingesta de lotes de compensacion (E5-T17, HU19).

`ingest_clearing_file(session, payload) -> {file_id, status, inserted,
duplicates}`: valida `file_ref`, checksum SHA-256, `item_count`,
montos/monedas/fechas; es idempotente por `file_ref` (mismo `file_ref` +
mismo checksum = replay sin duplicar) y deja los items en `PENDING`
para el matching (`E5-T18`). No mueve dinero ni crea asientos; no
publica eventos (si se agrega uno, va por `shared.outbox` en la misma
transaccion, regla de oro 8).

Contrato de errores (sin eco de valores, sin PII en logs):
- `VALIDATION_ERROR` (`ReconciliationValidationError`): `file_ref`,
  checksum, `item_count` o algun item invalido; el archivo queda
  `FAILED` sin items huerfanos ni escrituras parciales.
- `DUPLICATE_FILE` (replay controlado, no es error): mismo `file_ref` +
  mismo checksum ya ingerido; retorna el estado almacenado sin duplicar.
- Mismo `file_ref` + checksum distinto -> `VALIDATION_ERROR` y el
  archivo existente queda `FAILED` (no se pisan items).

Transaccion: `flush` sin `commit` (decide el llamante). Ante un fallo
de validacion con archivo nuevo, la fila `FAILED` ya queda en `flush`
para que el llamante la conserve con `commit`; ante un fallo tecnico
el llamante revierte sin parciales (el repositorio valida todo antes
de mutar).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.reconciliation import repository as recon_repo
from app.modules.reconciliation.models import ClearingFile, ClearingItem

logger = logging.getLogger(__name__)

#: Codigo de negocio de la ingesta invalida (`422` en el futuro `E5-T21`).
RECONCILIATION_VALIDATION_ERROR = "VALIDATION_ERROR"

#: Replay controlado (mismo `file_ref` + mismo checksum; no es error).
RECONCILIATION_DUPLICATE_FILE = "DUPLICATE_FILE"


def ingest_clearing_file(session: Session, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Ingiere un lote de compensacion (idempotente por `file_ref`).

    `payload`: `{source?, file_ref, checksum, item_count, storage_ref?,
    items: [{external_ref, amount_minor, currency, value_date,
    direction, raw?}]}` (espejo del adaptador `E5-T16`).

    Retorna `{"file_id", "status", "inserted", "duplicates"}`:
    - lote nuevo valido -> `PROCESSED`, `inserted = N`, `duplicates = 0`;
    - replay (mismo checksum) -> estado almacenado, `inserted = 0`,
      `duplicates = <items ya guardados>`.
    """
    if not isinstance(payload, Mapping):
        raise recon_repo.ReconciliationValidationError("lote de clearing invalido")
    code = recon_repo.coerce_file_ref(payload.get("file_ref"))
    existing = recon_repo.get_file_by_ref(session, code)
    if existing is not None:
        return _replay_or_reject(
            session,
            existing,
            checksum=payload.get("checksum"),
        )
    return _ingest_new(
        session,
        source=payload.get("source"),
        file_ref=code,
        checksum=payload.get("checksum"),
        item_count=payload.get("item_count"),
        storage_ref=payload.get("storage_ref"),
        items=payload.get("items"),
    )


def _replay_or_reject(
    session: Session, existing: ClearingFile, *, checksum: object
) -> dict[str, Any]:
    """Replay sin duplicar o rechazo por checksum distinto."""
    try:
        digest = recon_repo.coerce_checksum(checksum)
    except recon_repo.ReconciliationValidationError:
        recon_repo.mark_file_status(session, existing, "FAILED")
        raise
    if existing.checksum != digest:
        recon_repo.mark_file_status(session, existing, "FAILED")
        logger.warning(
            "clearing ingest checksum distinto file_hash=%s",
            recon_repo.hash_file_ref(existing.file_ref),
        )
        raise recon_repo.ReconciliationValidationError("checksum del lote inconsistente")
    stored = session.scalars(
        select(func.count()).select_from(ClearingItem).where(ClearingItem.file_id == existing.id)
    ).one()
    logger.info(
        "clearing ingest replay file_hash=%s status=%s duplicates=%d",
        recon_repo.hash_file_ref(existing.file_ref),
        existing.status,
        stored,
    )
    return {
        "file_id": str(existing.id),
        "status": existing.status,
        "inserted": 0,
        "duplicates": int(stored),
        "code": RECONCILIATION_DUPLICATE_FILE,
    }


def _ingest_new(
    session: Session,
    *,
    source: object | None,
    file_ref: str,
    checksum: object,
    item_count: object,
    storage_ref: object | None,
    items: object,
) -> dict[str, Any]:
    """Crea el archivo + items `PENDING` y lo marca `PROCESSED`.

    Cualquier `VALIDATION_ERROR` (checksum/`item_count`/items) deja el
    archivo `FAILED` (sin items) como evidencia antes de propagarse.
    """
    try:
        digest = recon_repo.coerce_checksum(checksum)
        count = recon_repo.coerce_item_count(item_count)
        if not isinstance(items, list):
            raise recon_repo.ReconciliationValidationError("items de clearing invalidos")
        row = recon_repo.create_file_with_items(
            session,
            source=source,
            file_ref=file_ref,
            checksum=digest,
            item_count=count,
            items=items,
            storage_ref=storage_ref,
            status="RECEIVED",
        )
    except (
        recon_repo.ReconciliationValidationError,
        recon_repo.ReconciliationConflictError,
    ):
        # `item_count` inconsistente, item invalido o carrera con otra
        # ingesta del mismo `file_ref`: se conserva el archivo `FAILED`
        # (sin items) como evidencia... salvo que la carrera ya la gano
        # otra ingesta (UQ), en cuyo caso se resuelve como
        # replay/rechazo contra la fila existente.
        try:
            recon_repo.record_failed_file(
                session,
                source=source,
                file_ref=file_ref,
                checksum=checksum,
                item_count=item_count,
                storage_ref=storage_ref,
            )
        except recon_repo.ReconciliationConflictError:
            existing = recon_repo.get_file_by_ref(session, file_ref)
            if existing is not None:
                return _replay_or_reject(session, existing, checksum=checksum)
        raise
    recon_repo.mark_file_status(session, row, "PROCESSED")
    logger.info(
        "clearing ingest ok file_hash=%s inserted=%d",
        recon_repo.hash_file_ref(file_ref),
        len(items),
    )
    return {
        "file_id": str(row.id),
        "status": row.status,
        "inserted": len(items),
        "duplicates": 0,
    }


__all__ = [
    "RECONCILIATION_DUPLICATE_FILE",
    "RECONCILIATION_VALIDATION_ERROR",
    "ingest_clearing_file",
]
