"""Job de liberacion de holds vencidos (E5-T06, HU17 CA-02).

- Parte A (sin BD): reglas estaticas (sin float, sin SQL directo, sin
  ledger, outbox perezoso con fallback, via repositorio E5-T02, sin commit).
- Parte B (SQLite en memoria + schema ATTACH): vencido liberado + tx en
  terminal; vigente intacto; reproceso idempotente; sin expires_at intacto;
  lote con limite; tx ya terminal no rompe.
- Parte C (Postgres `db_session`): humo de integracion; se omite si no hay BD.
"""

from __future__ import annotations

import sys
import types
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _install_fake_outbox(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Inyecta `app.core.outbox` fake (firma E5-T05)."""
    calls: list[dict] = []
    fake = types.ModuleType("app.core.outbox")

    def record(session, *, aggregate_type, aggregate_id, event_type, payload):
        calls.append(
            {
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "event_type": event_type,
                "payload": payload,
            }
        )
        return SimpleNamespace(id=uuid.uuid4())

    fake.record = record  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.core.outbox", fake)
    return calls


# ---------------------------------------------------------------- Parte A: sin BD
def test_job_static_rules_no_sql_no_float_lazy_outbox_ledger_via_facade():
    path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "modules"
        / "transactions"
        / "jobs"
        / "release_expired_holds.py"
    )
    content = path.read_text(encoding="utf-8")
    assert "float(" not in content, "dinero entero en centimos, nunca float"
    assert "publish(" not in content, "regla 8: eventos solo por outbox"
    assert "    from app.core.outbox import record as outbox_record" in content
    assert "except ImportError" in content, "fallback si E5-T05 pendiente"
    for token in (
        "list_active_holds",
        "update_hold_status",
        "transition_transaction",
        "get_transaction",
    ):
        assert token in content, f"el job debe ir via repositorio E5-T02: {token}"
    # Ledger solo por su fachada (mismo patron que E5-T03): compensatorio
    # HOLD_RELEASE 2100 -> 2000 (docs/04#7.3) + guarda de idempotencia.
    assert "ensure_customer_accounts" in content
    assert (
        "ledger_service.post_entry" in content or "ledger.service.post_entry" in content
    ), "el compensatorio va por la fachada ledger"
    assert "HOLD_RELEASE" in content
    assert "hold.id" in content, "el hold.id va en la descripcion (idempotencia)"
    for pattern in (
        "session.execute",
        "session.commit",
        "sa.delete",
        "session.delete",
        "JournalEntry(",
        "Posting(",
        "journal_entries",
        ".postings",
        "reverse_entry",
    ):
        assert pattern not in content, f"prohibido en el job: {pattern}"
    assert "release_expired_holds" in content
    assert "limit" in content, "el job procesa en lote con limite"


# ---------------------------------------------------------------- Parte B: SQLite
@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schemas `transactions` + `ledger` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.ledger.models as _lm  # noqa: F401 (registro)
    import app.modules.transactions.models as m

    assert _lm is not None  # evita unused-import en lint estricto
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS transactions")
        cur.execute("ATTACH DATABASE ':memory:' AS ledger")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            m.Transaction.__table__,
            m.TransactionStatusHistory.__table__,
            m.Hold.__table__,
            _lm.LedgerAccount.__table__,
            _lm.JournalEntry.__table__,
            _lm.Posting.__table__,
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_posted_tx_with_hold(session: Session, *, expires_at, amount_minor=1500):
    from app.modules.transactions import repository as repo

    tx = repo.create_transaction(
        session, type="OWN_TRANSFER", amount_minor=amount_minor, currency="PEN"
    )
    for target in ("VALIDATED", "AUTHORIZED", "FUNDS_HELD", "POSTED"):
        repo.transition_transaction(session, tx.id, target)
    hold = repo.create_hold(
        session,
        transaction_id=tx.id,
        account_id=uuid.uuid4(),
        amount_minor=amount_minor,
        currency="PEN",
        expires_at=expires_at,
    )
    return tx, hold


def _seed_hold_entry(session: Session, tx, hold):
    """Asiento original de retencion `2000 -> 2100` via fachada ledger."""
    from app.modules.ledger import service as ledger_service

    avail, retained = ledger_service.ensure_customer_accounts(
        session, hold.account_id, hold.currency
    )
    ledger_service.post_entry(
        session,
        entry_type="TRANSFER_HOLD",
        transaction_id=tx.id,
        description=f"hold {tx.id}",
        postings=[
            {
                "ledger_account_id": avail.id,
                "direction": "DEBIT",
                "amount_minor": hold.amount_minor,
                "currency": hold.currency,
                "account_ref": hold.account_id,
            },
            {
                "ledger_account_id": retained.id,
                "direction": "CREDIT",
                "amount_minor": hold.amount_minor,
                "currency": hold.currency,
                "account_ref": hold.account_id,
            },
        ],
    )
    return avail, retained


def _hold_release_entries(session: Session, tx_id):
    from app.modules.ledger.models import JournalEntry

    return list(
        session.scalars(
            sa.select(JournalEntry).where(
                JournalEntry.entry_type == "HOLD_RELEASE",
                JournalEntry.transaction_id == tx_id,
            )
        ).all()
    )


def _ledger_totals(session: Session):
    """Totales globales Debe/Haber sobre todos los postings (cuadre)."""
    from app.modules.ledger.models import Posting

    rows = session.scalars(sa.select(Posting)).all()
    debit = sum(r.amount_minor for r in rows if r.direction == "DEBIT")
    credit = sum(r.amount_minor for r in rows if r.direction == "CREDIT")
    return debit, credit, rows


def _count(session: Session, key: str) -> int:
    return session.scalar(
        sa.select(sa.func.count()).select_from(Base.metadata.tables[key])
    )


def test_expired_hold_released_and_tx_failed(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from app.core import events as domain_events
    from app.modules.transactions import repository as repo
    from app.modules.transactions.jobs.release_expired_holds import release_expired_holds

    outbox_calls = _install_fake_outbox(monkeypatch)
    tx, hold = _make_posted_tx_with_hold(
        sqlite_session, expires_at=_utcnow() - timedelta(seconds=1)
    )

    processed = release_expired_holds(sqlite_session, now=_utcnow())
    assert [h.id for h in processed] == [hold.id]
    assert hold.status == "EXPIRED" and hold.released_at is not None
    assert tx.status == "FAILED"  # terminal 04#3 (FAILED siempre libera holds)
    assert [h.to_status for h in repo.list_history(sqlite_session, tx.id)] == [
        "INITIATED",
        "VALIDATED",
        "AUTHORIZED",
        "FUNDS_HELD",
        "POSTED",
        "FAILED",
    ]
    released = [c for c in outbox_calls if c["event_type"] == domain_events.FUNDS_RELEASED]
    assert len(released) == 1
    assert released[0]["aggregate_id"] == tx.id
    assert released[0]["payload"]["hold_id"] == str(hold.id)
    assert released[0]["payload"]["status"] == "FAILED"


def test_active_hold_untouched(sqlite_session: Session, monkeypatch: pytest.MonkeyPatch):
    from app.modules.transactions import repository as repo
    from app.modules.transactions.jobs.release_expired_holds import release_expired_holds

    _install_fake_outbox(monkeypatch)
    tx, hold = _make_posted_tx_with_hold(
        sqlite_session, expires_at=_utcnow() + timedelta(hours=1)
    )

    assert release_expired_holds(sqlite_session, now=_utcnow()) == []
    assert hold.status == "ACTIVE" and hold.released_at is None
    assert tx.status == "POSTED"
    assert len(repo.list_history(sqlite_session, tx.id)) == 5


def test_reprocess_is_idempotent(sqlite_session: Session, monkeypatch: pytest.MonkeyPatch):
    from app.modules.transactions import repository as repo
    from app.modules.transactions.jobs.release_expired_holds import release_expired_holds

    outbox_calls = _install_fake_outbox(monkeypatch)
    tx, hold = _make_posted_tx_with_hold(
        sqlite_session, expires_at=_utcnow() - timedelta(seconds=1)
    )

    first = release_expired_holds(sqlite_session, now=_utcnow())
    assert [h.id for h in first] == [hold.id]
    hist_len = len(repo.list_history(sqlite_session, tx.id))
    n_events = len(outbox_calls)

    second = release_expired_holds(sqlite_session, now=_utcnow())
    assert second == []  # segunda corrida no cambia nada ni duplica
    assert hold.status == "EXPIRED" and tx.status == "FAILED"
    assert len(repo.list_history(sqlite_session, tx.id)) == hist_len
    assert len(outbox_calls) == n_events


def test_hold_without_expiry_never_touched(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from app.modules.transactions import repository as repo
    from app.modules.transactions.jobs.release_expired_holds import release_expired_holds

    _install_fake_outbox(monkeypatch)
    tx, hold = _make_posted_tx_with_hold(sqlite_session, expires_at=None)

    assert release_expired_holds(sqlite_session, now=_utcnow()) == []
    assert hold.status == "ACTIVE"
    assert tx.status == "POSTED"
    assert len(repo.list_history(sqlite_session, tx.id)) == 5


def test_processes_in_batches_with_limit(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from app.modules.transactions.jobs.release_expired_holds import release_expired_holds

    _install_fake_outbox(monkeypatch)
    past = _utcnow() - timedelta(minutes=5)
    pairs = [_make_posted_tx_with_hold(sqlite_session, expires_at=past) for _ in range(3)]

    first = release_expired_holds(sqlite_session, now=_utcnow(), limit=2)
    assert len(first) == 2
    remaining = [h for _, h in pairs if h.status == "ACTIVE"]
    assert len(remaining) == 1

    second = release_expired_holds(sqlite_session, now=_utcnow(), limit=2)
    assert [h.id for h in second] == [h.id for h in remaining]
    assert all(h.status == "EXPIRED" for _, h in pairs)

    with pytest.raises(ValueError):
        release_expired_holds(sqlite_session, limit=0)


def test_terminal_tx_hold_still_expires_without_crash(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """Tx ya terminal + hold ACTIVE vencido: el hold queda terminal, la tx intacta."""
    from app.modules.transactions import repository as repo
    from app.modules.transactions.jobs.release_expired_holds import release_expired_holds

    _install_fake_outbox(monkeypatch)
    tx, hold = _make_posted_tx_with_hold(
        sqlite_session, expires_at=_utcnow() - timedelta(seconds=1)
    )
    repo.transition_transaction(session=sqlite_session, tx_id=tx.id, target="FAILED")
    assert tx.status == "FAILED"

    processed = release_expired_holds(sqlite_session, now=_utcnow())
    assert [h.id for h in processed] == [hold.id]
    assert hold.status == "EXPIRED"  # nunca huerfano: terminal aunque la tx ya lo era
    assert tx.status == "FAILED"
    # Sin doble FAILED: un solo historial hacia FAILED.
    assert [h.to_status for h in repo.list_history(sqlite_session, tx.id)].count("FAILED") == 1


def test_expired_hold_posts_release_entry_2100_to_2000(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """Vencido -> compensatorio Debe 2100/Haber 2000 por el monto exacto."""
    from app.modules.ledger import service as ledger_service
    from app.modules.ledger.models import JournalEntry
    from app.modules.transactions.jobs.release_expired_holds import release_expired_holds

    _install_fake_outbox(monkeypatch)
    tx, hold = _make_posted_tx_with_hold(
        sqlite_session,
        expires_at=_utcnow() - timedelta(seconds=1),
        amount_minor=2500,
    )
    avail, retained = _seed_hold_entry(sqlite_session, tx, hold)

    processed = release_expired_holds(sqlite_session, now=_utcnow())
    assert [h.id for h in processed] == [hold.id]
    assert hold.status == "EXPIRED" and tx.status == "FAILED"

    releases = _hold_release_entries(sqlite_session, tx.id)
    assert len(releases) == 1
    assert str(hold.id) in (releases[0].description or "")
    legs = {
        (str(r.ledger_account_id), r.direction): r
        for r in ledger_service.list_postings(sqlite_session, releases[0].id)
    }
    assert set(legs) == {(str(retained.id), "DEBIT"), (str(avail.id), "CREDIT")}
    assert legs[(str(retained.id), "DEBIT")].amount_minor == 2500
    assert legs[(str(avail.id), "CREDIT")].amount_minor == 2500
    assert {r.currency for r in legs.values()} == {"PEN"}
    # Neto por cuenta: la retencion y la liberacion se cancelan.
    entries = list(
        sqlite_session.scalars(
            sa.select(JournalEntry).where(JournalEntry.transaction_id == tx.id)
        ).all()
    )
    assert len(entries) == 2
    net = {str(avail.id): 0, str(retained.id): 0}
    for table_entry in entries:
        for r in ledger_service.list_postings(sqlite_session, table_entry.id):
            net[str(r.ledger_account_id)] += (
                r.amount_minor if r.direction == "DEBIT" else -r.amount_minor
            )
    assert net[str(avail.id)] == 0 and net[str(retained.id)] == 0


def test_ledger_stays_balanced_after_release(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """Tras compensar, debitos == creditos global (partida doble)."""
    from app.modules.transactions.jobs.release_expired_holds import release_expired_holds

    _install_fake_outbox(monkeypatch)
    tx, hold = _make_posted_tx_with_hold(
        sqlite_session, expires_at=_utcnow() - timedelta(seconds=1)
    )
    _seed_hold_entry(sqlite_session, tx, hold)

    release_expired_holds(sqlite_session, now=_utcnow())
    debit, credit, rows = _ledger_totals(sqlite_session)
    assert len(rows) == 4  # 2 retencion + 2 liberacion
    assert debit == credit > 0


def test_reprocess_does_not_duplicate_release_entry(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """Reproceso: ningun segundo compensatorio para el mismo hold."""
    from app.modules.transactions.jobs.release_expired_holds import release_expired_holds

    _install_fake_outbox(monkeypatch)
    tx, hold = _make_posted_tx_with_hold(
        sqlite_session, expires_at=_utcnow() - timedelta(seconds=1)
    )
    _seed_hold_entry(sqlite_session, tx, hold)

    assert len(release_expired_holds(sqlite_session, now=_utcnow())) == 1
    assert len(_hold_release_entries(sqlite_session, tx.id)) == 1
    debit_before, credit_before, _ = _ledger_totals(sqlite_session)

    assert release_expired_holds(sqlite_session, now=_utcnow()) == []
    assert len(_hold_release_entries(sqlite_session, tx.id)) == 1
    debit_after, credit_after, _ = _ledger_totals(sqlite_session)
    assert (debit_after, credit_after) == (debit_before, credit_before)


def test_hold_release_guard_reads_via_ledger_facade(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """El guard de idempotencia lee por la fachada ledger (regla de oro 4)."""
    from app.modules.ledger import service as ledger_service
    from app.modules.transactions.jobs.release_expired_holds import release_expired_holds

    _install_fake_outbox(monkeypatch)
    tx, hold = _make_posted_tx_with_hold(
        sqlite_session, expires_at=_utcnow() - timedelta(seconds=1)
    )
    _seed_hold_entry(sqlite_session, tx, hold)

    job_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "modules"
        / "transactions"
        / "jobs"
        / "release_expired_holds.py"
    )
    assert "ledger.models" not in job_path.read_text(encoding="utf-8")

    assert len(release_expired_holds(sqlite_session, now=_utcnow())) == 1
    found = ledger_service.find_hold_release(
        sqlite_session, transaction_id=tx.id, hold_id=hold.id
    )
    assert found is not None and found.entry_type == "HOLD_RELEASE"

    assert release_expired_holds(sqlite_session, now=_utcnow()) == []
    assert len(_hold_release_entries(sqlite_session, tx.id)) == 1


def test_release_entry_failure_raises_and_rolls_back_cleanly(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """Si el compensatorio falla: raise con contexto, sin estado parcial."""
    from app.modules.ledger import service as ledger_service
    from app.modules.transactions.jobs.release_expired_holds import release_expired_holds

    _install_fake_outbox(monkeypatch)
    tx, hold = _make_posted_tx_with_hold(
        sqlite_session, expires_at=_utcnow() - timedelta(seconds=1)
    )
    _seed_hold_entry(sqlite_session, tx, hold)

    def _boom(session, **kwargs):
        raise RuntimeError("ledger caido")

    monkeypatch.setattr(ledger_service, "post_entry", _boom)
    with pytest.raises(RuntimeError, match="HOLD_RELEASE"):
        release_expired_holds(sqlite_session, now=_utcnow())
    sqlite_session.rollback()
    # Rollback total del llamante (mismo patron que E5-T03): sin estados ni
    # asientos residuales; el reintento sigue disponible (job idempotente).
    assert _count(sqlite_session, "transactions.transactions") == 0
    assert _count(sqlite_session, "transactions.holds") == 0
    assert _count(sqlite_session, "ledger.journal_entries") == 0
    assert _count(sqlite_session, "ledger.postings") == 0


# ---------------------------------------------------------------- Parte C: Postgres
def test_pg_hold_expiry_smoke(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    from app.modules.transactions import repository as repo
    from app.modules.transactions.jobs.release_expired_holds import release_expired_holds

    _install_fake_outbox(monkeypatch)
    tx = repo.create_transaction(
        db_session, type="OWN_TRANSFER", amount_minor=2000, currency="PEN"
    )
    for target in ("VALIDATED", "AUTHORIZED", "FUNDS_HELD", "POSTED"):
        repo.transition_transaction(db_session, tx.id, target)
    hold = repo.create_hold(
        db_session,
        transaction_id=tx.id,
        account_id=uuid.uuid4(),
        amount_minor=2000,
        currency="PEN",
        expires_at=_utcnow() - timedelta(seconds=1),
    )
    processed = release_expired_holds(db_session, now=_utcnow())
    assert [h.id for h in processed] == [hold.id]
    assert hold.status == "EXPIRED" and tx.status == "FAILED"
    # Reproceso seguro tambien en Postgres.
    assert release_expired_holds(db_session, now=_utcnow()) == []
