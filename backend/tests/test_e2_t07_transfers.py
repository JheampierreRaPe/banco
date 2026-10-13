"""Casos de uso de transferencia propia y a tercero (E2-T07, HU06 CA-01/CA-03).

Sobre el motor y el ledger con adaptadores reales (`ACCOUNTS_BALANCE_PORT`,
cuentas creadas por `accounts.repository`): dinero de juguete (UUIDs
sinteticos, sin PII), SQLite en memoria + schemas ATTACH, sin red, sin
`float`.

Mapeo brief -> test:
- propia atomica (un asiento `2000-S -> 2000-D`, sin hold) .. `test_own_*`
- tercero hold+settle, comision a `4100` .................. `test_third_*`
- insuficiente -> REJECTED sin tocar saldos ................ `test_insufficient_*`
- errores (destino, moneda, monto, misma cuenta, ajena, BLOCKED) varios
- idempotencia (misma clave) y concurrencia sin sobregiro .. `test_*idempoten*`, `test_concurrent_*`
- HTTP `POST /transfers/own|third-party` ................... `test_http_*`
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


def _held(session, account_id) -> int:
    from app.modules.accounts import repository as accounts_repo

    return int(accounts_repo.get_balance(session, account_id).held_minor)


def _count(session, key: str) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[key]))


def _entries(session):
    from app.modules.ledger.models import JournalEntry

    return list(session.scalars(sa.select(JournalEntry)).all())


def _postings(session, entry_id):
    from app.modules.ledger import service as ledger_service

    return ledger_service.list_postings(session, entry_id)


def _outbox_types(session, tx_id) -> set[str]:
    from app.modules.shared.models import OutboxEntry

    rows = session.scalars(sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx_id)).all()
    return {r.event_type for r in rows}


def _real_port():
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT

    return ACCOUNTS_BALANCE_PORT


# ---------------------------------------------------------------- Propia atomica
def test_own_single_entry_no_hold_balanced(svc_session):
    from app.modules.ledger import service as ledger_service
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions import service as svc

    session = svc_session["session"]
    a1, a2 = svc_session["a1"], svc_session["a2"]

    tx = svc.transfer_own(
        session,
        initiator_user_id=USER_A,
        source_account_id=a1,
        target_account_id=a2,
        amount_minor=3_000,
        currency="PEN",
        idempotency_key=f"e2t07-own-{uuid.uuid4()}",
        balance_port=_real_port(),
    )
    assert tx.status == "SETTLED"
    assert tx.type == "OWN_TRANSFER"
    assert int(tx.fee_minor) == 0
    # Sin hold: ni fila en `holds` ni retenido.
    assert tx_repo.list_holds_by_transaction(session, tx.id) == []
    assert _count(session, "transactions.holds") == 0
    assert _held(session, a1) == 0 and _held(session, a2) == 0
    # Un solo asiento con dos patas 2000-S -> 2000-D.
    entries = _entries(session)
    assert len(entries) == 1
    assert entries[0].entry_type == svc.OWN_SETTLE_ENTRY_TYPE
    rows = _postings(session, entries[0].id)
    assert len(rows) == 2
    debit = sum(r.amount_minor for r in rows if r.direction == "DEBIT")
    credit = sum(r.amount_minor for r in rows if r.direction == "CREDIT")
    assert debit == credit == 3_000
    src_avail, _ = ledger_service.ensure_customer_accounts(session, a1, "PEN")
    dst_avail, _ = ledger_service.ensure_customer_accounts(session, a2, "PEN")
    legs = {(str(r.ledger_account_id), r.direction): r.amount_minor for r in rows}
    assert legs[(str(src_avail.id), "DEBIT")] == 3_000
    assert legs[(str(dst_avail.id), "CREDIT")] == 3_000
    # Saldos coherentes con el ledger.
    assert _avail(session, a1) == 7_000
    assert _avail(session, a2) == 4_000
    # Historial sin FUNDS_HELD.
    assert [h.to_status for h in tx_repo.list_history(session, tx.id)] == [
        "INITIATED",
        "VALIDATED",
        "AUTHORIZED",
        "POSTED",
        "SETTLED",
    ]
    # Eventos: initiated + settled; nunca funds.held.
    from app.core import events as domain_events

    by_tx = _outbox_types(session, tx.id)
    assert domain_events.TRANSACTION_INITIATED in by_tx
    assert domain_events.TRANSFER_SETTLED in by_tx
    assert domain_events.FUNDS_HELD not in by_tx


def test_atomic_path_is_opt_in_and_validates_fee_settle(svc_session):
    from app.modules.transactions import service as svc

    session = svc_session["session"]
    a1, a2 = svc_session["a1"], svc_session["a2"]
    with pytest.raises(ValueError, match="comision"):
        svc.execute_transfer(
            session,
            source_account_id=a1,
            target_account_id=a2,
            amount_minor=100,
            currency="PEN",
            fee_minor=5,
            atomic=True,
            balance_port=_real_port(),
        )
    with pytest.raises(ValueError, match="settle=True"):
        svc.execute_transfer(
            session,
            source_account_id=a1,
            target_account_id=a2,
            amount_minor=100,
            currency="PEN",
            atomic=True,
            settle=False,
            balance_port=_real_port(),
        )
    session.rollback()


def test_state_machine_allows_authorized_to_posted_atomic():
    from app.modules.transactions.domain import can_transition

    assert can_transition("AUTHORIZED", "POSTED")
    assert can_transition("AUTHORIZED", "FUNDS_HELD")


# ---------------------------------------------------------------- Tercero hold+settle
def test_third_party_hold_settle_fee_to_4100(svc_session):
    from app.core import events as domain_events
    from app.modules.ledger.repository import get_by_code
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions import service as svc

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
        idempotency_key=f"e2t07-tp-{uuid.uuid4()}",
        balance_port=_real_port(),
    )
    assert tx.status == "SETTLED"
    assert tx.type == "THIRD_PARTY_TRANSFER"
    holds = tx_repo.list_holds_by_transaction(session, tx.id)
    assert len(holds) == 1 and holds[0].status == "CAPTURED"
    assert holds[0].amount_minor == 3_200
    entries = _entries(session)
    assert len(entries) == 2
    assert _count(session, "ledger.postings") == 6
    # La pata de comision cae en 4100, nunca en 4000.
    fee_account = get_by_code(session, "4100")
    assert fee_account is not None
    legacy = get_by_code(session, "4000")
    found_4100 = found_4000 = False
    for entry in entries:
        for row in _postings(session, entry.id):
            if row.direction == "CREDIT" and row.amount_minor == 200:
                if str(row.ledger_account_id) == str(fee_account.id):
                    found_4100 = True
                if legacy is not None and str(row.ledger_account_id) == str(legacy.id):
                    found_4000 = True
    assert found_4100 and not found_4000
    assert svc.FEE_ACCOUNT_CODE == "4100"
    # Cuadre por asiento y saldos coherentes.
    for entry in entries:
        rows = _postings(session, entry.id)
        debit = sum(r.amount_minor for r in rows if r.direction == "DEBIT")
        credit = sum(r.amount_minor for r in rows if r.direction == "CREDIT")
        assert debit == credit > 0
    assert _avail(session, a1) == 6_800
    assert _avail(session, b1) == 8_000
    assert [h.to_status for h in tx_repo.list_history(session, tx.id)] == [
        "INITIATED",
        "VALIDATED",
        "AUTHORIZED",
        "FUNDS_HELD",
        "POSTED",
        "SETTLED",
    ]
    by_tx = _outbox_types(session, tx.id)
    assert domain_events.FUNDS_HELD in by_tx
    assert domain_events.TRANSFER_SETTLED in by_tx


def test_third_party_without_fee_two_entries_four_postings(svc_session):
    from app.modules.transactions import service as svc

    session = svc_session["session"]
    tx = svc.transfer_third_party(
        session,
        initiator_user_id=USER_A,
        source_account_id=svc_session["a1"],
        target_account_id=svc_session["b1"],
        amount_minor=1_000,
        currency="PEN",
        fee_minor=0,
        idempotency_key=f"e2t07-tp0-{uuid.uuid4()}",
        balance_port=_real_port(),
    )
    assert tx.status == "SETTLED"
    assert len(_entries(session)) == 2
    assert _count(session, "ledger.postings") == 4
    assert _avail(session, svc_session["a1"]) == 9_000
    assert _avail(session, svc_session["b1"]) == 6_000


def test_third_party_fee_defaults_to_config_parameter(svc_session):
    from app.modules.transactions import service as svc

    session = svc_session["session"]
    # Sin tabla `config.parameters` rige el default 0 (best-effort).
    assert svc.resolve_third_party_fee(session, None) == 0
    assert svc.resolve_third_party_fee(session, 150) == 150
    with pytest.raises(ValueError):
        svc.resolve_third_party_fee(session, -1)
    # Con la tabla presente manda la regla configurable (fuera del codigo).
    session.execute(
        sa.text("CREATE TABLE config.parameters (key TEXT PRIMARY KEY, value_json JSON)")
    )
    session.execute(
        sa.text("INSERT INTO config.parameters (key, value_json) VALUES (:k, :v)"),
        {"k": svc.THIRD_PARTY_FEE_PARAM_KEY, "v": "250"},
    )
    assert svc.resolve_third_party_fee(session, None) == 250
    assert svc.resolve_third_party_fee(session, 150) == 150
    session.rollback()


# ---------------------------------------------------------------- Insuficiente -> REJECTED
def test_insufficient_funds_rejected_without_touching_balances(svc_session):
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions import service as svc

    session = svc_session["session"]
    a1, a2 = svc_session["a1"], svc_session["a2"]
    before = (_avail(session, a1), _held(session, a1), _avail(session, a2))

    tx = svc.transfer_own(
        session,
        initiator_user_id=USER_A,
        source_account_id=a1,
        target_account_id=a2,
        amount_minor=50_000,
        currency="PEN",
        balance_port=_real_port(),
    )
    assert tx.status == "REJECTED"
    assert tx_repo.list_holds_by_transaction(session, tx.id) == []
    assert _count(session, "ledger.journal_entries") == 0
    assert _count(session, "ledger.postings") == 0
    assert (_avail(session, a1), _held(session, a1), _avail(session, a2)) == before

    tx2 = svc.transfer_third_party(
        session,
        initiator_user_id=USER_A,
        source_account_id=a1,
        target_account_id=svc_session["b1"],
        amount_minor=9_900,
        currency="PEN",
        fee_minor=200,
        balance_port=_real_port(),
    )
    assert tx2.status == "REJECTED"
    assert _count(session, "ledger.journal_entries") == 0
    assert (_avail(session, a1), _held(session, a1)) == (before[0], before[1])


# ---------------------------------------------------------------- Errores de validacion
def test_validation_errors(svc_session):
    from app.modules.accounts import repository as accounts_repo
    from app.modules.transactions import service as svc

    session = svc_session["session"]
    a1, a2, b1 = svc_session["a1"], svc_session["a2"], svc_session["b1"]

    with pytest.raises(svc.AccountNotFoundError):
        svc.transfer_own(
            session,
            initiator_user_id=USER_A,
            source_account_id=a1,
            target_account_id=uuid.uuid4(),
            amount_minor=100,
            currency="PEN",
            balance_port=_real_port(),
        )
    with pytest.raises(svc.TransferError):
        svc.transfer_own(
            session,
            initiator_user_id=USER_A,
            source_account_id=a1,
            target_account_id=a1,
            amount_minor=100,
            currency="PEN",
            balance_port=_real_port(),
        )
    with pytest.raises(ValueError):
        svc.transfer_own(
            session,
            initiator_user_id=USER_A,
            source_account_id=a1,
            target_account_id=a2,
            amount_minor=0,
            currency="PEN",
            balance_port=_real_port(),
        )
    # Origen ajeno al titular.
    with pytest.raises(svc.AccountNotAuthorizedError):
        svc.transfer_third_party(
            session,
            initiator_user_id=USER_A,
            source_account_id=b1,
            target_account_id=a1,
            amount_minor=100,
            currency="PEN",
            balance_port=_real_port(),
        )
    # Propia a cuenta de otro titular.
    with pytest.raises(svc.AccountNotAuthorizedError):
        svc.transfer_own(
            session,
            initiator_user_id=USER_A,
            source_account_id=a1,
            target_account_id=b1,
            amount_minor=100,
            currency="PEN",
            balance_port=_real_port(),
        )
    # Moneda incoherente sin FX.
    usd = accounts_repo.create_account(
        session, user_id=USER_A, account_number="00110009999", type="AHORRO", currency="USD"
    )
    session.flush()
    with pytest.raises(svc.CurrencyMismatchError):
        svc.transfer_own(
            session,
            initiator_user_id=USER_A,
            source_account_id=a1,
            target_account_id=usd.id,
            amount_minor=100,
            currency="PEN",
            balance_port=_real_port(),
        )
    # Origen BLOCKED (propia) y origen BLOCKED (tercero).
    acc_a1 = session.get(accounts_repo.Account, a1)
    acc_a1.status = "BLOCKED"
    session.flush()
    with pytest.raises(svc.AccountBlockedError):
        svc.transfer_own(
            session,
            initiator_user_id=USER_A,
            source_account_id=a1,
            target_account_id=a2,
            amount_minor=100,
            currency="PEN",
            balance_port=_real_port(),
        )
    acc_a1.status = "ACTIVE"
    session.flush()
    acc_b1 = session.get(accounts_repo.Account, b1)
    acc_b1.status = "BLOCKED"
    session.flush()
    with pytest.raises(svc.AccountBlockedError):
        svc.transfer_third_party(
            session,
            initiator_user_id=USER_B,
            source_account_id=b1,
            target_account_id=a1,
            amount_minor=100,
            currency="PEN",
            fee_minor=0,
            balance_port=_real_port(),
        )
    acc_b1.status = "ACTIVE"
    session.flush()
    session.rollback()
    assert _count(session, "ledger.journal_entries") == 0


# ---------------------------------------------------------------- Idempotencia y concurrencia
def test_idempotency_same_key_single_debit_conflict_on_diff_body(svc_session):
    from app.modules.transactions import service as svc
    from app.modules.transactions.api.idempotency import (
        IdempotencyConflictError,
        execute_with_idempotency,
    )
    from app.modules.transactions.models import Transaction

    session = svc_session["session"]
    a1, a2 = svc_session["a1"], svc_session["a2"]
    key = f"e2t07-idem-{uuid.uuid4()}"
    body = {"source": str(a1), "target": str(a2), "amount": 1000}

    def _run():
        tx = svc.transfer_own(
            session,
            initiator_user_id=USER_A,
            source_account_id=a1,
            target_account_id=a2,
            amount_minor=1_000,
            currency="PEN",
            idempotency_key=key,
            balance_port=_real_port(),
        )
        return ({"data": {"transaction_id": str(tx.id)}}, tx.id)

    first = execute_with_idempotency(
        session,
        key=key,
        user_id=USER_A,
        endpoint="/transfers/own",
        method="POST",
        body=body,
        handler=_run,
    )
    assert first.replayed is False
    second = execute_with_idempotency(
        session,
        key=key,
        user_id=USER_A,
        endpoint="/transfers/own",
        method="POST",
        body=body,
        handler=_run,
    )
    assert second.replayed is True
    assert second.response == first.response
    assert session.scalar(sa.select(sa.func.count()).select_from(Transaction)) == 1
    assert _avail(session, a1) == 9_000  # un solo debito

    with pytest.raises(IdempotencyConflictError):
        execute_with_idempotency(
            session,
            key=key,
            user_id=USER_A,
            endpoint="/transfers/own",
            method="POST",
            body={"source": str(a1), "target": str(a2), "amount": 2000},
            handler=_run,
        )


def test_concurrent_own_transfers_no_overdraft(svc_session):
    """Dos debitos concurrentes: bloqueo en orden estable, sin sobregiro.

    Emula el `SELECT ... FOR UPDATE` productivo con un lock global (mismo
    criterio honesto que E5-T07: SQLite no bloquea filas).
    """
    from app.modules.accounts import repository as accounts_repo
    from app.modules.transactions import service as svc

    engine = _make_engine()
    setup = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        src = accounts_repo.create_account(
            setup, user_id=USER_A, account_number="00990001111", type="AHORRO", currency="PEN"
        )
        dst_a = accounts_repo.create_account(
            setup, user_id=USER_A, account_number="00990002222", type="AHORRO", currency="PEN"
        )
        dst_b = accounts_repo.create_account(
            setup, user_id=USER_A, account_number="00990003333", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            setup, src.id, available_delta=10_000, held_delta=0, currency="PEN"
        )
        setup.commit()
        src_id, dst_a_id, dst_b_id = src.id, dst_a.id, dst_b.id
    finally:
        setup.close()

    gate = threading.Lock()
    outcomes: dict[str, str] = {}

    def _worker(name: str, dst):
        session = Session(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            with gate:
                tx = svc.transfer_own(
                    session,
                    initiator_user_id=USER_A,
                    source_account_id=src_id,
                    target_account_id=dst,
                    amount_minor=8_000,
                    currency="PEN",
                    idempotency_key=f"e2t07-conc-{name}-{uuid.uuid4()}",
                    balance_port=_real_port(),
                )
                session.commit()
                outcomes[name] = tx.status
        except Exception as exc:  # noqa: BLE001 - se reporta como fallo del hilo
            session.rollback()
            outcomes[name] = f"ERROR:{type(exc).__name__}"
        finally:
            session.close()

    threads = [
        threading.Thread(target=_worker, args=("t1", dst_a_id)),
        threading.Thread(target=_worker, args=("t2", dst_b_id)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes.values()) == ["REJECTED", "SETTLED"]
    check = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        total_out = _avail(check, dst_a_id) + _avail(check, dst_b_id)
        assert total_out == 8_000
        assert _avail(check, src_id) == 2_000
    finally:
        check.close()
        engine.dispose()


# ---------------------------------------------------------------- HTTP
def test_http_own_happy_replay_and_openapi(http_client, svc_session):
    session = svc_session["session"]
    a1, a2 = svc_session["a1"], svc_session["a2"]
    key = f"e2t07-http-{uuid.uuid4()}"
    body = {
        "source_account_id": str(a1),
        "target_account_id": str(a2),
        "amount_minor": 1_500,
        "currency": "PEN",
    }
    first = http_client.post("/api/v1/transfers/own", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 201, first.text
    data = first.json()["data"]
    assert data["status"] == "SETTLED" and data["type"] == "OWN_TRANSFER"
    assert data["fee_minor"] == 0 and data["failure_code"] is None
    assert first.json()["meta"]["replayed"] is False

    replay = http_client.post("/api/v1/transfers/own", json=body, headers={"Idempotency-Key": key})
    assert replay.status_code == 201
    assert replay.json()["data"]["transaction_id"] == data["transaction_id"]
    assert replay.json()["meta"]["replayed"] is True
    session.expire_all()
    assert _avail(session, a1) == 8_500  # un solo debito
    assert _avail(session, a2) == 2_500

    spec = http_client.get("/openapi.json")
    assert "/api/v1/transfers/own" in spec.json()["paths"]
    assert "/api/v1/transfers/third-party" in spec.json()["paths"]


def test_http_errors(http_client, svc_session):
    a1, b1 = svc_session["a1"], svc_session["b1"]
    # Sin clave -> 422.
    missing = http_client.post(
        "/api/v1/transfers/own",
        json={
            "source_account_id": str(a1),
            "target_account_id": str(a1),
            "amount_minor": 1,
            "currency": "PEN",
        },
    )
    assert missing.status_code == 422
    # Sin auth -> 401.
    from app.main import app
    from app.modules.transactions.api import get_current_user_id

    app.dependency_overrides.pop(get_current_user_id, None)
    try:
        denied = http_client.post(
            "/api/v1/transfers/own",
            json={
                "source_account_id": str(a1),
                "target_account_id": str(a1),
                "amount_minor": 1,
                "currency": "PEN",
            },
            headers={"Idempotency-Key": f"e2t07-noauth-{uuid.uuid4()}"},
        )
        assert denied.status_code == 401
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: USER_A
    # Misma cuenta -> 400.
    same = http_client.post(
        "/api/v1/transfers/own",
        json={
            "source_account_id": str(a1),
            "target_account_id": str(a1),
            "amount_minor": 1,
            "currency": "PEN",
        },
        headers={"Idempotency-Key": f"e2t07-same-{uuid.uuid4()}"},
    )
    assert same.status_code == 400
    # Destino inexistente -> 404.
    ghost = http_client.post(
        "/api/v1/transfers/third-party",
        json={
            "source_account_id": str(a1),
            "target_account_id": str(uuid.uuid4()),
            "amount_minor": 100,
            "currency": "PEN",
        },
        headers={"Idempotency-Key": f"e2t07-ghost-{uuid.uuid4()}"},
    )
    assert ghost.status_code == 404
    # Propia a cuenta ajena -> 403.
    foreign = http_client.post(
        "/api/v1/transfers/own",
        json={
            "source_account_id": str(a1),
            "target_account_id": str(b1),
            "amount_minor": 100,
            "currency": "PEN",
        },
        headers={"Idempotency-Key": f"e2t07-foreign-{uuid.uuid4()}"},
    )
    assert foreign.status_code == 403
    # Misma clave + cuerpo distinto -> 409.
    clash = f"e2t07-clash-{uuid.uuid4()}"
    headers = {"Idempotency-Key": clash}
    ok_body = {
        "source_account_id": str(a1),
        "target_account_id": str(svc_session["a2"]),
        "amount_minor": 100,
        "currency": "PEN",
    }
    assert (
        http_client.post("/api/v1/transfers/own", json=ok_body, headers=headers).status_code == 201
    )
    other = dict(ok_body, amount_minor=200)
    assert http_client.post("/api/v1/transfers/own", json=other, headers=headers).status_code == 409
    # Monto <= 0 -> 422 de esquema.
    bad = http_client.post(
        "/api/v1/transfers/own",
        json=dict(ok_body, amount_minor=0),
        headers={"Idempotency-Key": f"e2t07-bad-{uuid.uuid4()}"},
    )
    assert bad.status_code == 422


def test_http_third_party_fee_and_insufficient(http_client, svc_session):
    session = svc_session["session"]
    a1, b1 = svc_session["a1"], svc_session["b1"]
    paid = http_client.post(
        "/api/v1/transfers/third-party",
        json={
            "source_account_id": str(a1),
            "target_account_id": str(b1),
            "amount_minor": 2_000,
            "currency": "PEN",
            "fee_minor": 100,
        },
        headers={"Idempotency-Key": f"e2t07-tphttp-{uuid.uuid4()}"},
    )
    assert paid.status_code == 201, paid.text
    assert paid.json()["data"]["status"] == "SETTLED"
    assert paid.json()["data"]["fee_minor"] == 100
    from app.modules.ledger.repository import get_by_code

    session.expire_all()
    fee_account = get_by_code(session, "4100")
    assert fee_account is not None
    assert any(
        str(r.ledger_account_id) == str(fee_account.id)
        and r.direction == "CREDIT"
        and r.amount_minor == 100
        for e in _entries(session)
        for r in _postings(session, e.id)
    )

    before = (_avail(session, a1), _avail(session, b1))
    # E2-T17: el gate de limite diario (CA-03, default 500_000 sin tabla
    # `config`) rechaza 1_000_000 como `DAILY_LIMIT_EXCEEDED` antes de
    # llegar a fondos (cubierto en `test_e2_t17_biometric.py`); para ejercer
    # la via de fondos insuficientes se usa un monto sobre el disponible
    # pero bajo el limite diario y el umbral biometrico.
    short = http_client.post(
        "/api/v1/transfers/third-party",
        json={
            "source_account_id": str(a1),
            "target_account_id": str(b1),
            "amount_minor": 50_000,
            "currency": "PEN",
        },
        headers={"Idempotency-Key": f"e2t07-short-{uuid.uuid4()}"},
    )
    assert short.status_code == 201
    assert short.json()["data"]["status"] == "REJECTED"
    assert short.json()["data"]["failure_code"] == "INSUFFICIENT_FUNDS"
    session.expire_all()
    assert (_avail(session, a1), _avail(session, b1)) == before
