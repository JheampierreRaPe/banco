"""Endpoints de cuentas y detalle de saldos (E2-T02, HU05 CA-01/CA-02).

- Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron A/B/C de
  `tests/test_accounts_repository.py`); `get_db` y `get_current_user_id`
  se inyectan via `app.dependency_overrides` (contexto de auth falso).
- Casos: consolidado multi-cuenta (suma bien), enmascaramiento (el numero
  completo nunca aparece en la respuesta), 403 cuenta ajena, 404
  inexistente, 401 sin auth, lista solo con cuentas propias.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_db
from app.main import app
from app.modules.accounts.api import get_current_user_id

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
FULL_A1 = "00110001234"
FULL_A2 = "00110005678"
FULL_B1 = "00220009999"


@pytest.fixture()
def seeded_client():
    """TestClient con BD SQLite aislada y auth falsa como USER_A."""
    import app.modules.accounts.models as _a  # noqa: F401 (registro de tablas)
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401
    import app.modules.transactions.models as _t  # noqa: F401
    from app.modules.accounts import repository as repo

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "ledger", "shared", "transactions"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["accounts.accounts"],
            Base.metadata.tables["accounts.account_balances"],
            Base.metadata.tables["accounts.daily_balance_snapshots"],
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["ledger.journal_entries"],
            Base.metadata.tables["ledger.postings"],
            Base.metadata.tables["ledger.ledger_balances"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["shared.processed_events"],
            Base.metadata.tables["transactions.transactions"],
            Base.metadata.tables["transactions.transaction_status_history"],
            Base.metadata.tables["transactions.holds"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        a1 = repo.create_account(
            session, user_id=USER_A, account_number=FULL_A1, type="AHORRO", currency="PEN"
        )
        a2 = repo.create_account(
            session, user_id=USER_A, account_number=FULL_A2, type="CORRIENTE", currency="PEN"
        )
        b1 = repo.create_account(
            session, user_id=USER_B, account_number=FULL_B1, type="AHORRO", currency="PEN"
        )
        # Estado de la proyeccion (lectura posterior; no calcula saldos nuevos).
        repo.apply_delta(session, a1.id, available_delta=50_000, held_delta=0, currency="PEN")
        repo.apply_delta(session, a1.id, available_delta=-8_000, held_delta=8_000, currency="PEN")
        repo.apply_delta(session, a2.id, available_delta=120_000, held_delta=0, currency="PEN")
        repo.apply_delta(session, b1.id, available_delta=9_999, held_delta=0, currency="PEN")
        session.commit()

        def _override_db():
            yield session

        def _override_user():
            return USER_A

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_user_id] = _override_user
        with TestClient(app) as client:
            yield {
                "client": client,
                "a1": a1.id,
                "a2": a2.id,
                "b1": b1.id,
                "session": session,
            }
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        session.close()
        engine.dispose()


def test_list_consolidates_multiple_accounts(seeded_client):
    client: TestClient = seeded_client["client"]
    resp = client.get("/api/v1/accounts")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"data", "meta"}
    assert body["meta"]["total"] == 2
    assert len(body["data"]) == 2
    by_id = {item["id"]: item for item in body["data"]}
    a1 = by_id[str(seeded_client["a1"])]
    a2 = by_id[str(seeded_client["a2"])]
    # a1: disponible 42_000 + retenido 8_000 = contable 50_000.
    assert (a1["available_minor"], a1["held_minor"], a1["balance_minor"]) == (42_000, 8_000, 50_000)
    assert a1["account_number_masked"] == "****1234"
    assert a1["type"] == "AHORRO" and a1["currency"] == "PEN"
    # a2: 120_000 + 0 = 120_000; consolidado total 170_000.
    assert (a2["available_minor"], a2["held_minor"], a2["balance_minor"]) == (120_000, 0, 120_000)
    assert a2["account_number_masked"] == "****5678"
    total = sum(i["balance_minor"] for i in body["data"])
    assert total == 170_000


def test_list_never_exposes_full_account_number(seeded_client):
    client: TestClient = seeded_client["client"]
    raw = client.get("/api/v1/accounts").text
    assert FULL_A1 not in raw and FULL_A2 not in raw
    detail = client.get(f"/api/v1/accounts/{seeded_client['a1']}").text
    assert FULL_A1 not in detail


def test_list_only_own_accounts(seeded_client):
    client: TestClient = seeded_client["client"]
    ids = {item["id"] for item in client.get("/api/v1/accounts").json()["data"]}
    assert str(seeded_client["b1"]) not in ids


def test_detail_returns_balances(seeded_client):
    client: TestClient = seeded_client["client"]
    resp = client.get(f"/api/v1/accounts/{seeded_client['a1']}")
    assert resp.status_code == 200
    item = resp.json()["data"]
    assert item["account_number_masked"] == "****1234"
    assert (item["available_minor"], item["held_minor"], item["balance_minor"]) == (
        42_000,
        8_000,
        50_000,
    )
    assert item["balance_minor"] == item["available_minor"] + item["held_minor"]
    assert item["status"] == "ACTIVE"


def test_detail_foreign_account_forbidden(seeded_client):
    client: TestClient = seeded_client["client"]
    resp = client.get(f"/api/v1/accounts/{seeded_client['b1']}")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"


def test_detail_missing_not_found(seeded_client):
    client: TestClient = seeded_client["client"]
    resp = client.get(f"/api/v1/accounts/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_missing_auth_unauthorized(seeded_client):
    client: TestClient = seeded_client["client"]
    app.dependency_overrides.pop(get_current_user_id, None)
    try:
        resp = client.get("/api/v1/accounts")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: USER_A
