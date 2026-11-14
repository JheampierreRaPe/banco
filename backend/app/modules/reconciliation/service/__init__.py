"""Servicio de ingesta de lotes de compensacion (E5-T17, HU19).

`ingest_clearing_file(session, payload) -> {file_id, status, inserted,
duplicates}`: valida `file_ref`, checksum SHA-256, `item_count`,
montos/monedas/fechas; es idempotente por `file_ref` (mismo `file_ref` +
mismo checksum = replay sin duplicar) y deja los items en `PENDING`
para el matching (`E5-T18`). No mueve dinero ni crea asientos; no
publica eventos (si se agrega uno, va por `shared.outbox` en la misma
transaccion, regla de oro 8).

`run_reconciliation(session, *, run_date, currency, executed_by=None)` (E5-T18
corte 2): cruza los items `PENDING` de la fecha/moneda contra los candidatos
internos de la fachada de solo lectura
`transactions.service.list_reconciliation_candidates` con el dominio puro
(min-cost max-flow: cardinalidad maxima + maximo de `EXACT`), actualiza los
items (`MATCHED`/`EXCEPTION`) y persiste la corrida `reconciliation_runs`
con contadores. Idempotente por `(run_date, currency)` (`COMPLETED` se
reutiliza; `RUNNING` concurrente es `CONFLICT`; `FAILED` se reintenta en la
misma fila). Si se supera una cota de pares evaluables la corrida queda
`FAILED` sin parciales (motivo `CANDIDATE_BUDGET_EXCEEDED`). No mueve
dinero ni crea asientos; auditoria best-effort sin PII.

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
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.reconciliation import repository as recon_repo
from app.modules.reconciliation.domain.matching import (
    MAX_CANDIDATE_PAIRS,
    CandidateBudgetExceeded,
    ClearingEntry,
    InternalOperation,
    Tolerances,
    match_items,
)
from app.modules.reconciliation.models import ClearingFile, ClearingItem, ReconciliationRun
from app.modules.shared.config import get_int_parameter

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
    "run_reconciliation",
]


#: Claves de `config.parameters` de la conciliacion (regla de oro 6).
RECONCILIATION_AMOUNT_TOLERANCE_KEY = "reconciliation.amount_tolerance_minor"
RECONCILIATION_DATE_TOLERANCE_KEY = "reconciliation.date_tolerance_days"
RECONCILIATION_MAX_PAIRS_KEY = "reconciliation.max_candidate_pairs"
RECONCILIATION_MAX_TOTAL_PAIRS_KEY = "reconciliation.max_candidate_pairs_per_run"

#: Techos sensatos antes de cruzar (nota (a) del brief): una tolerancia
#: mayor es error de configuracion y se recorta (nunca se rechaza la
#: corrida por un parametro alto: rige el techo y se registra).
MAX_AMOUNT_TOLERANCE_MINOR = 1_000_000  # S/ 10 000 en centimos
MAX_DATE_TOLERANCE_DAYS = 31
#: Techo defensivo de las cotas (evita un cero/absurdo que falle todo o
#: agote memoria; un valor no positivo cae al default documentado).
MAX_MAX_CANDIDATE_PAIRS = 5_000_000

#: Default del presupuesto total de pares evaluables por corrida cuando
#: `config.parameters` no esta sembrado (4x la cota por grupo).
DEFAULT_MAX_TOTAL_PAIRS = 1_000_000

#: Motivo de negocio cuando una cota de pares evaluables aborta la corrida
#: (decision del dueno (b): corrida `FAILED` + rollback, sin parciales).
CANDIDATE_BUDGET_EXCEEDED = "CANDIDATE_BUDGET_EXCEEDED"


def _read_tolerances(session: Session) -> Tolerances:
    """Tolerancias desde `config.parameters` con techos (nunca lanza).

    Valores ausentes/ilegibles -> defaults (`0`/`0`); valores sobre el
    techo -> techo (se registra sin PII: solo la clave y el techo).
    """
    amount = get_int_parameter(session, RECONCILIATION_AMOUNT_TOLERANCE_KEY, 0)
    if amount > MAX_AMOUNT_TOLERANCE_MINOR:
        logger.debug(
            "reconciliation tolerance recortada key=%s techo=%d",
            RECONCILIATION_AMOUNT_TOLERANCE_KEY,
            MAX_AMOUNT_TOLERANCE_MINOR,
        )
        amount = MAX_AMOUNT_TOLERANCE_MINOR
    days = get_int_parameter(session, RECONCILIATION_DATE_TOLERANCE_KEY, 0)
    if days > MAX_DATE_TOLERANCE_DAYS:
        logger.debug(
            "reconciliation tolerance recortada key=%s techo=%d",
            RECONCILIATION_DATE_TOLERANCE_KEY,
            MAX_DATE_TOLERANCE_DAYS,
        )
        days = MAX_DATE_TOLERANCE_DAYS
    return Tolerances(amount_tolerance_minor=amount, date_tolerance_days=days)


def _read_budgets(session: Session) -> tuple[int, int]:
    """Cotas `(por_grupo, por_corrida)` desde `config.parameters` (nunca lanza).

    Valores ausentes/ilegibles/no positivos -> defaults por entorno
    (`MAX_CANDIDATE_PAIRS` del dominio y `DEFAULT_MAX_TOTAL_PAIRS`); por
    encima del techo defensivo -> techo.
    """
    per_group = get_int_parameter(session, RECONCILIATION_MAX_PAIRS_KEY, MAX_CANDIDATE_PAIRS)
    if per_group <= 0:
        per_group = MAX_CANDIDATE_PAIRS
    per_group = min(per_group, MAX_MAX_CANDIDATE_PAIRS)
    per_run = get_int_parameter(
        session, RECONCILIATION_MAX_TOTAL_PAIRS_KEY, DEFAULT_MAX_TOTAL_PAIRS
    )
    if per_run <= 0:
        per_run = DEFAULT_MAX_TOTAL_PAIRS
    per_run = min(per_run, MAX_MAX_CANDIDATE_PAIRS)
    return per_group, per_run


def _coerce_executed_by(value: uuid.UUID | str | None) -> uuid.UUID | None:
    """Normaliza `executed_by` a UUID (`None` = corrida del sistema)."""
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise recon_repo.ReconciliationValidationError("ejecutor de corrida invalido") from exc


def _audit_run_best_effort(
    session: Session,
    *,
    run: ReconciliationRun,
    action: str,
    reason: str | None = None,
) -> None:
    """Auditoria de la corrida (best-effort, sin PII: solo conteos)."""
    try:
        from app.modules.audit.service import record as audit_record

        metadata: dict[str, Any] = {
            "matched_count": run.matched_count,
            "unmatched_count": run.unmatched_count,
            "total_minor": run.total_minor,
        }
        if reason is not None:
            metadata["reason"] = reason
        audit_record(
            session,
            actor=run.executed_by,
            action=action,
            entity="reconciliation_run",
            entity_id=run.id,
            metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001 - la auditoria no bloquea la corrida
        logger.warning("reconciliation audit best-effort omitida err=%s", type(exc).__name__)


def run_reconciliation(
    session: Session,
    *,
    run_date: object,
    currency: object,
    executed_by: uuid.UUID | str | None = None,
) -> ReconciliationRun:
    """Cruza los `PENDING` de `(run_date, currency)` y persiste la corrida.

    Lee candidatos internos por la fachada de solo lectura
    `transactions.service.list_reconciliation_candidates` (montos positivos
    del motor, incluidos `REVERSAL`: el dominio exige mismo signo y ambos
    lados son positivos, sin adaptacion de signo) y los items `PENDING`
    con `value_date == run_date`; ejecuta el dominio puro (min-cost
    max-flow: cardinalidad maxima + maximo de `EXACT`); actualiza los items
    (`MATCHED`/`EXCEPTION`) y cierra la corrida `COMPLETED` con contadores
    (`matched_count`, `unmatched_count` = items no conciliados,
    `total_minor` = suma de los items conciliados).

    Idempotencia: `COMPLETED` existente se devuelve sin re-cruzar;
    `RUNNING` concurrente -> `ReconciliationConflictError` (`409`);
    `FAILED` se reintenta en la misma fila. Cota superada
    (`CandidateBudgetExceeded`) -> corrida `FAILED` sin parciales (el
    cruce es puro y ocurre antes de mutar; motivo
    `CANDIDATE_BUDGET_EXCEEDED` en log/auditoria, sin `external_ref`).
    No mueve dinero ni crea asientos. `flush` sin `commit`.
    """
    day = recon_repo.coerce_run_date(run_date)
    code = recon_repo.coerce_run_currency(currency)
    actor = _coerce_executed_by(executed_by)

    existing = recon_repo.get_run_by_date_currency(session, day, code)
    if existing is not None:
        if existing.status == "COMPLETED":
            return existing
        if existing.status == "RUNNING":
            raise recon_repo.ReconciliationConflictError(
                "corrida de conciliacion ya en curso para la fecha y moneda"
            )

    tolerances = _read_tolerances(session)
    per_group, per_run = _read_budgets(session)

    from app.modules.transactions.service import (
        list_reconciliation_candidates as _candidates,
    )

    candidates = _candidates(session, day, code)
    items = recon_repo.list_pending_items_for_run(session, run_date=day, currency=code)

    internal = [
        InternalOperation(
            op_id=str(row["transaction_id"]),
            external_ref=str(row["external_ref"]),
            amount_minor=int(row["amount_minor"]),
            currency=str(row["currency"]),
            value_date=row["value_date"],
        )
        for row in candidates
    ]
    clearing = [
        ClearingEntry(
            item_id=str(item.id),
            external_ref=item.external_ref,
            amount_minor=item.amount_minor,
            currency=item.currency,
            value_date=item.value_date,
        )
        for item in items
    ]
    try:
        result = match_items(
            internal,
            clearing,
            tolerances,
            max_candidate_pairs=per_group,
            max_total_pairs=per_run,
        )
    except CandidateBudgetExceeded:
        # Sin parciales: el cruce es puro y ocurre antes de mutar, asi que
        # solo persiste la fila `FAILED` (nueva o reintentada en su fila).
        if existing is not None:
            run = recon_repo.mark_run_running(session, existing)
            if actor is not None:
                run.executed_by = actor
            recon_repo.fail_run(session, run)
        else:
            run = recon_repo.create_run(session, run_date=day, currency=code, executed_by=actor)
            recon_repo.fail_run(session, run)
        logger.warning(
            "reconciliation run fallida run_hash=%s motivo=%s matched=0 unmatched=0",
            recon_repo.hash_run_scope(day, code),
            CANDIDATE_BUDGET_EXCEEDED,
        )
        _audit_run_best_effort(
            session,
            run=run,
            action="reconciliation.run.failed",
            reason=CANDIDATE_BUDGET_EXCEEDED,
        )
        return run

    if existing is not None:
        run = recon_repo.mark_run_running(session, existing)
        if actor is not None:
            run.executed_by = actor
    else:
        try:
            run = recon_repo.create_run(session, run_date=day, currency=code, executed_by=actor)
        except recon_repo.ReconciliationConflictError:
            # Carrera: otro proceso creo la corrida; re-leer y decidir.
            raced = recon_repo.get_run_by_date_currency(session, day, code)
            if raced is not None and raced.status == "COMPLETED":
                return raced
            raise recon_repo.ReconciliationConflictError(
                "corrida de conciliacion ya en curso para la fecha y moneda"
            ) from None

    items_by_id = {str(item.id): item for item in items}
    matched_item_ids = {m.clearing_id for m in result.matches}
    matched_amounts = {str(item.id): item.amount_minor for item in items}
    for match in result.matches:
        recon_repo.mark_item_status(session, items_by_id[match.clearing_id], "MATCHED")
    unmatched_count = 0
    for exc in result.exceptions:
        if exc.clearing_id is not None and exc.clearing_id in items_by_id:
            recon_repo.mark_item_status(session, items_by_id[exc.clearing_id], "EXCEPTION")
            unmatched_count += 1
    total_minor = sum(matched_amounts[mid] for mid in matched_item_ids)
    recon_repo.complete_run(
        session,
        run,
        matched_count=len(result.matches),
        unmatched_count=unmatched_count,
        total_minor=total_minor,
    )
    logger.info(
        "reconciliation run completada run_hash=%s matched=%d unmatched=%d",
        recon_repo.hash_run_scope(day, code),
        len(result.matches),
        unmatched_count,
    )
    _audit_run_best_effort(session, run=run, action="reconciliation.run.completed")
    return run
