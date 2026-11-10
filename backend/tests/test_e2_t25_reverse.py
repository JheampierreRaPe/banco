"""Reverso/compensacion idempotente (E2-T25, HU06/HU17/HU18).

Sobre el motor y el ledger con adaptadores reales: dinero de juguete (UUIDs
sinteticos, sin PII), SQLite en memoria + schemas ATTACH, sin red, sin
`float`.

Mapeo brief -> test:

- feliz propia (`SETTLED` -> asiento inverso, saldos restaurados) `test_reverse_own_*`
- feliz tercero con comision (2 inversos, `4100` neta cero) ..... `test_reverse_third_*`
- diferida `POSTED` (libera hold `ACTIVE`) ...................... `test_reverse_posted_*`
- `CONCILIATED` reversible ...................................... `test_reverse_conciliated`
- estados no permitidos -> `409` sin asientos ................... `test_reverse_not_allowed_*`
- inexistente/ajena/motivo/clave ............................... `test_http_reverse_*`
- idempotencia (misma clave) y concurrencia sin doble asiento ... `test_reverse_*idempoten*`, `test_reverse_concurrent_*`
- delegacion interbancaria ...................................... `test_reverse_interbank_*`
"""

from __future__ import annotations

import threading
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()

TABLES = [
    "accounts.accounts",
    "accounts.account_balances",
    "accounts.daily_balance_snapshots",
    "ledger.ledger_accounts",
    "ledger.journal_entries",
    "ledger.postings",
    "ledger.ledger_balances",
    "shared.outbox",
    "shared.processed_events",
    "shared.idempotency_keys",
    "transactions.transactions",
    "transactions.transaction_status_history",
    "transactions.holds",
]


def _make_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # registro de tablas
    import app.modules.ledger.models as _l
    import app.modules.shared.models as _s
    import app.modules.transactions.models as _t

    assert _a is not None and _l is not None and _s is not None and _t is not None
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "ledger", "shared", "transactions", "config"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    return engine


@pytest.fixture()
def svc_session():
    """Sesion SQLite aislada con cuentas fondeadas (servicio + motor)."""
    from app.modules.accounts import repository as accounts_repo

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        a1 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="00110001111", type="AHORRO", currency="PEN"
        )
        a2 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="00110002222", type="CORRIENTE", currency="PEN"
        )
        b1 = accounts_repo.create_account(
            session, user_id=USER_B, account_number="00220003333", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a1.id, available_delta=10_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a2.id, available_delta=1_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, b1.id, available_delta=5_000, held_delta=0, currency="PEN"
        )
        session.commit()
        yield {"session": session, "a1": a1.id, "a2": a2.id, "b1": b1.id}
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def http_client(svc_session, monkeypatch):
    """TestClient con BD de `svc_session` y auth falsa como USER_A."""
    from app.core.db import get_db
    from app.main import app
    from app.modules.transactions.api import get_current_user_id

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    session = svc_session["session"]

    def _override_db():
        yield session

    def _override_user():
        return USER_A

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = _override_user
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)


def _avail(session, account_id) -> int:
    from app.modules.accounts import repository as accounts_repo

    return int(accounts_repo.get_balance(session, account_id).available_minor)


def _entries(session, tx_id=None):
    from app.modules.ledger.models import JournalEntry

    stmt = sa.select(JournalEntry)
    if tx_id is not None:
        stmt = stmt.where(JournalEntry.transaction_id == tx_id)
    return list(session.scalars(stmt).all())


def _postings(session, entry_id):
    from app.modules.ledger import service as ledger_service

    return ledger_service.list_postings(session, entry_id)


def _snapshot_postings(session, tx_id) -> dict:
    """Foto de los `postings` originales (para probar que siguen intactos)."""
    snap = {}
    for entry in _entries(session, tx_id):
        snap[str(entry.id)] = [
            (str(p.ledger_account_id), p.direction, int(p.amount_minor), p.currency)
            for p in _postings(session, entry.id)
        ]
    return snap


def _net_by_account(session, tx_id) -> dict:
    """Neto por cuenta (DEBIT +, CREDIT -): tras el reverso todo es cero."""
    net: dict = {}
    for entry in _entries(session, tx_id):
        for p in _postings(session, entry.id):
            key = (str(p.ledger_account_id), p.currency)
            delta = int(p.amount_minor) if p.direction == "DEBIT" else -int(p.amount_minor)
            net[key] = net.get(key, 0) + delta
    return net


def _outbox_types(session, tx_id) -> set[str]:
    from app.modules.shared.models import OutboxEntry

    rows = session.scalars(sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx_id)).all()
    return {r.event_type for r in rows}


def _real_port():
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT

    return ACCOUNTS_BALANCE_PORT


def _settle_own(svc_session, amount_minor: int = 3_000):
    from app.modules.transactions import service as svc

    session = svc_session["session"]
    return svc.transfer_own(
        session,
        initiator_user_id=USER_A,
        source_account_id=svc_session["a1"],
        target_account_id=svc_session["a2"],
        amount_minor=amount_minor,
        currency="PEN",
        idempotency_key=f"e2t25-own-{uuid.uuid4()}",
        balance_port=_real_port(),
    )


def _settle_third(svc_session, amount_minor: int = 3_000, fee_minor: int = 200):
    from app.modules.transactions import service as svc

    session = svc_session["session"]
    return svc.transfer_third_party(
        session,
        initiator_user_id=USER_A,
        source_account_id=svc_session["a1"],
        target_account_id=svc_session["b1"],
        amount_minor=amount_minor,
        currency="PEN",
        fee_minor=fee_minor,
        idempotency_key=f"e2t25-tp-{uuid.uuid4()}",
        balance_port=_real_port(),
    )


# ---------------------------------------------------------------- Feliz propia
def test_reverse_own_settled_happy(svc_session):
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import reversal as rev

    session = svc_session["session"]
    a1, a2 = svc_session["a1"], svc_session["a2"]
    tx = _settle_own(svc_session)
    assert tx.status == "SETTLED"
    assert _avail(session, a1) == 7_000 and _avail(session, a2) == 4_000
    before = _snapshot_postings(session, tx.id)
    assert len(before) == 1

    result = rev.reverse_transaction(session, tx.id, reason="duplicado", balance_port=_real_port())
    done = result["transaction"]
    assert done.status == "REVERSED"
    assert len(result["reversal_entry_ids"]) == 1
    assert len(result["reverses_entry_ids"]) == 1

    originals = _entries(session, tx.id)
    assert len(originals) == 2  # original + inverso, ambos permanecen
    by_id = {str(e.id): e for e in originals}
    original_id = result["reverses_entry_ids"][0]
    reversal_id = result["reversal_entry_ids"][0]
    assert by_id[reversal_id].reverses_entry_id == by_id[original_id].id
    # El original y sus postings siguen intactos (misma foto).
    assert _snapshot_postings(session, tx.id)[original_id] == before[original_id]

    rows = _postings(session, by_id[reversal_id].id)
    debit = sum(r.amount_minor for r in rows if r.direction == "DEBIT")
    credit = sum(r.amount_minor for r in rows if r.direction == "CREDIT")
    assert debit == credit == 3_000  # cuadra por moneda
    assert _net_by_account(session, tx.id) == {k: 0 for k in _net_by_account(session, tx.id)}

    # Saldos restaurados y traza completa.
    assert _avail(session, a1) == 10_000 and _avail(session, a2) == 1_000
    history = [h.to_status for h in tx_repo.list_history(session, tx.id)]
    assert history[-1] == "REVERSED"
    from app.modules.transactions.service import reversal as rev_mod

    assert rev_mod.TRANSFER_REVERSED_EVENT in _outbox_types(session, tx.id)


def test_ledger_entries_recorded_on_create(svc_session):
    session = svc_session["session"]
    own = _settle_own(svc_session)
    fresh = session.get(own.__class__, own.id)
    kinds = [e["kind"] for e in (fresh.meta or {}).get("ledger_entries", [])]
    assert kinds == ["own"]

    third = _settle_third(svc_session)
    fresh_third = session.get(third.__class__, third.id)
    kinds_third = [e["kind"] for e in (fresh_third.meta or {}).get("ledger_entries", [])]
    assert kinds_third == ["hold", "settle"]


# ---------------------------------------------------------------- Feliz tercero
def test_reverse_third_party_settled_with_fee(svc_session):
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import reversal as rev

    session = svc_session["session"]
    a1, b1 = svc_session["a1"], svc_session["b1"]
    tx = _settle_third(svc_session)
    assert tx.status == "SETTLED"
    assert _avail(session, a1) == 6_800 and _avail(session, b1) == 8_000

    result = rev.reverse_transaction(session, tx.id, balance_port=_real_port())
    assert result["transaction"].status == "REVERSED"
    assert len(result["reversal_entry_ids"]) == 2
    assert len(result["reverses_entry_ids"]) == 2
    assert len(_entries(session, tx.id)) == 4  # 2 originales + 2 inversos

    for reversal_id in result["reversal_entry_ids"]:
        rows = _postings(session, reversal_id)
        debit = sum(r.amount_minor for r in rows if r.direction == "DEBIT")
        credit = sum(r.amount_minor for r in rows if r.direction == "CREDIT")
        assert debit == credit  # cada inverso cuadra
    assert set(_net_by_account(session, tx.id).values()) == {0}  # original + inverso = 0

    # Saldos restaurados; el hold CAPTURED queda (terminal, no se reabre).
    assert _avail(session, a1) == 10_000 and _avail(session, b1) == 5_000
    holds = tx_repo.list_holds_by_transaction(session, tx.id)
    assert len(holds) == 1 and holds[0].status == "CAPTURED"


def test_reverse_posted_deferred_releases_hold(svc_session):
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions import service as svc
    from app.modules.transactions.service import reversal as rev

    session = svc_session["session"]
    a1, b1 = svc_session["a1"], svc_session["b1"]
    tx = svc.transfer_third_party(
        session,
        initiator_user_id=USER_A,
        source_account_id=a1,
        target_account_id=b1,
        amount_minor=3_000,
        currency="PEN",
        fee_minor=200,
        idempotency_key=f"e2t25-def-{uuid.uuid4()}",
        settle=False,
        balance_port=_real_port(),
    )
    assert tx.status == "POSTED"
    assert _avail(session, a1) == 6_800 and _avail(session, b1) == 5_000

    result = rev.reverse_transaction(session, tx.id, balance_port=_real_port())
    assert result["transaction"].status == "REVERSED"
    assert len(result["reversal_entry_ids"]) == 1  # solo el hold tenia asiento
    holds = tx_repo.list_holds_by_transaction(session, tx.id)
    assert len(holds) == 1 and holds[0].status == "RELEASED"
    assert _avail(session, a1) == 10_000 and _avail(session, b1) == 5_000


def test_reverse_conciliated(svc_session):
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import reversal as rev

    session = svc_session["session"]
    tx = _settle_own(svc_session)
    tx_repo.transition_transaction(session, tx.id, "CONCILIATED", reason="cruce")
    result = rev.reverse_transaction(session, tx.id, balance_port=_real_port())
    assert result["transaction"].status == "REVERSED"
    assert _avail(session, svc_session["a1"]) == 10_000


# ---------------------------------------------------------------- Estados no permitidos
def _tx_in_state(session, target: str):
    """Transaccion de juguete llevada a `target` por transiciones validas."""
    from app.modules.transactions import repository as tx_repo

    path = {
        "INITIATED": [],
        "VALIDATED": ["VALIDATED"],
        "PENDING_AUTHORIZATION": ["VALIDATED", "PENDING_AUTHORIZATION"],
        "AUTHORIZED": ["VALIDATED", "AUTHORIZED"],
        "FUNDS_HELD": ["VALIDATED", "AUTHORIZED", "FUNDS_HELD"],
        "REJECTED": ["REJECTED"],
        "FAILED": ["VALIDATED", "AUTHORIZED", "FUNDS_HELD", "FAILED"],
        "SETTLED_WITHOUT_TRACE": ["VALIDATED", "AUTHORIZED", "FUNDS_HELD", "POSTED", "SETTLED"],
    }[target]
    tx = tx_repo.create_transaction(
        session,
        type="OWN_TRANSFER",
        amount_minor=1_000,
        currency="PEN",
        initiator_user_id=USER_A,
        source_account_id=uuid.uuid4(),
        target_account_id=uuid.uuid4(),
        idempotency_key=f"e2t25-st-{uuid.uuid4()}",
    )
    for step in path:
        tx = tx_repo.transition_transaction(session, tx.id, step, reason="setup")
    return tx


@pytest.mark.parametrize(
    "state",
    [
        "INITIATED",
        "VALIDATED",
        "PENDING_AUTHORIZATION",
        "AUTHORIZED",
        "FUNDS_HELD",
        "REJECTED",
        "FAILED",
    ],
)
def test_reverse_not_allowed_states(svc_session, state):
    from app.modules.transactions.service import reversal as rev

    session = svc_session["session"]
    before = session.scalar(
        sa.select(sa.func.count()).select_from(Base.metadata.tables["ledger.journal_entries"])
    )
    tx = _tx_in_state(session, state)
    tx_id = tx.id
    session.commit()
    with pytest.raises(rev.ReversalStateError):
        rev.reverse_transaction(session, tx.id, balance_port=_real_port())
    session.rollback()  # descarta el SAVEPOINT del intento fallido
    session.expire_all()
    after = session.scalar(
        sa.select(sa.func.count()).select_from(Base.metadata.tables["ledger.journal_entries"])
    )
    assert after == before  # sin asientos nuevos
    assert session.get(tx.__class__, tx_id).status == state


def test_reverse_without_ledger_trace(svc_session):
    from app.modules.transactions.service import reversal as rev

    session = svc_session["session"]
    tx = _tx_in_state(session, "SETTLED_WITHOUT_TRACE")
    with pytest.raises(rev.ReversalStateError, match="asientos"):
        rev.reverse_transaction(session, tx.id, balance_port=_real_port())
    session.rollback()


# ---------------------------------------------------------------- Idempotencia y concurrencia
def test_reverse_idempotent_service_replay(svc_session):
    from app.modules.transactions.service import reversal as rev

    session = svc_session["session"]
    tx = _settle_own(svc_session)
    first = rev.reverse_transaction(session, tx.id, balance_port=_real_port())
    second = rev.reverse_transaction(session, tx.id, balance_port=_real_port())
    assert second["transaction"].status == "REVERSED"
    assert second["reversal_entry_ids"] == first["reversal_entry_ids"]
    assert second["reverses_entry_ids"] == first["reverses_entry_ids"]
    reversals = [e for e in _entries(session, tx.id) if e.reverses_entry_id is not None]
    assert len(reversals) == 1  # un solo asiento inverso


def test_reverse_concurrent_different_keys_single_compensation():
    """Dos reversos concurrentes (claves distintas) -> una sola compensacion."""
    from app.modules.accounts import repository as accounts_repo
    from app.modules.transactions.service import reversal as rev

    engine = _make_engine()
    setup = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        src = accounts_repo.create_account(
            setup, user_id=USER_A, account_number="00770001111", type="AHORRO", currency="PEN"
        )
        dst = accounts_repo.create_account(
            setup, user_id=USER_A, account_number="00770002222", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            setup, src.id, available_delta=10_000, held_delta=0, currency="PEN"
        )
        setup.commit()
        src_id, dst_id = src.id, dst.id

        from app.modules.transactions import service as svc

        tx = svc.transfer_own(
            setup,
            initiator_user_id=USER_A,
            source_account_id=src_id,
            target_account_id=dst_id,
            amount_minor=3_000,
            currency="PEN",
            idempotency_key=f"e2t25-conc-{uuid.uuid4()}",
            balance_port=_real_port(),
        )
        setup.commit()
        tx_id = tx.id
    finally:
        setup.close()

    gate = threading.Barrier(2, timeout=10)
    outcomes: dict[str, str] = {}

    def _worker(name: str):
        session = Session(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            gate.wait(timeout=10)
            result = rev.reverse_transaction(session, tx_id, balance_port=_real_port())
            session.commit()
            outcomes[name] = result["transaction"].status
        except Exception as exc:  # noqa: BLE001 - se reporta como fallo del hilo
            session.rollback()
            outcomes[name] = f"ERROR:{type(exc).__name__}"
        finally:
            session.close()

    threads = [threading.Thread(target=_worker, args=(f"t{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes.values()) == ["REVERSED", "REVERSED"]
    check = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        reversals = [e for e in _entries(check, tx_id) if e.reverses_entry_id is not None]
        assert len(reversals) == 1
    finally:
        check.close()
        engine.dispose()


# ---------------------------------------------------------------- HTTP
def test_http_reverse_happy_replay_and_conflicts(http_client, svc_session):
    session = svc_session["session"]
    tx = _settle_own(svc_session)
    url = f"/api/v1/transactions/{tx.id}/reverse"
    key = f"e2t25-http-{uuid.uuid4()}"
    body = {"reason": "operacion duplicada"}

    first = http_client.post(url, json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["transaction_id"] == str(tx.id)
    assert data["status"] == "REVERSED"
    assert data["reversal_entry_id"] and data["reverses_entry_id"]
    assert len(data["reversal_entry_ids"]) == 1 and len(data["reverses_entry_ids"]) == 1
    assert first.json()["meta"]["replayed"] is False
    session.expire_all()
    assert _avail(session, svc_session["a1"]) == 10_000

    replay = http_client.post(url, json=body, headers={"Idempotency-Key": key})
    assert replay.status_code == 200
    assert replay.json()["data"]["reversal_entry_id"] == data["reversal_entry_id"]
    assert replay.json()["meta"]["replayed"] is True
    session.expire_all()
    reversals = [e for e in _entries(session, tx.id) if e.reverses_entry_id is not None]
    assert len(reversals) == 1  # doble llamada no duplica

    conflict = http_client.post(
        url, json={"reason": "otro motivo"}, headers={"Idempotency-Key": key}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CONFLICT"

    naked = http_client.post(url, json=body)
    assert naked.status_code == 422  # Idempotency-Key obligatoria

    too_long = http_client.post(
        url, json={"reason": "x" * 281}, headers={"Idempotency-Key": f"e2t25-{uuid.uuid4()}"}
    )
    assert too_long.status_code == 422


def test_http_reverse_without_body(svc_session, http_client):
    session = svc_session["session"]
    tx = _settle_own(svc_session)
    resp = http_client.post(
        f"/api/v1/transactions/{tx.id}/reverse",
        headers={"Idempotency-Key": f"e2t25-nobody-{uuid.uuid4()}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "REVERSED"
    session.expire_all()


def test_http_reverse_errors(http_client, svc_session):
    from app.modules.transactions import repository as tx_repo

    session = svc_session["session"]
    before = len(_entries(session))

    unknown = http_client.post(
        f"/api/v1/transactions/{uuid.uuid4()}/reverse",
        json={},
        headers={"Idempotency-Key": f"e2t25-{uuid.uuid4()}"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "NOT_FOUND"

    malformed = http_client.post(
        "/api/v1/transactions/no-es-uuid/reverse",
        json={},
        headers={"Idempotency-Key": f"e2t25-{uuid.uuid4()}"},
    )
    assert malformed.status_code == 404

    foreign = tx_repo.create_transaction(
        session,
        type="OWN_TRANSFER",
        amount_minor=1_000,
        currency="PEN",
        status="SETTLED",
        initiator_user_id=USER_B,
        source_account_id=svc_session["b1"],
        target_account_id=svc_session["a1"],
        idempotency_key=f"e2t25-foreign-{uuid.uuid4()}",
    )
    denied = http_client.post(
        f"/api/v1/transactions/{foreign.id}/reverse",
        json={},
        headers={"Idempotency-Key": f"e2t25-{uuid.uuid4()}"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "NOT_AUTHORIZED"
    session.rollback()

    pending = _tx_in_state(session, "AUTHORIZED")
    locked = http_client.post(
        f"/api/v1/transactions/{pending.id}/reverse",
        json={},
        headers={"Idempotency-Key": f"e2t25-{uuid.uuid4()}"},
    )
    assert locked.status_code == 409
    assert locked.json()["error"]["code"] == "CONFLICT"
    session.rollback()

    assert len(_entries(session)) == before  # ningun error creo asientos


def test_http_reverse_interbank_delegated(http_client, svc_session):
    from app.modules.transactions import repository as tx_repo

    session = svc_session["session"]
    tx = tx_repo.create_transaction(
        session,
        type="INTERBANK_TRANSFER",
        amount_minor=1_000,
        currency="PEN",
        initiator_user_id=USER_A,
        source_account_id=svc_session["a1"],
        idempotency_key=f"e2t25-ib-{uuid.uuid4()}",
    )
    for step in ("VALIDATED", "AUTHORIZED", "FUNDS_HELD", "POSTED"):
        tx = tx_repo.transition_transaction(session, tx.id, step, reason="setup")
    resp = http_client.post(
        f"/api/v1/transactions/{tx.id}/reverse",
        json={"reason": "devolucion"},
        headers={"Idempotency-Key": f"e2t25-{uuid.uuid4()}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "REVERSED"
    session.expire_all()


def test_http_reverse_interbank_not_reversible_conflict(http_client, svc_session):
    """Interbancaria en estado no reversible -> 409 CONFLICT sin asientos.

    Regresion: `reversal.reverse_transaction` delegaba en la saga antes de
    validar el estado; `interbank.reverse_interbank` lanzaba
    `InvalidTransitionError` (ValueError) y el router lo mapeaba a 400
    `VALIDATION_ERROR`. Debe ser 409 `CONFLICT` como la ruta generica.
    """
    from app.modules.transactions import repository as tx_repo

    session = svc_session["session"]
    tx = tx_repo.create_transaction(
        session,
        type="INTERBANK_TRANSFER",
        amount_minor=1_000,
        currency="PEN",
        initiator_user_id=USER_A,
        source_account_id=svc_session["a1"],
        idempotency_key=f"e2t25-ib-norev-{uuid.uuid4()}",
    )
    for step in ("VALIDATED", "AUTHORIZED"):
        tx = tx_repo.transition_transaction(session, tx.id, step, reason="setup")
    tx_id = tx.id
    session.commit()
    before = len(_entries(session))
    assert session.get(tx.__class__, tx_id).status == "AUTHORIZED"

    resp = http_client.post(
        f"/api/v1/transactions/{tx_id}/reverse",
        json={"reason": "devolucion"},
        headers={"Idempotency-Key": f"e2t25-{uuid.uuid4()}"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CONFLICT"
    session.expire_all()
    assert len(_entries(session)) == before  # sin asientos nuevos
    assert session.get(tx.__class__, tx_id).status == "AUTHORIZED"
