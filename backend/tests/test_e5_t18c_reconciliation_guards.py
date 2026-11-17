"""Guardas de BD de la conciliacion (E5-T18, MEDIO/BAJO del review PR #27).

Cubre (`docs/tasks/E5-T18.md`, fixes MEDIO-1/MEDIO-2/BAJO-1/BAJO-2/BAJO-3):

- MEDIO-1: `UQ(transaction_id)` en `reconciliation_matches` (una operacion
  interna se concilia como maximo una vez en todo el historial).
- MEDIO-2: indice unico parcial `uq_reconciliation_runs_single_running`
  (`UNIQUE (run_date, currency) WHERE status='RUNNING'`): dos `RUNNING`
  simultaneas para la misma fecha/moneda -> una falla con `CONFLICT`;
  `create_run` convierte el `IntegrityError`; `mark_item_status` atomico
  (`UPDATE ... WHERE status='PENDING'`, `rowcount == 0` -> `CONFLICT`
  sin confiar en el estado en memoria: dos workers marcando el mismo
  item -> solo uno gana).
- BAJO-1: la rama `except ReconciliationConflictError` de
  `run_reconciliation` vuelve a ser alcanzable (carrera real por el
  indice parcial); `mark_run_running`/`complete_run`/`fail_run` son
  `UPDATE ... WHERE id=:id AND status=:from` con `rowcount == 0` ->
  `CONFLICT` (sin confiar en el estado en memoria: un objeto stale no
  gana si la fila ya cambio).
- BAJO-2: `list_used_transaction_ids` filtra por la ventana de `run_date`
  (± tolerancia) y moneda, sin traer toda la tabla.
- BAJO-3: "ultima corrida" determinista (`created_at DESC, id DESC`, sin
  depender de los `NULL` de `finished_at`).

SQLite en memoria con schemas `reconciliation`/`config` (ATTACH); sin
Postgres: corre en local y en CI. Sin PII en errores ni logs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.core.db import Base
from app.modules.reconciliation import repository as recon_repo
from app.modules.reconciliation.models import ReconciliationMatch, ReconciliationRun

BACKEND_DIR = Path(__file__).resolve().parents[1]

D = date(2026, 11, 14)


@pytest.fixture()
def db():
    """SQLite en memoria con `reconciliation` + `config` (sin motor)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("reconciliation", "config"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["reconciliation.clearing_files"],
            Base.metadata.tables["reconciliation.clearing_items"],
            Base.metadata.tables["reconciliation.reconciliation_runs"],
            Base.metadata.tables["reconciliation.reconciliation_matches"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def _file_with_items(db: Session, *, seed: str, entries) -> list:
    """Archivo `PROCESSED` con los items dados; retorna los items `PENDING`."""
    row = recon_repo.create_file_with_items(
        db,
        source="NETWORK",
        file_ref=f"FILE-{seed}",
        checksum="ab" * 32,
        item_count=len(entries),
        items=[
            {
                "external_ref": ref,
                "amount_minor": amount,
                "currency": currency,
                "value_date": day,
                "direction": "INBOUND",
            }
            for ref, amount, currency, day in entries
        ],
    )
    recon_repo.mark_file_status(db, row, "PROCESSED")
    return recon_repo.list_pending_items(db, file_id=row.id)


# ------------------------------------------------- MEDIO-1: UQ(transaction_id)


def test_match_transaction_id_unique_conflict(db: Session):
    """MEDIO-1: el mismo `transaction_id` en otro item -> `CONFLICT`.

    Regresion: sin `uq_reconciliation_matches_transaction` el segundo
    `create_match` se persiste (doble conciliacion de la operacion) y no
    lanza; con el fix la UQ decide (`ReconciliationConflictError`).
    """
    run = recon_repo.create_run(db, run_date=D, currency="PEN")
    items = _file_with_items(
        db,
        seed="m1",
        entries=[("M1-A", 100_00, "PEN", D), ("M1-B", 200_00, "PEN", D)],
    )
    tid = uuid.uuid4()
    recon_repo.create_match(
        db,
        run_id=run.id,
        clearing_item_id=items[0].id,
        transaction_id=tid,
        match_type="EXACT",
        amount_minor=100_00,
        currency="PEN",
    )
    with pytest.raises(recon_repo.ReconciliationConflictError):
        recon_repo.create_match(
            db,
            run_id=run.id,
            clearing_item_id=items[1].id,
            transaction_id=tid,
            match_type="EXACT",
            amount_minor=200_00,
            currency="PEN",
        )
    db.rollback()


def test_matches_declare_item_and_transaction_uniqueness(db: Session):
    """MEDIO-1: DDL declarado (UQ por item y por operacion) y unico parcial.

    Regresion declarativa: sin el fix los nombres no existen en la
    metadata (ni el `WHERE status = 'RUNNING'` en el DDL Postgres).
    """
    constraints = {
        const.name
        for const in ReconciliationMatch.__table__.constraints
        if isinstance(const, sa.UniqueConstraint)
    }
    assert constraints == {
        "uq_reconciliation_matches_item",
        "uq_reconciliation_matches_transaction",
    }
    idx = next(
        index
        for index in ReconciliationRun.__table__.indexes
        if index.name == "uq_reconciliation_runs_single_running"
    )
    assert idx.unique
    assert [col.name for col in idx.columns] == ["run_date", "currency"]
    ddl = str(sa.schema.CreateIndex(idx).compile(dialect=postgresql.dialect()))
    assert "UNIQUE" in ddl
    assert "WHERE status = 'RUNNING'" in ddl


# ------------------------------------------------- MEDIO-2: una sola RUNNING


def test_only_one_running_per_date_currency(db: Session):
    """MEDIO-2: segunda `RUNNING` simultanea -> `CONFLICT` (la BD decide).

    Regresion: sin el indice parcial el segundo `create_run` se persiste
    y no lanza; con el fix falla con `ReconciliationConflictError`.
    Distinta moneda/fecha o estados no-`RUNNING` si conviven; tras
    `COMPLETED` se puede abrir otra `RUNNING`.
    """
    first = recon_repo.create_run(db, run_date=D, currency="PEN")
    assert first.status == "RUNNING"
    with pytest.raises(recon_repo.ReconciliationConflictError):
        recon_repo.create_run(db, run_date=D, currency="PEN")
    # Alcances distintos no colisionan; `FAILED` convive con `RUNNING`.
    recon_repo.create_run(db, run_date=D, currency="USD")
    recon_repo.create_run(db, run_date=D + timedelta(days=1), currency="PEN")
    recon_repo.create_run(db, run_date=D, currency="PEN", status="FAILED")
    rows = recon_repo.list_runs_by_date_currency(db, D, "PEN")
    assert {row.status for row in rows} == {"RUNNING", "FAILED"}
    # Tras `COMPLETED` la guarda parcial ya no bloquea (sin `RUNNING`).
    running = next(row for row in rows if row.status == "RUNNING")
    recon_repo.complete_run(db, running, matched_count=0, unmatched_count=1, total_minor=0)
    reopened = recon_repo.create_run(db, run_date=D, currency="PEN")
    assert reopened.status == "RUNNING"
    assert reopened.id != running.id


def test_mark_item_status_atomic_stale_memory_loses(db: Session):
    """MEDIO-2: `mark_item_status` atomico; el estado en memoria no gana.

    Regresion: con el chequeo en memoria, un objeto stale (`status =
    'PENDING'` forjado tras conciliar la fila) pasaba el `if` y
    reescribia; con el fix el `UPDATE ... WHERE status='PENDING'` afecta
    0 filas y lanza `ReconciliationConflictError` (solo un worker gana).
    """
    run = recon_repo.create_run(db, run_date=D, currency="PEN")
    (item,) = _file_with_items(db, seed="m2", entries=[("M2-A", 100_00, "PEN", D)])
    recon_repo.mark_item_status(db, item, "MATCHED", run_id=run.id)
    assert item.status == "MATCHED"
    with pytest.raises(recon_repo.ReconciliationConflictError):
        recon_repo.mark_item_status(db, item, "EXCEPTION", run_id=run.id)
    # Estado en memoria stale (forjado sin `flush`): la fila ya esta
    # `MATCHED` en BD y el segundo worker debe perder igual.
    item.status = "PENDING"
    with db.no_autoflush, pytest.raises(recon_repo.ReconciliationConflictError):
        recon_repo.mark_item_status(db, item, "EXCEPTION", run_id=run.id)
    db.rollback()


# ------------------------------------------------- BAJO-1: transiciones con condicion de estado


def test_run_transitions_conditional_stale_memory_loses(db: Session):
    """BAJO-1: `mark/complete/fail_run` con `WHERE status=`; el stale pierde.

    Regresion: con el chequeo en memoria, forjar `status` en el objeto
    (sin `flush`) pasaba el `if` y reescribia la fila ya cerrada; con el
    fix el `UPDATE ... WHERE id=:id AND status=:from` afecta 0 filas y
    lanza `ReconciliationConflictError` (solo una transicion gana).
    `db.commit()` fija el estado real antes de forjar el stale (el
    `begin_nested` de `create_run` confirma cuando no hay transaccion
    abierta, asi que el `rollback` no aisla entre bloques: cada caso usa
    su propio alcance).
    """
    # `complete_run` stale: fila ya `COMPLETED`, memoria forjada a `RUNNING`.
    run = recon_repo.create_run(db, run_date=D, currency="PEN")
    recon_repo.complete_run(db, run, matched_count=1, unmatched_count=0, total_minor=100_00)
    db.commit()
    assert run.status == "COMPLETED"
    run.status = "RUNNING"
    with db.no_autoflush, pytest.raises(recon_repo.ReconciliationConflictError):
        recon_repo.complete_run(db, run, matched_count=9, unmatched_count=9, total_minor=9)
    db.rollback()

    # `mark_run_running` stale: fila `COMPLETED`, memoria forjada a `FAILED`.
    run = recon_repo.create_run(db, run_date=D + timedelta(days=1), currency="PEN")
    recon_repo.complete_run(db, run, matched_count=1, unmatched_count=0, total_minor=100_00)
    db.commit()
    run.status = "FAILED"
    with db.no_autoflush, pytest.raises(recon_repo.ReconciliationConflictError):
        recon_repo.mark_run_running(db, run)
    db.rollback()

    # `fail_run` stale: fila `COMPLETED`, memoria forjada a `RUNNING`.
    run = recon_repo.create_run(db, run_date=D + timedelta(days=2), currency="PEN")
    recon_repo.complete_run(db, run, matched_count=0, unmatched_count=1, total_minor=0)
    db.commit()
    run.status = "RUNNING"
    with db.no_autoflush, pytest.raises(recon_repo.ReconciliationConflictError):
        recon_repo.fail_run(db, run)
    db.rollback()


# ------------------------------------------------- BAJO-2: usados con ventana


def test_used_transaction_ids_scoped_by_window(db: Session):
    """BAJO-2: usados filtrados por moneda y ventana de fecha (sin tabla).

    Regresion: sin alcance la firma no acepta filtros (`TypeError`) y el
    servicio trae toda la tabla; con el fix solo los emparejados en la
    ventana (`run_date` ± tolerancia) y moneda.
    """
    run_pen = recon_repo.create_run(db, run_date=D, currency="PEN")
    run_usd = recon_repo.create_run(db, run_date=D, currency="USD")
    run_late = recon_repo.create_run(db, run_date=D + timedelta(days=10), currency="PEN")
    tx_pen, tx_usd, tx_late = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    recon_repo.create_match(
        db,
        run_id=run_pen.id,
        clearing_item_id=uuid.uuid4(),
        transaction_id=tx_pen,
        match_type="EXACT",
        amount_minor=100_00,
        currency="PEN",
    )
    recon_repo.create_match(
        db,
        run_id=run_usd.id,
        clearing_item_id=uuid.uuid4(),
        transaction_id=tx_usd,
        match_type="EXACT",
        amount_minor=100_00,
        currency="USD",
    )
    recon_repo.create_match(
        db,
        run_id=run_late.id,
        clearing_item_id=uuid.uuid4(),
        transaction_id=tx_late,
        match_type="EXACT",
        amount_minor=100_00,
        currency="PEN",
    )
    assert recon_repo.list_used_transaction_ids(db) == {
        str(tx_pen),
        str(tx_usd),
        str(tx_late),
    }
    assert recon_repo.list_used_transaction_ids(db, currency="PEN", run_date=D) == {str(tx_pen)}
    assert recon_repo.list_used_transaction_ids(db, currency="USD", run_date=D) == {str(tx_usd)}
    scoped = recon_repo.list_used_transaction_ids(
        db, currency="PEN", run_date=D, date_tolerance_days=10
    )
    assert scoped == {str(tx_pen), str(tx_late)}
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_repo.list_used_transaction_ids(db, currency="PEN", run_date=D, date_tolerance_days=-1)


# ------------------------------------------------- BAJO-3: ultima determinista


def test_last_run_ignores_finished_at_nulls(db: Session):
    """BAJO-3: la "ultima" es (`created_at DESC, id DESC`), sin `finished_at`.

    Regresion: con `finished_at` en el `ORDER BY`, el orden de los `NULL`
    varia por motor y la `COMPLETED` (unico `finished_at` no nulo) podia
    imponerse por ese criterio; con el fix mandan (`created_at`, `id`).
    Mismo instante -> gana el `id` mayor aunque siga sin `finished_at`.
    """
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    older = ReconciliationRun(
        id=uuid.UUID(int=1),
        run_date=D,
        currency="PEN",
        status="RUNNING",
        created_at=now,
    )
    newer = ReconciliationRun(
        id=uuid.UUID(int=2),
        run_date=D,
        currency="PEN",
        status="FAILED",
        created_at=now,
    )
    db.add_all([older, newer])
    db.flush()
    recon_repo.complete_run(db, older, matched_count=1, unmatched_count=0, total_minor=100_00)
    assert older.finished_at is not None
    assert newer.finished_at is None
    assert recon_repo.get_run_by_date_currency(db, D, "PEN").id == newer.id


# ------------------------------------------------- MEDIA: lease/reclaim de RUNNING huerfanas


def test_reclaim_stale_running_marks_orphan_failed(db: Session):
    """MEDIA: `reclaim_stale_running` cierra huerfanas y conserva recientes.

    Regresion: sin reclaim, una `RUNNING` cuyo worker murio bloquea el
    alcance para siempre (toda llamada posterior es `CONFLICT`); con el fix
    las anteriores al corte pasan a `FAILED` con `finished_at` (`rowcount`
    reportado) y las recientes u otros alcances quedan intactas.
    """
    orphan = recon_repo.create_run(db, run_date=D, currency="PEN")
    orphan.created_at = orphan.created_at - timedelta(minutes=60)
    recent = recon_repo.create_run(db, run_date=D + timedelta(days=1), currency="PEN")
    other = recon_repo.create_run(db, run_date=D, currency="USD")
    other.created_at = other.created_at - timedelta(minutes=60)
    db.flush()
    cutoff = orphan.created_at + timedelta(minutes=30)

    reclaimed = recon_repo.reclaim_stale_running(db, run_date=D, currency="PEN", older_than=cutoff)
    assert reclaimed == 1
    assert orphan.status == "FAILED"
    assert orphan.finished_at is not None
    assert recent.status == "RUNNING"
    assert other.status == "RUNNING"
    # Corte anterior a todo: nada que reclamar.
    assert (
        recon_repo.reclaim_stale_running(
            db,
            run_date=D,
            currency="USD",
            older_than=other.created_at - timedelta(seconds=1),
        )
        == 0
    )
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_repo.reclaim_stale_running(db, run_date=D, currency="PEN", older_than="ayer")
    db.rollback()
