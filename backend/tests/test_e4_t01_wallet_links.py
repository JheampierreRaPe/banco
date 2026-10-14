"""CRUD de billetera con tokenizacion (E4-T01, HU13 CA-01).

- Parte A (sin BD): tokenizador mock (determinista, valida PAN/Luhn,
  marca por BIN, nunca expone el crudo), enmascarado y fabrica.
- Parte B (SQLite en memoria + schemas ATTACH): alta de cuenta
  (fachada `accounts` simulada) y de tarjeta (mock), duplicados,
  errores neutros, idempotencia, aislamiento por titular,
  reactivacion, evento `wallet.link.created` sin PII y ausencia del
  PAN en BD/logs.
- Parte C (API TestClient): contrato REST (200/201/401/404/409/422),
  `Idempotency-Key`, enmascaramiento y aislamiento.
- Parte D (Postgres `db_session`): humo de integracion; se omite sin BD.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base, get_db
from app.core.errors import AppError
from app.main import app
from app.modules.wallet import api as wallet_api
from app.modules.wallet import repository as wallet_repo
from app.modules.wallet import service as wallet_service
from app.modules.wallet.api import get_current_user_id
from app.modules.wallet.domain import tokenization as tok
from app.modules.wallet.service import tokenization as tok_facade

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()

ACCOUNT_A = uuid.uuid4()
ACCOUNT_FOREIGN = uuid.uuid4()
ACCOUNT_CLOSED = uuid.uuid4()

ACCOUNT_NUMBER_A = "00110001111111"
VISA_PAN = "4111111111111111"
MASTERCARD_PAN = "5555555555554444"
AMEX_PAN = "378282246310005"


def _fake_accounts(monkeypatch, extra: dict | None = None):
    """Simula la fachada `accounts.service.get_account` (E2-T07)."""
    import app.modules.accounts.service as accounts_service

    views = {
        str(ACCOUNT_A): {
            "account_id": ACCOUNT_A,
            "user_id": USER_A,
            "status": "ACTIVE",
            "currency": "PEN",
            "account_number": ACCOUNT_NUMBER_A,
        },
        str(ACCOUNT_FOREIGN): {
            "account_id": ACCOUNT_FOREIGN,
            "user_id": USER_B,
            "status": "ACTIVE",
            "currency": "PEN",
            "account_number": "00220002222222",
        },
        str(ACCOUNT_CLOSED): {
            "account_id": ACCOUNT_CLOSED,
            "user_id": USER_A,
            "status": "CLOSED",
            "currency": "PEN",
            "account_number": "00330003333333",
        },
    }
    if extra:
        views.update(extra)

    def _fake(session, account_id):
        return views.get(str(account_id))

    monkeypatch.setattr(accounts_service, "get_account", _fake)


# ---------------------------------------------------------------- Parte A: tokenizacion pura


def test_tokenize_deterministic_and_masked():
    first = tok.MockWalletTokenizationProvider().tokenize_card(VISA_PAN)
    second = tok.MockWalletTokenizationProvider().tokenize_card("4111 1111 1111 1111")
    assert first == second
    assert first.token_ref.startswith("tok_mock_") and len(first.token_ref) <= 120
    assert first.last4 == "1111" and first.brand == "VISA"
    assert VISA_PAN not in first.token_ref


def test_tokenize_brands():
    provider = tok.MockWalletTokenizationProvider()
    assert provider.tokenize_card(MASTERCARD_PAN).brand == "MASTERCARD"
    assert provider.tokenize_card(AMEX_PAN).brand == "AMEX"
    assert provider.tokenize_card("6011111111111117").brand == "UNKNOWN"


def test_tokenize_rejects_bad_pan():
    provider = tok.MockWalletTokenizationProvider()
    with pytest.raises(ValueError):
        provider.tokenize_card("4111111111111112")  # Luhn invalido
    with pytest.raises(ValueError):
        provider.tokenize_card("1234")
    with pytest.raises(ValueError):
        provider.tokenize_card("4111-1111-1111-111X")
    with pytest.raises(TypeError):
        provider.tokenize_card(4111111111111111)


def test_mask_account_number():
    assert tok.mask_account_number(ACCOUNT_NUMBER_A) == "****1111"
    with pytest.raises(ValueError):
        tok.mask_account_number("123")


def test_provider_factory():
    assert isinstance(tok_facade.get_tokenization_provider(), tok.MockWalletTokenizationProvider)
    assert isinstance(
        tok_facade.get_tokenization_provider("mock"), tok.MockWalletTokenizationProvider
    )
    with pytest.raises(ValueError):
        tok_facade.get_tokenization_provider("proveedor-real")


# ---------------------------------------------------------------- Parte B: SQLite


@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con `wallet` + `shared` (patron E2-T13)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.shared.models as _s  # noqa: F401 (registro de tablas)
    import app.modules.wallet.models as _w  # noqa: F401 (registro de tablas)

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
    try:
        yield session
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def test_link_account_happy_path(sqlite_session, monkeypatch):
    _fake_accounts(monkeypatch)
    view = wallet_service.link_source(sqlite_session, USER_A, type="ACCOUNT", account_id=ACCOUNT_A)
    assert view["type"] == "ACCOUNT" and view["status"] == "ACTIVE"
    assert view["account_masked"] == "****1111"
    assert "account_id" not in view and "token_ref" not in view
    wallet = wallet_service.get_wallet_view(sqlite_session, USER_A)
    assert wallet["status"] == "ACTIVE" and len(wallet["links"]) == 1
    assert wallet["links"][0]["id"] == view["id"]
    # Una billetera por titular (on-demand, UQ).
    assert (
        sqlite_session.execute(
            sa.text("SELECT COUNT(*) FROM wallet.wallets WHERE user_id = :u"),
            {"u": str(USER_A).replace("-", "")},
        ).scalar()
        == 1
    )


def test_link_card_tokenized_pan_never_stored(sqlite_session):
    view = wallet_service.link_source(sqlite_session, USER_A, type="CARD", card={"pan": VISA_PAN})
    assert view["type"] == "CARD" and view["status"] == "ACTIVE"
    assert view["last4"] == "1111" and view["brand"] == "VISA"
    assert "pan" not in view and "token_ref" not in view
    row = sqlite_session.execute(
        sa.text("SELECT token_ref, last4, brand FROM wallet.wallet_links")
    ).one()
    assert row[0].startswith("tok_mock_") and row[1] == "1111" and row[2] == "VISA"
    blob = " ".join(
        str(v)
        for v in sqlite_session.execute(sa.text("SELECT * FROM wallet.wallet_links")).one()
        if v is not None
    )
    assert VISA_PAN not in blob


def test_link_card_pretokenized(sqlite_session):
    view = wallet_service.link_source(
        sqlite_session,
        USER_A,
        type="CARD",
        card={"token_ref": "tok_psp_abc123", "last4": "9999", "brand": "visa"},
    )
    assert view["last4"] == "9999" and view["brand"] == "VISA"
    stored = sqlite_session.execute(sa.text("SELECT token_ref FROM wallet.wallet_links")).scalar()
    assert stored == "tok_psp_abc123"


def test_link_account_neutral_rejections(sqlite_session, monkeypatch):
    _fake_accounts(monkeypatch)
    with pytest.raises(wallet_service.WalletAccountNotFoundError):
        wallet_service.link_source(
            sqlite_session, USER_A, type="ACCOUNT", account_id=ACCOUNT_FOREIGN
        )
    with pytest.raises(wallet_service.WalletAccountNotFoundError):
        wallet_service.link_source(
            sqlite_session, USER_A, type="ACCOUNT", account_id=ACCOUNT_CLOSED
        )
    with pytest.raises(wallet_service.WalletAccountNotFoundError):
        wallet_service.link_source(sqlite_session, USER_A, type="ACCOUNT", account_id=uuid.uuid4())
    with pytest.raises(ValueError):
        wallet_service.link_source(sqlite_session, USER_A, type="ACCOUNT", account_id="no-es-uuid")
    assert wallet_service.get_wallet_view(sqlite_session, USER_A)["links"] == []


def test_link_shape_validation_errors(sqlite_session, monkeypatch):
    _fake_accounts(monkeypatch)
    with pytest.raises(ValueError):
        wallet_service.link_source(sqlite_session, USER_A, type="CASH", card={"pan": VISA_PAN})
    with pytest.raises(ValueError):
        wallet_service.link_source(
            sqlite_session, USER_A, type="ACCOUNT", account_id=ACCOUNT_A, card={"pan": VISA_PAN}
        )
    with pytest.raises(ValueError):
        wallet_service.link_source(sqlite_session, USER_A, type="ACCOUNT")
    with pytest.raises(ValueError):
        wallet_service.link_source(
            sqlite_session, USER_A, type="CARD", card={"pan": VISA_PAN, "token_ref": "x"}
        )
    with pytest.raises(ValueError):
        wallet_service.link_source(
            sqlite_session, USER_A, type="CARD", card={"token_ref": "tok_x", "last4": "12"}
        )
    with pytest.raises(ValueError):
        wallet_service.link_source(sqlite_session, USER_A, type="CARD")


def test_duplicate_link_conflict(sqlite_session, monkeypatch):
    _fake_accounts(monkeypatch)
    wallet_service.link_source(sqlite_session, USER_A, type="ACCOUNT", account_id=ACCOUNT_A)
    with pytest.raises(wallet_repo.DuplicateLinkError):
        wallet_service.link_source(sqlite_session, USER_A, type="ACCOUNT", account_id=ACCOUNT_A)
    wallet_service.link_source(sqlite_session, USER_A, type="CARD", card={"pan": VISA_PAN})
    with pytest.raises(wallet_repo.DuplicateLinkError):
        wallet_service.link_source(sqlite_session, USER_A, type="CARD", card={"pan": VISA_PAN})
    assert len(wallet_service.get_wallet_view(sqlite_session, USER_A)["links"]) == 2


def test_relink_reactivates_inactive(sqlite_session, monkeypatch):
    _fake_accounts(monkeypatch)
    view = wallet_service.link_source(sqlite_session, USER_A, type="ACCOUNT", account_id=ACCOUNT_A)
    sqlite_session.execute(
        sa.text("UPDATE wallet.wallet_links SET status = 'INACTIVE' WHERE id = :id"),
        {"id": str(view["id"]).replace("-", "")},
    )
    again = wallet_service.link_source(sqlite_session, USER_A, type="ACCOUNT", account_id=ACCOUNT_A)
    assert again["id"] == view["id"] and again["status"] == "ACTIVE"
    assert len(wallet_service.get_wallet_view(sqlite_session, USER_A)["links"]) == 1


def test_idempotent_link_replay_and_conflict(sqlite_session, monkeypatch):
    _fake_accounts(monkeypatch)
    body = {"type": "ACCOUNT", "account_id": str(ACCOUNT_A)}
    first, replayed = wallet_service.link_source_idempotent(
        sqlite_session, USER_A, body=body, idempotency_key="clave-alta-1"
    )
    assert replayed is False
    second, replayed = wallet_service.link_source_idempotent(
        sqlite_session, USER_A, body=body, idempotency_key="clave-alta-1"
    )
    assert replayed is True and second["id"] == first["id"]
    assert len(wallet_service.get_wallet_view(sqlite_session, USER_A)["links"]) == 1
    with pytest.raises(wallet_service.IdempotencyConflictError):
        wallet_service.link_source_idempotent(
            sqlite_session,
            USER_A,
            body={"type": "CARD", "card": {"pan": VISA_PAN}},
            idempotency_key="clave-alta-1",
        )
    plain, replayed = wallet_service.link_source_idempotent(
        sqlite_session,
        USER_A,
        body={"type": "CARD", "card": {"pan": VISA_PAN}},
        idempotency_key=None,
    )
    assert replayed is False and plain["type"] == "CARD"


def test_idempotency_snapshot_without_pan(sqlite_session):
    wallet_service.link_source_idempotent(
        sqlite_session,
        USER_A,
        body={"type": "CARD", "card": {"pan": VISA_PAN}},
        idempotency_key="clave-tarjeta",
    )
    snapshot = sqlite_session.execute(
        sa.text("SELECT response_snapshot FROM shared.idempotency_keys WHERE key = :k"),
        {"k": "clave-tarjeta"},
    ).scalar()
    import json as _json

    text = snapshot if isinstance(snapshot, str) else _json.dumps(snapshot)
    assert VISA_PAN not in text and "pan" not in text


def test_isolation_by_holder(sqlite_session, monkeypatch):
    _fake_accounts(monkeypatch)
    wallet_service.link_source(sqlite_session, USER_A, type="ACCOUNT", account_id=ACCOUNT_A)
    assert wallet_service.get_wallet_view(sqlite_session, USER_B) == {
        "wallet_id": None,
        "status": "NONE",
        "links": [],
    }


def test_link_created_event_without_pii(sqlite_session, monkeypatch):
    _fake_accounts(monkeypatch)
    view = wallet_service.link_source(sqlite_session, USER_A, type="CARD", card={"pan": VISA_PAN})
    rows = sqlite_session.execute(
        sa.text("SELECT event_type, status, payload FROM shared.outbox")
    ).all()
    assert rows and all(r[0] == "wallet.link.created" and r[1] == "PENDING" for r in rows)
    import json as _json

    def _as_dict(payload):
        return _json.loads(payload) if isinstance(payload, str) else dict(payload)

    payload_text = _json.dumps([_as_dict(r[2]) for r in rows])
    assert view["id"] in payload_text
    for secret in (VISA_PAN, ACCOUNT_NUMBER_A):
        assert secret not in payload_text


def test_no_pan_in_logs(sqlite_session, caplog):
    import logging as _logging

    with caplog.at_level(_logging.DEBUG):
        wallet_service.link_source(sqlite_session, USER_A, type="CARD", card={"pan": VISA_PAN})
    assert VISA_PAN not in caplog.text
    assert "tok_mock_" not in caplog.text


# ---------------------------------------------------------------- Parte C: API


@pytest.fixture()
def api_client(sqlite_session, monkeypatch):
    """TestClient con BD SQLite y auth falsa como USER_A."""
    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")

    def _override_db():
        yield sqlite_session

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


def test_auth_rejects_without_bearer():
    with pytest.raises(AppError) as exc:
        wallet_api.get_current_user_id(None)
    assert exc.value.code == "NOT_AUTHENTICATED" and exc.value.status_code == 401


def test_api_get_empty_wallet(api_client):
    resp = api_client.get("/api/v1/wallet")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"wallet_id": None, "status": "NONE", "links": []}


def test_api_link_account_happy_path(api_client, monkeypatch):
    _fake_accounts(monkeypatch)
    created = api_client.post(
        "/api/v1/wallet/links", json={"type": "ACCOUNT", "account_id": str(ACCOUNT_A)}
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["type"] == "ACCOUNT" and data["account_masked"] == "****1111"
    assert "token_ref" not in created.text and ACCOUNT_NUMBER_A not in created.text
    assert created.json()["meta"]["replayed"] is False
    wallet = api_client.get("/api/v1/wallet")
    assert wallet.status_code == 200
    assert len(wallet.json()["data"]["links"]) == 1


def test_api_link_card_happy_path(api_client):
    created = api_client.post(
        "/api/v1/wallet/links", json={"type": "CARD", "card": {"pan": VISA_PAN}}
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["last4"] == "1111" and data["brand"] == "VISA"
    assert VISA_PAN not in created.text and "token_ref" not in data


def test_api_errors(api_client, monkeypatch):
    _fake_accounts(monkeypatch)
    foreign = api_client.post(
        "/api/v1/wallet/links", json={"type": "ACCOUNT", "account_id": str(ACCOUNT_FOREIGN)}
    )
    assert foreign.status_code == 404 and foreign.json()["error"]["code"] == "NOT_FOUND"
    bad_type = api_client.post("/api/v1/wallet/links", json={"type": "CASH"})
    assert bad_type.status_code == 422
    both = api_client.post(
        "/api/v1/wallet/links",
        json={"type": "ACCOUNT", "account_id": str(ACCOUNT_A), "card": {"pan": VISA_PAN}},
    )
    assert both.status_code == 422
    api_client.post("/api/v1/wallet/links", json={"type": "ACCOUNT", "account_id": str(ACCOUNT_A)})
    duplicate = api_client.post(
        "/api/v1/wallet/links", json={"type": "ACCOUNT", "account_id": str(ACCOUNT_A)}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "DUPLICATE_LINK"


def test_api_idempotency_key(api_client, monkeypatch):
    _fake_accounts(monkeypatch)
    headers = {"Idempotency-Key": "clave-api-1"}
    first = api_client.post(
        "/api/v1/wallet/links",
        json={"type": "CARD", "card": {"pan": MASTERCARD_PAN}},
        headers=headers,
    )
    assert first.status_code == 201, first.text
    replay = api_client.post(
        "/api/v1/wallet/links",
        json={"type": "CARD", "card": {"pan": MASTERCARD_PAN}},
        headers=headers,
    )
    assert replay.status_code == 201
    assert replay.json()["meta"]["replayed"] is True
    assert replay.json()["data"]["id"] == first.json()["data"]["id"]
    conflict = api_client.post(
        "/api/v1/wallet/links",
        json={"type": "CARD", "card": {"pan": VISA_PAN}},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(api_client.get("/api/v1/wallet").json()["data"]["links"]) == 1


# ---------------------------------------------------------------- Parte D: Postgres (humo)


def test_postgres_wallet_smoke(db_session, monkeypatch):
    """Humo en Postgres migrado: vinculo de tarjeta + lectura."""
    view = wallet_service.link_source(db_session, USER_A, type="CARD", card={"pan": VISA_PAN})
    assert view["status"] == "ACTIVE"
    wallet = wallet_service.get_wallet_view(db_session, USER_A)
    assert any(link["id"] == view["id"] for link in wallet["links"])
