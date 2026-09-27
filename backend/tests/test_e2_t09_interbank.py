"""Saga interbancaria: estados, timeout, polling y compensacion (E2-T09, HU06).

Sobre el motor y el ledger con adaptadores reales (`ACCOUNTS_BALANCE_PORT`,
cuentas creadas por `accounts.repository`): dinero de juguete (UUIDs
sinteticos, CCI de prueba, sin PII), SQLite en memoria + schemas ATTACH,
sin red, sin `float`.

Mapeo brief -> test:
- feliz via rapida (`CONFIRMED`) y via polling (`ACCEPTED` -> `POSTED` ->
  `SETTLED`); `2200-Transito` cuadra a cero ........ `test_happy_*`
- timeout: `FUNDS_HELD` se mantiene, reintenta y al expirar compensa
  (`FAILED` + `TRANSFER_TIMEOUT`, sin perdida) ......... `test_timeout_*`
- reverso: confirmacion de reverso y reverso explicito con asiento
  inverso, original intacto ........................... `test_reverse_*`
- insuficiente -> `REJECTED` ........................... `test_insufficient`
- titular/CCI: formato, mismatch, caida, timeout, rate limit (sin mover
  dinero, sin filtrar existencia) ..................... `test_holder_*`
- idempotencia (misma clave) y `external_ref` unica .... `test_idempoten*`
- concurrencia: misma clave y doble polling sin duplicar `test_concurrent_*`
- HTTP `POST /transfers/interbank` (202) y
  `GET /transactions/{id}` ............................ `test_http_*`
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.adapters.holder_lookup import (
    HolderLookupUnavailableError,
    HolderNotFoundError,
    HolderRateLimitedError,
    HolderValidationError,
    MockHolderLookupProvider,
    mock_holder_name_for,
)
from app.adapters.interbank_gateway import MockInterbankGateway
from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()

CCI_OK = "00200100200100200101"
CCI_OTHER = "00200100200100200102"
BANK_CODE = "BCP01"

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


@pytest.fixture(autouse=True)
def _clean_holder_cache():
    from app.modules.transactions import service as svc

    svc.clear_holder_cache()
    yield
    svc.clear_holder_cache()


@pytest.fixture()
def svc_session():
    """Sesion SQLite aislada con una cuenta fondeada (100_000 PEN)."""
    from app.modules.accounts import repository as accounts_repo

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        a1 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="00330001111", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a1.id, available_delta=100_000, held_delta=0, currency="PEN"
        )
        session.commit()
        yield {"session": session, "a1": a1.id}
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


def _holder():
    return MockHolderLookupProvider(mode="success")


def _holder_name(cci: str = CCI_OK) -> str:
    return mock_holder_name_for(cci)


def _real_port():
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT

    return ACCOUNTS_BALANCE_PORT


def _avail(session, account_id) -> int:
    from app.modules.accounts import repository as accounts_repo

    return int(accounts_repo.get_balance(session, account_id).available_minor)


def _tx_entries(session, tx_id):
    from app.modules.ledger.models import JournalEntry

    return list(
        session.scalars(sa.select(JournalEntry).where(JournalEntry.transaction_id == tx_id)).all()
    )


def _entry_types(session, tx_id) -> list[str]:
    return sorted(e.entry_type for e in _tx_entries(session, tx_id))


def _postings(session, entry_id):
    from app.modules.ledger import service as ledger_service

    return ledger_service.list_postings(session, entry_id)


def _assert_balanced(session, tx_id) -> None:
    for entry in _tx_entries(session, tx_id):
        rows = _postings(session, entry.id)
        assert len(rows) >= 2
        debit = sum(r.amount_minor for r in rows if r.direction == "DEBIT")
        credit = sum(r.amount_minor for r in rows if r.direction == "CREDIT")
        assert debit == credit > 0


def _transit_net(session) -> int:
    """Neto global de `2200-Transito` (DEBIT +, CREDIT -): debe ser 0."""
    from app.modules.ledger.repository import get_by_code

    transit = get_by_code(session, "2200")
    assert transit is not None
    total = 0
    for entry in _tx_entries_all(session):
        for row in _postings(session, entry.id):
            if str(row.ledger_account_id) == str(transit.id):
                total += row.amount_minor if row.direction == "DEBIT" else -row.amount_minor
    return total


def _tx_entries_all(session):
    from app.modules.ledger.models import JournalEntry

    return list(session.scalars(sa.select(JournalEntry)).all())


def _outbox_types(session, tx_id) -> set[str]:
    from app.modules.shared.models import OutboxEntry

    rows = session.scalars(sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx_id)).all()
    return {r.event_type for r in rows}


def _holds(session, tx_id):
    from app.modules.transactions import repository as tx_repo

    return list(tx_repo.list_holds_by_transaction(session, tx_id))


def _transfer(session, a1, gateway, **overrides):
    from app.modules.transactions.service import interbank as saga

    params = {
        "initiator_user_id": USER_A,
        "source_account_id": a1,
        "amount_minor": 10_000,
        "currency": "PEN",
        "cci": CCI_OK,
        "holder_name": _holder_name(),
        "bank_code": BANK_CODE,
        "fee_minor": 200,
        "idempotency_key": f"e2t09-{uuid.uuid4()}",
        "gateway": gateway,
        "holder_provider": _holder(),
        "timeout_seconds": 300,
        "max_attempts": 5,
        "balance_port": _real_port(),
    }
    params.update(overrides)
    return saga.transfer_interbank(session, **params)


# ---------------------------------------------------------------- Feliz
def test_happy_fast_confirm_settles_with_73_entries(svc_session):
    from app.modules.transactions import repository as tx_repo

    session = svc_session["session"]
    a1 = svc_session["a1"]
    gw = MockInterbankGateway(default_send="confirm")

    tx = _transfer(session, a1, gw)
    assert tx.status == "SETTLED"
    assert tx.type == "INTERBANK_TRANSFER"
    assert tx.external_ref and tx.external_ref.startswith("IB-")
    assert int(tx.fee_minor) == 200
    # Asiento 7.3 en tres pasos (hold total + send + settle con fee a 4100).
    assert _entry_types(session, tx.id) == [
        "INTERBANK_HOLD",
        "INTERBANK_SEND",
        "INTERBANK_SETTLE",
    ]
    _assert_balanced(session, tx.id)
    assert _transit_net(session) == 0, "2200-Transito cuadra a cero"
    # La comision vive en el settle (2100-A -> 4100) y solo se cobra al liquidar.
    from app.modules.ledger.repository import get_by_code

    fee_account = get_by_code(session, "4100")
    assert fee_account is not None
    settle = next(e for e in _tx_entries(session, tx.id) if e.entry_type == "INTERBANK_SETTLE")
    legs = {
        (str(r.ledger_account_id), r.direction): r.amount_minor
        for r in _postings(session, settle.id)
    }
    assert legs[(str(fee_account.id), "CREDIT")] == 200
    # Hold terminal capturado; disponible bajo en monto+fee (fondos salieron).
    (hold,) = _holds(session, tx.id)
    assert hold.status == "CAPTURED" and hold.expires_at is None
    assert _avail(session, a1) == 100_000 - 10_200
    # Historia completa y eventos por outbox.
    assert [h.to_status for h in tx_repo.list_history(session, tx.id)] == [
        "INITIATED",
        "VALIDATED",
        "AUTHORIZED",
        "FUNDS_HELD",
        "POSTED",
        "SETTLED",
    ]
    assert {
        "transaction.initiated",
        "funds.held",
        "transfer.settled",
    } <= _outbox_types(session, tx.id)
    # Sin PII: ni el CCI ni el titular en claro en meta/tracking.
    assert CCI_OK not in str(tx.meta)
    assert _holder_name() not in str(tx.meta)


def test_happy_accept_then_poll_settles_once(svc_session):
    from app.modules.transactions.service import interbank as saga

    session = svc_session["session"]
    a1 = svc_session["a1"]
    gw = MockInterbankGateway(default_send="accept", default_status="confirmed")

    tx = _transfer(session, a1, gw)
    assert tx.status == "POSTED"
    assert _entry_types(session, tx.id) == ["INTERBANK_HOLD", "INTERBANK_SEND"]

    settled = saga.poll_interbank(session, tx.id, gateway=gw, balance_port=_real_port())
    assert settled.status == "SETTLED"
    assert _entry_types(session, settled.id) == [
        "INTERBANK_HOLD",
        "INTERBANK_SEND",
        "INTERBANK_SETTLE",
    ]
    _assert_balanced(session, settled.id)
    assert _transit_net(session) == 0

    before = len(_tx_entries(session, settled.id))
    again = saga.poll_interbank(session, settled.id, gateway=gw, balance_port=_real_port())
    assert again.status == "SETTLED"
    assert len(_tx_entries(session, settled.id)) == before, "doble confirmacion sin doble asiento"


def test_timeout_keeps_held_then_retries_to_settled(svc_session):
    from app.modules.transactions.service import interbank as saga

    session = svc_session["session"]
    a1 = svc_session["a1"]
    gw = MockInterbankGateway()
    gw.program("IB-TIMEOUT-1", ["timeout", "accept"])

    tx = _transfer(session, a1, gw, external_ref="IB-TIMEOUT-1")
    assert tx.status == "FUNDS_HELD", "timeout: se mantiene FUNDS_HELD"
    assert _entry_types(session, tx.id) == ["INTERBANK_HOLD"]
    (hold,) = _holds(session, tx.id)
    assert hold.status == "ACTIVE"
    assert _avail(session, a1) == 100_000 - 10_200

    posted = saga.poll_interbank(session, tx.id, gateway=gw, balance_port=_real_port())
    assert posted.status == "POSTED"
    settled = saga.poll_interbank(session, posted.id, gateway=gw, balance_port=_real_port())
    assert settled.status == "SETTLED"
    assert _transit_net(session) == 0
    assert _avail(session, a1) == 100_000 - 10_200


# ---------------------------------------------------------------- Timeout y compensacion
def test_timeout_expiry_compensates_without_loss(svc_session):
    from app.modules.transactions.service import interbank as saga

    session = svc_session["session"]
    a1 = svc_session["a1"]
    gw = MockInterbankGateway()
    gw.program("IB-EXP-1", ["timeout"] * 5)

    tx = _transfer(session, a1, gw, external_ref="IB-EXP-1", max_attempts=2, timeout_seconds=3600)
    assert tx.status == "FUNDS_HELD"

    first = saga.poll_interbank(session, tx.id, gateway=gw, balance_port=_real_port())
    assert first.status == "FUNDS_HELD", "reintenta: el hold se mantiene"

    failed = saga.poll_interbank(session, first.id, gateway=gw, balance_port=_real_port())
    assert failed.status == "FAILED"
    assert saga.failure_code_of(failed) == "TRANSFER_TIMEOUT"
    # Compensacion: HOLD_RELEASE 2100->2000, hold terminal, disponible restaurado.
    assert "HOLD_RELEASE" in _entry_types(session, failed.id)
    _assert_balanced(session, failed.id)
    (hold,) = _holds(session, failed.id)
    assert hold.status == "RELEASED"
    assert _avail(session, a1) == 100_000, "sin perdida de fondos"
    assert _transit_net(session) == 0
    assert "funds.released" in _outbox_types(session, failed.id)

    replay = saga.poll_interbank(session, failed.id, gateway=gw, balance_port=_real_port())
    assert replay.status == "FAILED"
    assert _entry_types(session, failed.id).count("HOLD_RELEASE") == 1


def test_gateway_reject_compensates_immediately(svc_session):
    session = svc_session["session"]
    a1 = svc_session["a1"]
    gw = MockInterbankGateway()
    gw.program("IB-REJ-1", ["reject"])

    tx = _transfer(session, a1, gw, external_ref="IB-REJ-1")
    assert tx.status == "FAILED"
    from app.modules.transactions.service import interbank as saga

    assert saga.failure_code_of(tx) == "INTERBANK_REJECTED"
    assert _avail(session, a1) == 100_000
    assert _transit_net(session) == 0


def test_posted_gateway_failed_reverses_send_and_releases(svc_session):
    from app.modules.transactions.service import interbank as saga

    session = svc_session["session"]
    a1 = svc_session["a1"]
    gw = MockInterbankGateway()
    gw.program("IB-PF-1", ["accept", "failed"])

    tx = _transfer(session, a1, gw, external_ref="IB-PF-1")
    assert tx.status == "POSTED"
    failed = saga.poll_interbank(session, tx.id, gateway=gw, balance_port=_real_port())
    assert failed.status == "FAILED"
    # El SEND se invierte (original intacto, marcado REVERSED) + HOLD_RELEASE.
    types = _entry_types(session, failed.id)
    assert "HOLD_RELEASE" in types and "INTERBANK_SEND" in types

    sends = [e for e in _tx_entries(session, failed.id) if e.entry_type == "INTERBANK_SEND"]
    originals = [e for e in sends if e.reverses_entry_id is None]
    inverses = [e for e in sends if e.reverses_entry_id is not None]
    assert len(originals) == 1 and originals[0].status == "REVERSED"
    assert len(inverses) == 1 and str(inverses[0].reverses_entry_id) == str(originals[0].id)
    _assert_balanced(session, failed.id)
    assert _avail(session, a1) == 100_000
    assert _transit_net(session) == 0


# ---------------------------------------------------------------- Reverso
def test_reversed_after_settle_returns_funds_with_inverse_entries(svc_session):
    from app.modules.transactions.service import interbank as saga

    session = svc_session["session"]
    a1 = svc_session["a1"]
    gw = MockInterbankGateway(default_send="confirm")

    tx = _transfer(session, a1, gw)
    assert tx.status == "SETTLED"
    reversed_tx = saga.reverse_interbank(
        session,
        tx.id,
        reason="devolucion del banco destino",
        actor="ANALYST",
        balance_port=_real_port(),
    )
    assert reversed_tx.status == "REVERSED"
    # Asientos inversos del transito + retorno al disponible; originales intactos.
    entries = _tx_entries(session, tx.id)
    originals = [e for e in entries if e.reverses_entry_id is None]
    inverses = [e for e in entries if e.reverses_entry_id is not None]
    assert {e.entry_type for e in originals} >= {
        "INTERBANK_HOLD",
        "INTERBANK_SEND",
        "INTERBANK_SETTLE",
    }
    assert len(inverses) >= 2, "SEND y SETTLE invertidos"
    assert "INTERBANK_RETURN" in _entry_types(session, tx.id)
    _assert_balanced(session, tx.id)
    assert _transit_net(session) == 0
    # Anulacion total (incluye la comision: el reverso invierte el settle
    # completo): el disponible vuelve intacto.
    assert _avail(session, a1) == 100_000

    again = saga.reverse_interbank(session, tx.id, balance_port=_real_port())
    assert again.status == "REVERSED"
    assert len(_tx_entries(session, tx.id)) == len(entries), "reverso idempotente"


def test_gateway_reversed_maps_to_reversed_status(svc_session):
    from app.modules.transactions.service import interbank as saga

    session = svc_session["session"]
    a1 = svc_session["a1"]
    gw = MockInterbankGateway()
    gw.program("IB-REV-1", ["accept", "reversed"])

    tx = _transfer(session, a1, gw, external_ref="IB-REV-1")
    out = saga.poll_interbank(session, tx.id, gateway=gw, balance_port=_real_port())
    assert out.status == "REVERSED"
    assert _avail(session, a1) == 100_000
    assert _transit_net(session) == 0


def test_reverse_invalid_state_raises(svc_session):
    from app.modules.transactions.domain.state_machine import InvalidTransitionError
    from app.modules.transactions.service import interbank as saga

    session = svc_session["session"]
    a1 = svc_session["a1"]
    gw = MockInterbankGateway()
    gw.program("IB-NRV-1", ["timeout"])

    tx = _transfer(session, a1, gw, external_ref="IB-NRV-1")
    assert tx.status == "FUNDS_HELD"
    with pytest.raises(InvalidTransitionError):
        saga.reverse_interbank(session, tx.id, balance_port=_real_port())


# ---------------------------------------------------------------- Fondos insuficientes
def test_insufficient_rejected_without_touching_balances(svc_session):
    from app.modules.transactions import repository as tx_repo

    session = svc_session["session"]
    a1 = svc_session["a1"]
    gw = MockInterbankGateway(default_send="confirm")

    tx = _transfer(session, a1, gw, amount_minor=10_000_000)
    assert tx.status == "REJECTED"
    from app.modules.transactions.service import interbank as saga

    assert saga.failure_code_of(tx) == "INSUFFICIENT_FUNDS"
    assert tx_repo.list_holds_by_transaction(session, tx.id) == []
    assert _tx_entries(session, tx.id) == []
    assert _avail(session, a1) == 100_000


# ---------------------------------------------------------------- Titular/CCI (previo a mover dinero)
def test_holder_format_error_moves_no_money(svc_session):
    session = svc_session["session"]
    a1 = svc_session["a1"]
    before = _avail(session, a1)
    with pytest.raises(HolderValidationError):
        _transfer(session, a1, MockInterbankGateway(), cci="123")
    with pytest.raises(HolderValidationError):
        _transfer(session, a1, MockInterbankGateway(), cci="0020010020010020010X")
    assert _avail(session, a1) == before


def test_holder_mismatch_is_neutral_not_found(svc_session):
    session = svc_session["session"]
    a1 = svc_session["a1"]
    before = _avail(session, a1)
    with pytest.raises(HolderNotFoundError):
        _transfer(session, a1, MockInterbankGateway(), holder_name="OTRA PERSONA X")
    assert _avail(session, a1) == before


def test_holder_down_and_timeout_are_unavailable(svc_session):
    session = svc_session["session"]
    a1 = svc_session["a1"]
    before = _avail(session, a1)
    with pytest.raises(HolderLookupUnavailableError):
        _transfer(
            session,
            a1,
            MockInterbankGateway(),
            holder_provider=MockHolderLookupProvider(mode="down"),
        )
    with pytest.raises(HolderLookupUnavailableError):
        _transfer(
            session,
            a1,
            MockInterbankGateway(),
            holder_provider=MockHolderLookupProvider(mode="timeout"),
        )
    with pytest.raises(HolderRateLimitedError):
        from app.adapters.holder_lookup import HolderRateLimitedError as _RL

        class _Limited(MockHolderLookupProvider):
            def lookup_holder(self, *, cci, bank_code=""):
                raise _RL()

        _transfer(session, a1, MockInterbankGateway(), holder_provider=_Limited())
    assert _avail(session, a1) == before


# ---------------------------------------------------------------- Idempotencia y reference unica
def test_same_idempotency_key_single_hold(svc_session):
    session = svc_session["session"]
    a1 = svc_session["a1"]
    gw = MockInterbankGateway(default_send="confirm")
    key = f"e2t09-dup-{uuid.uuid4()}"

    first = _transfer(session, a1, gw, idempotency_key=key)
    second = _transfer(session, a1, gw, idempotency_key=key, amount_minor=10_000)
    assert second.id == first.id
    assert len(_holds(session, first.id)) == 1
    assert _avail(session, a1) == 100_000 - 10_200, "un solo debito"


def test_external_ref_unique_conflicts(svc_session):
    from app.modules.transactions.service.interbank import ExternalRefConflictError

    session = svc_session["session"]
    a1 = svc_session["a1"]
    gw = MockInterbankGateway(default_send="confirm")

    _transfer(session, a1, gw, external_ref="IB-CLASH-1")
    with pytest.raises(ExternalRefConflictError):
        _transfer(session, a1, gw, external_ref="IB-CLASH-1")


# ---------------------------------------------------------------- Concurrencia
def test_concurrent_same_key_single_execution(svc_session):
    from app.modules.transactions.service import interbank as saga

    engine = _make_engine()
    from app.modules.accounts import repository as accounts_repo

    setup = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        src = accounts_repo.create_account(
            setup, user_id=USER_A, account_number="00330009999", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            setup, src.id, available_delta=100_000, held_delta=0, currency="PEN"
        )
        setup.commit()
        src_id = src.id
    finally:
        setup.close()

    key = f"e2t09-conc-{uuid.uuid4()}"
    gate = threading.Lock()
    outcomes: dict[str, str] = {}

    def _worker(name: str):
        from app.modules.transactions.api.idempotency import execute_with_idempotency

        session = Session(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            with gate:
                body = {"source": str(src_id), "amount": 10_000, "cci": CCI_OK}

                def _run():
                    tx = saga.transfer_interbank(
                        session,
                        initiator_user_id=USER_A,
                        source_account_id=src_id,
                        amount_minor=10_000,
                        currency="PEN",
                        cci=CCI_OK,
                        holder_name=_holder_name(),
                        bank_code=BANK_CODE,
                        fee_minor=200,
                        idempotency_key=key,
                        gateway=MockInterbankGateway(default_send="confirm"),
                        holder_provider=_holder(),
                        balance_port=_real_port(),
                    )
                    return ({"id": str(tx.id)}, tx.id)

                result = execute_with_idempotency(
                    session,
                    key=key,
                    user_id=USER_A,
                    endpoint="/transfers/interbank",
                    method="POST",
                    body=body,
                    handler=_run,
                )
                session.commit()
                outcomes[name] = result.response["id"]
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

    assert set(outcomes.values()) == {next(iter(outcomes.values()))}, outcomes
    assert not any(str(v).startswith("ERROR") for v in outcomes.values())
    check = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        assert _avail(check, src_id) == 100_000 - 10_200, "un solo debito concurrente"
    finally:
        check.close()
        engine.dispose()


def test_concurrent_double_poll_single_settle(svc_session):
    from app.modules.transactions.service import interbank as saga

    engine = _make_engine()
    from app.modules.accounts import repository as accounts_repo

    setup = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        src = accounts_repo.create_account(
            setup, user_id=USER_A, account_number="00330008888", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            setup, src.id, available_delta=100_000, held_delta=0, currency="PEN"
        )
        setup.commit()
        src_id = src.id
        seeded = Session(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            tx = saga.transfer_interbank(
                seeded,
                initiator_user_id=USER_A,
                source_account_id=src_id,
                amount_minor=10_000,
                currency="PEN",
                cci=CCI_OK,
                holder_name=_holder_name(),
                bank_code=BANK_CODE,
                fee_minor=200,
                idempotency_key=f"e2t09-dpoll-{uuid.uuid4()}",
                external_ref="IB-DPOLL-1",
                gateway=MockInterbankGateway(default_send="accept"),
                holder_provider=_holder(),
                balance_port=_real_port(),
            )
            seeded.commit()
            tx_id = tx.id
            assert tx.status == "POSTED"
        finally:
            seeded.close()
    finally:
        setup.close()

    gate = threading.Lock()
    outcomes: dict[str, str] = {}

    def _worker(name: str):
        session = Session(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            with gate:
                gw = MockInterbankGateway(default_status="confirmed")
                out = saga.poll_interbank(session, tx_id, gateway=gw, balance_port=_real_port())
                session.commit()
                outcomes[name] = out.status
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

    assert sorted(outcomes.values()) == ["SETTLED", "SETTLED"], outcomes
    check = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        settles = [e for e in _tx_entries(check, tx_id) if e.entry_type == "INTERBANK_SETTLE"]
        assert len(settles) == 1, "doble polling no duplica la liquidacion"
        assert _transit_net(check) == 0
    finally:
        check.close()
        engine.dispose()


# ---------------------------------------------------------------- Job de polling
def test_job_processes_batch_and_expires_by_time(svc_session):
    from app.modules.transactions.jobs.interbank_poll import poll_interbank_once
    from app.modules.transactions.service import interbank as saga

    session = svc_session["session"]
    a1 = svc_session["a1"]
    gw = MockInterbankGateway(default_send="accept", default_status="confirmed")
    gw.program("IB-JOB-EXP", ["timeout", "timeout"])

    ok_tx = _transfer(session, a1, gw, external_ref="IB-JOB-OK")
    assert ok_tx.status == "POSTED"
    exp_tx = _transfer(
        session, a1, gw, external_ref="IB-JOB-EXP", timeout_seconds=60, max_attempts=99
    )
    assert exp_tx.status == "FUNDS_HELD", "timeout en el envio: aun no salio"

    result = poll_interbank_once(session, gateway=gw, balance_port=_real_port())
    assert str(ok_tx.id) in result["processed"] and result["failed"] == []
    session.expire_all()
    from app.modules.transactions import repository as tx_repo

    assert tx_repo.get_transaction(session, ok_tx.id).status == "SETTLED"
    pending = tx_repo.get_transaction(session, exp_tx.id)
    assert pending.status == "FUNDS_HELD", "reintenta: el hold se mantiene"

    future = datetime.now(UTC) + timedelta(hours=2)
    result = poll_interbank_once(session, gateway=gw, now=future, balance_port=_real_port())
    assert str(exp_tx.id) in result["processed"]
    session.expire_all()
    expired = tx_repo.get_transaction(session, exp_tx.id)
    assert expired.status == "FAILED"
    assert saga.failure_code_of(expired) == "TRANSFER_TIMEOUT"
    assert (
        _avail(session, a1) == 100_000 - 10_200
    ), "solo lo confirmado debito; lo expirado se compenso"


# ---------------------------------------------------------------- HTTP
def test_http_interbank_202_replay_and_tracking(http_client, svc_session, monkeypatch):
    import app.modules.transactions.service.interbank as saga_mod

    session = svc_session["session"]
    a1 = svc_session["a1"]
    gw = MockInterbankGateway(default_send="accept", default_status="confirmed")
    monkeypatch.setattr(saga_mod, "create_interbank_gateway", lambda *a, **k: gw)

    key = f"e2t09-http-{uuid.uuid4()}"
    body = {
        "source_account_id": str(a1),
        "amount_minor": 10_000,
        "currency": "PEN",
        "cci": CCI_OK,
        "holder_name": _holder_name(),
        "bank_code": BANK_CODE,
        "fee_minor": 200,
    }
    first = http_client.post(
        "/api/v1/transfers/interbank", json=body, headers={"Idempotency-Key": key}
    )
    assert first.status_code == 202, first.text
    data = first.json()["data"]
    assert data["status"] == "POSTED" and data["type"] == "INTERBANK_TRANSFER"
    assert data["external_ref"] and data["tracking"]["poll_url"].endswith(data["transaction_id"])
    assert data["failure_code"] is None
    assert first.json()["meta"]["replayed"] is False
    assert CCI_OK not in first.text, "sin PII en la respuesta"

    detail = http_client.get(f"/api/v1/transactions/{data['transaction_id']}")
    assert detail.status_code == 200
    assert [h["to_status"] for h in detail.json()["data"]["history"]][-1] == "POSTED"

    replay = http_client.post(
        "/api/v1/transfers/interbank", json=body, headers={"Idempotency-Key": key}
    )
    assert replay.status_code == 202
    assert replay.json()["data"]["transaction_id"] == data["transaction_id"]
    assert replay.json()["meta"]["replayed"] is True
    session.expire_all()
    assert _avail(session, a1) == 100_000 - 10_200, "un solo debito"
    assert len(_holds(session, uuid.UUID(data["transaction_id"]))) == 1

    spec = http_client.get("/openapi.json")
    assert "/api/v1/transfers/interbank" in spec.json()["paths"]
    assert "/api/v1/transactions/{transaction_id}" in spec.json()["paths"]


def test_http_holder_and_account_errors(http_client, svc_session, monkeypatch):
    import app.modules.transactions.service.interbank as saga_mod

    a1 = svc_session["a1"]
    monkeypatch.setattr(
        saga_mod, "create_interbank_gateway", lambda *a, **k: MockInterbankGateway()
    )

    def _post(body, key=None):
        headers = {"Idempotency-Key": key or f"e2t09-err-{uuid.uuid4()}"}
        return http_client.post("/api/v1/transfers/interbank", json=body, headers=headers)

    base = {
        "source_account_id": str(a1),
        "amount_minor": 1_000,
        "currency": "PEN",
        "cci": CCI_OK,
        "holder_name": _holder_name(),
    }
    # Sin clave -> 422.
    assert http_client.post("/api/v1/transfers/interbank", json=base).status_code == 422
    # CCI con formato invalido (no digitos) -> 422 neutro sin PII.
    bad = _post({**base, "cci": "0020010020010020010X"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
    assert CCI_OK not in bad.text and "0020010020010020010X" not in bad.text
    # Titular no coincide -> 404 neutro (igual que CCI inexistente).
    mismatch = _post({**base, "holder_name": "OTRA PERSONA X"})
    assert mismatch.status_code == 404
    assert mismatch.json()["error"]["code"] == "HOLDER_NOT_FOUND"
    assert "OTRA PERSONA" not in mismatch.text
    # Cuenta inexistente -> 404.
    ghost = _post({**base, "source_account_id": str(uuid.uuid4())})
    assert ghost.status_code == 404
    # Misma clave + cuerpo distinto -> 409.
    clash = f"e2t09-hclash-{uuid.uuid4()}"
    headers = {"Idempotency-Key": clash}
    assert (
        http_client.post("/api/v1/transfers/interbank", json=base, headers=headers).status_code
        == 202
    )
    other = {**base, "amount_minor": 2_000}
    assert (
        http_client.post("/api/v1/transfers/interbank", json=other, headers=headers).status_code
        == 409
    )
    # Insuficiente -> 202 REJECTED.
    short = _post({**base, "amount_minor": 50_000_000})
    assert short.status_code == 202
    assert short.json()["data"]["status"] == "REJECTED"
    assert short.json()["data"]["failure_code"] == "INSUFFICIENT_FUNDS"


def test_http_holder_down_is_503(http_client, svc_session, monkeypatch):
    import app.modules.transactions.service.interbank as saga_mod

    a1 = svc_session["a1"]
    monkeypatch.setenv("HOLDER_LOOKUP_MOCK_MODE", "down")
    monkeypatch.setattr(
        saga_mod, "create_interbank_gateway", lambda *a, **k: MockInterbankGateway()
    )
    resp = http_client.post(
        "/api/v1/transfers/interbank",
        json={
            "source_account_id": str(a1),
            "amount_minor": 1_000,
            "currency": "PEN",
            "cci": CCI_OTHER,
            "holder_name": _holder_name(CCI_OTHER),
        },
        headers={"Idempotency-Key": f"e2t09-down-{uuid.uuid4()}"},
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "HOLDER_LOOKUP_UNAVAILABLE"
    assert CCI_OTHER not in resp.text


def test_http_detail_not_found_forbidden_and_bad_id(http_client, svc_session):
    from app.main import app
    from app.modules.transactions.api import get_current_user_id

    a1 = svc_session["a1"]
    gw = MockInterbankGateway(default_send="confirm")
    tx = _transfer(http_client and svc_session["session"], a1, gw)
    # 404 inexistente y 404 id malformado (neutro).
    assert http_client.get(f"/api/v1/transactions/{uuid.uuid4()}").status_code == 404
    assert http_client.get("/api/v1/transactions/no-es-uuid").status_code == 404
    # 200 propia.
    assert http_client.get(f"/api/v1/transactions/{tx.id}").status_code == 200
    # 403 de otro titular.
    app.dependency_overrides[get_current_user_id] = lambda: USER_B
    try:
        assert http_client.get(f"/api/v1/transactions/{tx.id}").status_code == 403
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: USER_A


# ---------------------------------------------------------------- Higiene estatica
def test_static_hygiene_no_float_no_commit_ledger_via_facade():
    from pathlib import Path

    service_src = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "modules"
        / "transactions"
        / "service"
        / "interbank.py"
    ).read_text(encoding="utf-8")
    assert "float(" not in service_src, "dinero entero en centimos, nunca float"
    assert "session.commit" not in service_src, "solo flush; el llamante decide commit"
    assert "from app.core.outbox import record as outbox_record" in service_src
    assert "except ImportError" in service_src
    for token in ("ledger_service.post_entry", "ledger_service.reverse_entry"):
        assert token in service_src, f"ledger solo por fachada: {token}"
    for pattern in ("JournalEntry(", "Posting(", "journal_entries", ".postings"):
        assert pattern not in service_src, f"prohibido en la saga: {pattern}"
    assert CCI_OK not in service_src
    job_src = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "modules"
        / "transactions"
        / "jobs"
        / "interbank_poll.py"
    ).read_text(encoding="utf-8")
    assert "session.commit" not in job_src
    assert "post_entry" not in job_src, "el job no asienta: delega en el servicio"
    assert "limit" in job_src
