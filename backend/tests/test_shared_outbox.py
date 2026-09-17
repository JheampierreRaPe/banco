"""Outbox transversal + worker + bus interno (E5-T05, HU17).

- Parte A (sin BD): metadatos de modelos y migracion segun `03b#2.2/#2.3`,
  bus en memoria, backoff puro y reglas (sin `commit` en `record`, sin
  publicar dentro de la transaccion, sin `float`, sin tocar otros schemas).
- Parte B (SQLite en memoria + schema ATTACH): el evento se persiste junto
  al cambio en la misma transaccion (rollback revierte ambos);
  `publish_pending` publica cada pendiente una sola vez (claim atomico);
  consumidor idempotente (`processed_events`); FAILED tras N intentos con
  backoff; orden por agregado.
- Parte C (Postgres `db_session`): humo de integracion; se omite si no hay BD.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base


class _FakeSession:
    """Sesion minima para validar sin BD (add + flush, sin commit)."""

    def __init__(self) -> None:
        self.added: list = []
        self.flushed = 0
        self.committed = False

    def add(self, row) -> None:
        self.added.append(row)

    def flush(self) -> None:
        self.flushed += 1

    def commit(self) -> None:  # pragma: no cover - no debe llamarse
        self.committed = True


# ---------------------------------------------------------------- Parte A: modelo
def test_tables_registered_with_schema_columns_constraints_indexes():
    import app.modules.shared.models as m  # noqa: F401 (registro)

    for key in ("shared.outbox", "shared.processed_events"):
        assert key in Base.metadata.tables, f"falta tabla {key}"

    outbox = Base.metadata.tables["shared.outbox"]
    assert outbox.schema == "shared"
    cols = {c.name for c in outbox.columns}
    for expected in (
        "id",
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "payload",
        "status",
        "attempts",
        "available_at",
        "created_at",
        "published_at",
    ):
        assert expected in cols, f"falta columna outbox.{expected}"
    assert isinstance(outbox.columns["payload"].type, sa.JSON)
    assert isinstance(outbox.columns["attempts"].type, sa.SmallInteger)
    checks = {c.name for c in outbox.constraints if isinstance(c, sa.CheckConstraint)}
    assert "ck_outbox_status" in checks
    idx = {i.name for i in outbox.indexes}
    for expected in (
        "ix_outbox_event_type",
        "ix_outbox_aggregate_id",
        "ix_outbox_status",
        "ix_outbox_available_at",
        "ix_outbox_status_available_at",
    ):
        assert expected in idx, f"falta indice {expected}"

    inbox = Base.metadata.tables["shared.processed_events"]
    assert inbox.schema == "shared"
    cols = {c.name for c in inbox.columns}
    assert cols == {"event_id", "consumer", "processed_at"}
    pk = {c.name for c in inbox.primary_key.columns}
    assert pk == {"event_id", "consumer"}, "PK compuesta (event_id, consumer)"


def test_no_foreign_keys_to_other_schemas():
    for key in ("shared.outbox", "shared.processed_events"):
        table = Base.metadata.tables[key]
        assert not list(table.foreign_keys), f"{key} no debe tener FK fisicas"
    outbox = Base.metadata.tables["shared.outbox"]
    assert not outbox.columns["aggregate_id"].foreign_keys, (
        "aggregate_id debe ser UUID logico sin FK fisica a otro schema"
    )


def test_migration_0005_exists_and_matches_models():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0005_shared_outbox.py"
    )
    assert path.exists(), "falta migracion 0005_shared_outbox.py"
    content = path.read_text(encoding="utf-8")
    for token in (
        'revision = "0005_shared_outbox"',
        'down_revision = "0004_ledger_double_entry"',
        '"outbox"',
        '"processed_events"',
        '"shared"',
        "ck_outbox_status",
        "ix_outbox_event_type",
        "ix_outbox_aggregate_id",
        "ix_outbox_status",
        "ix_outbox_available_at",
        "ix_outbox_status_available_at",
    ):
        assert token in content, f"migracion sin {token}"
    for foreign in ("ledger.", "transactions.", "accounts.", "identity."):
        assert foreign not in content, f"la migracion no debe tocar {foreign}"


def test_record_signature_is_stable_contract():
    import inspect

    from app.core.outbox import fetch_pending, mark_failed, mark_published, record

    sig = inspect.signature(record)
    params = list(sig.parameters.values())
    assert params[0].name == "session"
    rest = {p.name for p in params[1:]}
    assert rest == {"aggregate_type", "aggregate_id", "event_type", "payload"}
    for p in params[1:]:
        assert p.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{p.name} debe ser keyword-only"
        )
    # Import perezoso (contrato E5-T03) sin efectos laterales.
    assert callable(fetch_pending) and callable(mark_published) and callable(mark_failed)


def test_record_validates_without_db_and_flushes_without_commit():
    from app.core.outbox import record

    session = _FakeSession()
    entry = record(
        session,
        aggregate_type="transaction",
        aggregate_id=uuid.uuid4(),
        event_type="transfer.settled",
        payload={"amount_minor": 10_000},
    )
    assert entry.status == "PENDING"
    assert entry.attempts == 0
    assert session.flushed == 1 and not session.committed
    # aggregate_id acepta str (UUID en texto).
    as_str = record(
        _FakeSession(),
        aggregate_type="transaction",
        aggregate_id=str(uuid.uuid4()),
        event_type="transfer.settled",
        payload={},
    )
    assert isinstance(as_str.aggregate_id, uuid.UUID)

    bad_session = _FakeSession()
    with pytest.raises(ValueError):
        record(
            bad_session,
            aggregate_type="transaction",
            aggregate_id="no-es-uuid",
            event_type="transfer.settled",
            payload={},
        )
    with pytest.raises(ValueError):
        record(
            bad_session,
            aggregate_type="transaction",
            aggregate_id=uuid.uuid4(),
            event_type="transfer.settled",
            payload=["no-dict"],
        )
    with pytest.raises(ValueError):
        record(
            bad_session,
            aggregate_type="transaction",
            aggregate_id=uuid.uuid4(),
            event_type="transfer.settled",
            payload={"bad": object()},
        )
    with pytest.raises(ValueError):
        record(
            bad_session,
            aggregate_type="",
            aggregate_id=uuid.uuid4(),
            event_type="transfer.settled",
            payload={},
        )
    assert bad_session.added == [], "lo invalido no deja filas residuales"


def test_backoff_is_exponential():
    from app.modules.shared.repository import compute_backoff_seconds

    assert compute_backoff_seconds(1, 60) == 60
    assert compute_backoff_seconds(2, 60) == 120
    assert compute_backoff_seconds(3, 60) == 240
    with pytest.raises(ValueError):
        compute_backoff_seconds(0, 60)


def test_bus_registers_handlers_per_event_type_and_dispatches():
    from app.core.bus import EventBus

    bus = EventBus()
    seen: list = []
    bus.subscribe("transfer.settled", lambda e: seen.append(("a", e.event_type)))
    bus.subscribe("transfer.settled", lambda e: seen.append(("b", e.event_type)))

    class _Entry:
        event_type = "transfer.settled"

    assert bus.handler_count("transfer.settled") == 2
    assert bus.dispatch(_Entry()) == 2
    assert [s[0] for s in seen] == ["a", "b"]

    class _Other:
        event_type = "kyc.completed"

    assert bus.dispatch(_Other()) == 0  # sin handlers: entregado, sin error

    def _boom(entry):
        raise RuntimeError("fallo consumidor")

    bus.subscribe("transfer.settled", _boom)
    with pytest.raises(RuntimeError):
        bus.dispatch(_Entry())


def test_outbox_modules_have_no_commit_publish_float_nor_business_logic():
    base = Path(__file__).resolve().parents[1]
    for rel in (
        Path("app/core/outbox.py"),
        Path("app/core/bus.py"),
        Path("app/modules/shared/repository/outbox.py"),
        Path("app/modules/shared/jobs/publisher.py"),
        Path("app/modules/shared/models/__init__.py"),
    ):
        content = (base / rel).read_text(encoding="utf-8")
        assert ".commit(" not in content, f"{rel} no debe hacer commit"
        assert "float(" not in content, f"{rel} sin atajos float"
    repo_content = (base / "app/modules/shared/repository/outbox.py").read_text(
        encoding="utf-8"
    )
    assert "session.delete" not in repo_content
    core_content = (base / "app/core/outbox.py").read_text(encoding="utf-8")
    assert "publish_pending" not in core_content, (
        "`record` no publica: el worker publica DESPUES"
    )


# ---------------------------------------------------------------- Parte B: SQLite
@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schema `shared` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.shared.models as m  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS shared")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["shared.processed_events"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _naive(moment: datetime) -> datetime:
    """SQLite devuelve datetimes naive y Postgres aware: comparar sin tz."""
    return moment.replace(tzinfo=None) if moment.tzinfo is not None else moment


def _pending_count(session: Session) -> int:
    return session.scalar(
        sa.select(sa.func.count()).select_from(Base.metadata.tables["shared.outbox"])
    )


def test_event_persisted_with_business_change_and_rollback_reverts_both(
    sqlite_session: Session,
):
    from app.core.outbox import record

    record(
        sqlite_session,
        aggregate_type="transaction",
        aggregate_id=uuid.uuid4(),
        event_type="transfer.settled",
        payload={"amount_minor": 10_000},
    )
    assert _pending_count(sqlite_session) == 1
    sqlite_session.rollback()  # el negocio falla: revierte cambio + outbox
    assert _pending_count(sqlite_session) == 0


def test_publish_pending_publishes_each_once(sqlite_session: Session):
    from app.core.bus import EventBus
    from app.core.outbox import fetch_pending, record
    from app.modules.shared.jobs import publish_pending

    bus = EventBus()
    delivered: list = []
    bus.subscribe("transfer.settled", lambda e: delivered.append(e.id))
    for _ in range(2):
        record(
            sqlite_session,
            aggregate_type="transaction",
            aggregate_id=uuid.uuid4(),
            event_type="transfer.settled",
            payload={"amount_minor": 100},
        )
    result = publish_pending(sqlite_session, bus=bus)
    assert result == {"published": 2, "failed": 0, "skipped": 0}
    assert len(delivered) == 2 and len(set(delivered)) == 2
    assert fetch_pending(sqlite_session) == []  # nada pendiente
    again = publish_pending(sqlite_session, bus=bus)
    assert again["published"] == 0 and len(delivered) == 2, "no duplica"


def test_worker_retry_uses_atomic_claim_without_duplicating(sqlite_session: Session):
    from app.core.bus import EventBus
    from app.core.outbox import record
    from app.modules.shared.jobs import publish_pending

    bus = EventBus()
    calls: list = []

    def _flaky(entry):
        calls.append(entry.id)
        if len(calls) == 1:
            raise RuntimeError("caida transitoria")

    bus.subscribe("transfer.settled", _flaky)
    entry = record(
        sqlite_session,
        aggregate_type="transaction",
        aggregate_id=uuid.uuid4(),
        event_type="transfer.settled",
        payload={},
    )
    entry_id = entry.id
    first = publish_pending(sqlite_session, bus=bus, backoff_base_seconds=60)
    assert first["failed"] == 1 and len(calls) == 1
    # Backoff: el reintento inmediato se omite (claim no disponible).
    second = publish_pending(sqlite_session, bus=bus, backoff_base_seconds=60)
    assert second == {"published": 0, "failed": 0, "skipped": 0}
    assert len(calls) == 1, "sin reclamo duplicado durante el backoff"
    # Vencido el backoff, reintenta y publica exactamente una vez mas.
    from app.modules.shared.models import OutboxEntry

    stored = sqlite_session.get(OutboxEntry, entry_id)
    assert stored is not None and stored.status == "PENDING" and stored.attempts == 1
    assert _naive(stored.available_at) > _naive(stored.created_at), (
        "backoff difiere la entrega"
    )
    stored.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    sqlite_session.flush()
    third = publish_pending(sqlite_session, bus=bus, backoff_base_seconds=60)
    assert third["published"] == 1 and len(calls) == 2


def test_consumer_is_idempotent(sqlite_session: Session):
    from app.modules.shared.repository import is_processed, try_mark_processed

    event_id = uuid.uuid4()
    assert try_mark_processed(
        sqlite_session, event_id=event_id, consumer="notifications"
    ) is True
    assert try_mark_processed(
        sqlite_session, event_id=event_id, consumer="notifications"
    ) is False, "segundo consumo se ignora"
    assert is_processed(sqlite_session, event_id=event_id, consumer="notifications")
    # Otro consumidor si procesa el mismo evento.
    assert try_mark_processed(sqlite_session, event_id=event_id, consumer="audit")
    assert not is_processed(sqlite_session, event_id=uuid.uuid4(), consumer="audit")


def test_failed_after_n_attempts_with_backoff(sqlite_session: Session):
    from app.core.bus import EventBus
    from app.core.outbox import record
    from app.modules.shared.jobs import publish_pending
    from app.modules.shared.models import OutboxEntry

    bus = EventBus()
    bus.subscribe("transfer.settled", lambda e: (_ for _ in ()).throw(RuntimeError("x")))
    entry = record(
        sqlite_session,
        aggregate_type="transaction",
        aggregate_id=uuid.uuid4(),
        event_type="transfer.settled",
        payload={},
    )
    entry_id = entry.id
    for attempt in range(1, 4):
        stored = sqlite_session.get(OutboxEntry, entry_id)
        stored.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        sqlite_session.flush()
        result = publish_pending(
            sqlite_session, bus=bus, max_attempts=3, backoff_base_seconds=60
        )
        stored = sqlite_session.get(OutboxEntry, entry_id)
        if attempt < 3:
            assert result["failed"] == 1
            assert stored.status == "PENDING" and stored.attempts == attempt
        else:
            assert stored.status == "FAILED" and stored.attempts == 3
    # Terminal: ya no se reintenta.
    result = publish_pending(sqlite_session, bus=bus, max_attempts=3)
    assert result == {"published": 0, "failed": 0, "skipped": 0}


def test_pending_ordered_by_aggregate(sqlite_session: Session):
    from app.core.outbox import fetch_pending, record

    agg_b, agg_a = uuid.uuid4(), uuid.uuid4()
    if str(agg_a) > str(agg_b):
        agg_a, agg_b = agg_b, agg_a
    record(
        sqlite_session,
        aggregate_type="transaction",
        aggregate_id=agg_b,
        event_type="transfer.settled",
        payload={"n": 1},
    )
    record(
        sqlite_session,
        aggregate_type="transaction",
        aggregate_id=agg_a,
        event_type="transfer.settled",
        payload={"n": 2},
    )
    pending = fetch_pending(sqlite_session)
    assert [e.aggregate_id for e in pending] == [agg_a, agg_b]


# ---------------------------------------------------------------- Parte C: Postgres
def test_integration_postgres_outbox_smoke(db_session: Session):
    from app.core.bus import EventBus
    from app.core.outbox import record
    from app.modules.shared.jobs import publish_pending
    from app.modules.shared.repository import is_processed, try_mark_processed

    bus = EventBus()
    delivered: list = []
    bus.subscribe("transfer.settled", lambda e: delivered.append(e.id))
    entry = record(
        db_session,
        aggregate_type="transaction",
        aggregate_id=uuid.uuid4(),
        event_type="transfer.settled",
        payload={"amount_minor": 5000},
    )
    result = publish_pending(db_session, bus=bus)
    assert result["published"] == 1 and delivered == [entry.id]
    assert try_mark_processed(
        db_session, event_id=entry.id, consumer="notifications"
    )
    assert not try_mark_processed(
        db_session, event_id=entry.id, consumer="notifications"
    )
    assert is_processed(db_session, event_id=entry.id, consumer="notifications")
