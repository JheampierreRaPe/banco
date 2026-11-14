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


def _ingest(db: Session, entries, *, seed=None):
    """Crea un archivo de clearing con los items dados (todos `PENDING`)."""
    seed = seed or f"t{uuid.uuid4().hex[:8]}"
    items = [
        {
            "external_ref": ref,
            "amount_minor": amount,
            "currency": currency,
            "value_date": day,
            "direction": "INBOUND",
        }
        for ref, amount, currency, day in entries
    ]
    return recon_repo.create_file_with_items(
        db,
        source="NETWORK",
        file_ref=f"FILE-{seed}",
        checksum="ab" * 32,
        item_count=len(items),
        items=items,
    )


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
    assert run.total_minor == 600_00
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
    assert second.total_minor == 101_00


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
    assert run.total_minor == 100_00


def test_run_does_not_move_money(db: Session):
    """La corrida no crea ni modifica operaciones del motor."""
    before = {
        row.id: (row.status, row.amount_minor, row.currency)
        for row in db.scalars(sa.select(Transaction)).all()
    }
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
    """Repositorio de corridas: crear, duplicar, completar, fallar, reintentar."""
    run = recon_repo.create_run(db, run_date=D, currency="PEN")
    assert run.status == "RUNNING"
    assert recon_repo.get_run_by_date_currency(db, D, "PEN").id == run.id
    with pytest.raises(recon_repo.ReconciliationConflictError):
        recon_repo.create_run(db, run_date=D, currency="PEN")
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_repo.complete_run(db, run, matched_count=-1, unmatched_count=0, total_minor=0)
    recon_repo.complete_run(db, run, matched_count=2, unmatched_count=1, total_minor=300)
    assert run.status == "COMPLETED"
    assert run.finished_at is not None
    assert (run.matched_count, run.unmatched_count, run.total_minor) == (2, 1, 300)
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
    }
    assert rows[0]["amount_minor"] == 100_00
    assert rows[0]["value_date"] == D
    assert rows[0]["type"] == "THIRD_PARTY_TRANSFER"
    assert rows[0]["status"] == "SETTLED"


def test_facade_filters_status_currency_and_date(db: Session):
    """Solo conciliables de la moneda y fecha (`settled_at` manda)."""
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
    """La fachada no deja pendientes (`new`) ni muta filas."""
    from app.modules.transactions.service import list_reconciliation_candidates

    before = [(row.id, row.status) for row in db.scalars(sa.select(Transaction)).all()]
    list_reconciliation_candidates(db, D, "PEN")
    assert len(db.new) == 0
    after = [(row.id, row.status) for row in db.scalars(sa.select(Transaction)).all()]
    assert after == before


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
    """`0051` encadena `0050`, crea `reconciliation_runs` y siembra claves."""
    text = MIGRATION_0051.read_text(encoding="utf-8")
    assert 'revision = "0051_reconciliation_runs"' in text
    assert 'down_revision = "0050_reconciliation"' in text
    for token in (
        "reconciliation_runs",
        "uq_reconciliation_runs_date_currency",
        "ck_reconciliation_runs_status",
        "ck_reconciliation_runs_matched_nonneg",
        "ck_reconciliation_runs_unmatched_nonneg",
        "ck_reconciliation_runs_total_nonneg",
        "ix_reconciliation_runs_status",
        "reconciliation.amount_tolerance_minor",
        "reconciliation.date_tolerance_days",
        "reconciliation.max_candidate_pairs",
        "reconciliation.max_candidate_pairs_per_run",
        "ON CONFLICT (key) DO NOTHING",
    ):
        assert token in text, f"migracion 0051 sin {token}"
    assert 'drop_table("reconciliation_runs"' in text
