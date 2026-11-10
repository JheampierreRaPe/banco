"""Rol de comercio en `GET /wallet` (E4-T23, HU13/HU14).

- `get_wallet_view` expone `is_merchant`/`merchant_status` (solo lectura,
  sin PII): `True`/`ACTIVE` solo con fila propia `ACTIVE` en
  `wallet.merchant_accounts`; personal/CLOSED/sin billetera -> `False`.
- Contrato API: `GET /wallet` devuelve el flag en `data` sin datos del
  comercio (ni RUC ni nombre).
- Sin regresion: `POST /merchants/qr` del no-comercio sigue `403`.

SQLite en memoria + schemas ATTACH (sin red).
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.modules.shared.models as _shared
import app.modules.wallet.models as _wallet
from app.core.db import Base, get_db
from app.main import app
from app.modules.shared.config import parameters as params
from app.modules.wallet import service as wallet_service
from app.modules.wallet.api import get_current_user_id
from app.modules.wallet.repository import qr as qr_repo

assert _shared is not None and _wallet is not None

USER_PLAIN = uuid.uuid4()  # titular sin comercio
USER_MERCHANT = uuid.uuid4()  # titular con comercio ACTIVE
USER_INACTIVE = uuid.uuid4()  # titular con comercio CLOSED
USER_WALLETLESS = uuid.uuid4()  # titular sin billetera ni comercio

TABLES = [
    "wallet.wallets",
    "wallet.wallet_links",
    "wallet.merchant_accounts",
    "wallet.qr_charges",
    "shared.outbox",
    "shared.idempotency_keys",
]

SCHEMAS = ("wallet", "shared", "config")


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
def e23_session():
    """SQLite con billetera + comercios (uno `ACTIVE`, uno `INACTIVE`)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

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
        _seed_param(session, params.WALLET_QR_TTL_SECONDS_KEY, "300")
        from app.modules.wallet import repository as wallet_repo

        wallet_repo.create_wallet(session, USER_PLAIN)
        wallet_repo.create_wallet(session, USER_MERCHANT)
        wallet_repo.create_wallet(session, USER_INACTIVE)
        qr_repo.create_merchant(session, user_id=USER_MERCHANT, business_name="Bodega A")
        row = qr_repo.create_merchant(session, user_id=USER_INACTIVE, business_name="Cafe B")
        row.status = "CLOSED"
        session.flush()
        session.commit()
        yield session
        session.rollback()
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def e23_client(e23_session, monkeypatch):
    """TestClient con BD SQLite y auth conmutable."""
    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    session = e23_session

    def _override_db():
        yield session

    def _override_user():
        return USER_PLAIN

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = _override_user
    try:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)


def _act_as(user_id: uuid.UUID):
    app.dependency_overrides[get_current_user_id] = lambda: user_id


# ------------------------------------------------------- flag de rol


def test_personal_wallet_view_is_not_merchant(e23_session):
    view = wallet_service.get_wallet_view(e23_session, USER_PLAIN)
    assert view["is_merchant"] is False
    assert view["merchant_status"] is None
    assert view["wallet_id"] is not None and view["status"] == "ACTIVE"
    assert view["links"] == []


def test_merchant_wallet_view_is_merchant_active(e23_session):
    view = wallet_service.get_wallet_view(e23_session, USER_MERCHANT)
    assert view["is_merchant"] is True
    assert view["merchant_status"] == "ACTIVE"
    assert view["wallet_id"] is not None


def test_inactive_merchant_is_not_merchant(e23_session):
    view = wallet_service.get_wallet_view(e23_session, USER_INACTIVE)
    assert view["is_merchant"] is False
    assert view["merchant_status"] == "CLOSED"


def test_walletless_view_defaults_to_not_merchant(e23_session):
    view = wallet_service.get_wallet_view(e23_session, USER_WALLETLESS)
    assert view == {
        "wallet_id": None,
        "status": "NONE",
        "links": [],
        "is_merchant": False,
        "merchant_status": None,
    }


def test_api_wallet_exposes_flag_without_pii(e23_client):
    _act_as(USER_MERCHANT)
    resp = e23_client.get("/api/v1/wallet")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["is_merchant"] is True
    assert data["merchant_status"] == "ACTIVE"
    assert set(data) >= {"wallet_id", "status", "links", "is_merchant"}
    blob = resp.text
    assert "Bodega A" not in blob and "20123456789" not in blob

    _act_as(USER_PLAIN)
    resp = e23_client.get("/api/v1/wallet")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["is_merchant"] is False


def test_merchants_qr_still_403_for_non_merchant(e23_client):
    _act_as(USER_PLAIN)
    resp = e23_client.post(
        "/api/v1/merchants/qr",
        json={"amount_minor": 5_000, "currency": "PEN"},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"
