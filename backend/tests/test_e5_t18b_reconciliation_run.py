"""Corrida de conciliacion (E5-T18 corte 2, HU19).

Cubre (`docs/tasks/E5-T18.md#pruebas-requeridas`, parte servicio):

- Dominio min-cost max-flow: el ejemplo del revisor (ops [100,100,300] vs
  items [100,400,300], tol 100 -> 2 EXACT + AMOUNT_DIFF), optimalidad
  (cardinalidad maxima + maximo de EXACT) contra fuerza bruta en
  instancias aleatorias pequenas, determinismo bajo permutacion y
  presupuesto total de corrida (`max_total_pairs`).
- Servicio `run_reconciliation`: corrida completa (contadores, items
  `MATCHED`/`EXCEPTION`), tolerancias y cotas desde `config.parameters`
  (con defaults y techos), idempotencia (`COMPLETED` reutilizada,
  `RUNNING` -> `CONFLICT`, `FAILED` reintentable en su fila), cota por
  grupo y por corrida -> `FAILED` sin parciales (rollback: los items
  siguen `PENDING`), alcance exacto por fecha/moneda, reversos
  (`type='REVERSAL'`, monto positivo) y filas sin referencia omitidas.
- Fachada `transactions.service.list_reconciliation_candidates`: dicts
  minimos, solo lectura (sin `flush`), filtros de moneda/estado/fecha y
  errores `TypeError`/`ValueError` (mapeables a `VALIDATION_ERROR`).
- Frontera: `reconciliation` no importa modelos/repositorios de
  `transactions` (solo la fachada), ni `ledger`/`accounts`; dominio sin
  `transactions` ni logs; sin PII en logs (ni `external_ref` en errores).
- No mueve dinero: ninguna fila de `transactions` se crea o modifica.

SQLite en memoria con schemas `reconciliation`/`transactions`/`config`
(ATTACH); `config.parameters` se crea a mano (la fachada la lee
best-effort con fallback). Sin Postgres: corre en local y en CI.
"""

from __future__ import annotations

import itertools
import random
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.modules.reconciliation.models as _recon_models
from app.core.db import Base
from app.modules.reconciliation import repository as recon_repo
from app.modules.reconciliation import service as recon_service
from app.modules.reconciliation.domain.matching import (
    AMOUNT_DIFF,
    CandidateBudgetExceeded,
    ClearingEntry,
    InternalOperation,
    Tolerances,
    match_items,
)
from app.modules.reconciliation.models import ReconciliationRun
from app.modules.transactions.models import Transaction

assert _recon_models is not None

BACKEND_DIR = Path(__file__).resolve().parents[1]
RUN_TABLE = "reconciliation.reconciliation_runs"
MIGRATION_0051 = BACKEND_DIR / "migrations" / "versions" / "0051_reconciliation_runs.py"

D = date(2026, 11, 14)


@pytest.fixture()
def db():
    """SQLite en memoria con `reconciliation` + `transactions` + `config`."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("reconciliation", "transactions", "config"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["reconciliation.clearing_files"],
            Base.metadata.tables["reconciliation.clearing_items"],
            Base.metadata.tables[RUN_TABLE],
            Base.metadata.tables["reconciliation.reconciliation_matches"],
            Base.metadata.tables["transactions.transactions"],
            Base.metadata.tables["transactions.transaction_status_history"],
            Base.metadata.tables["transactions.holds"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    session.execute(
        sa.text(
            "CREATE TABLE config.parameters "
            "(key TEXT PRIMARY KEY, value_json TEXT, module TEXT, description TEXT)"
        )
    )
    try:
        yield session
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def _set_param(db: Session, key: str, value: str) -> None:
    db.execute(
        sa.text(
            "INSERT INTO config.parameters (key, value_json, module) "
            "VALUES (:key, :value, 'reconciliation') "
            "ON CONFLICT (key) DO UPDATE SET value_json = :value"
        ),
        {"key": key, "value": value},
    )
    db.flush()


def _op(op_id="op-1", ref="REF-1", amount=100_00, currency="PEN", value_date=D):
    return InternalOperation(
        op_id=op_id,
        external_ref=ref,
        amount_minor=amount,
        currency=currency,
        value_date=value_date,
    )


def _item(item_id="it-1", ref="REF-1", amount=100_00, currency="PEN", value_date=D):
    return ClearingEntry(
        item_id=item_id,
        external_ref=ref,
        amount_minor=amount,
        currency=currency,
        value_date=value_date,
    )


def _tx(
    db: Session,
    *,
    ref,
    amount,
    currency="PEN",
    status="SETTLED",
    day=D,
    ttype="THIRD_PARTY_TRANSFER",
    settled=True,
    meta=None,
):
    row = Transaction(
        type=ttype,
        status=status,
        amount_minor=amount,
        currency=currency,
        external_ref=ref,
        meta=meta,
        created_at=datetime(day.year, day.month, day.day, 12, 0, 0),
        settled_at=(datetime(day.year, day.month, day.day, 18, 0, 0) if settled else None),
    )
    db.add(row)
    db.flush()
    return row


def _ingest(db: Session, entries, *, seed=None, direction="OUTBOUND"):
    """Crea un archivo de clearing con los items dados (todos `PENDING`).

    Marca el archivo `PROCESSED` como hace `ingest_clearing_file` en
    produccion: solo los `PROCESSED` entran al cruce (H1 del review PR #27).
    `direction` por defecto `OUTBOUND` (cruza contra transferencias del
    motor, que la fachada deriva a `OUTBOUND`; M5 exige misma direccion).
    """
    seed = seed or f"t{uuid.uuid4().hex[:8]}"
    items = [
        {
            "external_ref": ref,
            "amount_minor": amount,
            "currency": currency,
            "value_date": day,
            "direction": direction,
        }
        for ref, amount, currency, day in entries
    ]
    row = recon_repo.create_file_with_items(
        db,
        source="NETWORK",
        file_ref=f"FILE-{seed}",
        checksum="ab" * 32,
        item_count=len(items),
        items=items,
    )
    recon_repo.mark_file_status(db, row, "PROCESSED")
    return row


# ------------------------------------------------- dominio: min-cost max-flow


def test_min_cost_prefers_two_exact_reviewer_example():
    """Ejemplo del revisor: 2 EXACT + AMOUNT_DIFF, no 1 exacto + 1 tolerado.

    ops [100,100,300] vs items [100,400,300], tol 100. Los dos maximos de
    cardinalidad 2 con 2 EXACT son simetricos en op-0/op-1 (mismo monto);
    se afirma la garantia (2 EXACT sobre it-0/it-2 + AMOUNT_DIFF del
    huerfano it-1 con diff 300), no el desempate entre simetricos.
    """
    tol = Tolerances(amount_tolerance_minor=100, date_tolerance_days=0)
    result = match_items(
        [_op(op_id=f"op-{i}", amount=a) for i, a in enumerate([100, 100, 300])],
        [_item(item_id=f"it-{i}", amount=a) for i, a in enumerate([100, 400, 300])],
        tol,
    )
    assert result.matched_count == 2
    assert {m.qualifier for m in result.matches} == {"EXACT"}
    assert {m.clearing_id for m in result.matches} == {"it-0", "it-2"}
    assert {m.internal_id for m in result.matches} <= {"op-0", "op-1", "op-2"}
    (exc,) = result.exceptions
    assert exc.reason == AMOUNT_DIFF
    assert exc.clearing_id == "it-1"
    assert exc.amount_diff_minor == 300


def _brute_force_optimum(ops, items, tol):
    """Mejor (cardinalidad, exactos) por enumeracion exhaustiva."""
    from app.modules.reconciliation.domain.matching import Match, _classify

    edges = {}
    for op in ops:
        for entry in items:
            outcome = _classify(op, entry, tol)
            if isinstance(outcome, Match):
                edges[(op.op_id, entry.item_id)] = 1 if outcome.qualifier == "EXACT" else 0
    best = (-1, -1)
    edge_list = list(edges)
    for size in range(len(edge_list) + 1):
        for combo in itertools.combinations(edge_list, size):
            used_ops, used_items = set(), set()
            for op_id, item_id in combo:
                if op_id in used_ops or item_id in used_items:
                    break
                used_ops.add(op_id)
                used_items.add(item_id)
            else:
                candidate = (len(combo), sum(edges[p] for p in combo))
                best = max(best, candidate)
    return best


def test_min_cost_matches_brute_force_optimum():
    """Propiedad: (cardinalidad, EXACT) igual al optimo exhaustivo."""
    rng = random.Random(20261114)
    for trial in range(150):
        n, m = rng.randint(1, 3), rng.randint(1, 3)
        ops = [
            _op(
                op_id=f"o{trial}-{i}",
                amount=rng.randint(1, 5),
                currency=rng.choice(["PEN", "PEN", "USD"]),
                value_date=D + timedelta(days=rng.randint(0, 1)),
            )
            for i in range(n)
        ]
        items = [
            _item(
                item_id=f"e{trial}-{i}",
                amount=rng.randint(1, 5),
                currency=rng.choice(["PEN", "PEN", "USD"]),
                value_date=D + timedelta(days=rng.randint(0, 1)),
            )
            for i in range(m)
        ]
        tol = Tolerances(
            amount_tolerance_minor=rng.randint(0, 2),
            date_tolerance_days=rng.randint(0, 1),
        )
        got = match_items(ops, items, tol)
        exact = sum(1 for match in got.matches if match.qualifier == "EXACT")
        assert (got.matched_count, exact) == _brute_force_optimum(ops, items, tol)


def test_min_cost_deterministic_under_permutation():
    """Misma entrada en distinto orden -> mismo resultado."""
    rng = random.Random(7)
    ops = [_op(op_id=f"op-{i:02d}", amount=100_00 + (i * 37) % 200) for i in range(12)]
    items = [_item(item_id=f"it-{i:02d}", amount=100_00 + (i * 53) % 200) for i in range(12)]
    tol = Tolerances(amount_tolerance_minor=50, date_tolerance_days=0)
    first = match_items(ops, items, tol)
    for _ in range(5):
        shuffled_ops = list(ops)
        shuffled_items = list(items)
        rng.shuffle(shuffled_ops)
        rng.shuffle(shuffled_items)
        other = match_items(shuffled_ops, shuffled_items, tol)
        assert [(m.internal_id, m.clearing_id, m.qualifier) for m in other.matches] == [
            (m.internal_id, m.clearing_id, m.qualifier) for m in first.matches
        ]
        assert [(e.reason, e.internal_id, e.clearing_id) for e in other.exceptions] == [
            (e.reason, e.internal_id, e.clearing_id) for e in first.exceptions
        ]


def test_max_total_pairs_budget_counts_all_groups():
    """`max_total_pairs` acota la suma de pares de todos los grupos."""
    tol = Tolerances(amount_tolerance_minor=10**9, date_tolerance_days=10**4)
    ops = [_op(op_id=f"op-{i}", ref=f"REF-{i}", amount=100_00) for i in range(4)]
    items = [_item(item_id=f"it-{i}", ref=f"REF-{i}", amount=100_00) for i in range(4)]
    ok_result = match_items(ops, items, tol, max_total_pairs=4)
    assert ok_result.matched_count == 4
    with pytest.raises(CandidateBudgetExceeded, match="presupuesto total"):
        match_items(ops, items, tol, max_total_pairs=3)


def test_max_total_pairs_error_hides_refs_and_is_value_error():
    """La cota por corrida no expone refs y sigue siendo `ValueError`."""
    assert issubclass(CandidateBudgetExceeded, ValueError)
    tol = Tolerances(amount_tolerance_minor=10**9, date_tolerance_days=10**4)
    ref = "REF-SENSIBLE-999"
    ops = [_op(op_id=f"op-{i}", ref=ref, amount=100_00) for i in range(4)]
    items = [_item(item_id=f"it-{i}", ref=ref, amount=100_00) for i in range(4)]
    with pytest.raises(CandidateBudgetExceeded) as excinfo:
        match_items(ops, items, tol, max_total_pairs=3)
    message = str(excinfo.value)
    assert "cota de seguridad" in message
    assert ref not in message
    assert "external_ref" not in message
    with pytest.raises(TypeError, match="max_total_pairs"):
        match_items(ops, items, tol, max_total_pairs=True)
    with pytest.raises(ValueError, match="max_total_pairs"):
        match_items(ops, items, tol, max_total_pairs=-1)


def test_dense_group_completes_within_budget():
    """120x120 denso termina rapido (el 500x500 se mide fuera del suite)."""
    rng = random.Random(21)
    ops = [_op(op_id=f"op-{i:03d}", ref="DENSE", amount=rng.randint(1, 50000)) for i in range(120)]
    items = [
        _item(item_id=f"it-{i:03d}", ref="DENSE", amount=rng.randint(1, 50000)) for i in range(120)
    ]
    tol = Tolerances(amount_tolerance_minor=10**9, date_tolerance_days=10**4)
    start = time.perf_counter()
    result = match_items(ops, items, tol, max_candidate_pairs=10**9)
    assert time.perf_counter() - start < 30.0
    assert result.matched_count == 120
    assert result.exception_count == 0


# ------------------------------------------------- servicio: corrida completa


def test_run_completes_exact_batch(db: Session):
    """Lote exacto -> `COMPLETED`, `matched = N`, items `MATCHED`."""
    _tx(db, ref="R-1", amount=100_00)
    _tx(db, ref="R-2", amount=200_00)
    _tx(db, ref="R-3", amount=300_00)
    _ingest(
        db,
        [("R-1", 100_00, "PEN", D), ("R-2", 200_00, "PEN", D), ("R-3", 300_00, "PEN", D)],
    )
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert isinstance(run, ReconciliationRun)
    assert run.status == "COMPLETED"
    assert run.matched_count == 3
    assert run.unmatched_count == 0
    # M5: `total_minor` con signo (items `OUTBOUND` restan).
    assert run.total_minor == -600_00
    assert run.finished_at is not None
    items = recon_repo.list_pending_items_for_run(db, run_date=D, currency="PEN")
    assert items == []
    db.commit()
    assert (
        db.scalars(
            sa.select(sa.func.count())
            .select_from(recon_repo.ClearingItem)
            .where(recon_repo.ClearingItem.status == "MATCHED")
        ).one()
        == 3
    )


def test_run_amount_tolerance_from_config(db: Session):
    """La tolerancia de `config.parameters` decide entre match y excepcion."""
    _tx(db, ref="R-T", amount=100_00)
    _ingest(db, [("R-T", 101_00, "PEN", D)], seed="tol")
    first = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert first.matched_count == 0
    assert first.unmatched_count == 1
    db.rollback()

    _set_param(db, "reconciliation.amount_tolerance_minor", "100")
    day2 = D + timedelta(days=1)
    _tx(db, ref="R-U", amount=100_00, day=day2)
    _ingest(db, [("R-U", 101_00, "PEN", day2)], seed="tol2")
    second = recon_service.run_reconciliation(db, run_date=day2, currency="PEN")
    assert second.matched_count == 1
    assert second.unmatched_count == 0
    assert second.total_minor == -101_00


def test_run_tolerance_ceiling_clamps_instead_of_failing(db: Session):
    """Tolerancia absurda en config -> techo, no error (nota (a) del brief)."""
    _set_param(db, "reconciliation.amount_tolerance_minor", "999999999")
    assert recon_service._read_tolerances(db).amount_tolerance_minor == (
        recon_service.MAX_AMOUNT_TOLERANCE_MINOR
    )
    _set_param(db, "reconciliation.date_tolerance_days", "9999")
    assert recon_service._read_tolerances(db).date_tolerance_days == (
        recon_service.MAX_DATE_TOLERANCE_DAYS
    )


def test_run_idempotent_completed_reuses_row(db: Session):
    """Reejecutar `COMPLETED` no duplica ni re-cruza."""
    _tx(db, ref="R-I", amount=100_00)
    _ingest(db, [("R-I", 100_00, "PEN", D)], seed="idem")
    first = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    second = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert second.id == first.id
    assert second.status == "COMPLETED"
    assert (second.matched_count, second.unmatched_count) == (1, 0)
    assert db.scalars(sa.select(sa.func.count()).select_from(ReconciliationRun)).one() == 1


def test_run_running_concurrent_is_conflict(db: Session):
    """`RUNNING` existente -> `ReconciliationConflictError` (409)."""
    recon_repo.create_run(db, run_date=D, currency="PEN")
    with pytest.raises(recon_repo.ReconciliationConflictError):
        recon_service.run_reconciliation(db, run_date=D, currency="PEN")


def test_run_group_budget_exceeded_fails_without_partials(db: Session, caplog):
    """Cota por grupo -> `FAILED`, items siguen `PENDING` (rollback)."""
    _set_param(db, "reconciliation.max_candidate_pairs", "2")
    secret = "REF-SECRETA-001"
    for i in range(3):
        _tx(db, ref=secret, amount=100_00 + i)
    _ingest(db, [(secret, 100_00 + i, "PEN", D) for i in range(3)], seed="bnd")
    with caplog.at_level("WARNING", logger="app.modules.reconciliation.service"):
        run = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert run.status == "FAILED"
    assert (run.matched_count, run.unmatched_count, run.total_minor) == (0, 0, 0)
    pending = recon_repo.list_pending_items_for_run(db, run_date=D, currency="PEN")
    assert len(pending) == 3
    assert all(item.status == "PENDING" for item in pending)
    assert recon_service.CANDIDATE_BUDGET_EXCEEDED in caplog.text
    assert secret not in caplog.text


def test_run_total_budget_exceeded_fails_without_partials(db: Session):
    """Cota por corrida -> `FAILED` aunque cada grupo pase su cota."""
    _set_param(db, "reconciliation.max_candidate_pairs_per_run", "1")
    for i in range(2):
        _tx(db, ref=f"REF-T-{i}", amount=100_00)
    _ingest(
        db,
        [(f"REF-T-{i}", 100_00, "PEN", D) for i in range(2)],
        seed="bndt",
    )
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert run.status == "FAILED"
    pending = recon_repo.list_pending_items_for_run(db, run_date=D, currency="PEN")
    assert len(pending) == 2
    assert all(item.status == "PENDING" for item in pending)


def test_run_failed_is_retryable_in_place(db: Session):
    """`FAILED` se reintenta en la misma fila tras ajustar la cota."""
    _set_param(db, "reconciliation.max_candidate_pairs", "1")
    _tx(db, ref="REF-RT", amount=100_00)
    _tx(db, ref="REF-RT", amount=100_00)
    _ingest(db, [("REF-RT", 100_00, "PEN", D), ("REF-RT", 100_00, "PEN", D)], seed="rty")
    failed = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert failed.status == "FAILED"
    _set_param(db, "reconciliation.max_candidate_pairs", "250000")
    retried = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert retried.id == failed.id
    assert retried.status == "COMPLETED"
    assert retried.matched_count == 2


def test_run_scope_is_exact_date_and_currency(db: Session):
    """Solo cruza `PENDING` con `value_date == run_date` y la moneda."""
    _tx(db, ref="R-S", amount=100_00, day=D)
    _tx(db, ref="R-S", amount=100_00, day=D + timedelta(days=1))
    _tx(db, ref="R-S", amount=100_00, day=D, currency="USD")
    _ingest(
        db,
        [
            ("R-S", 100_00, "PEN", D),
            ("R-S", 100_00, "PEN", D + timedelta(days=1)),
            ("R-S", 100_00, "USD", D),
        ],
        seed="scope",
    )
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert (run.matched_count, run.unmatched_count) == (1, 0)
    left = recon_repo.list_pending_items(db, limit=10)
    assert len(left) == 2


def test_run_skips_empty_ref_and_rejected_and_includes_reversal(db: Session):
    """Sin ref -> fuera (nota (b)); `REJECTED` fuera; `REVERSAL` cruza."""
    _tx(db, ref=None, amount=100_00)
    _tx(db, ref="R-X", amount=100_00, status="REJECTED")
    _tx(db, ref="R-V", amount=100_00, ttype="REVERSAL")
    _ingest(db, [("R-V", 100_00, "PEN", D)], seed="rev")
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert (run.matched_count, run.unmatched_count) == (1, 0)
    # M5: el `REVERSAL` del motor deriva a `OUTBOUND` (default) y el item es
    # `OUTBOUND`: cruzan, y el total resta.
    assert run.total_minor == -100_00


def test_run_does_not_move_money(db: Session):
    """La corrida no crea ni modifica operaciones del motor."""
    _tx(db, ref="R-M", amount=100_00)
    _tx(db, ref="R-N", amount=200_00, status="REJECTED")
    _ingest(db, [("R-M", 100_00, "PEN", D), ("R-N", 200_00, "PEN", D)], seed="nomoney")
    before = {
        row.id: (row.status, row.amount_minor, row.currency)
        for row in db.scalars(sa.select(Transaction)).all()
    }
    count_before = len(before)
    recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    after = {
        row.id: (row.status, row.amount_minor, row.currency)
        for row in db.scalars(sa.select(Transaction)).all()
    }
    assert after == before
    assert len(after) == count_before


def test_run_validation_errors(db: Session):
    """Fecha/moneda/ejecutor invalidos -> `VALIDATION_ERROR`."""
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_service.run_reconciliation(db, run_date="no-fecha", currency="PEN")
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_service.run_reconciliation(db, run_date=datetime(2026, 11, 14, 12, 0), currency="PEN")
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_service.run_reconciliation(db, run_date=D, currency="XX")
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_service.run_reconciliation(db, run_date=D, currency="PEN", executed_by="x")


def test_run_stores_executed_by(db: Session):
    """`executed_by` se persiste (UUID o texto UUID)."""
    actor = uuid.uuid4()
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN", executed_by=actor)
    assert run.executed_by == actor
    assert run.status == "COMPLETED"


def test_run_repository_transitions(db: Session):
    """Repositorio de corridas: crear, completar, fallar, reintentar (H3).

    Sin UQ natural (review PR #27, H3): una fecha/moneda admite varias
    corridas (lotes tardios); la idempotencia la decide el servicio. Con
    guarda de BD (MEDIO-2, indice parcial
    `uq_reconciliation_runs_single_running`): dos `RUNNING` simultaneas
    para la misma fecha/moneda -> `ReconciliationConflictError`; el lote
    tardio se crea tras cerrar la anterior.
    """
    run = recon_repo.create_run(db, run_date=D, currency="PEN")
    assert run.status == "RUNNING"
    # Relojes gruesos (Windows) pueden empatar `created_at`: se fuerza un
    # instante anterior para que "ultima" sea determinista.
    run.created_at = run.created_at - timedelta(days=1)
    db.flush()
    assert recon_repo.get_run_by_date_currency(db, D, "PEN").id == run.id
    # Segunda `RUNNING` simultanea: conflicto por la guarda de BD (MEDIO-2).
    with pytest.raises(recon_repo.ReconciliationConflictError):
        recon_repo.create_run(db, run_date=D, currency="PEN")
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_repo.complete_run(db, run, matched_count=-1, unmatched_count=0, total_minor=0)
    recon_repo.complete_run(
        db, run, matched_count=2, unmatched_count=1, unmatched_item_count=1, total_minor=300
    )
    assert run.status == "COMPLETED"
    assert run.finished_at is not None
    assert (run.matched_count, run.unmatched_count, run.total_minor) == (2, 1, 300)
    # Lote tardio (H3): tras `COMPLETED` si se admite otra corrida.
    run_late = recon_repo.create_run(db, run_date=D, currency="PEN")
    assert run_late.id != run.id
    assert recon_repo.get_run_by_date_currency(db, D, "PEN").id == run_late.id
    assert [r.id for r in recon_repo.list_runs_by_date_currency(db, D, "PEN")] == [
        run.id,
        run_late.id,
    ]
    with pytest.raises(recon_repo.ReconciliationConflictError):
        recon_repo.mark_run_running(db, run)
    run2 = recon_repo.create_run(db, run_date=D + timedelta(days=1), currency="PEN")
    recon_repo.fail_run(db, run2)
    assert run2.status == "FAILED"
    assert run2.finished_at is not None
    reopened = recon_repo.mark_run_running(db, run2)
    assert reopened.status == "RUNNING"
    assert (reopened.matched_count, reopened.unmatched_count) == (0, 0)


# ------------------------------------------------- fachada de transactions


def test_facade_returns_minimal_dicts_sorted(db: Session):
    """Dicts minimos ordenados por (`external_ref`, `transaction_id`)."""
    from app.modules.transactions.service import list_reconciliation_candidates

    _tx(db, ref="R-B", amount=200_00, day=D)
    _tx(db, ref="R-A", amount=100_00, day=D)
    rows = list_reconciliation_candidates(db, D, "PEN")
    assert [row["external_ref"] for row in rows] == ["R-A", "R-B"]
    assert set(rows[0]) == {
        "transaction_id",
        "external_ref",
        "amount_minor",
        "currency",
        "value_date",
        "type",
        "status",
        "direction",
    }
    assert rows[0]["amount_minor"] == 100_00
    assert rows[0]["value_date"] == D
    assert rows[0]["type"] == "THIRD_PARTY_TRANSFER"
    assert rows[0]["status"] == "SETTLED"
    # M5: las transferencias derivan a `OUTBOUND` (default documentado).
    assert rows[0]["direction"] == "OUTBOUND"


def test_facade_derives_inbound_direction_for_deposits(db: Session):
    """`TOPUP`/`LOAN_DISBURSEMENT`/`SALARY_DISTRIBUTION` -> `INBOUND` (M5)."""
    from app.modules.transactions.service import list_reconciliation_candidates

    _tx(db, ref="R-IN-T", amount=100_00, day=D, ttype="TOPUP")
    _tx(db, ref="R-IN-L", amount=100_00, day=D, ttype="LOAN_DISBURSEMENT")
    _tx(db, ref="R-IN-S", amount=100_00, day=D, ttype="SALARY_DISTRIBUTION")
    _tx(db, ref="R-OUT-Q", amount=100_00, day=D, ttype="QR_PAYMENT")
    by_ref = {
        row["external_ref"]: row["direction"]
        for row in list_reconciliation_candidates(db, D, "PEN")
    }
    assert by_ref == {
        "R-IN-L": "INBOUND",
        "R-IN-S": "INBOUND",
        "R-IN-T": "INBOUND",
        "R-OUT-Q": "OUTBOUND",
    }


def test_facade_filters_status_currency_and_date(db: Session):
    """Solo conciliables de la moneda y dia de negocio (M2/M3).

    Con tolerancia 0 el alcance es el dia exacto; `settled_at` manda sobre
    `created_at` (si aun no liquida, `created_at`); el dia es de negocio en
    Lima, no UTC (M3).
    """
    from app.modules.transactions.service import list_reconciliation_candidates

    _tx(db, ref="R-OK", amount=100_00, day=D)
    _tx(db, ref="R-DAY", amount=100_00, day=D + timedelta(days=1))
    _tx(db, ref="R-USD", amount=100_00, day=D, currency="USD")
    _tx(db, ref="R-REJ", amount=100_00, day=D, status="REJECTED")
    _tx(db, ref="R-INIT", amount=100_00, day=D, status="INITIATED")
    rows = list_reconciliation_candidates(db, D, "PEN")
    assert [row["external_ref"] for row in rows] == ["R-OK"]
    assert list_reconciliation_candidates(db, D, "pen") != []
    created_only = _tx(db, ref="R-POST", amount=50_00, day=D, settled=False)
    assert created_only.settled_at is None
    refs = [row["external_ref"] for row in list_reconciliation_candidates(db, D, "PEN")]
    assert "R-POST" in refs


def test_facade_window_expands_with_date_tolerance(db: Session):
    """M2: `date_tolerance_days` amplia el alcance interno ([D-tol, D+tol]).

    Con tol 0 la operacion liquidada el dia siguiente no es candidata; con
    tol 1 si (y el dominio la califica `DATE_DIFF`).
    """
    from app.modules.transactions.service import list_reconciliation_candidates

    _tx(db, ref="R-W", amount=100_00, day=D + timedelta(days=1))
    assert list_reconciliation_candidates(db, D, "PEN") == []
    assert list_reconciliation_candidates(db, D, "PEN", date_tolerance_days=0) == []
    rows = list_reconciliation_candidates(db, D, "PEN", date_tolerance_days=1)
    assert [row["external_ref"] for row in rows] == ["R-W"]
    assert rows[0]["value_date"] == D + timedelta(days=1)
    with pytest.raises(TypeError):
        list_reconciliation_candidates(db, D, "PEN", date_tolerance_days=True)
    with pytest.raises(ValueError):
        list_reconciliation_candidates(db, D, "PEN", date_tolerance_days=-1)


def test_facade_business_day_is_lima_not_utc(db: Session):
    """M3: dia de negocio `America/Lima` (liquidada 23:30 Lima = candidata del 14).

    Operacion liquidada el 2026-11-14 23:30 Lima (2026-11-15 04:30 UTC):
    es candidata de la corrida del 14 y NO de la del 15 (con `date()` UTC
    seria al reves).
    """
    from app.modules.transactions.service import list_reconciliation_candidates

    row = _tx(db, ref="R-LIMA", amount=100_00, day=D)
    # 2026-11-14 23:30 en Lima = 2026-11-15 04:30 UTC (ingenuo = UTC).
    row.settled_at = datetime(2026, 11, 15, 4, 30, 0)
    db.flush()
    assert [r["external_ref"] for r in list_reconciliation_candidates(db, D, "PEN")] == ["R-LIMA"]
    assert list_reconciliation_candidates(db, D + timedelta(days=1), "PEN") == []


def test_facade_business_timezone_explicit_and_invalid(db: Session):
    """M3: `business_timezone` explicita manda; invalida -> `ValueError`.

    Con `UTC` la operacion de las 04:30 UTC es del dia 15 (no del 14).
    """
    from app.modules.transactions.service import list_reconciliation_candidates

    row = _tx(db, ref="R-TZ", amount=100_00, day=D)
    row.settled_at = datetime(2026, 11, 15, 4, 30, 0)
    db.flush()
    assert list_reconciliation_candidates(db, D, "PEN", business_timezone="UTC") == []
    assert [
        r["external_ref"]
        for r in list_reconciliation_candidates(
            db, D + timedelta(days=1), "PEN", business_timezone="UTC"
        )
    ] == ["R-TZ"]
    with pytest.raises(ValueError):
        list_reconciliation_candidates(db, D, "PEN", business_timezone="No/Existe")


def test_facade_falls_back_to_metadata_reference(db: Session):
    """Sin `external_ref` usa `metadata.reference`; sin ambas, se omite."""
    from app.modules.transactions.service import list_reconciliation_candidates

    _tx(db, ref=None, amount=100_00, day=D, meta={"reference": "META-1"})
    _tx(db, ref=None, amount=100_00, day=D, meta={})
    _tx(db, ref="   ", amount=100_00, day=D)
    refs = [row["external_ref"] for row in list_reconciliation_candidates(db, D, "PEN")]
    assert refs == ["META-1"]


def test_facade_validates_inputs(db: Session):
    """Fecha/moneda invalidas -> `TypeError`/`ValueError` (VALIDATION_ERROR)."""
    from app.modules.transactions.service import list_reconciliation_candidates

    with pytest.raises(TypeError):
        list_reconciliation_candidates(db, "2026-11-14", "PEN")
    with pytest.raises(TypeError):
        list_reconciliation_candidates(db, datetime(2026, 11, 14), "PEN")
    with pytest.raises(TypeError):
        list_reconciliation_candidates(db, D, 123)
    with pytest.raises(ValueError):
        list_reconciliation_candidates(db, D, "XX")


def test_facade_is_read_only(db: Session):
    """La fachada no deja pendientes (`new`) ni muta filas (B5: con flush real).

    BAJO PR #27 B5: con `autoflush=True` un `add`/`flush` accidental se
    haria visible (o fallaria el `assert` de `new`/`dirty`); ademas se
    detecta cualquier `flush` via listener para que un `flush` silencioso
    haga fallar la prueba.
    """
    from app.modules.transactions.service import list_reconciliation_candidates

    flushed: list[str] = []

    def _on_flush(session, _ctx) -> None:  # pragma: no cover - solo guarda bandera
        flushed.append("flush")

    db.autoflush = True
    event.listen(db, "after_flush", _on_flush)
    try:
        before = [(row.id, row.status) for row in db.scalars(sa.select(Transaction)).all()]
        flushes_before = len(flushed)
        list_reconciliation_candidates(db, D, "PEN")
        assert len(flushed) == flushes_before
        assert len(db.new) == 0
        assert len(db.dirty) == 0
        after = [(row.id, row.status) for row in db.scalars(sa.select(Transaction)).all()]
        assert after == before
    finally:
        event.remove(db, "after_flush", _on_flush)
        db.autoflush = False


# ------------------------------------------------- fronteras, migracion, PII


def test_reconciliation_boundary_allows_only_facade():
    """Sin modelos/repositorios de `transactions` (CA-05); solo la fachada."""
    for rel in (
        "app/modules/reconciliation/domain/matching.py",
        "app/modules/reconciliation/repository/__init__.py",
        "app/modules/reconciliation/models/__init__.py",
    ):
        text = (BACKEND_DIR / rel).read_text(encoding="utf-8")
        import_lines = [
            line
            for line in text.splitlines()
            if line.strip().startswith(("import ", "from ")) and "transactions" in line
        ]
        assert import_lines == [], f"frontera rota en {rel}: {import_lines}"
    service_text = (BACKEND_DIR / "app/modules/reconciliation/service/__init__.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "app.modules.transactions.models",
        "app.modules.transactions.repository",
        "app.modules.ledger",
        "app.modules.accounts",
        "app.modules.risk",
        "app.modules.fx",
    ):
        assert forbidden not in service_text, f"frontera rota: {forbidden}"
    assert "float(" not in (
        BACKEND_DIR / "app/modules/reconciliation/domain/matching.py"
    ).read_text(encoding="utf-8")


def test_migration_0051_chains_0050_with_runs_table_and_seeds():
    """`0051` encadena `0050`: runs + `run_id` + `reconciliation_matches`."""
    text = MIGRATION_0051.read_text(encoding="utf-8")
    assert 'revision = "0051_reconciliation_runs"' in text
    assert 'down_revision = "0050_reconciliation"' in text
    for token in (
        "reconciliation_runs",
        "reconciliation_matches",
        "uq_reconciliation_matches_item",
        "uq_reconciliation_matches_transaction",
        "uq_reconciliation_runs_single_running",
        "fk_reconciliation_matches_run",
        "fk_reconciliation_matches_item",
        "ck_reconciliation_runs_status",
        "ck_reconciliation_runs_matched_nonneg",
        "ck_reconciliation_runs_unmatched_nonneg",
        "ck_reconciliation_runs_unmatched_item_nonneg",
        "ck_reconciliation_runs_unmatched_operation_nonneg",
        "ck_reconciliation_runs_unmatched_total",
        "unmatched_item_count",
        "unmatched_operation_count",
        "amount_diff_minor",
        "date_diff_days",
        "ck_reconciliation_matches_type",
        "ck_reconciliation_matches_amount_nonneg",
        "ix_reconciliation_runs_status",
        "ix_reconciliation_runs_date_currency",
        "ix_clearing_items_run_id",
        # M6 (review PR #27): alcance + R2 (la `clearing_files (status)` ya
        # existe desde `0050`: `ix_clearing_files_status`, sin duplicar).
        "ix_clearing_items_status_value_currency",
        "ix_transactions_recon_candidates",
        "ix_transactions_recon_settle",
        "ix_reconciliation_matches_run_id",
        "ix_reconciliation_matches_transaction_id",
        "reconciliation.amount_tolerance_minor",
        "reconciliation.date_tolerance_days",
        "reconciliation.business_timezone",
        "reconciliation.max_candidate_pairs",
        "reconciliation.max_candidate_pairs_per_run",
        "ON CONFLICT (key) DO NOTHING",
    ):
        assert token in text, f"migracion 0051 sin {token}"
    # M5: `total_minor` con signo (sin CHECK `>= 0`).
    assert (
        "ck_reconciliation_runs_total_nonneg" not in text
    ), "M5: total con signo INBOUND +/OUTBOUND -, sin CHECK de no negatividad"
    assert "uq_reconciliation_runs_date_currency" not in text, (
        "sin UQ natural: las COMPLETED son inmutables y los lotes tardios "
        "crean corridas nuevas (H3)"
    )
    assert 'drop_table("reconciliation_matches"' in text
    assert 'drop_column("clearing_items", "run_id"' in text
    assert 'drop_table("reconciliation_runs"' in text


# ------------------------------------------------- H1: FAILED nunca cruza


def test_run_ignores_failed_file_items(db: Session):
    """H1: items de archivos `FAILED` no se cruzan.

    Regresion: un archivo que pasa legitimamente a `FAILED` cierra sus
    items `PENDING` como `EXCEPTION` (sin `run_id`: 3.4 del comentario
    PR #27, sin limbo) mas un archivo `PROCESSED` con un item cruzable ->
    solo cruza el del `PROCESSED` (`matched=1`); los del `FAILED` quedan
    `EXCEPTION` con `run_id` nulo. B1 (BAJO PR #27, `unmatched_count`
    total): la operacion `H1-B` queda sin item cruzable ->
    `MISSING_EXTERNAL` y cuenta (`unmatched=1`).
    """
    _tx(db, ref="H1-A", amount=100_00)
    _tx(db, ref="H1-B", amount=200_00)
    failed = recon_repo.create_file_with_items(
        db,
        source="NETWORK",
        file_ref="FILE-h1-failed",
        checksum="cd" * 32,
        item_count=1,
        items=[
            {
                "external_ref": "H1-B",
                "amount_minor": 200_00,
                "currency": "PEN",
                "value_date": D,
                "direction": "OUTBOUND",
            }
        ],
    )
    recon_repo.mark_file_status(db, failed, "FAILED")
    _ingest(db, [("H1-A", 100_00, "PEN", D)], seed="h1-ok")
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert run.status == "COMPLETED"
    assert (run.matched_count, run.unmatched_count) == (1, 1)
    left = recon_repo.list_pending_items_for_run(db, run_date=D, currency="PEN")
    assert left == []
    failed_items = recon_repo.list_pending_items(db, file_id=failed.id)
    assert failed_items == [], "el FAILED no deja PENDING en el limbo"
    stored_failed = db.scalars(
        sa.select(recon_repo.ClearingItem).where(recon_repo.ClearingItem.file_id == failed.id)
    ).all()
    assert len(stored_failed) == 1
    assert stored_failed[0].status == "EXCEPTION"
    assert stored_failed[0].run_id is None


def test_run_ignores_received_file_items(db: Session):
    """H1: archivos aun `RECEIVED` (sin procesar) tampoco entran al cruce.

    B1 (BAJO PR #27, `unmatched_count` total): la operacion `H1-R` queda
    sin item cruzable -> `MISSING_EXTERNAL` y cuenta (`unmatched=1`).
    """
    _tx(db, ref="H1-R", amount=100_00)
    recon_repo.create_file_with_items(
        db,
        source="NETWORK",
        file_ref="FILE-h1-received",
        checksum="ef" * 32,
        item_count=1,
        items=[
            {
                "external_ref": "H1-R",
                "amount_minor": 100_00,
                "currency": "PEN",
                "value_date": D,
                "direction": "OUTBOUND",
            }
        ],
    )
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert run.status == "COMPLETED"
    assert (run.matched_count, run.unmatched_count) == (0, 1)


# ------------------------------------------------- H2: emparejamiento persistido


def test_run_persists_matches_and_item_run_id(db: Session):
    """H2: cada `Match` deja fila auditable y marca `run_id` en el item."""
    tx = _tx(db, ref="H2-A", amount=100_00)
    _ingest(db, [("H2-A", 100_00, "PEN", D)], seed="h2")
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert run.status == "COMPLETED"
    matches = recon_repo.list_matches_by_run(db, run.id)
    assert len(matches) == 1
    (match,) = matches
    assert match.run_id == run.id
    assert str(match.transaction_id) == str(tx.id)
    assert match.match_type == "EXACT"
    assert (match.amount_minor, match.currency) == (100_00, "PEN")
    item = recon_repo.get_match_by_item(db, match.clearing_item_id)
    assert item is not None and item.id == match.id
    stored = db.scalars(
        sa.select(recon_repo.ClearingItem).where(
            recon_repo.ClearingItem.id == match.clearing_item_id
        )
    ).one()
    assert stored.status == "MATCHED"
    assert stored.run_id == run.id
    with pytest.raises(recon_repo.ReconciliationConflictError):
        recon_repo.create_match(
            db,
            run_id=run.id,
            clearing_item_id=match.clearing_item_id,
            transaction_id=tx.id,
            match_type="EXACT",
            amount_minor=100_00,
            currency="PEN",
        )
    db.rollback()


def test_run_exception_items_record_run_id_without_match(db: Session):
    """H2: las excepciones registran `run_id` en el item (sin crear match)."""
    _ingest(db, [("H2-X", 100_00, "PEN", D)], seed="h2x")
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert run.status == "COMPLETED"
    assert (run.matched_count, run.unmatched_count) == (0, 1)
    assert recon_repo.list_matches_by_run(db, run.id) == []
    pending_all = db.scalars(
        sa.select(recon_repo.ClearingItem).where(recon_repo.ClearingItem.status == "EXCEPTION")
    ).all()
    assert len(pending_all) == 1
    assert pending_all[0].run_id == run.id


def test_run_does_not_reuse_matched_operation(db: Session):
    """H2: dos corridas no reutilizan la misma operacion interna."""
    tx = _tx(db, ref="H2-R", amount=100_00)
    _ingest(db, [("H2-R", 100_00, "PEN", D)], seed="h2r1")
    first = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert first.matched_count == 1
    _ingest(db, [("H2-R", 100_00, "PEN", D)], seed="h2r2")
    second = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert second.id != first.id
    assert second.status == "COMPLETED"
    assert (second.matched_count, second.unmatched_count) == (0, 1)
    used = recon_repo.list_used_transaction_ids(db)
    assert str(tx.id) in used


def test_run_replay_without_new_items_creates_no_duplicate_match(db: Session):
    """H2: replay sin pendientes nuevos no duplica matches."""
    _tx(db, ref="H2-D", amount=100_00)
    _ingest(db, [("H2-D", 100_00, "PEN", D)], seed="h2d")
    first = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    count_before = len(recon_repo.list_matches_by_run(db, first.id))
    assert count_before == 1
    again = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert again.id == first.id
    assert len(recon_repo.list_matches_by_run(db, again.id)) == 1


# ------------------------------------------------- H3: corridas tardias


def test_run_late_batch_creates_new_run_and_reexecution_reuses_it(db: Session):
    """H3: lote tardio -> corrida nueva; reejecutar sin nuevos la reutiliza."""
    _tx(db, ref="H3-A", amount=100_00)
    _ingest(db, [("H3-A", 100_00, "PEN", D)], seed="h3lote1")
    first = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert first.status == "COMPLETED"
    assert first.matched_count == 1
    # `created_at` anteriores: la segunda corrida debe ser la "ultima" aun
    # con relojes gruesos que empatarian los instantes (determinismo H3).
    first.created_at = first.created_at - timedelta(seconds=10)
    db.flush()

    _tx(db, ref="H3-B", amount=200_00)
    _ingest(db, [("H3-B", 200_00, "PEN", D)], seed="h3lote2")
    second = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert second.id != first.id
    assert second.status == "COMPLETED"
    assert (second.matched_count, second.unmatched_count) == (1, 0)
    assert first.status == "COMPLETED"
    assert first.matched_count == 1

    third = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert third.id == second.id
    assert db.scalars(sa.select(sa.func.count()).select_from(ReconciliationRun)).one() == 2
    matches = recon_repo.list_matches_by_run(db, second.id)
    assert len(matches) == 1
    assert matches[0].match_type == "EXACT"


# ------------------------------------------------- MEDIA (review PR #27 M2/M5)


def test_run_date_tolerance_widens_internal_scope_to_date_diff(db: Session):
    """M2: operacion interna en D+1 con tol 1 -> `DATE_DIFF`; con tol 0 -> huerfano.

    La fachada cubre [`run_date` - tol, `run_date` + tol] por dia de negocio
    y el dominio califica el par (el clearing sigue exacto en `run_date`).
    """
    _tx(db, ref="M2-D", amount=100_00, day=D + timedelta(days=1))
    _ingest(db, [("M2-D", 100_00, "PEN", D)], seed="m2")
    bare = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert (bare.matched_count, bare.unmatched_count) == (0, 1)
    db.rollback()

    _set_param(db, "reconciliation.date_tolerance_days", "1")
    day2 = D + timedelta(days=2)
    _tx(db, ref="M2-E", amount=100_00, day=day2 + timedelta(days=1))
    _ingest(db, [("M2-E", 100_00, "PEN", day2)], seed="m2tol")
    tolled = recon_service.run_reconciliation(db, run_date=day2, currency="PEN")
    assert tolled.status == "COMPLETED"
    assert (tolled.matched_count, tolled.unmatched_count) == (1, 0)
    (match,) = recon_repo.list_matches_by_run(db, tolled.id)
    assert match.match_type == "DATE_DIFF"


def test_run_direction_mismatch_does_not_match(db: Session):
    """M5: cargo `OUTBOUND` vs abono `INBOUND` (misma ref/monto) no matchean.

    La transferencia del motor deriva a `OUTBOUND` y el item es `INBOUND`:
    0 matches y 1 excepcion (`DIRECTION_MISMATCH`), sin fila de match.
    """
    _tx(db, ref="M5-D", amount=100_00, ttype="THIRD_PARTY_TRANSFER")
    _ingest(db, [("M5-D", 100_00, "PEN", D)], seed="m5", direction="INBOUND")
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert run.status == "COMPLETED"
    assert (run.matched_count, run.unmatched_count) == (0, 1)
    assert recon_repo.list_matches_by_run(db, run.id) == []
    assert run.total_minor == 0


def test_run_total_minor_is_signed_by_direction(db: Session):
    """M5: `total_minor` suma con signo (`INBOUND` +, `OUTBOUND` -).

    Un abono `INBOUND` (+100) y un cargo `OUTBOUND` (-200) conciliados dan
    neto -100 (puede ser negativo: sin CHECK `>= 0` desde la `0051`).
    """
    _tx(db, ref="M5-IN", amount=100_00, ttype="TOPUP")
    _tx(db, ref="M5-OUT", amount=200_00, ttype="THIRD_PARTY_TRANSFER")
    _ingest(db, [("M5-IN", 100_00, "PEN", D)], seed="m5in", direction="INBOUND")
    _ingest(db, [("M5-OUT", 200_00, "PEN", D)], seed="m5out", direction="OUTBOUND")
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert run.status == "COMPLETED"
    assert (run.matched_count, run.unmatched_count) == (2, 0)
    assert run.total_minor == 100_00 - 200_00


def test_run_complete_run_accepts_negative_signed_total(db: Session):
    """M5: `complete_run` persiste totales negativos (suma con signo)."""
    run = recon_repo.create_run(db, run_date=D, currency="PEN")
    recon_repo.complete_run(db, run, matched_count=1, unmatched_count=0, total_minor=-5000)
    assert run.status == "COMPLETED"
    assert run.total_minor == -5000


# ------------------------------------------------- BAJO (review PR #27 B1/B2)


def test_run_unmatched_counts_missing_external_total(db: Session):
    """B1: `unmatched_count` = TOTAL de excepciones (incluye `MISSING_EXTERNAL`).

    Opcion total sin columna nueva: una operacion interna sin item de
    clearing deja `unmatched_count >= 1` (antes era invisible: solo se
    contaba el lado clearing). Sin PII en el conteo: solo fecha/moneda.
    """
    _tx(db, ref="B1-SOLO", amount=100_00)
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert run.status == "COMPLETED"
    assert run.matched_count == 0
    assert run.unmatched_count == 1
    assert recon_repo.list_matches_by_run(db, run.id) == []


def test_run_executed_by_comes_from_caller_not_payload(db: Session):
    """B2: `executed_by` lo pasa el llamante autenticado, no el payload.

    El servicio jamas lee `executed_by` de los datos de entrada (el lote
    trae un `raw` con un actor falso y la corrida persiste el del llamante;
    E5-T21 debe pasarlo desde el principal del JWT). `None` = sistema.
    """
    actor = uuid.uuid4()
    impostor = uuid.uuid4()
    _tx(db, ref="B2-A", amount=100_00)
    row = recon_repo.create_file_with_items(
        db,
        source="NETWORK",
        file_ref="FILE-b2",
        checksum="b2" * 32,
        item_count=1,
        items=[
            {
                "external_ref": "B2-A",
                "amount_minor": 100_00,
                "currency": "PEN",
                "value_date": D,
                "direction": "OUTBOUND",
                "raw": {"executed_by": str(impostor)},
            }
        ],
    )
    recon_repo.mark_file_status(db, row, "PROCESSED")
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN", executed_by=actor)
    assert run.executed_by == actor
    assert run.executed_by != impostor
    system = recon_service.run_reconciliation(db, run_date=D + timedelta(days=30), currency="PEN")
    assert system.executed_by is None


# ------------------------------------------------- MEDIA (review PR #27): lease/reclaim + CONFLICT coherente


def test_run_reclaims_stale_running_and_retries_in_place(db: Session):
    """MEDIA: `RUNNING` huerfana (lease vencido) no bloquea: se reclama y reintenta.

    Regresion: sin reclaim la segunda llamada es `CONFLICT` aunque el worker
    muriera hace una hora; con el fix (lease default 30 min) la huerfana
    pasa a `FAILED` y la corrida se reintenta en su fila hasta `COMPLETED`.
    """
    orphan = recon_repo.create_run(db, run_date=D, currency="PEN")
    orphan.created_at = orphan.created_at - timedelta(minutes=60)
    db.flush()
    _tx(db, ref="L-1", amount=100_00)
    _ingest(db, [("L-1", 100_00, "PEN", D)], seed="lease")
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert run.id == orphan.id
    assert run.status == "COMPLETED"
    assert (run.matched_count, run.unmatched_count) == (1, 0)


def test_run_recent_running_is_conflict_within_lease(db: Session):
    """MEDIA: `RUNNING` reciente (dentro del lease) sigue siendo `CONFLICT`."""
    recon_repo.create_run(db, run_date=D, currency="PEN")
    with pytest.raises(recon_repo.ReconciliationConflictError):
        recon_service.run_reconciliation(db, run_date=D, currency="PEN")


def test_run_lease_minutes_is_configurable(db: Session):
    """MEDIA: `reconciliation.run_lease_minutes` configura el lease (default 30)."""
    assert recon_service._read_run_lease_minutes(db) == recon_service.DEFAULT_RUN_LEASE_MINUTES
    assert recon_service.DEFAULT_RUN_LEASE_MINUTES == 30
    _set_param(db, "reconciliation.run_lease_minutes", "0")
    assert recon_service._read_run_lease_minutes(db) == 30
    _set_param(db, "reconciliation.run_lease_minutes", "120")
    assert recon_service._read_run_lease_minutes(db) == 120
    orphan = recon_repo.create_run(db, run_date=D, currency="PEN")
    orphan.created_at = orphan.created_at - timedelta(minutes=60)
    db.flush()
    # Con lease 120, la de hace 60 min sigue vigente: `CONFLICT`.
    with pytest.raises(recon_repo.ReconciliationConflictError):
        recon_service.run_reconciliation(db, run_date=D, currency="PEN")


def test_run_budget_race_returns_coherent_conflict(db: Session, monkeypatch):
    """MEDIA: carrera en la rama `CandidateBudgetExceeded` -> `CONFLICT` coherente.

    Regresion: sin el `try/except` de la rama de presupuesto, la carrera
    (otro worker inserto la `RUNNING` tras el chequeo inicial) propagaba el
    error crudo de `create_run` ("duplicada"); con el fix se relee y se
    devuelve el `CONFLICT` coherente ("ya en curso", igual que la rama feliz).
    """
    _set_param(db, "reconciliation.max_candidate_pairs_per_run", "1")
    for i in range(2):
        _tx(db, ref=f"REF-Q-{i}", amount=100_00)
    _ingest(db, [(f"REF-Q-{i}", 100_00, "PEN", D) for i in range(2)], seed="bque")

    def _racing_create(session, **kwargs):
        raise recon_repo.ReconciliationConflictError("corrida de conciliacion duplicada")

    monkeypatch.setattr(recon_repo, "create_run", _racing_create)
    with pytest.raises(recon_repo.ReconciliationConflictError, match="ya en curso"):
        recon_service.run_reconciliation(db, run_date=D, currency="PEN")


# ------------------------------------------------- comentario PR #27 (3.1/3.2/3.3/3.4 + menores 5/7/9/10)


def test_run_date_tolerance_reoffer_excludes_used_in_neighbor_runs(db: Session):
    """3.1: tol=1 en D-1/D/D+1: la op del 14 conciliada el 13 no rompe el 15.

    Regresion: con usados acotados por ventana de `run_date`, la corrida
    del 15 volvia a ofertar la operacion del 14 (ya conciliada el 13) y el
    intento de re-match la marcaba como conflicto/duplicado. Con el enfoque
    por conjunto (`WHERE transaction_id IN (...)`, sin ventana), la op se
    excluye en las vecinas: cada corrida queda `COMPLETED` (la del 13 con
    `DATE_DIFF`; 14/15 con el item como excepcion) y nunca se lanza
    `ReconciliationConflictError`.
    """
    _set_param(db, "reconciliation.date_tolerance_days", "1")
    _tx(db, ref="TOL-1", amount=100_00, day=D)
    _ingest(db, [("TOL-1", 100_00, "PEN", D - timedelta(days=1))], seed="tol13")
    run13 = recon_service.run_reconciliation(db, run_date=D - timedelta(days=1), currency="PEN")
    assert run13.status == "COMPLETED"
    assert (run13.matched_count, run13.unmatched_count) == (1, 0)
    _ingest(db, [("TOL-1", 100_00, "PEN", D)], seed="tol14")
    run14 = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert run14.status == "COMPLETED"
    assert (run14.matched_count, run14.unmatched_count) == (0, 1)
    assert run14.unmatched_item_count == 1
    assert run14.unmatched_operation_count == 0
    _ingest(db, [("TOL-1", 100_00, "PEN", D + timedelta(days=1))], seed="tol15")
    run15 = recon_service.run_reconciliation(db, run_date=D + timedelta(days=1), currency="PEN")
    assert run15.status == "COMPLETED"
    assert (run15.matched_count, run15.unmatched_count) == (0, 1)
    total_matches = db.scalars(
        sa.select(sa.func.count()).select_from(recon_repo.ReconciliationMatch)
    ).one()
    assert total_matches == 1, "la operacion no se reoferta en las vecinas"


def test_run_mutation_conflict_leaves_failed_without_partials_and_session_usable(
    db: Session, monkeypatch
):
    """3.2: conflicto a mitad de mutacion -> `FAILED` sin parciales, sesion usable.

    Regresion: sin `SAVEPOINT`, el primer match/marca quedaba en `flush` y la
    sesion externa moria (el `commit` del llamante persistia parciales). Con
    el fix, el savepoint revierte, la corrida queda `FAILED`, el llamante
    confirma y la sesion sigue usable (el reintento en la misma fila
    concilia todo).
    """
    _tx(db, ref="P-1", amount=100_00)
    _tx(db, ref="P-2", amount=200_00)
    _ingest(db, [("P-1", 100_00, "PEN", D), ("P-2", 200_00, "PEN", D)], seed="part")
    real_create_match = recon_repo.create_match
    calls = {"n": 0}

    def _flaky(session, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_create_match(session, **kwargs)
        raise recon_repo.ReconciliationConflictError("emparejamiento duplicado")

    monkeypatch.setattr(recon_repo, "create_match", _flaky)
    with pytest.raises(recon_repo.ReconciliationConflictError):
        recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    # El llamante confirma: la corrida queda `FAILED` y la sesion sirve.
    db.commit()
    run = recon_repo.get_run_by_date_currency(db, D, "PEN")
    assert run is not None and run.status == "FAILED"
    assert recon_repo.list_matches_by_run(db, run.id) == []
    leftovers = recon_repo.list_pending_items_for_run(db, run_date=D, currency="PEN")
    assert len(leftovers) == 2
    assert all(item.status == "PENDING" and item.run_id is None for item in leftovers)
    monkeypatch.undo()
    retried = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert retried.id == run.id
    assert retried.status == "COMPLETED"
    assert (retried.matched_count, retried.unmatched_count) == (2, 0)
    assert len(recon_repo.list_matches_by_run(db, run.id)) == 2


def test_run_real_unique_violation_mid_mutation_leaves_failed_and_retries(db: Session, monkeypatch):
    """H-4 del review PR #27: `IntegrityError` real de UQ a mitad de mutacion.

    Carrera real: la operacion se concilio en otra corrida despues de leer
    los usados (aqui se fuerza con `list_used_transaction_ids` -> vacio).
    El `create_match` choca con `uq_reconciliation_matches_transaction` en
    mitad de la mutacion: el savepoint revierte los parciales, la corrida
    queda `FAILED`, la sesion sigue usable (`commit` del llamante) y el
    reintento queda `COMPLETED` (la op ya usada se excluye y el item queda
    como excepcion).
    Nota de motores: en SQLite el `IntegrityError` deja la sesion usable
    tras el rollback del savepoint; en Postgres el error aborta la
    transaccion y el rollback del `SAVEPOINT` (`begin_nested`) la recupera.
    """
    op = _tx(db, ref="R-1", amount=100_00)
    _ingest(db, [("R-1", 100_00, "PEN", D)], seed="race")
    # La "otra corrida" (D-1) concilia la misma operacion primero.
    rival = recon_repo.create_run(db, run_date=D - timedelta(days=1), currency="PEN")
    rival_file = _ingest(db, [("RIVAL-1", 50_00, "PEN", D - timedelta(days=1))], seed="rival")
    (rival_item,) = recon_repo.list_pending_items(db, file_id=rival_file.id)
    recon_repo.create_match(
        db,
        run_id=rival.id,
        clearing_item_id=rival_item.id,
        transaction_id=op.id,
        match_type="EXACT",
        amount_minor=100_00,
        currency="PEN",
    )
    recon_repo.mark_item_status(db, rival_item, "MATCHED", run_id=rival.id)
    recon_repo.complete_run(db, rival, matched_count=1, unmatched_count=0, total_minor=50_00)
    # Simula la carrera: los usados se leyeron antes de que la otra
    # corrida persistiera su match.
    monkeypatch.setattr(recon_repo, "list_used_transaction_ids", lambda *a, **k: set())
    with pytest.raises(recon_repo.ReconciliationConflictError):
        recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    # El llamante confirma: la corrida queda `FAILED` y la sesion sirve.
    db.commit()
    run = recon_repo.get_run_by_date_currency(db, D, "PEN")
    assert run is not None and run.status == "FAILED"
    assert recon_repo.list_matches_by_run(db, run.id) == []
    leftovers = recon_repo.list_pending_items_for_run(db, run_date=D, currency="PEN")
    assert len(leftovers) == 1
    assert leftovers[0].status == "PENDING" and leftovers[0].run_id is None
    monkeypatch.undo()
    retried = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert retried.id == run.id
    assert retried.status == "COMPLETED"
    assert (retried.matched_count, retried.unmatched_count) == (0, 1)
    assert retried.unmatched_item_count == 1
    assert retried.unmatched_operation_count == 0
    assert recon_repo.list_matches_by_run(db, run.id) == []


def test_run_unmatched_breakdown_item_vs_operation(db: Session):
    """3.3: 100 ops internas + 1 item -> matched=1, item=0, operation=99, total=99."""
    for i in range(100):
        _tx(db, ref=f"B-{i:03d}", amount=100_00 + i)
    _ingest(db, [("B-000", 100_00, "PEN", D)], seed="brk")
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert run.status == "COMPLETED"
    assert run.matched_count == 1
    assert run.unmatched_item_count == 0
    assert run.unmatched_operation_count == 99
    assert run.unmatched_count == 99
    assert run.unmatched_count == run.unmatched_item_count + run.unmatched_operation_count


def test_run_checksum_mismatch_replay_still_conciliates_afterwards(db: Session):
    """3.4: el replay con checksum distinto no bloquea la corrida posterior."""
    _tx(db, ref="C-1", amount=100_00)
    payload = {
        "source": "NETWORK",
        "file_ref": "CLR-POST-1",
        "checksum": "ab" * 32,
        "item_count": 1,
        "items": [
            {
                "external_ref": "C-1",
                "amount_minor": 100_00,
                "currency": "PEN",
                "value_date": D,
                "direction": "OUTBOUND",
            }
        ],
    }
    recon_service.ingest_clearing_file(db, payload)
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_service.ingest_clearing_file(db, dict(payload, checksum="00" * 32))
    db.commit()
    assert recon_repo.get_file_by_ref(db, "CLR-POST-1").status == "PROCESSED"
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert run.status == "COMPLETED"
    assert (run.matched_count, run.unmatched_count) == (1, 0)


def test_run_completed_replay_skips_candidate_lookups(db: Session, monkeypatch):
    """Menor 7: replay `COMPLETED` sin pendientes no consulta candidatos/usados."""
    _tx(db, ref="E-1", amount=100_00)
    _ingest(db, [("E-1", 100_00, "PEN", D)], seed="early")
    first = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert first.status == "COMPLETED"

    def _boom(*args, **kwargs):
        raise AssertionError("no debe consultar candidatos/usados en replay")

    import app.modules.transactions.service as tx_service

    monkeypatch.setattr(tx_service, "list_reconciliation_candidates", _boom)
    monkeypatch.setattr(recon_repo, "list_used_transaction_ids", _boom)
    again = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert again.id == first.id
    assert again.status == "COMPLETED"


def test_run_persists_amount_and_date_diffs(db: Session):
    """Menor 10: los matches guardan `amount_diff_minor`/`date_diff_days`."""
    _set_param(db, "reconciliation.amount_tolerance_minor", "100")
    _set_param(db, "reconciliation.date_tolerance_days", "1")
    _tx(db, ref="DF-A", amount=100_00, day=D)
    _tx(db, ref="DF-B", amount=100_00, day=D)
    _tx(db, ref="DF-C", amount=100_00, day=D + timedelta(days=1))
    _ingest(
        db,
        [
            ("DF-A", 100_00, "PEN", D),
            ("DF-B", 101_00, "PEN", D),
            ("DF-C", 100_00, "PEN", D),
        ],
        seed="dif",
    )
    run = recon_service.run_reconciliation(db, run_date=D, currency="PEN")
    assert run.status == "COMPLETED"
    assert run.matched_count == 3
    items_by_id = {
        str(item.id): item.external_ref
        for item in db.scalars(sa.select(recon_repo.ClearingItem)).all()
    }
    diffs = {
        items_by_id[str(match.clearing_item_id)]: (
            match.match_type,
            match.amount_diff_minor,
            match.date_diff_days,
        )
        for match in recon_repo.list_matches_by_run(db, run.id)
    }
    assert diffs["DF-A"] == ("EXACT", None, None)
    assert diffs["DF-B"] == ("AMOUNT_DIFF", 100, None)
    assert diffs["DF-C"] == ("DATE_DIFF", None, -1)


def test_facade_invalid_config_timezone_falls_back_to_default(db: Session):
    """Menor 5: zona malformada en config (`'/etc/passwd'`) -> default sin lanzar."""
    from app.modules.transactions.service import list_reconciliation_candidates

    _set_param(db, "reconciliation.business_timezone", '"/etc/passwd"')
    row = _tx(db, ref="TZ-F", amount=100_00, day=D)
    found = list_reconciliation_candidates(db, D, "PEN")
    assert [entry["transaction_id"] for entry in found] == [str(row.id)]
    with pytest.raises(ValueError):
        list_reconciliation_candidates(db, D, "PEN", business_timezone="/etc/passwd")


def test_create_match_non_unique_integrity_is_persistence_error(db: Session, monkeypatch):
    """Menor 9: `IntegrityError` no-UQ en `create_match` -> persistencia (no `CONFLICT`)."""
    from sqlalchemy.exc import IntegrityError as SAIntegrityError
    from sqlalchemy.orm import Session as SASession

    run = recon_repo.create_run(db, run_date=D, currency="PEN")

    def _broken_flush(self):
        raise SAIntegrityError(
            "INSERT INTO reconciliation.reconciliation_matches",
            {},
            Exception("CHECK constraint failed: ck_reconciliation_matches_amount_nonneg"),
        )

    monkeypatch.setattr(SASession, "flush", _broken_flush)
    with pytest.raises(recon_repo.ReconciliationError) as excinfo:
        recon_repo.create_match(
            db,
            run_id=run.id,
            clearing_item_id=uuid.uuid4(),
            transaction_id=uuid.uuid4(),
            match_type="EXACT",
            amount_minor=100_00,
            currency="PEN",
        )
    assert type(excinfo.value) is recon_repo.ReconciliationError
    assert "persistir" in str(excinfo.value)
