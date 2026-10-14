"""QA de billetera HU13: limites y tokenizacion (E4-T05).

Suite de integracion/API sobre el app real (SQLite en memoria +
`TestClient`, sin red, sin `float`), complementaria —sin solapar— a
`E4-T01` (vinculos/tokenizacion), `E4-T02` (express/limites) y `E4-T03`
(adaptador interop). Cierra los CA de HU13 de punta a punta:

- CA-01: vincular cuenta + tarjeta -> `GET /wallet` enmascarado.
- CA-02: express <= base liquida sin reto (< 3 s, asiento cuadrado);
  sobre la base exige refuerzo y agota a `REJECTED` sin debito.
- CA-03: cadena interop determinista que rechaza manipulados.
- CA-04: limite diario por titular (aislado entre titulares).
- Transversales: tokenizacion (crudo en ningun lado), idempotencia
  (un solo asiento), seguridad (aislamiento, 404 neutro, sin PII).

Sin endpoint publico de pago (G66: servicio interno); sin red real.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.modules.accounts.models as _accounts
import app.modules.audit.models as _audit
import app.modules.ledger.models as _ledger
import app.modules.shared.models as _shared
import app.modules.transactions.models as _transactions
import app.modules.wallet.models as _wallet
from app.adapters.wallet_interop import (
    INTEROP_UNAVAILABLE,
    INTEROP_VALIDATION_ERROR,
    InteropUnavailableError,
    InteropValidationError,
    MockWalletInteropGateway,
    decode_payload,
    encode_payload,
)
from app.core.db import Base, get_db
from app.main import app
from app.modules.shared.config import parameters as params
from app.modules.transactions.service import biometric as biometric_service
from app.modules.wallet import service as wallet_service
from app.modules.wallet.api import get_current_user_id
from app.modules.wallet.service import express as express_service

assert _accounts is not None and _audit is not None and _ledger is not None
assert _shared is not None and _transactions is not None and _wallet is not None

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()

ACCOUNT_A = uuid.uuid4()
ACCOUNT_B = uuid.uuid4()
PAYEE = uuid.uuid4()

NUMBER_A = "00110001111111"
NUMBER_B = "00220002222222"
NUMBER_PAYEE = "00330003333333"

VISA_PAN = "4111111111111111"

FUNDED = 1_000_000
EXPRESS_BASE = 20_000
DAILY = 500_000

TABLES = [
    "wallet.wallets",
    "wallet.wallet_links",
    "accounts.accounts",
    "accounts.account_balances",
    "ledger.ledger_accounts",
    "ledger.journal_entries",
    "ledger.postings",
    "ledger.ledger_balances",
    "transactions.transactions",
    "transactions.transaction_status_history",
    "transactions.holds",
    "shared.outbox",
    "shared.processed_events",
    "shared.idempotency_keys",
    "audit.audit_log",
]

SCHEMAS = ("wallet", "accounts", "ledger", "transactions", "shared", "audit", "config")

PII_CANDIDATES = (VISA_PAN, NUMBER_A, NUMBER_B, NUMBER_PAYEE)


def _seed_param(session: Session, key: str, value: str, module: str = "wallet") -> None:
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, :module)"
        ),
        {"id": uuid.uuid4().hex, "key": key, "value": value, "module": module},
    )
    session.flush()


@pytest.fixture()
def svc(monkeypatch):
    """Motor real en SQLite: dos titulares fondeados + vinculos + parametros."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from app.modules.accounts import repository as accounts_repo

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in SCHEMAS:
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE config.parameters (id TEXT PRIMARY KEY, "
                "key TEXT NOT NULL UNIQUE, value_json TEXT NOT NULL, "
                "module TEXT NOT NULL, description TEXT, updated_by TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
        )
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        _seed_param(session, params.WALLET_EXPRESS_LIMIT_MINOR_KEY, str(EXPRESS_BASE))
        _seed_param(session, params.WALLET_DAILY_LIMIT_MINOR_KEY, str(DAILY))
        _seed_param(session, params.AUTH_MAX_FAILED_ATTEMPTS_KEY, "5", module="auth")
        acc_a = accounts_repo.create_account(
            session, user_id=USER_A, account_number=NUMBER_A, type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, acc_a.id, available_delta=FUNDED, held_delta=0, currency="PEN"
        )
        acc_b = accounts_repo.create_account(
            session, user_id=USER_B, account_number=NUMBER_B, type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, acc_b.id, available_delta=FUNDED, held_delta=0, currency="PEN"
        )
        payee = accounts_repo.create_account(
            session,
            user_id=uuid.uuid4(),
            account_number=NUMBER_PAYEE,
            type="AHORRO",
            currency="PEN",
        )
        link_a = wallet_service.link_source(session, USER_A, type="ACCOUNT", account_id=acc_a.id)
        link_b = wallet_service.link_source(session, USER_B, type="ACCOUNT", account_id=acc_b.id)
        session.commit()
        yield {
            "session": session,
            "acc_a": acc_a.id,
            "acc_b": acc_b.id,
            "payee": payee.id,
            "link_a": link_a["id"],
            "link_b": link_b["id"],
        }
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def _fake_accounts(monkeypatch):
    import app.modules.accounts.service as accounts_service

    views = {
        str(ACCOUNT_A): {
            "account_id": ACCOUNT_A,
            "user_id": USER_A,
            "status": "ACTIVE",
            "currency": "PEN",
            "account_number": NUMBER_A,
        },
        str(ACCOUNT_B): {
            "account_id": ACCOUNT_B,
            "user_id": USER_B,
            "status": "ACTIVE",
            "currency": "PEN",
            "account_number": NUMBER_B,
        },
    }

    def _fake(session, account_id):
        return views.get(str(account_id))

    monkeypatch.setattr(accounts_service, "get_account", _fake)


@pytest.fixture()
def api_client(monkeypatch):
    """TestClient con BD SQLite (wallet + shared) y auth falsa como USER_A."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    _fake_accounts(monkeypatch)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("wallet", "shared"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["wallet.wallets"],
            Base.metadata.tables["wallet.wallet_links"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["shared.idempotency_keys"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)

    def _override_db():
        yield session

    def _override_user():
        return USER_A

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = _override_user
    try:
        with TestClient(app) as client:
            yield client, session
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        session.close()
        engine.dispose()


def _balances(session: Session, *account_ids: uuid.UUID) -> dict[str, tuple[int, int]]:
    from app.modules.accounts.models import AccountBalance

    out = {}
    for aid in account_ids:
        row = session.scalars(
            sa.select(AccountBalance).where(AccountBalance.account_id == aid)
        ).one()
        out[str(aid)] = (int(row.available_minor), int(row.held_minor))
    return out


def _tx_count(session: Session) -> int:
    return int(session.execute(sa.text("SELECT COUNT(*) FROM transactions.transactions")).scalar())


def _entry_count(session: Session) -> int:
    return int(session.execute(sa.text("SELECT COUNT(*) FROM ledger.journal_entries")).scalar())


def _assert_balanced_postings(session: Session, tx_id: uuid.UUID) -> None:
    from app.modules.ledger.models import JournalEntry, Posting

    entries = session.scalars(
        sa.select(JournalEntry).where(JournalEntry.transaction_id == tx_id)
    ).all()
    assert entries, "el pago debe dejar asiento(s) en el ledger"
    for entry in entries:
        postings = session.scalars(
            sa.select(Posting).where(Posting.journal_entry_id == entry.id)
        ).all()
        debit = sum(int(p.amount_minor) for p in postings if p.direction == "DEBIT")
        credit = sum(int(p.amount_minor) for p in postings if p.direction == "CREDIT")
        assert debit > 0 and debit == credit, f"asiento descuadrado: {entry.id}"


def _wallet_tables_dump(session: Session) -> str:
    parts = []
    for table in ("wallet.wallets", "wallet.wallet_links"):
        rows = session.execute(sa.text(f"SELECT * FROM {table}")).all()
        parts.append(" ".join(str(v) for row in rows for v in row if v is not None))
    return " ".join(parts)


# ------------------------------------------------------- CA-01: vincular (cuenta + tarjeta)


def test_qa_link_journey_both_sources_masked(api_client):
    """CA-01: cuenta + tarjeta mock -> GET /wallet con ambos enmascarados."""
    client, _session = api_client
    created_acc = client.post(
        "/api/v1/wallet/links", json={"type": "ACCOUNT", "account_id": str(ACCOUNT_A)}
    )
    assert created_acc.status_code == 201, created_acc.text
    created_card = client.post(
        "/api/v1/wallet/links", json={"type": "CARD", "card": {"pan": VISA_PAN}}
    )
    assert created_card.status_code == 201, created_card.text
    wallet = client.get("/api/v1/wallet")
    assert wallet.status_code == 200, wallet.text
    data = wallet.json()["data"]
    assert data["status"] == "ACTIVE" and len(data["links"]) == 2
    by_type = {link["type"]: link for link in data["links"]}
    assert by_type["ACCOUNT"]["account_masked"] == "****1111"
    assert re.fullmatch(r"\*\*\*\*\d{4}", by_type["ACCOUNT"]["account_masked"])
    assert by_type["CARD"]["last4"] == "1111" and by_type["CARD"]["brand"] == "VISA"
    raw = created_acc.text + created_card.text + wallet.text
    for secret in PII_CANDIDATES:
        assert secret not in raw
    assert "token_ref" not in raw and "pan" not in raw.lower()


# ------------------------------------------------------- Tokenizacion de punta a punta


def test_qa_tokenization_never_persists_nor_leaks(api_client, caplog):
    """Tokenizacion: el crudo no esta en BD, respuestas, snapshots ni logs."""
    client, session = api_client
    with caplog.at_level(logging.DEBUG):
        created = client.post(
            "/api/v1/wallet/links",
            json={"type": "CARD", "card": {"pan": VISA_PAN}},
            headers={"Idempotency-Key": "qa-token-1"},
        )
    assert created.status_code == 201, created.text
    assert VISA_PAN not in created.text
    stored = session.execute(
        sa.text("SELECT token_ref, last4, brand FROM wallet.wallet_links")
    ).one()
    assert stored[0].startswith("tok_mock_") and VISA_PAN not in stored[0]
    assert stored[1] == "1111" and stored[2] == "VISA"
    assert VISA_PAN not in _wallet_tables_dump(session)
    snapshot = session.execute(
        sa.text("SELECT response_snapshot FROM shared.idempotency_keys WHERE key = :k"),
        {"k": "qa-token-1"},
    ).scalar()
    text = snapshot if isinstance(snapshot, str) else json.dumps(snapshot)
    assert VISA_PAN not in text and "pan" not in text.lower()
    outbox = session.execute(sa.text("SELECT payload FROM shared.outbox")).all()
    assert VISA_PAN not in json.dumps([r[0] for r in outbox])
    assert VISA_PAN not in caplog.text


# ------------------------------------------------------- CA-02: express bajo el base


def test_qa_express_under_base_settles_balanced_under_3s(svc, caplog):
    """CA-02: pago <= base liquida sin reto, cuadrado y en < 3 s."""
    session = svc["session"]
    start = time.perf_counter()
    with caplog.at_level(logging.INFO, logger="app.modules.wallet.service.express"):
        tx = express_service.pay_express(
            session,
            initiator_user_id=USER_A,
            link_id=svc["link_a"],
            target_account_id=svc["payee"],
            amount_minor=5_000,
            currency="PEN",
            idempotency_key="qa-exp-1",
        )
    elapsed = time.perf_counter() - start
    session.commit()
    assert tx.status == "SETTLED" and tx.type == "THIRD_PARTY_TRANSFER"
    assert "biometric" not in (tx.meta or {})
    assert (tx.meta or {}).get("wallet_link_id") == str(svc["link_a"])
    balances = _balances(session, svc["acc_a"], svc["payee"])
    assert balances[str(svc["acc_a"])] == (FUNDED - 5_000, 0)
    assert balances[str(svc["payee"])] == (5_000, 0)
    _assert_balanced_postings(session, tx.id)
    assert elapsed < 3.0, f"pago express supero 3 s: {elapsed:.3f}s"
    for secret in PII_CANDIDATES:
        assert secret not in caplog.text


# ------------------------------------------------------- CA-02: refuerzo que agota a REJECTED


def test_qa_express_over_base_exhausts_pin_to_preventive_block(svc):
    """CA-02 refuerzo: sobre el base exige PIN; N fallos -> REJECTED sin debito."""
    session = svc["session"]
    params.set_parameter(session, key=params.AUTH_MAX_FAILED_ATTEMPTS_KEY, value=2)
    session.flush()
    amount = EXPRESS_BASE + 1
    tx = express_service.pay_express(
        session,
        initiator_user_id=USER_A,
        link_id=svc["link_a"],
        target_account_id=svc["payee"],
        amount_minor=amount,
        currency="PEN",
        idempotency_key="qa-exp-block-1",
    )
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    assert isinstance(biometric_service.get_block(tx), dict)

    def _wrong(_s, _u, _p):
        return _p == "1234"

    with pytest.raises(biometric_service.NotAuthorizedError):
        biometric_service.authorize_transaction_with_pin(
            session,
            tx.id,
            pin="0000",
            pin_verifier=_wrong,
            daily_limit_reader=express_service.wallet_daily_limit_reader,
        )
    session.flush()
    blocked = biometric_service.authorize_transaction_with_pin(
        session,
        tx.id,
        pin="0000",
        pin_verifier=_wrong,
        daily_limit_reader=express_service.wallet_daily_limit_reader,
    )
    session.commit()
    assert blocked.status == "REJECTED"
    assert blocked.failure_reason == biometric_service.PREVENTIVE_BLOCK
    assert (blocked.meta or {}).get("failure_code") == biometric_service.PREVENTIVE_BLOCK
    holds = session.scalars(
        sa.select(_transactions.Hold).where(_transactions.Hold.transaction_id == tx.id)
    ).all()
    assert holds and all(h.status == "RELEASED" for h in holds)
    balances = _balances(session, svc["acc_a"], svc["payee"])
    assert balances[str(svc["acc_a"])] == (FUNDED, 0)
    assert balances[str(svc["payee"])] == (0, 0)


# ------------------------------------------------------- CA-04: limite diario por titular


def test_qa_daily_limit_rejects_and_is_per_holder(svc):
    """CA-04: acumulado + total > diario -> REJECTED intacto; por titular."""
    session = svc["session"]
    params.set_parameter(session, key=params.WALLET_DAILY_LIMIT_MINOR_KEY, value=10_000)
    session.flush()
    first = express_service.pay_express(
        session,
        initiator_user_id=USER_A,
        link_id=svc["link_a"],
        target_account_id=svc["payee"],
        amount_minor=6_000,
        currency="PEN",
        idempotency_key="qa-daily-a1",
    )
    assert first.status == "SETTLED"
    entries_after_first = _entry_count(session)
    second = express_service.pay_express(
        session,
        initiator_user_id=USER_A,
        link_id=svc["link_a"],
        target_account_id=svc["payee"],
        amount_minor=5_000,
        currency="PEN",
        idempotency_key="qa-daily-a2",
    )
    session.commit()
    assert second.status == "REJECTED"
    assert second.failure_reason == "DAILY_LIMIT_EXCEEDED"
    assert (second.meta or {}).get("failure_code") == "DAILY_LIMIT_EXCEEDED"
    balances = _balances(session, svc["acc_a"], svc["payee"])
    assert balances[str(svc["acc_a"])] == (FUNDED - 6_000, 0)
    assert _entry_count(session) == entries_after_first
    other = express_service.pay_express(
        session,
        initiator_user_id=USER_B,
        link_id=svc["link_b"],
        target_account_id=svc["payee"],
        amount_minor=5_000,
        currency="PEN",
        idempotency_key="qa-daily-b1",
    )
    session.commit()
    assert other.status == "SETTLED"
    balances_b = _balances(session, svc["acc_b"], svc["payee"])
    assert balances_b[str(svc["acc_b"])] == (FUNDED - 5_000, 0)
    assert balances_b[str(svc["payee"])] == (6_000 + 5_000, 0)
    _assert_balanced_postings(session, other.id)


# ------------------------------------------------------- CA-03: interoperabilidad


def test_qa_interop_deterministic_chain_rejects_tampered(caplog):
    """CA-03: encode -> decode -> validate determinista; manipulado se rechaza."""
    gateway = MockWalletInteropGateway()
    kwargs = {
        "issuer": "BO-PE-001",
        "peer": "PAR-BANCO-01",
        "ref": "QR-COBRO-123",
        "amount_minor": 15_000,
        "currency": "PEN",
    }
    first = gateway.encode_payload(**kwargs)
    assert gateway.encode_payload(**kwargs) == first == encode_payload(**kwargs)
    decoded = gateway.decode_payload(first)
    assert decoded == decode_payload(first)
    assert (decoded.issuer, decoded.peer, decoded.amount_minor) == (
        "BO-PE-001",
        "PAR-BANCO-01",
        15_000,
    )
    assert gateway.validate_counterpart(peer_ref="PAR-BANCO-01").valid is True
    tampered = ("A" if first[0] != "A" else "B") + first[1:]
    with pytest.raises(InteropValidationError) as exc_info:
        decode_payload(tampered)
    assert exc_info.value.code == INTEROP_VALIDATION_ERROR
    with pytest.raises(InteropValidationError):
        gateway.validate_counterpart(peer_ref="!!!")
    with pytest.raises(InteropUnavailableError) as exc_info:
        MockWalletInteropGateway(mode="down").validate_counterpart(peer_ref="PAR-BANCO-01")
    assert exc_info.value.code == INTEROP_UNAVAILABLE
    with caplog.at_level(logging.INFO, logger="app.adapters.wallet_interop"):
        gateway.validate_counterpart(peer_ref="PAR-BANCO-01")
    assert "PAR-BANCO-01" not in caplog.text


# ------------------------------------------------------- Idempotencia: un solo asiento


def test_qa_idempotent_replay_single_settlement_no_overdraft(svc):
    """Idempotencia: misma clave = una operacion y un solo debito cuadrado."""
    from app.modules.ledger.models import JournalEntry

    session = svc["session"]
    first = express_service.pay_express(
        session,
        initiator_user_id=USER_A,
        link_id=svc["link_a"],
        target_account_id=svc["payee"],
        amount_minor=5_000,
        currency="PEN",
        idempotency_key="qa-idem-1",
    )
    session.commit()
    entries_before = session.scalars(
        sa.select(JournalEntry).where(JournalEntry.transaction_id == first.id)
    ).all()
    again = express_service.pay_express(
        session,
        initiator_user_id=USER_A,
        link_id=svc["link_a"],
        target_account_id=svc["payee"],
        amount_minor=5_000,
        currency="PEN",
        idempotency_key="qa-idem-1",
    )
    session.commit()
    assert again.id == first.id
    assert _tx_count(session) == 1
    entries_after = session.scalars(
        sa.select(JournalEntry).where(JournalEntry.transaction_id == first.id)
    ).all()
    assert len(entries_after) == len(entries_before) >= 1
    balances = _balances(session, svc["acc_a"], svc["payee"])
    assert balances[str(svc["acc_a"])] == (FUNDED - 5_000, 0)
    assert balances[str(svc["payee"])] == (5_000, 0)
    _assert_balanced_postings(session, first.id)
    with pytest.raises(express_service.ExpressConflictError):
        express_service.pay_express(
            session,
            initiator_user_id=USER_A,
            link_id=svc["link_a"],
            target_account_id=svc["payee"],
            amount_minor=7_000,
            currency="PEN",
            idempotency_key="qa-idem-1",
        )
    session.rollback()
    assert _tx_count(session) == 1
    assert _balances(session, svc["acc_a"], svc["payee"])[str(svc["acc_a"])] == (
        FUNDED - 5_000,
        0,
    )


# ------------------------------------------------------- Seguridad: aislamiento + 404 neutro


def test_qa_cross_holder_neutral_404_and_wallet_isolation(svc, api_client):
    """Aislamiento: vinculo ajeno -> NOT_FOUND neutro; billeteras no se ven."""
    session = svc["session"]
    before = _tx_count(session)
    with pytest.raises(express_service.ExpressNotFoundError) as exc_info:
        express_service.pay_express(
            session,
            initiator_user_id=USER_B,
            link_id=svc["link_a"],
            target_account_id=svc["payee"],
            amount_minor=1_000,
            currency="PEN",
            idempotency_key="qa-foreign-1",
        )
    assert exc_info.value.code == "NOT_FOUND"
    session.rollback()
    assert _tx_count(session) == before
    assert _balances(session, svc["acc_a"], svc["payee"])[str(svc["acc_a"])] == (FUNDED, 0)
    client, _api_session = api_client
    foreign = client.post(
        "/api/v1/wallet/links", json={"type": "ACCOUNT", "account_id": str(ACCOUNT_B)}
    )
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "NOT_FOUND"
    assert NUMBER_B not in foreign.text
    mine = client.get("/api/v1/wallet")
    assert mine.status_code == 200
    assert mine.json()["data"]["links"] == []


# ------------------------------------------------------- Seguridad: sin PII en outbox/auditoria


def test_qa_no_pii_in_outbox_audit(svc):
    """Sin PII: outbox + auditoria del ciclo express sin numeros ni PAN."""
    from app.modules.shared.models import OutboxEntry

    session = svc["session"]
    tx = express_service.pay_express(
        session,
        initiator_user_id=USER_A,
        link_id=svc["link_a"],
        target_account_id=svc["payee"],
        amount_minor=5_000,
        currency="PEN",
        idempotency_key="qa-pii-1",
    )
    session.commit()
    kinds = {
        row.event_type
        for row in session.scalars(
            sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx.id)
        ).all()
    }
    assert "transaction.initiated" in kinds and "transfer.settled" in kinds
    payloads = [
        row.payload
        for row in session.scalars(
            sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx.id)
        ).all()
    ]
    blob = json.dumps(payloads, default=str)
    for secret in PII_CANDIDATES:
        assert secret not in blob
    audit_rows = session.execute(
        sa.text("SELECT after_json FROM audit.audit_log WHERE action = :action"),
        {"action": express_service.AUDIT_ACTION},
    ).all()
    assert len(audit_rows) == 1
    assert all(s not in json.dumps(audit_rows, default=str) for s in PII_CANDIDATES)


# ------------------------------------------------------- Cuadre global


def test_qa_ledger_globally_balanced_after_wallet_cycle(svc):
    """Cuadre: tras vincular + 2 express, sum(debitos) = sum(creditos)."""
    from app.modules.ledger.models import Posting

    session = svc["session"]
    for key, amount in (("qa-ledger-1", 5_000), ("qa-ledger-2", 7_000)):
        tx = express_service.pay_express(
            session,
            initiator_user_id=USER_A,
            link_id=svc["link_a"],
            target_account_id=svc["payee"],
            amount_minor=amount,
            currency="PEN",
            idempotency_key=key,
        )
        assert tx.status == "SETTLED"
    session.commit()
    postings = session.scalars(sa.select(Posting)).all()
    assert postings
    debit = sum(int(p.amount_minor) for p in postings if p.direction == "DEBIT")
    credit = sum(int(p.amount_minor) for p in postings if p.direction == "CREDIT")
    assert debit > 0 and debit == credit
    balances = _balances(session, svc["acc_a"], svc["payee"])
    assert balances[str(svc["acc_a"])] == (FUNDED - 12_000, 0)
    assert balances[str(svc["payee"])] == (12_000, 0)
