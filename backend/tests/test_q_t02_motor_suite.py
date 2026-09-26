"""Suite Q-T02: 12 casos obligatorios del motor transaccional/ledger.

Cubre las pruebas requeridas del brief `docs/tasks/archive/Q-T02.md` (CA-03 de HU17
y CA-01..CA-04 de HU18) sin tocar codigo de produccion: solo usa las
fachadas (`transactions.service.execute_transfer`, `ledger.service`,
`release_expired_holds`, `run_daily_closing`) sobre SQLite en memoria.
Dinero solo de juguete (identificadores sinteticos, sin PII); sin red,
sin BD externa, sin `float`.

Mapeo brief -> test:
1. feliz propia y a tercero ............ `test_q_t02_01_...`
2. saldo insuficiente ................... `test_q_t02_02_...`
3. doble peticion idempotente ............ `test_q_t02_03_...`
4. debitos concurrentes sin sobregiro .... `test_q_t02_04_...`
5. reverso con original intacto .......... `test_q_t02_05_...`
6. cuadre por asiento .................... `test_q_t02_06_...`
7. cierre diario cuadrado ................ `test_q_t02_07_...`
8. interbancario exito/timeout/compens. .. `test_q_t02_08_...`
9. atomicidad ante fallo parcial ......... `test_q_t02_09_...`
10. hold vencido liberado por job ......... `test_q_t02_10_...`
11. UUID unico por asiento ................ `test_q_t02_11_...`
12. latencia del camino de escritura ...... `test_q_t02_12_...`
"""

from __future__ import annotations

import statistics
import sys
import threading
import time
import types
import uuid
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
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
    import app.modules.transactions.models as _tm

    assert _lm is not None and _tm is not None
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
            Base.metadata.tables["ledger.ledger_balances"],
            Base.metadata.tables["ledger.daily_closings"],
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


def _entry_totals_by_currency(sess: Session, entry_id) -> tuple[dict, dict]:
    from app.modules.ledger import service as ledger_service

    debits: dict[str, int] = {}
    credits: dict[str, int] = {}
    for row in ledger_service.list_postings(sess, entry_id):
        bucket = debits if row.direction == "DEBIT" else credits
        bucket[row.currency] = bucket.get(row.currency, 0) + row.amount_minor
    return debits, credits


def _assert_entry_balanced(sess: Session, entry_id) -> None:
    debits, credits = _entry_totals_by_currency(sess, entry_id)
    assert debits == credits and sum(debits.values()) > 0


# ---------------------------------------------------------------- Caso 1: feliz propia y a tercero
def test_q_t02_01_transferencia_feliz_propia_y_tercero(
    session: Session, monkeypatch: pytest.MonkeyPatch
):
    """Propia (sin fee, 04#7.1) y a tercero (con comision, 04#7.2)."""
    from app.core import events as domain_events
    from app.modules.ledger import service as ledger_service
    from app.modules.transactions import repository as repo
    from app.modules.transactions import service as svc

    outbox_calls = _install_fake_outbox(monkeypatch)

    # Propia: 3000 sin comision.
    src_a, dst_a = uuid.uuid4(), uuid.uuid4()
    port_a = FakeBalances(_funds(src_a, dst_a))
    tx_a = svc.execute_transfer(
        session,
        source_account_id=src_a,
        target_account_id=dst_a,
        amount_minor=3000,
        currency="PEN",
        balance_port=port_a,
    )
    assert tx_a.status == "SETTLED"
    assert port_a.funds[str(src_a)]["available_minor"] == 7000
    assert port_a.funds[str(dst_a)]["available_minor"] == 3000

    # Tercero: 3000 + 200 de comision (04#7.2: fee a 4000-Comisiones).
    src_b, dst_b = uuid.uuid4(), uuid.uuid4()
    port_b = FakeBalances(_funds(src_b, dst_b))
    tx_b = svc.execute_transfer(
        session,
        source_account_id=src_b,
        target_account_id=dst_b,
        amount_minor=3000,
        currency="PEN",
        fee_minor=200,
        balance_port=port_b,
    )
    assert tx_b.status == "SETTLED"
    assert port_b.funds[str(src_b)]["available_minor"] == 6800
    assert port_b.funds[str(dst_b)]["available_minor"] == 3000

    # El asiento de liquidacion del tercero lleva la comision a la 4000.
    from app.modules.ledger.models import JournalEntry

    settle = session.scalars(
        sa.select(JournalEntry).where(
            JournalEntry.entry_type == "TRANSFER_SETTLE",
            JournalEntry.transaction_id == tx_b.id,
        )
    ).one()
    legs = [(r.direction, r.amount_minor) for r in ledger_service.list_postings(session, settle.id)]
    assert sorted(legs) == [("CREDIT", 200), ("CREDIT", 3000), ("DEBIT", 200), ("DEBIT", 3000)]

    # Outbox: retencion + liquidacion por cada transferencia.
    by_type = [c["event_type"] for c in outbox_calls]
    assert by_type.count(domain_events.FUNDS_HELD) == 2
    assert by_type.count(domain_events.TRANSFER_SETTLED) == 2

    # Historial completo de la via feliz.
    assert [h.to_status for h in repo.list_history(session, tx_b.id)] == [
        "INITIATED",
        "VALIDATED",
        "AUTHORIZED",
        "FUNDS_HELD",
        "POSTED",
        "SETTLED",
    ]


# ---------------------------------------------------------------- Caso 2: saldo insuficiente
def test_q_t02_02_saldo_insuficiente_rechazado_sin_cambios(
    session: Session, monkeypatch: pytest.MonkeyPatch
):
    """REJECTED de negocio: sin asientos, sin holds, saldos intactos."""
    from app.modules.transactions import repository as repo
    from app.modules.transactions import service as svc

    _install_fake_outbox(monkeypatch)
    src, dst = uuid.uuid4(), uuid.uuid4()
    port = FakeBalances(_funds(src, dst, src_amount=1000))

    tx = svc.execute_transfer(
        session,
        source_account_id=src,
        target_account_id=dst,
        amount_minor=5000,
        currency="PEN",
        balance_port=port,
    )
    assert tx.status == "REJECTED"
    assert repo.list_holds_by_transaction(session, tx.id) == []
    assert _count(session, "ledger.journal_entries") == 0
    assert _count(session, "ledger.postings") == 0
    assert port.funds[str(src)]["available_minor"] == 1000
    assert port.funds[str(dst)]["available_minor"] == 0
    assert [h.to_status for h in repo.list_history(session, tx.id)] == [
        "INITIATED",
        "VALIDATED",
        "AUTHORIZED",
        "REJECTED",
    ]


# ---------------------------------------------------------------- Caso 3: idempotencia
def test_q_t02_03_doble_peticion_idempotente_un_solo_debito(
    session: Session, monkeypatch: pytest.MonkeyPatch
):
    """Misma Idempotency-Key: la repeticion devuelve la tx original sin re-debitar."""
    from app.modules.transactions import repository as repo
    from app.modules.transactions import service as svc

    _install_fake_outbox(monkeypatch)
    src, dst = uuid.uuid4(), uuid.uuid4()
    port = FakeBalances(_funds(src, dst))
    key = f"q-t02-{uuid.uuid4()}"

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


# ---------------------------------------------------------------- Caso 4: concurrencia
def test_q_t02_04_debitos_concurrentes_sin_sobregiro(
    monkeypatch: pytest.MonkeyPatch,
):
    """Dos debitos concurrentes de 4000 sobre 5000: uno liquida, otro rechaza.

    El puerto fake serializa cada transferencia con un lock global para
    emular el `SELECT ... FOR UPDATE` del adaptador productivo (E2-T01);
    el motor aporta la regla de negocio (`available >= monto` o REJECTED
    sin movimientos). El invariante verificado es que jamas hay sobregiro.
    """
    from app.modules.transactions import service as svc

    _install_fake_outbox(monkeypatch)
    engine = _make_engine()
    src, dst_a, dst_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    funds = _funds(src, dst_a, src_amount=5000)
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
                    amount_minor=4000,
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
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    try:
        assert not errors, f"hilos con error: {errors}"
        assert sorted(results.values()) == ["REJECTED", "SETTLED"]
        assert port.funds[str(src)]["available_minor"] == 1000
        assert port.funds[str(src)]["available_minor"] >= 0, "sobregiro prohibido"
    finally:
        engine.dispose()


# ---------------------------------------------------------------- Caso 5: reverso
def test_q_t02_05_reverso_con_original_intacto(session: Session):
    """El reverso crea un compensatorio inverso; el original permanece."""
    from app.modules.ledger import repository as repo

    avail, hold = repo.ensure_customer_accounts(session, uuid.uuid4(), "PEN")
    original = repo.post_entry(
        session,
        entry_type="OWN_TRANSFER",
        postings=[
            {
                "ledger_account_id": avail.id,
                "direction": "DEBIT",
                "amount_minor": 5000,
                "currency": "PEN",
            },
            {
                "ledger_account_id": hold.id,
                "direction": "CREDIT",
                "amount_minor": 5000,
                "currency": "PEN",
            },
        ],
    )
    before = sorted(
        (str(r.ledger_account_id), r.direction, r.amount_minor)
        for r in repo.list_postings(session, original.id)
    )

    comp = repo.reverse_entry(session, original.id)
    assert repo.get_entry(session, original.id).status == "REVERSED"
    assert comp.status == "POSTED"
    assert comp.reverses_entry_id == original.id
    # Original intacto: mismos postings, sin UPDATE/DELETE (2 + 2 filas).
    after = sorted(
        (str(r.ledger_account_id), r.direction, r.amount_minor)
        for r in repo.list_postings(session, original.id)
    )
    assert after == before
    assert _count(session, "ledger.postings") == 4
    # Compensatorio invertido y cuadrado.
    assert sorted((r.direction, r.amount_minor) for r in repo.list_postings(session, comp.id)) == [
        ("CREDIT", 5000),
        ("DEBIT", 5000),
    ]
    _assert_entry_balanced(session, comp.id)


# ---------------------------------------------------------------- Caso 6: cuadre por asiento
def test_q_t02_06_cuadre_por_asiento(session: Session, monkeypatch: pytest.MonkeyPatch):
    """Todo asiento cumple sum(debitos) = sum(creditos) por moneda."""
    from app.modules.ledger.models import JournalEntry
    from app.modules.transactions import service as svc

    _install_fake_outbox(monkeypatch)
    src, dst = uuid.uuid4(), uuid.uuid4()
    svc.execute_transfer(
        session,
        source_account_id=src,
        target_account_id=dst,
        amount_minor=3000,
        currency="PEN",
        fee_minor=200,
        balance_port=FakeBalances(_funds(src, dst)),
    )
    entries = session.scalars(sa.select(JournalEntry)).all()
    assert len(entries) == 2  # hold + liquidacion
    for entry in entries:
        _assert_entry_balanced(session, entry.id)


# ---------------------------------------------------------------- Caso 7: cierre diario
def test_q_t02_07_cierre_diario_cuadrado(session: Session):
    """El cierre del dia cuadra (balanced=true, closed_at fijado, totales)."""
    from app.modules.ledger import repository as repo
    from app.modules.ledger.jobs.daily_closing import run_daily_closing

    avail, hold = repo.ensure_customer_accounts(session, uuid.uuid4(), "PEN")
    day = date(2026, 9, 20)
    for amount in (10_000, 4_000):
        repo.post_entry_and_update_balances(
            session,
            entry_type="OWN_TRANSFER",
            postings=[
                {
                    "ledger_account_id": avail.id,
                    "direction": "DEBIT",
                    "amount_minor": amount,
                    "currency": "PEN",
                },
                {
                    "ledger_account_id": hold.id,
                    "direction": "CREDIT",
                    "amount_minor": amount,
                    "currency": "PEN",
                },
            ],
            value_date=day,
        )
    closing, alert = run_daily_closing(session, closing_date=day, currency="PEN")
    assert alert is None
    assert closing.balanced is True
    assert closing.closed_at is not None
    assert closing.total_debits_minor == 14_000
    assert closing.total_credits_minor == 14_000
    assert closing.postings_count == 4


# ---------------------------------------------------------------- Caso 8: interbancario
def test_q_t02_08_interbancario_exito_timeout_compensacion(
    session: Session, monkeypatch: pytest.MonkeyPatch
):
    """Exito (SETTLED con comision) / timeout (POSTED->FAILED) / compensacion 2100->2000."""
    from app.core import events as domain_events
    from app.modules.ledger import service as ledger_service
    from app.modules.ledger.models import JournalEntry
    from app.modules.transactions import repository as repo
    from app.modules.transactions import service as svc
    from app.modules.transactions.jobs.release_expired_holds import release_expired_holds

    outbox_calls = _install_fake_outbox(monkeypatch)

    # Exito: interbancaria inmediata 100 + 2 de comision (04#7.3).
    src_ok, dst_ok = uuid.uuid4(), uuid.uuid4()
    tx_ok = svc.execute_transfer(
        session,
        source_account_id=src_ok,
        target_account_id=dst_ok,
        amount_minor=100,
        currency="PEN",
        fee_minor=2,
        tx_type="INTERBANK_OUT",
        balance_port=FakeBalances(_funds(src_ok, dst_ok)),
    )
    assert tx_ok.status == "SETTLED"

    # Timeout: diferida (settle=False) queda POSTED con hold ACTIVE reteniendo
    # el total (04#7.3 paso 1: 2000 -> 2100).
    src_t, dst_t = uuid.uuid4(), uuid.uuid4()
    port_t = FakeBalances(_funds(src_t, dst_t))
    tx_t = svc.execute_transfer(
        session,
        source_account_id=src_t,
        target_account_id=dst_t,
        amount_minor=100,
        currency="PEN",
        fee_minor=2,
        tx_type="INTERBANK_OUT",
        settle=False,
        balance_port=port_t,
    )
    assert tx_t.status == "POSTED"
    (hold_t,) = repo.list_holds_by_transaction(session, tx_t.id)
    assert hold_t.status == "ACTIVE" and hold_t.amount_minor == 102

    # Expira el plazo del tercero: el job libera y compensa.
    hold_t.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.flush()
    processed = release_expired_holds(session, now=datetime.now(UTC))
    assert [h.id for h in processed] == [hold_t.id]
    assert hold_t.status == "EXPIRED"
    assert tx_t.status == "FAILED"  # FAILED siempre libera holds (04#3)

    # Compensacion: asiento HOLD_RELEASE Debe 2100 / Haber 2000 por 102.
    releases = session.scalars(
        sa.select(JournalEntry).where(
            JournalEntry.entry_type == "HOLD_RELEASE",
            JournalEntry.transaction_id == tx_t.id,
        )
    ).all()
    assert len(releases) == 1
    _, retained = ledger_service.ensure_customer_accounts(session, src_t, "PEN")
    avail, _ = ledger_service.ensure_customer_accounts(session, src_t, "PEN")
    legs = {
        (str(r.ledger_account_id), r.direction): r.amount_minor
        for r in ledger_service.list_postings(session, releases[0].id)
    }
    assert legs == {(str(retained.id), "DEBIT"): 102, (str(avail.id), "CREDIT"): 102}

    # Eventos: liquidacion del exito + liberacion del timeout.
    by_type = [c["event_type"] for c in outbox_calls]
    assert domain_events.TRANSFER_SETTLED in by_type
    assert domain_events.FUNDS_RELEASED in by_type

    # Cuadre global tras la compensacion.
    for entry in session.scalars(sa.select(JournalEntry)).all():
        _assert_entry_balanced(session, entry.id)


# ---------------------------------------------------------------- Caso 9: atomicidad
def test_q_t02_09_atomicidad_ante_fallo_parcial(session: Session, monkeypatch: pytest.MonkeyPatch):
    """Fallo del ledger a mitad de camino: rollback total, sin residual."""
    import pytest as _pytest

    from app.modules.ledger import service as ledger_service
    from app.modules.transactions import service as svc

    _install_fake_outbox(monkeypatch)
    src, dst = uuid.uuid4(), uuid.uuid4()
    real_post = ledger_service.post_entry
    calls = {"n": 0}

    def flaky_post(sess, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("falla del ledger a mitad de camino")
        return real_post(sess, **kwargs)

    monkeypatch.setattr(ledger_service, "post_entry", flaky_post)
    with _pytest.raises(RuntimeError, match="mitad de camino"):
        svc.execute_transfer(
            session,
            source_account_id=src,
            target_account_id=dst,
            amount_minor=3000,
            currency="PEN",
            balance_port=FakeBalances(_funds(src, dst)),
        )
    session.rollback()
    assert _count(session, "transactions.transactions") == 0
    assert _count(session, "transactions.transaction_status_history") == 0
    assert _count(session, "transactions.holds") == 0
    assert _count(session, "ledger.journal_entries") == 0
    assert _count(session, "ledger.postings") == 0


# ---------------------------------------------------------------- Caso 10: hold vencido
def test_q_t02_10_hold_vencido_liberado_por_job(session: Session, monkeypatch: pytest.MonkeyPatch):
    """El job libera el hold vencido, falla la tx y no duplica al reprocesar."""
    from app.core import events as domain_events
    from app.modules.transactions import repository as repo
    from app.modules.transactions.jobs.release_expired_holds import release_expired_holds

    outbox_calls = _install_fake_outbox(monkeypatch)
    tx = repo.create_transaction(session, type="OWN_TRANSFER", amount_minor=1500, currency="PEN")
    for target in ("VALIDATED", "AUTHORIZED", "FUNDS_HELD", "POSTED"):
        repo.transition_transaction(session, tx.id, target)
    hold = repo.create_hold(
        session,
        transaction_id=tx.id,
        account_id=uuid.uuid4(),
        amount_minor=1500,
        currency="PEN",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    assert [h.id for h in release_expired_holds(session, now=datetime.now(UTC))] == [hold.id]
    assert hold.status == "EXPIRED" and hold.released_at is not None
    assert tx.status == "FAILED"
    released = [c for c in outbox_calls if c["event_type"] == domain_events.FUNDS_RELEASED]
    assert len(released) == 1 and released[0]["aggregate_id"] == tx.id

    # Reproceso idempotente: nada cambia ni se duplica.
    hist_len = len(repo.list_history(session, tx.id))
    assert release_expired_holds(session, now=datetime.now(UTC)) == []
    assert len(repo.list_history(session, tx.id)) == hist_len
    assert len(outbox_calls) == 1


# ---------------------------------------------------------------- Caso 11: UUID unico
def test_q_t02_11_uuid_unico_por_asiento(session: Session):
    """Cada asiento tiene UUID unico y hash encadenado al anterior."""
    from app.modules.ledger import repository as repo

    avail, hold = repo.ensure_customer_accounts(session, uuid.uuid4(), "PEN")
    entries = [
        repo.post_entry(
            session,
            entry_type="OWN_TRANSFER",
            postings=[
                {
                    "ledger_account_id": avail.id,
                    "direction": "DEBIT",
                    "amount_minor": 1000 + i,
                    "currency": "PEN",
                },
                {
                    "ledger_account_id": hold.id,
                    "direction": "CREDIT",
                    "amount_minor": 1000 + i,
                    "currency": "PEN",
                },
            ],
        )
        for i in range(3)
    ]
    assert len({e.id for e in entries}) == 3
    assert len({e.hash for e in entries}) == 3
    assert entries[0].prev_hash is None
    for prev, current in pairwise(entries):
        assert current.prev_hash == prev.hash


# ---------------------------------------------------------------- Caso 12: latencia
def test_q_t02_12_latencia_camino_escritura(session: Session, monkeypatch: pytest.MonkeyPatch):
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
