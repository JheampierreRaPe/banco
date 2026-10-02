"""QA de transferencias HU06 (E2-T12).

Suite E2E a nivel API (HTTP) que cierra HU06 sin duplicar las suites de
unidad/servicio (`E2-T07` propia/tercero, `E2-T09` saga interbancaria,
`E2-T10` comprobante, `E2-T25` reverso):

- feliz propia (asiento unico sin hold), tercero (hold+settle, comision a
  `4100`) e interbancaria (`202` + polling hasta `SETTLED`);
- insuficiente -> `REJECTED` sin tocar saldos ni asientos;
- duplicada/idempotencia: misma `Idempotency-Key` = replay (1 debito);
  clave + cuerpo distinto = `409`;
- timeout interbancario: compensacion sin perdida, holds terminales;
- reverso: solo estados permitidos, asiento inverso, `409` si no reversible;
- comprobante: `OP-<12hex>`, cuentas enmascaradas, no emitible en
  `REJECTED`/`FAILED`;
- cuadre `SUM(DEBIT)=SUM(CREDIT)` por asiento y `2200-Transito` a cero;
- eventos `outbox` e historial de estados.

Dinero de juguete (UUIDs sinteticos, CCI de prueba, sin PII), SQLite en
memoria + schemas ATTACH, sin red, sin `float`. Concurrencia/idempotencia
concurrente ya cubierta en `E2-T07`/`E2-T09` (no se duplica aqui).
"""

from __future__ import annotations

import re
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.adapters.holder_lookup import mock_holder_name_for
from app.adapters.interbank_gateway import MockInterbankGateway
from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()

CCI_OK = "00200100200100200101"
BANK_CODE = "BCP01"
OP_PATTERN = re.compile(r"^OP-[0-9A-Fa-f]{12}$")

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
    """Cuentas fondeadas: a1/a2 de USER_A, b1 de USER_B (centimos)."""
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
            session, a1.id, available_delta=100_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a2.id, available_delta=5_000, held_delta=0, currency="PEN"
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


def _holder_name(cci: str = CCI_OK) -> str:
    return mock_holder_name_for(cci)


def _avail(session, account_id) -> int:
    from app.modules.accounts import repository as accounts_repo

    return int(accounts_repo.get_balance(session, account_id).available_minor)


def _held(session, account_id) -> int:
    from app.modules.accounts import repository as accounts_repo

    return int(accounts_repo.get_balance(session, account_id).held_minor)


def _tx_entries(session, tx_id):
    from app.modules.ledger.models import JournalEntry

    return list(
        session.scalars(sa.select(JournalEntry).where(JournalEntry.transaction_id == tx_id)).all()
    )


def _all_entries(session):
    from app.modules.ledger.models import JournalEntry

    return list(session.scalars(sa.select(JournalEntry)).all())


def _postings(session, entry_id):
    from app.modules.ledger import service as ledger_service

    return ledger_service.list_postings(session, entry_id)


def _assert_balanced(session, tx_id) -> None:
    entries = _tx_entries(session, tx_id)
    assert entries, "toda transferencia con movimiento deja asientos"
    for entry in entries:
        rows = _postings(session, entry.id)
        assert len(rows) >= 2
        debit = sum(r.amount_minor for r in rows if r.direction == "DEBIT")
        credit = sum(r.amount_minor for r in rows if r.direction == "CREDIT")
        assert debit == credit > 0, "cuadre SUM(DEBIT)=SUM(CREDIT)"


def _transit_net(session) -> int:
    from app.modules.ledger.repository import get_by_code

    transit = get_by_code(session, "2200")
    assert transit is not None
    total = 0
    for entry in _all_entries(session):
        for row in _postings(session, entry.id):
            if str(row.ledger_account_id) == str(transit.id):
                total += row.amount_minor if row.direction == "DEBIT" else -row.amount_minor
    return total


def _outbox_types(session, tx_id) -> set[str]:
    from app.modules.shared.models import OutboxEntry

    rows = session.scalars(sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx_id)).all()
    return {r.event_type for r in rows}


def _holds(session, tx_id):
    from app.modules.transactions import repository as tx_repo

    return list(tx_repo.list_holds_by_transaction(session, tx_id))


# ------------------------------------------------- Feliz propia (CA-01)
def test_qa_own_happy_settled_single_entry(http_client, svc_session):
    session = svc_session["session"]
    a1, a2 = svc_session["a1"], svc_session["a2"]
    body = {
        "source_account_id": str(a1),
        "target_account_id": str(a2),
        "amount_minor": 3_000,
        "currency": "PEN",
    }
    resp = http_client.post(
        "/api/v1/transfers/own", json=body, headers={"Idempotency-Key": f"e2t12-own-{uuid.uuid4()}"}
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "SETTLED" and data["type"] == "OWN_TRANSFER"
    assert data["fee_minor"] == 0 and data["failure_code"] is None

    session.expire_all()
    tx_id = uuid.UUID(data["transaction_id"])
    entries = _tx_entries(session, tx_id)
    assert len(entries) == 1, "propia: asiento unico sin hold"
    _assert_balanced(session, tx_id)
    assert _holds(session, tx_id) == []
    assert _avail(session, a1) == 100_000 - 3_000
    assert _avail(session, a2) == 5_000 + 3_000
    assert _held(session, a1) == 0

    detail = http_client.get(f"/api/v1/transactions/{tx_id}")
    assert detail.status_code == 200
    history = [h["to_status"] for h in detail.json()["data"]["history"]]
    assert history[-1] == "SETTLED"
    assert "transaction.initiated" in _outbox_types(session, tx_id)
    assert "transfer.settled" in _outbox_types(session, tx_id)


# ---------------------------------- Feliz tercero con comision 4100 (CA-01)
def test_qa_third_party_fee_to_4100(http_client, svc_session):
    from app.modules.ledger.repository import get_by_code

    session = svc_session["session"]
    a1, b1 = svc_session["a1"], svc_session["b1"]
    resp = http_client.post(
        "/api/v1/transfers/third-party",
        json={
            "source_account_id": str(a1),
            "target_account_id": str(b1),
            "amount_minor": 2_000,
            "currency": "PEN",
            "fee_minor": 200,
        },
        headers={"Idempotency-Key": f"e2t12-third-{uuid.uuid4()}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "SETTLED" and data["fee_minor"] == 200

    session.expire_all()
    tx_id = uuid.UUID(data["transaction_id"])
    _assert_balanced(session, tx_id)
    fee_account = get_by_code(session, "4100")
    assert fee_account is not None
    assert any(
        str(r.ledger_account_id) == str(fee_account.id)
        and r.direction == "CREDIT"
        and r.amount_minor == 200
        for e in _tx_entries(session, tx_id)
        for r in _postings(session, e.id)
    ), "comision acreditada a 4100"
    assert _avail(session, a1) == 100_000 - 2_200
    assert _avail(session, b1) == 5_000 + 2_000
    assert _transit_net(session) == 0
    assert "funds.held" in _outbox_types(session, tx_id)
    assert "transfer.settled" in _outbox_types(session, tx_id)


# ------------------------------- Interbancaria 202 + polling a SETTLED (CA-02)
def test_qa_interbank_202_poll_to_settled(http_client, svc_session, monkeypatch):
    import app.modules.transactions.service.interbank as saga_mod
    from app.modules.transactions.service import interbank as saga

    session = svc_session["session"]
    a1 = svc_session["a1"]
    gw = MockInterbankGateway(default_send="accept", default_status="confirmed")
    monkeypatch.setattr(saga_mod, "create_interbank_gateway", lambda *a, **k: gw)

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
        "/api/v1/transfers/interbank",
        json=body,
        headers={"Idempotency-Key": f"e2t12-ib-{uuid.uuid4()}"},
    )
    assert first.status_code == 202, first.text
    data = first.json()["data"]
    assert data["type"] == "INTERBANK_TRANSFER"
    assert data["tracking"]["poll_url"].endswith(data["transaction_id"])
    assert CCI_OK not in first.text, "sin CCI en claro"
    tx_id = uuid.UUID(data["transaction_id"])

    # Polling hasta SETTLED (avanza la saga con la misma pasarela).
    session.expire_all()
    settled = saga.poll_interbank(session, tx_id, gateway=gw, balance_port=_real_port())
    assert settled.status == "SETTLED"
    session.expire_all()
    _assert_balanced(session, tx_id)
    assert _transit_net(session) == 0
    assert _avail(session, a1) == 100_000 - 10_200

    detail = http_client.get(f"/api/v1/transactions/{tx_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "SETTLED"


def _real_port():
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT

    return ACCOUNTS_BALANCE_PORT


# --------------------------------------- Insuficiente REJECTED sin cambios (CA-03)
def test_qa_insufficient_rejected_no_side_effects(http_client, svc_session):
    session = svc_session["session"]
    a1, a2 = svc_session["a1"], svc_session["a2"]
    before = (_avail(session, a1), _avail(session, a2))
    entries_before = len(_all_entries(session))

    resp = http_client.post(
        "/api/v1/transfers/own",
        json={
            "source_account_id": str(a1),
            "target_account_id": str(a2),
            "amount_minor": 200_000,  # sobre el disponible, bajo el limite diario
            "currency": "PEN",
        },
        headers={"Idempotency-Key": f"e2t12-short-{uuid.uuid4()}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "REJECTED"
    assert data["failure_code"] == "INSUFFICIENT_FUNDS"

    session.expire_all()
    assert (_avail(session, a1), _avail(session, a2)) == before, "saldos intactos"
    assert len(_all_entries(session)) == entries_before, "sin asientos"
    assert _tx_entries(session, uuid.UUID(data["transaction_id"])) == []
    assert _holds(session, uuid.UUID(data["transaction_id"])) == []

    receipt = http_client.get(f"/api/v1/transactions/{data['transaction_id']}/receipt")
    assert receipt.status_code == 409, "rechazada no emite comprobante"


# ------------------------------------- Duplicada: replay + 409 (idempotencia)
def test_qa_idempotency_replay_single_debit_and_conflict(http_client, svc_session):
    session = svc_session["session"]
    a1, a2 = svc_session["a1"], svc_session["a2"]
    key = f"e2t12-idem-{uuid.uuid4()}"
    body = {
        "source_account_id": str(a1),
        "target_account_id": str(a2),
        "amount_minor": 1_500,
        "currency": "PEN",
    }
    headers = {"Idempotency-Key": key}
    first = http_client.post("/api/v1/transfers/own", json=body, headers=headers)
    assert first.status_code == 201, first.text
    assert first.json()["meta"]["replayed"] is False

    replay = http_client.post("/api/v1/transfers/own", json=body, headers=headers)
    assert replay.status_code == 201
    assert replay.json()["data"]["transaction_id"] == first.json()["data"]["transaction_id"]
    assert replay.json()["meta"]["replayed"] is True

    session.expire_all()
    assert _avail(session, a1) == 100_000 - 1_500, "un solo debito"
    assert _avail(session, a2) == 5_000 + 1_500

    clash = http_client.post(
        "/api/v1/transfers/own", json={**body, "amount_minor": 1_600}, headers=headers
    )
    assert clash.status_code == 409, "misma clave + cuerpo distinto"


# --------------------------- Timeout interbancario: compensa sin perdida
def test_qa_interbank_timeout_compensates_no_loss(http_client, svc_session):
    from app.modules.transactions.service import interbank as saga

    session = svc_session["session"]
    a1 = svc_session["a1"]
    gw = MockInterbankGateway()
    gw.program("E2T12-TIMEOUT-1", ["timeout"] * 5)

    tx = saga.transfer_interbank(
        session,
        initiator_user_id=USER_A,
        source_account_id=a1,
        amount_minor=10_000,
        currency="PEN",
        cci=CCI_OK,
        holder_name=_holder_name(),
        bank_code=BANK_CODE,
        fee_minor=200,
        idempotency_key=f"e2t12-to-{uuid.uuid4()}",
        external_ref="E2T12-TIMEOUT-1",
        gateway=gw,
        timeout_seconds=3600,
        max_attempts=2,
        balance_port=_real_port(),
    )
    assert tx.status == "FUNDS_HELD"
    first = saga.poll_interbank(session, tx.id, gateway=gw, balance_port=_real_port())
    assert first.status == "FUNDS_HELD", "reintenta con hold vigente"
    failed = saga.poll_interbank(session, first.id, gateway=gw, balance_port=_real_port())
    assert failed.status == "FAILED"
    assert saga.failure_code_of(failed) == "TRANSFER_TIMEOUT"

    session.expire_all()
    _assert_balanced(session, failed.id)
    (hold,) = _holds(session, failed.id)
    assert hold.status == "RELEASED", "holds terminales, nunca huerfanos"
    assert _avail(session, a1) == 100_000, "sin perdida de fondos"
    assert _transit_net(session) == 0
    assert "funds.released" in _outbox_types(session, failed.id)

    detail = http_client.get(f"/api/v1/transactions/{failed.id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "FAILED"
    receipt = http_client.get(f"/api/v1/transactions/{failed.id}/receipt")
    assert receipt.status_code == 409, "fallida no emite comprobante"


# --------------------------------------- CCI/titular neutro sin mover dinero (CA-02)
def test_qa_holder_invalid_neutral_no_money(http_client, svc_session, monkeypatch):
    import app.modules.transactions.service.interbank as saga_mod

    session = svc_session["session"]
    a1 = svc_session["a1"]
    monkeypatch.setattr(
        saga_mod, "create_interbank_gateway", lambda *a, **k: MockInterbankGateway()
    )
    before = _avail(session, a1)
    base = {
        "source_account_id": str(a1),
        "amount_minor": 1_000,
        "currency": "PEN",
        "cci": CCI_OK,
        "holder_name": _holder_name(),
    }

    bad_format = http_client.post(
        "/api/v1/transfers/interbank",
        json={**base, "cci": "0020010020010020010X"},
        headers={"Idempotency-Key": f"e2t12-cci-{uuid.uuid4()}"},
    )
    assert bad_format.status_code == 422
    assert bad_format.json()["error"]["code"] == "VALIDATION_ERROR"

    mismatch = http_client.post(
        "/api/v1/transfers/interbank",
        json={**base, "holder_name": "OTRA PERSONA X"},
        headers={"Idempotency-Key": f"e2t12-holder-{uuid.uuid4()}"},
    )
    assert mismatch.status_code == 404
    assert mismatch.json()["error"]["code"] == "HOLDER_NOT_FOUND"
    assert "OTRA PERSONA" not in mismatch.text, "neutro sin filtrar titular"

    session.expire_all()
    assert _avail(session, a1) == before, "titular/CCI no mueve dinero"


# ---------------------- Reverso: asiento inverso + 409 si no reversible
def test_qa_reverse_settled_inverse_and_409_on_rejected(http_client, svc_session):
    session = svc_session["session"]
    a1, a2 = svc_session["a1"], svc_session["a2"]

    paid = http_client.post(
        "/api/v1/transfers/own",
        json={
            "source_account_id": str(a1),
            "target_account_id": str(a2),
            "amount_minor": 4_000,
            "currency": "PEN",
        },
        headers={"Idempotency-Key": f"e2t12-rev-{uuid.uuid4()}"},
    )
    assert paid.status_code == 201, paid.text
    tx_id = paid.json()["data"]["transaction_id"]
    session.expire_all()
    originals = [e for e in _tx_entries(session, uuid.UUID(tx_id)) if e.reverses_entry_id is None]
    assert len(originals) == 1

    rev = http_client.post(
        f"/api/v1/transactions/{tx_id}/reverse",
        json={"reason": "qa reverso"},
        headers={"Idempotency-Key": f"e2t12-revkey-{uuid.uuid4()}"},
    )
    assert rev.status_code == 200, rev.text
    assert rev.json()["data"]["status"] == "REVERSED"
    session.expire_all()
    inverses = [
        e for e in _tx_entries(session, uuid.UUID(tx_id)) if e.reverses_entry_id is not None
    ]
    assert len(inverses) == 1, "asiento inverso"
    assert str(inverses[0].reverses_entry_id) == str(originals[0].id)
    assert originals[0].status != "DELETED", "original intacto"
    _assert_balanced(session, uuid.UUID(tx_id))
    assert _avail(session, a1) == 100_000
    assert _avail(session, a2) == 5_000

    rejected = http_client.post(
        "/api/v1/transfers/own",
        json={
            "source_account_id": str(a1),
            "target_account_id": str(a2),
            "amount_minor": 200_000,
            "currency": "PEN",
        },
        headers={"Idempotency-Key": f"e2t12-revshort-{uuid.uuid4()}"},
    )
    assert rejected.json()["data"]["status"] == "REJECTED"
    locked = http_client.post(
        f"/api/v1/transactions/{rejected.json()['data']['transaction_id']}/reverse",
        json={},
        headers={"Idempotency-Key": f"e2t12-revlock-{uuid.uuid4()}"},
    )
    assert locked.status_code == 409, "no reversible -> 409"


# -------------------------------- Comprobante OP-12hex y enmascarado (CA-04)
def test_qa_receipt_operation_number_masked(http_client, svc_session):
    from app.modules.transactions.service import receipt as receipt_svc

    paid = http_client.post(
        "/api/v1/transfers/own",
        json={
            "source_account_id": str(svc_session["a1"]),
            "target_account_id": str(svc_session["a2"]),
            "amount_minor": 3_000,
            "currency": "PEN",
        },
        headers={"Idempotency-Key": f"e2t12-rcpt-{uuid.uuid4()}"},
    )
    assert paid.status_code == 201, paid.text
    tx_id = paid.json()["data"]["transaction_id"]

    first = http_client.get(f"/api/v1/transactions/{tx_id}/receipt")
    assert first.status_code == 200, first.text
    assert first.headers["content-type"] == "application/pdf"
    assert first.content.startswith(b"%PDF")
    expected = receipt_svc.operation_number_of(uuid.UUID(tx_id)).encode("ascii")
    assert OP_PATTERN.match(expected.decode("ascii")), "nro operacion OP-<12hex>"
    assert expected in first.content, "mismo nº estable"
    assert b"****1111" in first.content and b"****2222" in first.content
    assert b"00110001111" not in first.content, "cuentas enmascaradas"

    second = http_client.get(f"/api/v1/transactions/{tx_id}/receipt")
    assert second.status_code == 200
    assert expected in second.content, "relectura idempotente"


# --------------------------------- Trazabilidad: outbox + historial + cuadre
def test_qa_outbox_history_and_ledger_square(http_client, svc_session):
    session = svc_session["session"]
    paid = http_client.post(
        "/api/v1/transfers/third-party",
        json={
            "source_account_id": str(svc_session["a1"]),
            "target_account_id": str(svc_session["b1"]),
            "amount_minor": 2_500,
            "currency": "PEN",
            "fee_minor": 100,
        },
        headers={"Idempotency-Key": f"e2t12-trace-{uuid.uuid4()}"},
    )
    assert paid.status_code == 201, paid.text
    tx_id = uuid.UUID(paid.json()["data"]["transaction_id"])

    session.expire_all()
    kinds = _outbox_types(session, tx_id)
    assert "transaction.initiated" in kinds
    assert "funds.held" in kinds
    assert "transfer.settled" in kinds

    detail = http_client.get(f"/api/v1/transactions/{tx_id}")
    assert detail.status_code == 200
    chain = [(h["from_status"], h["to_status"]) for h in detail.json()["data"]["history"]]
    assert chain, "cada transicion deja fila en el historial"
    assert chain[-1][1] == "SETTLED"

    for entry in _all_entries(session):
        rows = _postings(session, entry.id)
        debit = sum(r.amount_minor for r in rows if r.direction == "DEBIT")
        credit = sum(r.amount_minor for r in rows if r.direction == "CREDIT")
        assert debit == credit > 0
