"""Ingesta de clearing `reconciliation` (E5-T17, HU19).

Cubre `docs/tasks/E5-T17.md#pruebas-requeridas`:

- Migracion: `0050` encadena `0049`, crea las 2 tablas con CHECKs e
  indices, `downgrade` reversible; un solo head.
- Camino feliz: lote valido -> archivo `PROCESSED` + items `PENDING`.
- Idempotencia: mismo lote dos veces -> un archivo y N items.
- Errores: checksum distinto, `item_count` inconsistente, monto <= 0,
  moneda invalida, fecha invalida, checksum malformado -> `FAILED`
  sin items parciales.
- Job: una pasada con fuente mock; `commit` por archivo; no arranca
  si el flag esta apagado.
- No mueve dinero; sin PII en logs.

SQLite en memoria con schema `reconciliation` (ATTACH); las REF son
inexistentes (sin FK fisica fuera del schema), asi que no requiere
tablas de otros modulos.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.modules.reconciliation.models as _recon_models
from app.adapters.clearing_source import build_deterministic_batch
from app.core.db import Base
from app.modules.reconciliation import repository as recon_repo
from app.modules.reconciliation import service as recon_service
from app.modules.reconciliation.jobs.clearing_ingest import run_clearing_ingest_once
from app.modules.reconciliation.models import ClearingFile, ClearingItem

assert _recon_models is not None

BACKEND_DIR = Path(__file__).resolve().parents[1]
MIGRATION_PATH = BACKEND_DIR / "migrations" / "versions" / "0050_reconciliation.py"

TABLES = [
    "reconciliation.clearing_files",
    "reconciliation.clearing_items",
]


@pytest.fixture()
def db():
    """SQLite en memoria con las 2 tablas de `reconciliation`."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS reconciliation")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def _payload(seed="e5t17", source="NETWORK", day="2026-01-15", n=3, **overrides):
    file, items = build_deterministic_batch(seed=seed, source=source, day=day, items_per_file=n)
    payload = {
        "source": file.source,
        "file_ref": file.file_ref,
        "checksum": file.checksum,
        "item_count": file.item_count,
        "items": [
            {
                "external_ref": item.external_ref,
                "amount_minor": item.amount_minor,
                "currency": item.currency,
                "value_date": item.value_date,
                "direction": item.direction,
                "raw": dict(item.raw),
            }
            for item in items
        ],
    }
    payload.update(overrides)
    return payload


def _count_items(db: Session, file_id) -> int:
    return db.scalars(
        sa.select(sa.func.count()).select_from(ClearingItem).where(ClearingItem.file_id == file_id)
    ).one()


# ------------------------------------------------- migracion (estatica)
def test_migration_chains_head_with_two_tables():
    """`0050` encadena `0049` y crea las 2 tablas con CHECKs/indices."""
    text = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "0050_reconciliation"' in text
    assert 'down_revision = "0049_fx"' in text
    for token in (
        "clearing_files",
        "clearing_items",
        "uq_clearing_files_file_ref",
        "fk_clearing_items_file",
        "ck_clearing_files_status",
        "ck_clearing_files_item_count_nonneg",
        "ck_clearing_items_amount_pos",
        "ck_clearing_items_currency_iso",
        "ck_clearing_items_direction",
        "ck_clearing_items_status",
        "ix_clearing_files_source",
        "ix_clearing_files_status",
        "ix_clearing_items_file_id",
        "ix_clearing_items_external_ref",
        "ix_clearing_items_status",
        "RECEIVED",
        "PROCESSED",
        "FAILED",
        "PENDING",
        "MATCHED",
        "EXCEPTION",
        "INBOUND",
        "OUTBOUND",
        "JSONB",
        "def downgrade",
    ):
        assert token in text, f"migracion incompleta: {token}"
    assert text.count("op.create_table") == 2, "solo las 2 tablas de `reconciliation`"
    for arg in (
        'schema="accounts"',
        'schema="transactions"',
        'schema="ledger"',
        'schema="risk"',
        'schema="fx"',
    ):
        assert arg not in text, f"la migracion no debe tocar {arg}"


def test_migration_has_no_cross_schema_fk():
    """Solo 1 FK, dentro de `reconciliation` (file_id -> clearing_files)."""
    text = MIGRATION_PATH.read_text(encoding="utf-8")
    assert text.count("ForeignKeyConstraint") == 1, "solo file_id lleva FK"
    assert '["reconciliation.clearing_files.id"]' in text


def test_single_head_is_0050():
    """Un solo head tras el brief (`alembic check` limpio lo exige)."""
    from tests.db_utils import get_head_revisions

    assert get_head_revisions() == ("0050_reconciliation",)


# ------------------------------------------------- camino feliz e idempotencia
def test_ingest_valid_batch_creates_processed_file(db: Session):
    """Lote valido -> archivo `PROCESSED` + items `PENDING`."""
    payload = _payload()
    result = recon_service.ingest_clearing_file(db, payload)
    db.commit()
    assert result["status"] == "PROCESSED"
    assert result["inserted"] == 3
    assert result["duplicates"] == 0
    row = recon_repo.get_file_by_ref(db, payload["file_ref"])
    assert row is not None and row.status == "PROCESSED"
    assert row.checksum == payload["checksum"]
    assert _count_items(db, row.id) == 3
    pending = recon_repo.list_pending_items(db, file_id=row.id)
    assert len(pending) == 3
    assert all(item.status == "PENDING" for item in pending)


def test_ingest_is_idempotent_on_replay(db: Session):
    """Mismo lote dos veces -> un archivo y N items (no duplica)."""
    payload = _payload()
    first = recon_service.ingest_clearing_file(db, payload)
    db.commit()
    second = recon_service.ingest_clearing_file(db, payload)
    db.commit()
    assert second["file_id"] == first["file_id"]
    assert second["status"] == "PROCESSED"
    assert second["inserted"] == 0
    assert second["duplicates"] == 3
    row = recon_repo.get_file_by_ref(db, payload["file_ref"])
    assert row is not None
    assert _count_items(db, row.id) == 3


def test_checksum_stable_across_builds():
    """El mock es determinista: mismo lote -> mismo checksum."""
    assert _payload()["checksum"] == _payload()["checksum"]


# ------------------------------------------------- errores -> FAILED sin parciales
def test_checksum_mismatch_marks_failed_without_touching_items(db: Session):
    """Mismo `file_ref` + checksum distinto -> `FAILED`, items intactos."""
    payload = _payload()
    recon_service.ingest_clearing_file(db, payload)
    db.commit()
    tampered = dict(payload, checksum="0" * 64)
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_service.ingest_clearing_file(db, tampered)
    db.commit()
    row = recon_repo.get_file_by_ref(db, payload["file_ref"])
    assert row is not None and row.status == "FAILED"
    assert _count_items(db, row.id) == 3, "no se pisan ni agregan items"


def test_item_count_mismatch_leaves_failed_without_items(db: Session):
    """`item_count` inconsistente -> archivo `FAILED`, sin items."""
    payload = _payload(item_count=99)
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_service.ingest_clearing_file(db, payload)
    db.commit()
    row = recon_repo.get_file_by_ref(db, payload["file_ref"])
    assert row is not None and row.status == "FAILED"
    assert _count_items(db, row.id) == 0


def test_invalid_amount_leaves_failed_without_items(db: Session):
    """Monto <= 0 -> `VALIDATION_ERROR`, archivo `FAILED` sin parciales."""
    payload = _payload()
    payload["items"][0]["amount_minor"] = 0
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_service.ingest_clearing_file(db, payload)
    db.commit()
    row = recon_repo.get_file_by_ref(db, payload["file_ref"])
    assert row is not None and row.status == "FAILED"
    assert _count_items(db, row.id) == 0


def test_invalid_currency_leaves_failed_without_items(db: Session):
    """Moneda invalida -> `VALIDATION_ERROR`, archivo `FAILED` sin parciales."""
    payload = _payload()
    payload["items"][1]["currency"] = "XX"
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_service.ingest_clearing_file(db, payload)
    db.commit()
    row = recon_repo.get_file_by_ref(db, payload["file_ref"])
    assert row is not None and row.status == "FAILED"
    assert _count_items(db, row.id) == 0


def test_invalid_value_date_leaves_failed_without_items(db: Session):
    """Fecha invalida -> `VALIDATION_ERROR`, archivo `FAILED` sin parciales."""
    payload = _payload()
    payload["items"][2]["value_date"] = "no-es-fecha"
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_service.ingest_clearing_file(db, payload)
    db.commit()
    row = recon_repo.get_file_by_ref(db, payload["file_ref"])
    assert row is not None and row.status == "FAILED"
    assert _count_items(db, row.id) == 0


def test_malformed_checksum_leaves_failed_without_items(db: Session):
    """Checksum malformado -> `VALIDATION_ERROR`, archivo `FAILED`."""
    payload = _payload(checksum="corto")
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_service.ingest_clearing_file(db, payload)
    db.commit()
    row = recon_repo.get_file_by_ref(db, payload["file_ref"])
    assert row is not None and row.status == "FAILED"
    assert _count_items(db, row.id) == 0


def test_invalid_file_ref_raises_without_file(db: Session):
    """`file_ref` invalido -> `VALIDATION_ERROR` sin fila (no hay clave)."""
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_service.ingest_clearing_file(db, _payload(file_ref=""))
    db.rollback()
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_service.ingest_clearing_file(db, "no-es-mapeo")
    db.rollback()


# ------------------------------------------------- repositorio
def test_repository_get_create_list_mark(db: Session):
    """`get_file_by_ref` / `create_file_with_items` / `list` / `mark`."""
    assert recon_repo.get_file_by_ref(db, "CLR-NO-EXISTE") is None
    assert recon_repo.get_file_by_ref(db, "") is None
    payload = _payload(seed="repo")
    row = recon_repo.create_file_with_items(
        db,
        source=payload["source"],
        file_ref=payload["file_ref"],
        checksum=payload["checksum"],
        item_count=payload["item_count"],
        items=payload["items"],
    )
    # `flush` sin `commit`: visible en la sesion antes de confirmar.
    assert recon_repo.get_file_by_ref(db, payload["file_ref"]) is not None
    assert row.status == "RECEIVED"
    db.commit()
    assert recon_repo.list_pending_items(db) != []
    assert recon_repo.list_pending_items(db, file_id=row.id) != []
    assert recon_repo.list_pending_items(db, file_id=uuid.uuid4()) == []
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_repo.list_pending_items(db, file_id="no-uuid")
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_repo.list_pending_items(db, limit=0)
    recon_repo.mark_file_status(db, row, "PROCESSED")
    recon_repo.mark_file_status(db, row, "PROCESSED")  # idempotente
    db.commit()
    assert recon_repo.get_file_by_ref(db, payload["file_ref"]).status == "PROCESSED"
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_repo.mark_file_status(db, row, "BORRADO")
    db.rollback()


def test_repository_duplicate_file_conflicts(db: Session):
    """`file_ref` duplicado -> `ReconciliationConflictError` (la UQ decide)."""
    payload = _payload(seed="dupe")
    recon_repo.create_file_with_items(
        db,
        source=payload["source"],
        file_ref=payload["file_ref"],
        checksum=payload["checksum"],
        item_count=payload["item_count"],
        items=payload["items"],
    )
    db.commit()
    with pytest.raises(recon_repo.ReconciliationConflictError):
        recon_repo.create_file_with_items(
            db,
            source=payload["source"],
            file_ref=payload["file_ref"],
            checksum=payload["checksum"],
            item_count=payload["item_count"],
            items=payload["items"],
        )
    db.rollback()


def test_repository_record_failed_file(db: Session):
    """Evidencia tolerante: `FAILED` sin items aunque el lote sea malo."""
    row = recon_repo.record_failed_file(
        db,
        source="NETWORK",
        file_ref="CLR-EVIDENCIA-1",
        checksum="malformado",
        item_count="invalido",
    )
    db.commit()
    assert row.status == "FAILED"
    assert _count_items(db, row.id) == 0
    with pytest.raises(recon_repo.ReconciliationValidationError):
        recon_repo.record_failed_file(db, file_ref="")
    db.rollback()


# ------------------------------------------------- integridad a nivel BD
def test_db_checks_amount_currency_status(db: Session):
    """`amount_minor > 0`, moneda ISO, estados y `direction` tambien en BD."""
    payload = _payload(seed="checks")
    recon_service.ingest_clearing_file(db, payload)
    db.commit()
    row = recon_repo.get_file_by_ref(db, payload["file_ref"])
    assert row is not None
    with pytest.raises(IntegrityError):
        db.execute(
            sa.text(
                "INSERT INTO reconciliation.clearing_items "
                "(id, file_id, external_ref, amount_minor, currency, value_date, "
                "direction, status) VALUES "
                "(:id, :file_id, 'EXT-MALO', 0, 'PEN', '2026-01-15', "
                "'INBOUND', 'PENDING')"
            ),
            {"id": uuid.uuid4().hex, "file_id": row.id.hex},
        )
    db.rollback()
    with pytest.raises(IntegrityError):
        db.execute(
            sa.text(
                "UPDATE reconciliation.clearing_files SET status = 'BORRADO' " "WHERE id = :id"
            ),
            {"id": row.id.hex},
        )
    db.rollback()
    with pytest.raises(IntegrityError):
        db.execute(
            sa.text(
                "INSERT INTO reconciliation.clearing_items "
                "(id, file_id, external_ref, amount_minor, currency, value_date, "
                "direction, status) VALUES "
                "(:id, :file_id, 'EXT-MALO', 100, 'PEN', '2026-01-15', "
                "'SIDEWAYS', 'PENDING')"
            ),
            {"id": uuid.uuid4().hex, "file_id": row.id.hex},
        )
    db.rollback()


def test_db_unique_file_ref(db: Session):
    """UQ `file_ref` tambien en BD (CA-02)."""
    payload = _payload(seed="uq")
    recon_service.ingest_clearing_file(db, payload)
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            sa.text(
                "INSERT INTO reconciliation.clearing_files "
                "(id, source, file_ref, checksum, item_count) VALUES "
                "(:id, 'NETWORK', :ref, :checksum, 0)"
            ),
            {"id": uuid.uuid4().hex, "ref": payload["file_ref"], "checksum": "a" * 64},
        )
    db.rollback()


# ------------------------------------------------- job de una pasada
class _FakeSource:
    """Fuente mock inyectable (lotes en memoria, sin red)."""

    def __init__(self, batches, *, fail_files=False, fail_items_for=()):
        self._batches = list(batches)
        self._fail_files = fail_files
        self._fail_items_for = set(fail_items_for)

    def fetch_files(self, *, since=None, source=None):
        if self._fail_files:
            from app.adapters.clearing_source import ClearingSourceUnavailableError

            raise ClearingSourceUnavailableError("caida simulada")
        return [file for file, _items in self._batches]

    def fetch_items(self, file_ref):
        for file, items in self._batches:
            if file.file_ref == file_ref:
                if file_ref in self._fail_items_for:
                    from app.adapters.clearing_source import ClearingSourceTimeoutError

                    raise ClearingSourceTimeoutError("timeout simulado")
                return list(items)
        from app.adapters.clearing_source import ClearingSourceValidationError

        raise ClearingSourceValidationError("archivo desconocido")


def _batches(seed="job", n=3):
    file, items = build_deterministic_batch(
        seed=seed, source="NETWORK", day="2026-01-15", items_per_file=n
    )
    return [(file, items)]


def test_job_once_processes_and_commits_per_file(db: Session):
    """Una pasada ingiere y confirma (`commit` por archivo)."""
    result = run_clearing_ingest_once(db, create_source=lambda: _FakeSource(_batches()))
    assert result["error"] is None
    assert len(result["processed"]) == 1
    assert result["failed"] == [] and result["skipped"] == []
    row = recon_repo.get_file_by_ref(db, result["processed"][0])
    assert row is not None and row.status == "PROCESSED"
    assert _count_items(db, row.id) == 3


def test_job_second_pass_replays_without_duplicates(db: Session):
    """Segunda pasada: replay sin duplicar (idempotente)."""
    batches = _batches(seed="replay")
    first = run_clearing_ingest_once(db, create_source=lambda: _FakeSource(batches))
    second = run_clearing_ingest_once(db, create_source=lambda: _FakeSource(batches))
    assert first["processed"] == second["processed"]
    row = recon_repo.get_file_by_ref(db, first["processed"][0])
    assert row is not None
    assert _count_items(db, row.id) == 3


def test_job_failed_file_is_committed_as_evidence(db: Session):
    """Lote invalido: `failed` y la fila `FAILED` queda confirmada."""
    file, items = build_deterministic_batch(
        seed="mala", source="NETWORK", day="2026-01-15", items_per_file=3
    )
    from app.adapters.clearing_source import ClearingFilePayload

    broken = ClearingFilePayload(
        source=file.source, file_ref=file.file_ref, checksum=file.checksum, item_count=99
    )
    result = run_clearing_ingest_once(db, create_source=lambda: _FakeSource([(broken, items)]))
    assert result["processed"] == [] and result["skipped"] == []
    assert result["failed"] == [file.file_ref]
    row = recon_repo.get_file_by_ref(db, file.file_ref)
    assert row is not None and row.status == "FAILED"
    assert _count_items(db, row.id) == 0


def test_job_skips_when_source_is_down(db: Session):
    """Proveedor caido: pasada vacia con `error`, sin filas."""
    result = run_clearing_ingest_once(db, create_source=lambda: _FakeSource([], fail_files=True))
    assert result["error"] == "CLEARING_SOURCE_UNAVAILABLE"
    assert result["processed"] == [] and result["failed"] == []
    assert recon_repo.list_pending_items(db) == []


def test_job_skips_missing_items_and_continues(db: Session):
    """Items no disponibles: `skipped` y la pasada continua con el resto."""
    good = _batches(seed="bueno")
    other_file, other_items = build_deterministic_batch(
        seed="omitido", source="NETWORK", day="2026-01-15", items_per_file=2
    )
    batches = good + [(other_file, other_items)]
    result = run_clearing_ingest_once(
        db,
        create_source=lambda: _FakeSource(batches, fail_items_for={other_file.file_ref}),
    )
    assert result["error"] is None
    assert result["processed"] == [good[0][0].file_ref]
    assert result["skipped"] == [other_file.file_ref]
    assert recon_repo.get_file_by_ref(db, other_file.file_ref) is None


def test_job_rejects_invalid_limit(db: Session):
    """Limite invalido -> `VALIDATION_ERROR` sin efectos."""
    with pytest.raises(recon_repo.ReconciliationValidationError):
        run_clearing_ingest_once(db, limit=0, create_source=lambda: _FakeSource([]))
    db.rollback()


# ------------------------------------------------- cableado (patron M-2 de E3-T19)
def test_ingest_flag_off_by_default(monkeypatch):
    """Apagado por defecto: no altera las suites existentes (CA-04)."""
    from app import composition

    monkeypatch.delenv("RECONCILIATION_INGEST_ENABLED", raising=False)
    assert composition.reconciliation_ingest_enabled() is False
    monkeypatch.setenv("RECONCILIATION_INGEST_ENABLED", "1")
    assert composition.reconciliation_ingest_enabled() is True
    monkeypatch.setenv("RECONCILIATION_INGEST_ENABLED", "0")
    assert composition.reconciliation_ingest_enabled() is False


def test_ingest_interval_configurable(monkeypatch):
    """Intervalo fuera del codigo (regla de oro 6)."""
    from app import composition

    monkeypatch.delenv("RECONCILIATION_JOBS_INTERVAL_SECONDS", raising=False)
    assert composition.reconciliation_jobs_interval_seconds() == 300.0
    monkeypatch.setenv("RECONCILIATION_JOBS_INTERVAL_SECONDS", "45")
    assert composition.reconciliation_jobs_interval_seconds() == 45.0


def test_lifespan_does_not_start_ingest_when_disabled(monkeypatch):
    """Flag apagado: el `lifespan` no arranca el worker (CA-04)."""
    from app import composition
    from app.main import app

    def _forbidden(*args, **kwargs):
        raise AssertionError("ingesta inerte con RECONCILIATION_INGEST_ENABLED=0")

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "0")
    monkeypatch.setenv("RECONCILIATION_INGEST_ENABLED", "0")
    monkeypatch.setattr(composition, "start_reconciliation_ingest_worker", _forbidden)

    from fastapi.testclient import TestClient

    with TestClient(app):
        pass


def test_lifespan_starts_ingest_when_enabled(monkeypatch):
    """Flag encendido: el `lifespan` arranca el worker con el intervalo."""
    import threading

    from app import composition
    from app.main import app

    calls: list[dict] = []
    stop_events: list[threading.Event] = []

    def _fake_start_worker(*, interval_seconds=None, stop_event=None, **kwargs):
        event = stop_event or threading.Event()
        stop_events.append(event)
        calls.append({"interval_seconds": interval_seconds, "kwargs": kwargs})
        return object(), event

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "0")
    monkeypatch.setenv("RECONCILIATION_INGEST_ENABLED", "1")
    monkeypatch.setenv("RECONCILIATION_JOBS_INTERVAL_SECONDS", "45")
    monkeypatch.setattr(composition, "start_reconciliation_ingest_worker", _fake_start_worker)

    from fastapi.testclient import TestClient

    with TestClient(app):
        pass

    assert len(calls) == 1
    assert calls[0]["interval_seconds"] == 45.0
    assert calls[0]["kwargs"] == {}
    assert len(stop_events) == 1 and stop_events[0].is_set()


def _load_entrypoint():
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader(
        "worker_entrypoint", str(BACKEND_DIR.parent / "infra" / "docker" / "worker_entrypoint.py")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_entrypoint_inert_when_disabled(monkeypatch, caplog):
    """Worker inerte con el flag apagado (CA-04)."""
    entrypoint = _load_entrypoint()
    monkeypatch.setenv("RECONCILIATION_INGEST_ENABLED", "0")
    with caplog.at_level("INFO", logger="worker"):
        assert entrypoint.maybe_start_reconciliation_ingest() == (None, None)


def test_entrypoint_starts_when_enabled(monkeypatch):
    """Worker arranca con el flag encendido (patron M-2)."""
    import threading

    entrypoint = _load_entrypoint()
    calls: list[dict] = []

    def _fake_start(*, interval_seconds=None, stop_event=None):
        event = stop_event or threading.Event()
        calls.append({"interval_seconds": interval_seconds})
        return object(), event

    monkeypatch.setenv("RECONCILIATION_INGEST_ENABLED", "1")
    monkeypatch.setenv("RECONCILIATION_JOBS_INTERVAL_SECONDS", "45")
    monkeypatch.setattr("app.composition.start_reconciliation_ingest_worker", _fake_start)
    thread, stop = entrypoint.maybe_start_reconciliation_ingest()
    assert thread is not None and stop is not None
    assert calls and calls[0]["interval_seconds"] == 45.0


# ------------------------------------------------- fronteras (reglas de oro)
def test_no_money_movement():
    """`reconciliation` no importa el motor ni el ledger (regla 1/4)."""
    for rel in (
        "app/modules/reconciliation/models/__init__.py",
        "app/modules/reconciliation/repository/__init__.py",
        "app/modules/reconciliation/service/__init__.py",
        "app/modules/reconciliation/jobs/clearing_ingest.py",
    ):
        text = (BACKEND_DIR / rel).read_text(encoding="utf-8")
        for forbidden in (
            "app.modules.transactions",
            "app.modules.ledger",
            "app.modules.accounts",
            "post_entry",
            "journal_entry",
        ):
            assert forbidden not in text, f"frontera rota en {rel}: {forbidden}"


def test_models_registered_without_pii_columns():
    """Ninguna columna de `reconciliation` guarda PII (regla 7)."""
    pii_forbidden = {
        "email",
        "phone",
        "phone_number",
        "document_number",
        "doc_number",
        "document",
        "national_id",
        "full_name",
        "first_name",
        "last_name",
        "business_name",
        "address",
        "password",
        "password_hash",
        "pin",
        "pin_hash",
        "secret",
        "token",
    }
    for table in TABLES:
        assert table in Base.metadata.tables, f"falta {table}"
        cols = {c.name for c in Base.metadata.tables[table].columns}
        assert "id" in cols
        assert not (pii_forbidden & {c.lower() for c in cols}), f"PII en {table}"
    for cls in (ClearingFile, ClearingItem):
        assert "id" in {c.name for c in cls.__table__.columns}


def test_no_pii_nor_secrets_in_logs(db, caplog):
    """Sin PII/secretos en logs: las refs nunca salen en claro (regla 7/9)."""
    import logging

    payload = _payload(seed="logs")
    with caplog.at_level(logging.INFO):
        recon_service.ingest_clearing_file(db, payload)
        db.commit()
    digest = hashlib.sha256(payload["file_ref"].encode()).hexdigest()[:16]
    assert digest in caplog.text or "inserted=3" in caplog.text
    assert payload["file_ref"] not in caplog.text
    assert payload["items"][0]["external_ref"] not in caplog.text
    assert payload["checksum"] not in caplog.text
