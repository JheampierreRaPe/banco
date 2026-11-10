"""Regresion de los fixes M-1/M-2/M-3 (sprint-3, sin commit).

- M-1 (frontera): `wallet` no importa `transactions.repository` ni
  `transactions.models`; el replay del express usa la fachada
  `transactions.service.find_by_idempotency_key`.
- M-2 (aislamiento): un `token_ref` INACTIVE ajeno no se reactiva ni
  expone metadatos (409 generico, fila ajena intacta).
- M-3 (baja): `unlink_source` + `DELETE /wallet/links/{id}` pasan a
  `INACTIVE`, sin contar como activo; repetida/ajena = 404 neutro.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base, get_db
from app.main import app
from app.modules.wallet import repository as wallet_repo
from app.modules.wallet import service as wallet_service
from app.modules.wallet.api import get_current_user_id
from app.modules.wallet.service import express as express_service

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()

VISA_PAN = "4111111111111111"
MASTERCARD_PAN = "5555555555554444"


# ------------------------------------------------------------ fixtures


@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con `wallet` + `shared` (patron E4-T01)."""
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


# ------------------------------------------------------------ M-1 frontera


def test_wallet_never_touches_transactions_tables_directly():
    """M-1: `wallet` solo consume la fachada de `transactions.service`."""
    for module in (express_service, wallet_service):
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "transactions.repository",
            "transactions.models",
        ):
            assert forbidden not in source, f"{module.__name__}: {forbidden}"
    express_source = pathlib.Path(express_service.__file__).read_text(encoding="utf-8")
    assert "find_by_idempotency_key" in express_source


def test_transactions_facade_exposes_find_by_idempotency_key():
    """M-1: la fachada aditiva existe y es la que usa el express."""
    from app.modules.transactions import service as tx_service

    assert callable(tx_service.find_by_idempotency_key)
    assert "find_by_idempotency_key" in tx_service.__all__


# ------------------------------------------------------------ M-2 aislamiento


def test_foreign_inactive_token_is_not_reactivated(sqlite_session):
    """M-2: el `token_ref` INACTIVE ajeno no se reactiva ni se expone."""
    view_a = wallet_service.link_source(sqlite_session, USER_A, type="CARD", card={"pan": VISA_PAN})
    sqlite_session.execute(
        sa.text("UPDATE wallet.wallet_links SET status = 'INACTIVE' WHERE id = :id"),
        {"id": str(view_a["id"]).replace("-", "")},
    )
    with pytest.raises(wallet_repo.DuplicateLinkError):
        wallet_service.link_source(sqlite_session, USER_B, type="CARD", card={"pan": VISA_PAN})
    # La fila ajena sigue INACTIVE y con su billetera original.
    row = wallet_repo.get_link(sqlite_session, view_a["id"])
    assert row is not None and str(row.status) == "INACTIVE"
    wallet_b = wallet_service.get_wallet_view(sqlite_session, USER_B)
    assert wallet_b["links"] == []
    # El titular original si puede reactivar lo propio.
    again = wallet_service.link_source(sqlite_session, USER_A, type="CARD", card={"pan": VISA_PAN})
    assert again["id"] == view_a["id"] and again["status"] == "ACTIVE"


# ------------------------------------------------------------ M-3 baja


def test_unlink_marks_inactive_and_repeat_is_neutral(sqlite_session):
    """M-3 (servicio): baja logica, sin activo restante, repetida = 404."""
    view = wallet_service.link_source(sqlite_session, USER_A, type="CARD", card={"pan": VISA_PAN})
    out = wallet_service.unlink_source(sqlite_session, USER_A, view["id"])
    assert out["id"] == view["id"] and out["status"] == "INACTIVE"
    row = wallet_repo.get_link(sqlite_session, view["id"])
    assert row is not None and str(row.status) == "INACTIVE"
    active = [
        link
        for link in wallet_service.get_wallet_view(sqlite_session, USER_A)["links"]
        if link["status"] == "ACTIVE"
    ]
    assert active == []
    with pytest.raises(wallet_service.WalletLinkNotFoundError):
        wallet_service.unlink_source(sqlite_session, USER_A, view["id"])
    with pytest.raises(wallet_service.WalletLinkNotFoundError):
        wallet_service.unlink_source(sqlite_session, USER_A, uuid.uuid4())
    with pytest.raises(wallet_service.WalletLinkNotFoundError):
        wallet_service.unlink_source(sqlite_session, USER_A, "no-es-uuid")


def test_unlink_foreign_link_is_neutral(sqlite_session):
    """M-3: desvincular un vinculo ajeno es 404 sin tocar la fila."""
    view = wallet_service.link_source(sqlite_session, USER_A, type="CARD", card={"pan": VISA_PAN})
    with pytest.raises(wallet_service.WalletLinkNotFoundError):
        wallet_service.unlink_source(sqlite_session, USER_B, view["id"])
    row = wallet_repo.get_link(sqlite_session, view["id"])
    assert row is not None and str(row.status) == "ACTIVE"


def test_api_delete_link_happy_path_and_neutral(api_client):
    """M-3 (API): DELETE baja a INACTIVE; repetido/ajeno = 404 neutro."""
    created = api_client.post(
        "/api/v1/wallet/links", json={"type": "CARD", "card": {"pan": MASTERCARD_PAN}}
    )
    assert created.status_code == 201, created.text
    link_id = created.json()["data"]["id"]
    deleted = api_client.delete(f"/api/v1/wallet/links/{link_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["id"] == link_id
    assert deleted.json()["data"]["status"] == "INACTIVE"
    wallet = api_client.get("/api/v1/wallet")
    assert wallet.status_code == 200
    assert [lnk for lnk in wallet.json()["data"]["links"] if lnk["status"] == "ACTIVE"] == []
    again = api_client.delete(f"/api/v1/wallet/links/{link_id}")
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "NOT_FOUND"
    missing = api_client.delete(f"/api/v1/wallet/links/{uuid.uuid4()}")
    assert missing.status_code == 404
    malformed = api_client.delete("/api/v1/wallet/links/no-es-uuid")
    assert malformed.status_code == 404
