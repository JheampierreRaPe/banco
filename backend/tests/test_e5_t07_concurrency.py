"""Suite E5-T07 (HU17 CA-01/03/04): concurrencia, duplicidad y saldo.

Cubre los casos criticos del brief `docs/tasks/archive/E5-T07.md` sin tocar codigo
de produccion: solo usa las fachadas (`transactions.service.execute_transfer`,
`transactions.api.idempotency.execute_with_idempotency`) sobre SQLite en
memoria. Dinero solo de juguete (identificadores sinteticos, sin PII);
sin red, sin BD externa, sin `float`.

Mapeo brief -> test:
- 2 hilos debitando (sin sobregiro) .. `test_e5_t07_two_threads_no_overdraft`
- reintento de red (misma clave) ...... `test_e5_t07_network_retry_single_debit`
- misma clave + distinto cuerpo -> 409  `test_e5_t07_same_key_different_body_conflicts`
- saldo justo / insuficiente ........... `test_e5_t07_exact_balance_boundary`
- latencia del camino de escritura ..... `test_e5_t07_write_path_latency`

Reglas aplicadas de `docs/04-motor-transaccional-y-ledger.md`:
- #10 "Carrera de dos debitos": el bloqueo de filas + validacion de saldo
  evita el sobregiro; `REJECTED` no modifica nada.
- #10 "Peticion duplicada": devuelve el resultado original (idempotencia).
- #12.3/#12.4: doble peticion -> un solo debito; debitos concurrentes ->
  sin sobregiro. #12.8: carga con latencia < 200 ms en escritura.

Limitacion honesta del caso concurrente: el puerto de saldos es en memoria
(E2-T01 pendiente: el adaptador real lo provee) y no puede implementar
`SELECT ... FOR UPDATE` por si solo; los hilos se serializan con un lock
global para emular el bloqueo pesimista de filas por cuenta del adaptador
productivo (mismo patron que `test_q_t02_04`). El motor aporta la regla de
negocio (`available >= monto`, si no `REJECTED` sin movimientos) y el
invariante verificado es que jamas hay sobregiro.
"""

from __future__ import annotations

import statistics
import sys
import threading
import time
import types
import uuid
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

LATENCY_BUDGET_SECONDS = 0.200  # docs/04#12: camino de escritura < 200 ms


# ---------------------------------------------------------------- Helpers
class FakeBalances:
    """Port de saldos en memoria (E2-T01 pendiente: el adaptador real lo provee)."""

    def __init__(self, funds: dict[str, dict] | None = None):
        self.funds: dict[str, dict] = dict(funds or {})
        self.deltas: list[tuple[str, int]] = []

    def lock_and_get(self, session: Session, account_id):
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
    """Inyecta `app.core.outbox` fake (no depende del modulo real)."""
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


def _make_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.ledger.models as _lm
    import app.modules.shared.models as _sm
    import app.modules.transactions.models as _tm

    assert _lm is not None and _sm is not None and _tm is not None
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS transactions")
        cur.execute("ATTACH DATABASE ':memory:' AS ledger")
        cur.execute("ATTACH DATABASE ':memory:' AS shared")
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
            Base.metadata.tables["shared.idempotency_keys"],
        ],
    )
    return engine


@pytest.fixture()
def session():
    engine = _make_engine()
    sess = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()


def _funds(src, dst, src_amount=10_000, dst_amount=0, currency="PEN"):
    return {
        str(src): {"available_minor": src_amount, "currency": currency},
        str(dst): {"available_minor": dst_amount, "currency": currency},
    }


def _count(sess: Session, key: str) -> int:
    return sess.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[key]))


# ------------------------------------------------- Caso 1: 2 hilos debitando
def test_e5_t07_two_threads_no_overdraft(monkeypatch: pytest.MonkeyPatch):
    """Dos debitos concurrentes de 6000 sobre 10000: uno liquida, otro rechaza."""
    from app.modules.transactions import service as svc

    _install_fake_outbox(monkeypatch)
    engine = _make_engine()
    src, dst_a, dst_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    funds = _funds(src, dst_a, src_amount=10_000)
    funds[str(dst_b)] = {"available_minor": 0, "currency": "PEN"}
    port = FakeBalances(funds)
    gate = threading.Lock()
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def _run(name: str, dst):
        sess = Session(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            with gate:  # emula el bloqueo pesimista de filas por cuenta
                tx = svc.execute_transfer(
                    sess,
                    source_account_id=src,
                    target_account_id=dst,
                    amount_minor=6000,
                    currency="PEN",
                    balance_port=port,
                )
                sess.commit()
                results[name] = tx.status
        except BaseException as exc:  # noqa: BLE001 - se reporta al final
            errors.append(exc)
        finally:
            sess.close()

    threads = [
        threading.Thread(target=_run, args=("t1", dst_a)),
        threading.Thread(target=_run, args=("t2", dst_b)),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not errors, f"hilos con error: {errors}"
        assert sorted(results.values()) == ["REJECTED", "SETTLED"]
        assert port.funds[str(src)]["available_minor"] == 4000
        assert port.funds[str(src)]["available_minor"] >= 0, "sobregiro prohibido"
    finally:
        engine.dispose()


# --------------------------------------- Caso 2: reintento de red (duplicidad)
def test_e5_t07_network_retry_single_debit(session: Session, monkeypatch: pytest.MonkeyPatch):
    """Reintento con la misma Idempotency-Key: un solo debito, misma tx."""
    from app.modules.transactions import repository as repo
    from app.modules.transactions import service as svc

    _install_fake_outbox(monkeypatch)
    src, dst = uuid.uuid4(), uuid.uuid4()
    port = FakeBalances(_funds(src, dst))
    key = f"e5-t07-retry-{uuid.uuid4()}"

    first = svc.execute_transfer(
        session,
        source_account_id=src,
        target_account_id=dst,
        amount_minor=3000,
        currency="PEN",
        fee_minor=200,
        idempotency_key=key,
        balance_port=port,
    )
    assert first.status == "SETTLED"
    journals_before = _count(session, "ledger.journal_entries")
    history_before = len(repo.list_history(session, first.id))

    # El reintento de red repite clave + cuerpo exactos.
    second = svc.execute_transfer(
        session,
        source_account_id=src,
        target_account_id=dst,
        amount_minor=3000,
        currency="PEN",
        fee_minor=200,
        idempotency_key=key,
        balance_port=port,
    )
    assert second.id == first.id
    assert second.status == "SETTLED"
    # Un solo debito: el origen bajo una sola vez y no hay asientos nuevos.
    assert port.funds[str(src)]["available_minor"] == 6800
    assert port.funds[str(dst)]["available_minor"] == 3000
    assert _count(session, "ledger.journal_entries") == journals_before
    assert len(repo.list_history(session, first.id)) == history_before
    assert [d for d in port.deltas if d[0] == str(src)] == [(str(src), -3200)]


# ------------------------------- Caso 3: misma clave + distinto cuerpo -> 409
def test_e5_t07_same_key_different_body_conflicts(session: Session):
    """Misma clave con otro cuerpo: 409 sin ejecutar de nuevo (sin doble debito)."""
    from app.modules.transactions.api.idempotency import (
        IdempotencyConflictError,
        execute_with_idempotency,
    )

    calls: list = []
    user_id = uuid.uuid4()
    base = {
        "key": f"e5-t07-conflicto-{uuid.uuid4().hex[:8]}",
        "user_id": user_id,
        "endpoint": "/transfers",
        "method": "POST",
    }

    def _handler():
        calls.append(1)
        return ({"status": "SETTLED", "amount_minor": 100}, uuid.uuid4())

    execute_with_idempotency(session, handler=_handler, body={"amount_minor": 100}, **base)
    with pytest.raises(IdempotencyConflictError):
        execute_with_idempotency(session, handler=_handler, body={"amount_minor": 200}, **base)
    assert len(calls) == 1, "el cuerpo distinto no re-ejecuta el debito"


# --------------------------------- Caso 4: saldo justo (borde) e insuficiente
def test_e5_t07_exact_balance_boundary(session: Session, monkeypatch: pytest.MonkeyPatch):
    """Monto == disponible liquida; monto == disponible + 1 rechaza sin cambios."""
    from app.modules.transactions import repository as repo
    from app.modules.transactions import service as svc

    _install_fake_outbox(monkeypatch)

    # Borde exacto: 5000 sobre 5000 -> SETTLED y saldo en cero (sin sobregiro).
    src_ok, dst_ok = uuid.uuid4(), uuid.uuid4()
    port_ok = FakeBalances(_funds(src_ok, dst_ok, src_amount=5000))
    tx_ok = svc.execute_transfer(
        session,
        source_account_id=src_ok,
        target_account_id=dst_ok,
        amount_minor=5000,
        currency="PEN",
        balance_port=port_ok,
    )
    assert tx_ok.status == "SETTLED"
    assert port_ok.funds[str(src_ok)]["available_minor"] == 0
    assert port_ok.funds[str(dst_ok)]["available_minor"] == 5000

    # Un centimo por encima: REJECTED de negocio, sin asientos ni holds nuevos.
    src_ko, dst_ko = uuid.uuid4(), uuid.uuid4()
    port_ko = FakeBalances(_funds(src_ko, dst_ko, src_amount=5000))
    journals_before = _count(session, "ledger.journal_entries")
    tx_ko = svc.execute_transfer(
        session,
        source_account_id=src_ko,
        target_account_id=dst_ko,
        amount_minor=5001,
        currency="PEN",
        balance_port=port_ko,
    )
    assert tx_ko.status == "REJECTED"
    assert repo.list_holds_by_transaction(session, tx_ko.id) == []
    assert _count(session, "ledger.journal_entries") == journals_before
    assert port_ko.funds[str(src_ko)]["available_minor"] == 5000
    assert port_ko.funds[str(dst_ko)]["available_minor"] == 0
    assert [h.to_status for h in repo.list_history(session, tx_ko.id)] == [
        "INITIATED",
        "VALIDATED",
        "AUTHORIZED",
        "REJECTED",
    ]


# --------------------------------------- Caso 5: latencia del camino de escritura
def test_e5_t07_write_path_latency(session: Session, monkeypatch: pytest.MonkeyPatch):
    """El camino de escritura (hold + 2 asientos + estados) baja de 200 ms."""
    from app.modules.transactions import service as svc

    _install_fake_outbox(monkeypatch)
    samples: list[float] = []
    for _ in range(5):
        src, dst = uuid.uuid4(), uuid.uuid4()
        port = FakeBalances(_funds(src, dst, src_amount=1_000_000))
        started = time.perf_counter()
        tx = svc.execute_transfer(
            session,
            source_account_id=src,
            target_account_id=dst,
            amount_minor=1000,
            currency="PEN",
            balance_port=port,
        )
        samples.append(time.perf_counter() - started)
        assert tx.status == "SETTLED"
    median = statistics.median(samples)
    assert median < LATENCY_BUDGET_SECONDS, f"mediana {median * 1000:.1f} ms >= 200 ms"
