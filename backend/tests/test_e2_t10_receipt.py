"""Comprobante PDF con numero de operacion unico (E2-T10, HU06 CA-04).

Sobre SQLite en memoria + schemas ATTACH (mismo patron que
`test_e2_t07_transfers.py`): dinero de juguete (UUIDs sinteticos, sin
PII), sin red, sin `float`.

Mapeo brief -> test:
- feliz SETTLED -> PDF valido con nº de operacion y montos .. `test_receipt_*`
- nº unico por tx y estable entre llamadas (sin filas nuevas) ..... `test_operation_number_*`
- contenido correcto + cuentas enmascaradas ....................... `test_receipt_*`
- errores 404/403/409/501 ........................................ `test_receipt_*`
"""

from __future__ import annotations

import re
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()

OP_PATTERN = re.compile(r"^OP-[0-9A-F]{12}$")

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
    """Sesion SQLite aislada con cuentas fondeadas (sin PII)."""
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


def _real_port():
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT

    return ACCOUNTS_BALANCE_PORT


def _count(session, key: str) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[key]))


def _snapshot_counts(session) -> dict[str, int]:
    return {
        key: _count(session, key)
        for key in (
            "transactions.transactions",
            "transactions.transaction_status_history",
            "transactions.holds",
            "shared.outbox",
            "ledger.journal_entries",
        )
    }


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
        idempotency_key=f"e2t10-own-{uuid.uuid4()}",
        balance_port=_real_port(),
    )


# ------------------------------------------------------- Numero de operacion
def test_operation_number_format_stable_unique():
    from app.modules.transactions.service import receipt as receipt_svc

    tx_a, tx_b = uuid.uuid4(), uuid.uuid4()
    op_a1 = receipt_svc.operation_number_of(tx_a)
    op_a2 = receipt_svc.operation_number_of(tx_a)
    op_b = receipt_svc.operation_number_of(tx_b)
    assert OP_PATTERN.match(op_a1)
    assert op_a1 == op_a2  # estable entre reintentos
    assert op_a1 != op_b  # unico por transaccion
    assert receipt_svc.operation_number_of(str(tx_a)) == op_a1  # acepta texto


def test_operation_number_rejects_non_uuid():
    from app.modules.transactions.service import receipt as receipt_svc

    with pytest.raises(ValueError):
        receipt_svc.operation_number_of("no-es-uuid")


# ------------------------------------------------------------- Camino feliz
def test_receipt_own_settled_pdf(svc_session):
    from app.modules.transactions.service import receipt as receipt_svc

    session = svc_session["session"]
    tx = _settle_own(svc_session)
    assert tx.status == "SETTLED"

    data, pdf = receipt_svc.get_receipt_pdf(session, tx)
    assert OP_PATTERN.match(data["operation_number"])
    assert data["operation_number"] == receipt_svc.operation_number_of(tx.id)
    assert data["amount_minor"] == 3_000
    assert data["fee_minor"] == 0
    assert data["total_minor"] == 3_000
    assert data["currency"] == "PEN"
    assert data["status"] == "SETTLED"
    assert data["source_account_masked"] == "****1111"
    assert data["destination_account_masked"] == "****2222"
    assert data["created_at"] and data["settled_at"]
    assert pdf.startswith(b"%PDF")
    assert data["operation_number"].encode("ascii") in pdf
    assert b"****1111" in pdf and b"****2222" in pdf
    assert b"00110001111" not in pdf  # numero completo jamas en claro


def test_receipt_third_party_with_fee(svc_session):
    from app.modules.transactions import service as svc
    from app.modules.transactions.service import receipt as receipt_svc

    session = svc_session["session"]
    tx = svc.transfer_third_party(
        session,
        initiator_user_id=USER_A,
        source_account_id=svc_session["a1"],
        target_account_id=svc_session["b1"],
        amount_minor=2_000,
        currency="PEN",
        fee_minor=100,
        idempotency_key=f"e2t10-third-{uuid.uuid4()}",
        balance_port=_real_port(),
    )
    assert tx.status == "SETTLED"
    data, pdf = receipt_svc.get_receipt_pdf(session, tx)
    assert data["fee_minor"] == 100
    assert data["total_minor"] == 2_100
    assert data["destination_account_masked"] == "****3333"
    assert pdf.startswith(b"%PDF")
    assert b"00220003333" not in pdf


def test_receipt_interbank_posted_masked(svc_session):
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import receipt as receipt_svc

    session = svc_session["session"]
    tx = tx_repo.create_transaction(
        session,
        type="INTERBANK_TRANSFER",
        amount_minor=5_000,
        currency="PEN",
        initiator_user_id=USER_A,
        source_account_id=svc_session["a1"],
        fee_minor=50,
        metadata={
            "interbank": {
                "cci_hash": "hash-no-reversible",
                "bank_code": "003",
                "holder_name_masked": "J*** P***",
            }
        },
    )
    for target in ("VALIDATED", "AUTHORIZED", "FUNDS_HELD", "POSTED"):
        tx = tx_repo.transition_transaction(session, tx.id, target, reason="e2t10")
    data, pdf = receipt_svc.get_receipt_pdf(session, tx)
    assert data["destination_account_masked"] == "CCI **** (banco 003)"
    assert data["holder_name_masked"] == "J*** P***"
    assert data["source_account_masked"] == "****1111"
    assert pdf.startswith(b"%PDF")
    assert b"003" in pdf


def test_receipt_http_pdf_and_idempotent(svc_session, http_client):
    tx = _settle_own(svc_session)
    before = _snapshot_counts(svc_session["session"])

    first = http_client.get(f"/api/v1/transactions/{tx.id}/receipt")
    assert first.status_code == 200, first.text
    assert first.headers["content-type"] == "application/pdf"
    assert first.content.startswith(b"%PDF")
    assert "attachment;" in first.headers["content-disposition"]

    second = http_client.get(f"/api/v1/transactions/{tx.id}/receipt")
    assert second.status_code == 200
    # Mismo nº de operacion en ambas respuestas (idempotente).
    from app.modules.transactions.service import receipt as receipt_svc

    expected = receipt_svc.operation_number_of(tx.id).encode("ascii")
    assert expected in first.content and expected in second.content
    # Solo lectura: ni asientos ni filas nuevas.
    assert _snapshot_counts(svc_session["session"]) == before


# ------------------------------------------------------------------- Errores
def test_receipt_404_unknown_and_bad_uuid(http_client):
    missing = http_client.get(f"/api/v1/transactions/{uuid.uuid4()}/receipt")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    bad = http_client.get("/api/v1/transactions/no-es-uuid/receipt")
    assert bad.status_code == 404
    assert bad.json()["error"]["code"] == "NOT_FOUND"


def test_receipt_403_foreign_transaction(svc_session, http_client):
    from app.modules.transactions import service as svc

    session = svc_session["session"]
    foreign = svc.transfer_third_party(
        session,
        initiator_user_id=USER_B,
        source_account_id=svc_session["b1"],
        target_account_id=svc_session["a1"],
        amount_minor=500,
        currency="PEN",
        fee_minor=0,
        idempotency_key=f"e2t10-foreign-{uuid.uuid4()}",
        balance_port=_real_port(),
    )
    # Iniciada por USER_B: ajena para USER_A (autenticado en `http_client`).
    assert foreign.initiator_user_id == USER_B
    resp = http_client.get(f"/api/v1/transactions/{foreign.id}/receipt")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"


def test_receipt_409_rejected_has_no_receipt(svc_session, http_client):
    from app.modules.transactions.service import receipt as receipt_svc

    tx = _settle_own(svc_session, amount_minor=999_999)  # sin fondos -> REJECTED
    assert tx.status == "REJECTED"
    with pytest.raises(receipt_svc.ReceiptNotIssuableError):
        receipt_svc.build_receipt(svc_session["session"], tx)
    resp = http_client.get(f"/api/v1/transactions/{tx.id}/receipt")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_receipt_501_without_pdf_backend(svc_session, http_client, monkeypatch):
    from app.modules.transactions.service import receipt as receipt_svc

    tx = _settle_own(svc_session)
    monkeypatch.setattr(receipt_svc, "pdf_backend_available", lambda: None)
    with pytest.raises(receipt_svc.ReceiptFormatUnavailableError):
        receipt_svc.render_receipt_pdf({"operation_number": "OP-ABC"})
    resp = http_client.get(f"/api/v1/transactions/{tx.id}/receipt")
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "RECEIPT_FORMAT_NOT_SUPPORTED"
