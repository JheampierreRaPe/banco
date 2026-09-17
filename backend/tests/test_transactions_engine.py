"""Motor transaccional: saldo, bloqueo y holds (E5-T03, HU17 CA-01/02/04).

- Parte A (sin BD): validacion pura, puerto por defecto pendiente (E2-T01),
  reglas estaticas (sin float, sin publish directo, ledger por fachada,
  outbox perezoso con fallback E5-T05).
- Parte B (SQLite en memoria + schemas ATTACH): feliz (asiento + hold +
  estados), saldo insuficiente -> REJECTED sin movimientos, fallo
  intermedio -> rollback total, hold 2000->2100 con total invariante,
  bloqueo en orden estable, outbox fake inyectado.
- Parte C (Postgres `db_session`): humo de integracion; se omite si no hay BD.
"""

from __future__ import annotations

import re
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base


# ---------------------------------------------------------------- Helpers
class FakeBalances:
    """Port en memoria (E2-T01 pendiente: el adaptador real lo provee)."""

    def __init__(self, funds: dict[str, dict] | None = None):
        self.funds: dict[str, dict] = dict(funds or {})
        self.lock_order: list[str] = []
        self.deltas: list[tuple[str, int]] = []

    def lock_and_get(self, session: Session, account_id):
        self.lock_order.append(str(account_id))
        cur = self.funds.get(str(account_id), {"available_minor": 0, "currency": "PEN"})
        return SimpleNamespace(
            available_minor=cur["available_minor"],
            currency=cur["currency"],
            account_id=account_id,
        )

    def apply_delta(self, session: Session, account_id, delta_minor: int, currency: str):
        assert isinstance(delta_minor, int) and not isinstance(delta_minor, bool)
        cur = self.funds.setdefault(str(account_id), {"available_minor": 0, "currency": currency})
        cur["available_minor"] += delta_minor
        cur["currency"] = currency
        self.deltas.append((str(account_id), delta_minor))


def _install_fake_outbox(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Inyecta `app.core.outbox` fake (no depende del modulo real E5-T05)."""
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


def _sqlite_engine_with_schemas():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.ledger.models as _lm  # noqa: F401 (registro)
    import app.modules.transactions.models as _tm  # noqa: F401 (registro)

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
            Base.metadata.tables["transactions.transactions"],
            Base.metadata.tables["transactions.transaction_status_history"],
            Base.metadata.tables["transactions.holds"],
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["ledger.journal_entries"],
            Base.metadata.tables["ledger.postings"],
        ],
    )
    return engine


def _count(session: Session, key: str) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[key]))


def _postings_of(session: Session, entry_id) -> list:
    from app.modules.ledger import service as ledger_service

    return ledger_service.list_postings(session, entry_id)


# ---------------------------------------------------------------- Parte A: sin BD
def test_rejects_bad_input_without_touching_db():
    from app.modules.transactions import service as svc

    port = FakeBalances()
    session = SimpleNamespace()  # type: ignore[arg-type]
    src, dst = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(ValueError):
        svc.execute_transfer(
            session,
            source_account_id=src,
            target_account_id=dst,
            amount_minor=0,
            currency="PEN",
            balance_port=port,
        )
    with pytest.raises(TypeError):
        svc.execute_transfer(
            session,
            source_account_id=src,
            target_account_id=dst,
            amount_minor=10.5,
            currency="PEN",
            balance_port=port,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        svc.execute_transfer(
            session,
            source_account_id=src,
            target_account_id=dst,
            amount_minor=100,
            currency="pen",
            balance_port=port,
        )
    with pytest.raises(ValueError, match="distintas"):
        svc.execute_transfer(
            session,
            source_account_id=src,
            target_account_id=src,
            amount_minor=100,
            currency="PEN",
            balance_port=port,
        )
    with pytest.raises(ValueError):
        svc.execute_transfer(
            session,
            source_account_id=src,
            target_account_id=dst,
            amount_minor=100,
            currency="PEN",
            fee_minor=-1,
            balance_port=port,
        )
    with pytest.raises(ValueError):
        svc.execute_transfer(
            session,
            source_account_id="no-uuid",
            target_account_id=dst,
            amount_minor=100,
            currency="PEN",
            balance_port=port,
        )


def test_default_port_raises_e2_t01_pending():
    from app.modules.transactions import service as svc

    with pytest.raises(NotImplementedError, match="E2-T01 pendiente"):
        svc.DEFAULT_BALANCE_PORT.lock_and_get(SimpleNamespace(), uuid.uuid4())
    with pytest.raises(NotImplementedError, match="E2-T01 pendiente"):
        svc.DEFAULT_BALANCE_PORT.apply_delta(SimpleNamespace(), uuid.uuid4(), 1, "PEN")


def test_service_static_rules_no_float_no_direct_publish_no_direct_insert():
    path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "modules"
        / "transactions"
        / "service"
        / "__init__.py"
    )
    content = path.read_text(encoding="utf-8")
    assert "float(" not in content, "dinero entero en centimos, nunca float"
    assert "publish(" not in content, "regla 8: eventos solo por outbox"
    assert not re.search(
        r"^from app\.core\.outbox import", content, re.MULTILINE
    ), "outbox con import perezoso (no a nivel de modulo)"
    assert not re.search(r"^import app\.core\.outbox", content, re.MULTILINE)
    assert "    from app.core.outbox import record as outbox_record" in content
    assert "except ImportError" in content, "fallback si E5-T05 pendiente"
    assert "E2-T01 pendiente" in content
    assert "E5-T05" in content
    for pattern in (
        r"session\.delete",
        r"sa\.delete\s*\(",
        r"JournalEntry\s*\(",
        r"Posting\s*\(",
        r"journal_entries",
        r"\.postings\.",
    ):
        assert not re.search(pattern, content), f"ledger directo prohibido: {pattern}"
    assert "ledger.service.post_entry" in content or "ledger_service.post_entry" in content
    assert "ensure_customer_accounts" in content
    assert "BalancePort" in content and "lock_and_get" in content and "apply_delta" in content


# ---------------------------------------------------------------- Parte B: SQLite
@pytest.fixture()
def sqlite_session():
    engine = _sqlite_engine_with_schemas()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _funds(src, dst, src_amount=10_000, dst_amount=0, currency="PEN"):
    return {
        str(src): {"available_minor": src_amount, "currency": currency},
        str(dst): {"available_minor": dst_amount, "currency": currency},
    }


def test_happy_path_settles_with_entries_hold_and_history(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from app.modules.ledger import service as ledger_service
    from app.modules.transactions import repository as repo
    from app.modules.transactions import service as svc

    outbox_calls = _install_fake_outbox(monkeypatch)
    src, dst = uuid.uuid4(), uuid.uuid4()
    port = FakeBalances(_funds(src, dst))

    tx = svc.execute_transfer(
        sqlite_session,
        source_account_id=src,
        target_account_id=dst,
        amount_minor=3000,
        currency="PEN",
        fee_minor=200,
        balance_port=port,
    )
    assert tx.status == "SETTLED"
    # Historial: creada + 5 transiciones.
    assert [h.to_status for h in repo.list_history(sqlite_session, tx.id)] == [
        "INITIATED",
        "VALIDATED",
        "AUTHORIZED",
        "FUNDS_HELD",
        "POSTED",
        "SETTLED",
    ]
    # Hold capturado por el total (monto + fee).
    holds = repo.list_holds_by_transaction(sqlite_session, tx.id)
    assert len(holds) == 1
    assert holds[0].amount_minor == 3200 and holds[0].status == "CAPTURED"
    # Dos asientos: hold (2000->2100) y liquidacion (2100->2000 + fee a 4000).
    assert _count(sqlite_session, "ledger.journal_entries") == 2
    assert _count(sqlite_session, "ledger.postings") == 6
    # Proyeccion del port: origen -3200, destino +3000 (fee a ingresos).
    assert port.funds[str(src)]["available_minor"] == 6800
    assert port.funds[str(dst)]["available_minor"] == 3000
    # Outbox via constantes de app.core.events (firma compartida exacta).
    from app.core import events as domain_events

    by_type = {c["event_type"] for c in outbox_calls}
    assert domain_events.FUNDS_HELD in by_type
    assert domain_events.TRANSFER_SETTLED in by_type
    for call in outbox_calls:
        assert call["aggregate_type"] == "transaction"
        assert call["aggregate_id"] == tx.id
    # Bloqueo en orden estable de account_id.
    assert port.lock_order == sorted(port.lock_order)
    # Cuadre por asiento.
    from app.modules.ledger.models import JournalEntry

    entries = sqlite_session.scalars(sa.select(JournalEntry)).all()
    assert len(entries) == 2
    for table_entry in entries:
        rows = _postings_of(sqlite_session, table_entry.id)
        debit = sum(r.amount_minor for r in rows if r.direction == "DEBIT")
        credit = sum(r.amount_minor for r in rows if r.direction == "CREDIT")
        assert debit == credit > 0
    _ = ledger_service  # fachada usada via servicio (sin insert directo)


def test_insufficient_funds_rejected_without_movements(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from app.modules.transactions import repository as repo
    from app.modules.transactions import service as svc

    _install_fake_outbox(monkeypatch)
    src, dst = uuid.uuid4(), uuid.uuid4()
    port = FakeBalances(_funds(src, dst, src_amount=1000))

    tx = svc.execute_transfer(
        sqlite_session,
        source_account_id=src,
        target_account_id=dst,
        amount_minor=5000,
        currency="PEN",
        balance_port=port,
    )
    assert tx.status == "REJECTED"
    assert repo.list_holds_by_transaction(sqlite_session, tx.id) == []
    assert _count(sqlite_session, "ledger.journal_entries") == 0
    assert _count(sqlite_session, "ledger.postings") == 0
    assert port.funds[str(src)]["available_minor"] == 1000
    assert port.funds[str(dst)]["available_minor"] == 0
    assert [h.to_status for h in repo.list_history(sqlite_session, tx.id)] == [
        "INITIATED",
        "VALIDATED",
        "AUTHORIZED",
        "REJECTED",
    ]


def test_intermediate_failure_rolls_back_everything(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from app.modules.ledger import service as ledger_service
    from app.modules.transactions import service as svc

    _install_fake_outbox(monkeypatch)
    src, dst = uuid.uuid4(), uuid.uuid4()
    port = FakeBalances(_funds(src, dst))

    real_post = ledger_service.post_entry
    calls = {"n": 0}

    def flaky_post(session, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("falla del ledger a mitad de camino")
        return real_post(session, **kwargs)

    monkeypatch.setattr(ledger_service, "post_entry", flaky_post)
    with pytest.raises(RuntimeError, match="mitad de camino"):
        svc.execute_transfer(
            sqlite_session,
            source_account_id=src,
            target_account_id=dst,
            amount_minor=3000,
            currency="PEN",
            balance_port=port,
        )
    sqlite_session.rollback()
    # Rollback total: sin transaction/asiento/hold residual.
    assert _count(sqlite_session, "transactions.transactions") == 0
    assert _count(sqlite_session, "transactions.transaction_status_history") == 0
    assert _count(sqlite_session, "transactions.holds") == 0
    assert _count(sqlite_session, "ledger.journal_entries") == 0
    assert _count(sqlite_session, "ledger.postings") == 0


def test_hold_moves_2000_to_2100_with_invariant_total(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from app.modules.ledger import service as ledger_service
    from app.modules.transactions import repository as repo
    from app.modules.transactions import service as svc

    _install_fake_outbox(monkeypatch)
    src, dst = uuid.uuid4(), uuid.uuid4()
    port = FakeBalances(_funds(src, dst))

    tx = svc.execute_transfer(
        sqlite_session,
        source_account_id=src,
        target_account_id=dst,
        amount_minor=4000,
        currency="PEN",
        settle=False,
        balance_port=port,
    )
    assert tx.status == "POSTED"
    holds = repo.list_holds_by_transaction(sqlite_session, tx.id)
    assert len(holds) == 1 and holds[0].status == "ACTIVE"

    avail, hold_acct = ledger_service.ensure_customer_accounts(sqlite_session, src, "PEN")
    from app.modules.ledger.models import JournalEntry

    entries = sqlite_session.scalars(sa.select(JournalEntry)).all()
    assert len(entries) == 1
    rows = _postings_of(sqlite_session, entries[0].id)
    legs = {(str(r.ledger_account_id), r.direction): r.amount_minor for r in rows}
    assert legs[(str(avail.id), "DEBIT")] == 4000
    assert legs[(str(hold_acct.id), "CREDIT")] == 4000
    # Total del cliente invariante: neto 2000 + neto 2100 == 0.
    net = {str(avail.id): 0, str(hold_acct.id): 0}
    for r in rows:
        net[str(r.ledger_account_id)] += (
            r.amount_minor if r.direction == "DEBIT" else -r.amount_minor
        )
    assert net[str(avail.id)] + net[str(hold_acct.id)] == 0


def test_outbox_missing_continues_without_publishing(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from app.modules.transactions import service as svc

    # Simula E5-T05 pendiente: `sys.modules[...] = None` fuerza ImportError
    # en el import perezoso (no depende del modulo real).
    monkeypatch.setitem(sys.modules, "app.core.outbox", None)
    with pytest.raises(ImportError):
        from app.core.outbox import record  # noqa: F401 (verifica el truco)
    src, dst = uuid.uuid4(), uuid.uuid4()
    tx = svc.execute_transfer(
        sqlite_session,
        source_account_id=src,
        target_account_id=dst,
        amount_minor=1000,
        currency="PEN",
        settle=False,
        balance_port=FakeBalances(_funds(src, dst)),
    )
    assert tx.status == "POSTED"


# ---------------------------------------------------------------- Parte C: Postgres
def test_pg_engine_smoke(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    from app.modules.transactions import service as svc

    outbox_calls = _install_fake_outbox(monkeypatch)
    src, dst = uuid.uuid4(), uuid.uuid4()
    tx = svc.execute_transfer(
        db_session,
        source_account_id=src,
        target_account_id=dst,
        amount_minor=1500,
        currency="PEN",
        idempotency_key=f"e5t03-smoke-{uuid.uuid4()}",
        balance_port=FakeBalances(_funds(src, dst)),
    )
    assert tx.status == "SETTLED"
    assert any(c["aggregate_id"] == tx.id for c in outbox_calls)
