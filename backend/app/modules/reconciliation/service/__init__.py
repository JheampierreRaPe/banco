"""Servicio de ingesta de lotes de compensacion (E5-T17, HU19).

`ingest_clearing_file(session, payload) -> {file_id, status, inserted,
duplicates}`: valida `file_ref`, checksum SHA-256, `item_count`,
montos/monedas/fechas; es idempotente por `file_ref` (mismo `file_ref` +
mismo checksum = replay sin duplicar) y deja los items en `PENDING`
para el matching (`E5-T18`). No mueve dinero ni crea asientos; no
publica eventos (si se agrega uno, va por `shared.outbox` en la misma
transaccion, regla de oro 8).

`run_reconciliation(session, *, run_date, currency, executed_by=None)` (E5-T18
+ review PR #27): cruza los items `PENDING` de archivos `PROCESSED` de la
fecha/moneda contra los candidatos internos de la fachada de solo lectura
`transactions.service.list_reconciliation_candidates` con el dominio puro
(min-cost max-flow: cardinalidad maxima + maximo de `EXACT`), persiste un
`reconciliation_matches` por par, actualiza los items (`MATCHED`/`EXCEPTION`,
siempre con `run_id`) y persiste la corrida `reconciliation_runs` con
contadores (`matched_count`, `unmatched_count` = TOTAL de excepciones del
dominio —incluye `MISSING_EXTERNAL` de operaciones internas sin item— con
desglose `unmatched_item_count`/`unmatched_operation_count` por lado,
`total_minor` = suma con signo de los items conciliados). El alcance interno
cubre la ventana de negocio [`run_date` - tol, `run_date` + tol] en la zona
`reconciliation.business_timezone` (M2/M3: la tolerancia de fecha amplia el
alcance y el dominio califica `DATE_DIFF`); el clearing sigue exacto en
`run_date`. `total_minor` es la suma con signo de los items conciliados
(M5: `INBOUND` +, `OUTBOUND` -). Los ya usados se excluyen por conjunto de
candidatos (`WHERE transaction_id IN (...)`, sin ventana de fechas: 3.1 del
comentario PR #27) y la mutacion corre en `SAVEPOINT` (si falla, `FAILED`
sin parciales y sesion usable: 3.2). Idempotencia y corridas tardias:
`COMPLETED` inmutable (sin `PENDING` nuevos se reutiliza sin consultar
candidatos/usados; con `PENDING` nuevos se crea una corrida
nueva); `RUNNING` concurrente es `CONFLICT`; `FAILED` se reintenta en la
misma fila; candidatos `CONCILIATED` se excluyen. Si se supera
una cota de pares evaluables la corrida queda `FAILED` sin parciales
(motivo `CANDIDATE_BUDGET_EXCEEDED`). No mueve dinero ni crea asientos;
auditoria best-effort sin PII.

Contrato de errores (sin eco de valores, sin PII en logs):
- `VALIDATION_ERROR` (`ReconciliationValidationError`): `file_ref`,
  checksum, `item_count` o algun item invalido; el archivo queda
  `FAILED` sin items huerfanos ni escrituras parciales.
- `DUPLICATE_FILE` (replay controlado, no es error): mismo `file_ref` +
  mismo checksum ya ingerido; retorna el estado almacenado sin duplicar.
- Mismo `file_ref` + checksum distinto -> `VALIDATION_ERROR` SIN mutar el
  archivo original (sigue conciliable); el rechazo se registra aparte como
  fila `FAILED` con `file_ref` derivado (evidencia, sin items). Si el
  archivo pasa legitimamente a `FAILED` (ingesta invalida), sus items
  `PENDING` se cierran como `EXCEPTION` (sin `run_id`) para que no queden
  en el limbo.

Transaccion: `flush` sin `commit` (decide el llamante). Ante un fallo
de validacion con archivo nuevo, la fila `FAILED` ya queda en `flush`
para que el llamante la conserve con `commit`; ante un fallo tecnico
el llamante revierte sin parciales (el repositorio valida todo antes
de mutar).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
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


def _rejection_file_ref(code: str, checksum: object) -> str:
    """`file_ref` derivado para registrar un rechazo sin mutar el original.

    Determinista (`file_ref` + checksum entrante): un segundo replay con el
    mismo checksum colisiona en la UQ y se trata como rechazo ya registrado
    (se sigue devolviendo el error, sin duplicar). Truncado a 64
    (`coerce_file_ref`) y solo caracteres del regex de `file_ref`.
    """
    digest = hashlib.sha256(str(checksum).encode("utf-8")).hexdigest()[:8]
    suffix = f"-REJ-{digest}"
    return f"{code[: 64 - len(suffix)]}{suffix}"


def _replay_or_reject(
    session: Session, existing: ClearingFile, *, checksum: object
) -> dict[str, Any]:
    """Replay sin duplicar o rechazo por checksum distinto (sin mutar).

    3.4 del comentario PR #27: un replay con checksum distinto NO muta el
    archivo original valido (seguiria conciliable en la corrida posterior);
    el rechazo se registra aparte como una fila `FAILED` con `file_ref`
    derivado (evidencia, sin items) y se devuelve el error
    (`VALIDATION_ERROR`). Lo mismo ante checksum malformado (ni siquiera
    se puede comparar: se registra el rechazo y se lanza).
    """
    try:
        digest = recon_repo.coerce_checksum(checksum)
    except recon_repo.ReconciliationValidationError:
        _record_rejection(session, existing, checksum=checksum)
        raise
    if existing.checksum != digest:
        logger.warning(
            "clearing ingest checksum distinto file_hash=%s",
            recon_repo.hash_file_ref(existing.file_ref),
        )
        _record_rejection(session, existing, checksum=digest)
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


def _record_rejection(session: Session, existing: ClearingFile, *, checksum: object) -> None:
    """Registra el rechazo aparte (fila `FAILED` con `file_ref` derivado).

    Best-effort (H-5 del review PR #27): si el rechazo ya estaba registrado
    (UQ del derivado) no se duplica, y si el registro falla por cualquier
    otra causa (p. ej. un fallo tecnico) se registra y se continua: el
    error original de validacion/rechazo siempre se propaga intacto, nunca
    se enmascara. Nunca muta `existing` (el original conserva su estado y
    sus items).
    """
    try:
        recon_repo.record_failed_file(
            session,
            source=existing.source,
            file_ref=_rejection_file_ref(existing.file_ref, checksum),
            checksum=checksum,
            item_count=0,
            storage_ref=None,
        )
    except recon_repo.ReconciliationConflictError:
        logger.debug(
            "clearing ingest rechazo ya registrado file_hash=%s",
            recon_repo.hash_file_ref(existing.file_ref),
        )
    except Exception as exc:  # noqa: BLE001 - best-effort: no enmascara el error original
        logger.warning(
            "clearing ingest rechazo no registrado err=%s",
            type(exc).__name__,
        )


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

#: Lease de una corrida `RUNNING` en minutos (MEDIA del review PR #27):
#: una `RUNNING` con `created_at` anterior a `ahora - lease` se considera
#: huerfana (el worker murio sin cerrarla) y `run_reconciliation` la
#: reclama a `FAILED` (`reclaim_stale_running`, sin columnas nuevas) para
#: continuar con una corrida nueva o un reintento; una `RUNNING` reciente
#: sigue siendo `CONFLICT`. Sin semilla en migraciones: ausente/ilegible
#: rige el default.
RECONCILIATION_RUN_LEASE_KEY = "reconciliation.run_lease_minutes"

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

#: Default del lease de `RUNNING` (30 minutos) y techo defensivo (24 h:
#: un valor absurdo se recorta, nunca se rechaza la corrida por un
#: parametro alto; un valor no positivo cae al default documentado).
DEFAULT_RUN_LEASE_MINUTES = 30
MAX_RUN_LEASE_MINUTES = 24 * 60

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


def _read_run_lease_minutes(session: Session) -> int:
    """Lease de `RUNNING` en minutos desde `config.parameters` (nunca lanza).

    Valores ausentes/ilegibles/no positivos -> default documentado
    (`DEFAULT_RUN_LEASE_MINUTES`); por encima del techo defensivo ->
    techo (`MAX_RUN_LEASE_MINUTES`).
    """
    lease = get_int_parameter(session, RECONCILIATION_RUN_LEASE_KEY, DEFAULT_RUN_LEASE_MINUTES)
    if lease <= 0:
        return DEFAULT_RUN_LEASE_MINUTES
    return min(lease, MAX_RUN_LEASE_MINUTES)


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
            "unmatched_item_count": run.unmatched_item_count,
            "unmatched_operation_count": run.unmatched_operation_count,
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
    `transactions.service.list_reconciliation_candidates` con la ventana de
    negocio [`run_date` - tol, `run_date` + tol] (M2/M3 del review PR #27:
    la fachada deriva el dia de negocio en la zona
    `reconciliation.business_timezone` desde `settled_at`/`created_at`; la
    tolerancia de fecha `reconciliation.date_tolerance_days` amplia el
    alcance interno y el dominio califica `DATE_DIFF`; montos positivos del
    motor, incluidos `REVERSAL`: el dominio exige mismo signo y ambos lados
    son positivos, sin adaptacion de signo) y los items `PENDING` de
    archivos `PROCESSED` con `value_date == run_date` (alcance de clearing
    exacto); ejecuta el dominio puro (min-cost max-flow: cardinalidad maxima
    + maximo de `EXACT`; exige misma `direction` en ambos lados, M5);
    persiste un `reconciliation_matches` por par (auditoria item ->
    `transaction_id`, con `amount_diff_minor`/`date_diff_days` para
    `AMOUNT_DIFF`/`DATE_DIFF`) y cierra la corrida `COMPLETED` con contadores
    (`matched_count`, `unmatched_count` = TOTAL de excepciones del dominio
    —incluye `MISSING_EXTERNAL` de operaciones internas sin item— con
    desglose `unmatched_item_count`/`unmatched_operation_count`,
    `total_minor` = suma con signo de los items conciliados: `INBOUND` +,
    `OUTBOUND` -, M5). La fase de mutacion (matches + marcas + cierre)
    corre en un `SAVEPOINT`: si falla a mitad, la corrida queda `FAILED`
    sin parciales y la sesion externa sigue usable (3.2 del comentario
    PR #27).

    Idempotencia y corridas tardias (H2/H3 del review PR #27): las
    `COMPLETED` son inmutables y nunca se reabren. Si la ultima corrida del
    alcance esta `COMPLETED` y no hay `PENDING` nuevos (de archivos
    `PROCESSED`), se devuelve sin re-cruzar NI consultar candidatos/usados
    (menor 7 del comentario PR #27: el replay no hace trabajo
    innecesario); si hay `PENDING` nuevos (lote tardio), se crea una
    corrida NUEVA que procesa solo esos items (varias corridas por
    fecha/moneda). `RUNNING` reciente ->
    `ReconciliationConflictError` (`409`); las `RUNNING` huerfanas (lease
    `reconciliation.run_lease_minutes`, default 30 minutos: `created_at`
    anterior a `ahora - lease`) se reclaman primero a `FAILED`
    (`reclaim_stale_running`, sin columnas nuevas) y se continua con una
    corrida nueva o un reintento. `FAILED` se reintenta en la misma
    fila. Doble conciliacion: los candidatos ya usados en
    `reconciliation_matches` (solo ese conjunto: `WHERE transaction_id IN
    (...)` troceado en lotes de 5.000 —H-1 del review PR #27: Postgres
    admite 65.535 parametros y SQLite 32.767 por consulta—, sin ventana de
    fechas —3.1 del comentario PR #27—) o con estado
    `CONCILIATED` se excluyen antes de cruzar. La asignacion de excepciones
    a sobrantes es codiciosa y determinista (se recorre por (`external_ref`,
    `item_id`) y cada item toma su mejor candidato libre por `_pair_key`):
    puede no ser la explicacion optima global, solo la del dominio; el
    servicio ademas filtra por dia/moneda (BAJO PR #27 B4). `executed_by`
    lo pasa el llamante autenticado (E5-T21 debe pasarlo desde el principal
    del JWT, nunca desde el payload/body: el servicio jamas lo lee de los
    datos de entrada; BAJO PR #27 B2). `None` = corrida del sistema.
    Cota superada
    (`CandidateBudgetExceeded`) -> corrida `FAILED` sin parciales (el
    cruce es puro y ocurre antes de mutar; motivo
    `CANDIDATE_BUDGET_EXCEEDED` en log/auditoria, sin `external_ref`).
    No mueve dinero ni crea asientos. `flush` sin `commit`. NO esta cableada
    a job ni endpoint (correcto para el corte 2; E5-T21 la expondra — BAJO
    PR #27 B7: hoy no cambia produccion aun).
    """
    day = recon_repo.coerce_run_date(run_date)
    code = recon_repo.coerce_run_currency(currency)
    actor = _coerce_executed_by(executed_by)

    # MEDIA del review PR #27 (lease de `RUNNING`): antes de decidir si
    # hay corrida en curso se reclaman las huerfanas del alcance
    # (`created_at` anterior a `ahora - lease` -> `FAILED`); si se
    # recupero alguna se continua con una corrida nueva o un reintento.
    # Solo una `RUNNING` reciente sigue siendo `CONFLICT`.
    lease_minutes = _read_run_lease_minutes(session)
    cutoff = datetime.now(UTC) - timedelta(minutes=lease_minutes)
    recon_repo.reclaim_stale_running(session, run_date=day, currency=code, older_than=cutoff)

    existing = recon_repo.get_run_by_date_currency(session, day, code)
    if existing is not None and existing.status == "RUNNING":
        raise recon_repo.ReconciliationConflictError(
            "corrida de conciliacion ya en curso para la fecha y moneda"
        )

    # Menor 7 del comentario PR #27: el early-return de `COMPLETED` sin
    # pendientes va ANTES de consultar candidatos/usados (el replay no hace
    # trabajo innecesario: ni la fachada ni el `IN` de usados se tocan).
    items = recon_repo.list_pending_items_for_run(session, run_date=day, currency=code)

    # H3: `COMPLETED` inmutable; sin pendientes nuevos se reutiliza.
    if existing is not None and existing.status == "COMPLETED" and not items:
        return existing
    retry_in_place = existing is not None and existing.status == "FAILED"

    tolerances = _read_tolerances(session)
    per_group, per_run = _read_budgets(session)

    from app.modules.transactions.service import (
        list_reconciliation_candidates as _candidates,
    )

    raw_candidates = _candidates(
        session, day, code, date_tolerance_days=tolerances.date_tolerance_days
    )
    # 3.1 del comentario PR #27: los ya usados se consultan SOLO para este
    # conjunto de candidatos (`WHERE transaction_id IN (...)`, sin ventana
    # de fechas). El dominio puede emparejar operaciones a ± tolerancia y
    # acotar por ventana de `run_date` reoferta operaciones ya conciliadas
    # en corridas vecinas (D-1/D/D+1): con el `IN` se excluyen siempre y
    # nunca rompen la corrida vecina (se excluyen o quedan como excepcion,
    # nunca `ReconciliationConflictError`).
    used_transaction_ids = recon_repo.list_used_transaction_ids(
        session,
        transaction_ids=[str(row.get("transaction_id")) for row in raw_candidates],
    )
    candidates = [
        row
        for row in raw_candidates
        if str(row.get("transaction_id")) not in used_transaction_ids
        and str(row.get("status")) != "CONCILIATED"
    ]

    internal = [
        InternalOperation(
            op_id=str(row["transaction_id"]),
            external_ref=str(row["external_ref"]),
            amount_minor=int(row["amount_minor"]),
            currency=str(row["currency"]),
            value_date=row["value_date"],
            direction=str(row.get("direction") or "OUTBOUND"),
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
            direction=item.direction,
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
        # La carrera con otra corrida concurrente se resuelve igual que la
        # rama feliz: `create_run` convierte la guarda de BD en `CONFLICT`
        # y aqui se devuelve el `CONFLICT` coherente (no un `FAILED`).
        if retry_in_place:
            if existing is None:  # sin `assert`: error explicito en produccion
                raise recon_repo.ReconciliationConflictError(
                    "corrida de conciliacion ya en curso para la fecha y moneda"
                )
            run = recon_repo.mark_run_running(session, existing)
            if actor is not None:
                run.executed_by = actor
            recon_repo.fail_run(session, run)
        else:
            try:
                run = recon_repo.create_run(session, run_date=day, currency=code, executed_by=actor)
                recon_repo.fail_run(session, run)
            except recon_repo.ReconciliationConflictError:
                # Carrera real: otro proceso inserto una corrida `RUNNING`
                # para la misma fecha/moneda y el indice unico parcial
                # `uq_reconciliation_runs_single_running` lo rechazo;
                # re-leer y decidir (mismo criterio que la rama feliz).
                raced = recon_repo.get_run_by_date_currency(session, day, code)
                if raced is not None and raced.status == "COMPLETED" and not items:
                    return raced
                raise recon_repo.ReconciliationConflictError(
                    "corrida de conciliacion ya en curso para la fecha y moneda"
                ) from None
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

    if retry_in_place:
        if existing is None:  # sin `assert`: error explicito en produccion
            raise recon_repo.ReconciliationConflictError(
                "corrida de conciliacion ya en curso para la fecha y moneda"
            )
        run = recon_repo.mark_run_running(session, existing)
        if actor is not None:
            run.executed_by = actor
    else:
        if existing is not None and existing.status == "RUNNING":
            raise recon_repo.ReconciliationConflictError(
                "corrida de conciliacion ya en curso para la fecha y moneda"
            )
        try:
            run = recon_repo.create_run(session, run_date=day, currency=code, executed_by=actor)
        except recon_repo.ReconciliationConflictError:
            # Carrera real (MEDIO-2, BAJO-1): otro proceso inserto una
            # corrida `RUNNING` para la misma fecha/moneda y el indice
            # unico parcial `uq_reconciliation_runs_single_running` lo
            # rechazo; re-leer y decidir.
            raced = recon_repo.get_run_by_date_currency(session, day, code)
            if raced is not None and raced.status == "COMPLETED" and not items:
                return raced
            raise recon_repo.ReconciliationConflictError(
                "corrida de conciliacion ya en curso para la fecha y moneda"
            ) from None

    items_by_id = {str(item.id): item for item in items}
    internal_by_id = {op.op_id: op for op in internal}
    matched_item_ids = {m.clearing_id for m in result.matches}
    # 3.2 del comentario PR #27: la fase de mutacion (matches + marcas +
    # cierre) corre en un `SAVEPOINT`. Si algo falla a mitad (p. ej. un
    # `CONFLICT` de la UQ en carrera), el savepoint revierte los parciales,
    # se expira el mapa de identidad (los objetos en memoria dirian
    # `MATCHED` pero la BD dice `PENDING`), la corrida queda `FAILED` y se
    # relanza el error original: el llamante conserva una sesion usable
    # (puede hacer `commit` y la corrida queda `FAILED`, sin parciales).
    # Nota de motores (H-4 del review PR #27): en Postgres el error aborta
    # la transaccion y el rollback del savepoint la recupera; en SQLite el
    # rollback revierte los parciales y la sesion sigue usable.
    try:
        with session.begin_nested():
            for match in result.matches:
                item = items_by_id[match.clearing_id]
                op = internal_by_id[match.internal_id]
                amount_diff = item.amount_minor - op.amount_minor
                date_diff = (item.value_date - op.value_date).days
                recon_repo.create_match(
                    session,
                    run_id=run.id,
                    clearing_item_id=item.id,
                    transaction_id=match.internal_id,
                    match_type=match.qualifier,
                    amount_minor=item.amount_minor,
                    currency=item.currency,
                    # 10 del comentario PR #27: diferencias persistidas
                    # (solo `AMOUNT_DIFF`/`DATE_DIFF`; `EXACT` deja `NULL`).
                    amount_diff_minor=amount_diff if amount_diff != 0 else None,
                    date_diff_days=date_diff if date_diff != 0 else None,
                )
                recon_repo.mark_item_status(session, item, "MATCHED", run_id=run.id)
            for exc in result.exceptions:
                if exc.clearing_id is not None and exc.clearing_id in items_by_id:
                    recon_repo.mark_item_status(
                        session, items_by_id[exc.clearing_id], "EXCEPTION", run_id=run.id
                    )
            # 3.3 del comentario PR #27: desglose por lado. `unmatched_count`
            # se mantiene como el TOTAL (suma del desglose) por
            # compatibilidad (`E5-T18` CA-01).
            unmatched_item_count = sum(
                1 for exc in result.exceptions if exc.clearing_id is not None
            )
            unmatched_operation_count = sum(
                1 for exc in result.exceptions if exc.clearing_id is None
            )
            unmatched_count = unmatched_item_count + unmatched_operation_count
            # M5: `total_minor` con signo (documentado en `docs/03b#12.2`):
            # cada item conciliado suma `+amount` si es `INBOUND` y `-amount`
            # si es `OUTBOUND`; puede ser negativo (sin CHECK `>= 0`).
            total_minor = sum(
                item.amount_minor if item.direction == "INBOUND" else -item.amount_minor
                for item in (items_by_id[mid] for mid in matched_item_ids)
            )
            recon_repo.complete_run(
                session,
                run,
                matched_count=len(result.matches),
                unmatched_count=unmatched_count,
                unmatched_item_count=unmatched_item_count,
                unmatched_operation_count=unmatched_operation_count,
                total_minor=total_minor,
            )
    except Exception as exc:
        session.expire_all()
        try:
            recon_repo.fail_run(session, run)
        except Exception:  # noqa: BLE001 - el error original manda; solo se registra
            logger.warning(
                "reconciliation run no pudo marcarse FAILED run_hash=%s",
                recon_repo.hash_run_scope(day, code),
            )
        _audit_run_best_effort(
            session,
            run=run,
            action="reconciliation.run.failed",
            reason=type(exc).__name__,
        )
        raise
    logger.info(
        "reconciliation run completada run_hash=%s matched=%d unmatched=%d",
        recon_repo.hash_run_scope(day, code),
        len(result.matches),
        unmatched_count,
    )
    _audit_run_best_effort(session, run=run, action="reconciliation.run.completed")
    return run
