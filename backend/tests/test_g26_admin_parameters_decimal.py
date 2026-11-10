"""Regresion G26: `PUT /admin/parameters` acepta decimales (E3-T07/G26).

El endpoint (E2-T26) solo validaba enteros y devolvia `422` para las
claves decimales `loan.max_dti_ratio` / `loan.review_margin` (hoy solo
editables por SQL/`set_parameter`). Tras el fix, el `PUT` delega en el
soporte decimal de `shared/config` (`set_parameter` + getters decimales).

Cubre (SQLite en memoria + `TestClient`, mismo patron que
`test_e2_t26_admin_parameters.py`):

- Camino feliz: `PUT` con numero JSON / texto numerico / entero actualiza
  `loan.max_dti_ratio` (y `loan.review_margin`) y la fachada lo lee
  correcto (`Decimal`), con auditoria (`updated_by`).
- Errores: texto no numerico / negativo / booleano -> `422` sin escribir.
- RBAC intacto: sin rol admin -> `403` (GET y PUT sobre clave decimal).
- Sin regresion: las claves enteras siguen enteras (`12.5` -> `422`).

Sin PII ni secretos en logs: solo claves, jamas valores.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session


@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con `config.parameters` (sin modelo ORM)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS config")
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
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_param(
    session: Session,
    key: str,
    value: str,
    module: str = "credits",
    description: str | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module, description, "
            "created_at, updated_at) VALUES (:id, :key, :value, :module, :description, "
            ":now, :now)"
        ),
        {
            "id": uuid.uuid4().hex,
            "key": key,
            "value": value,
            "module": module,
            "description": description,
            "now": now,
        },
    )
    session.flush()


def _seed_loan_defaults(session: Session) -> None:
    _seed_param(session, "loan.max_dti_ratio", "0.4")
    _seed_param(session, "loan.max_amount_minor", "5000000")
    _seed_param(session, "loan.min_score", "650")
    _seed_param(session, "loan.review_margin", "50")
    _seed_param(session, "loan.max_debt_minor", "10000000")
    _seed_param(
        session,
        "transfer.daily_limit_minor",
        "500000",
        module="transactions",
    )


@pytest.fixture()
def admin_client(sqlite_session: Session, monkeypatch):
    """`TestClient` con `get_db` a SQLite (worker del outbox desactivado)."""
    from fastapi.testclient import TestClient

    from app.core.db import get_db
    from app.main import app

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")

    def _override():
        yield sqlite_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _bearer(user_id: uuid.UUID, roles: list[str] | None = None) -> dict:
    from app.core.security import create_access_token

    token = create_access_token(subject=str(user_id), roles=roles)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def admin_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def admin_headers(admin_id: uuid.UUID) -> dict:
    return _bearer(admin_id, roles=["admin"])


# ------------------------------------------------------- Camino feliz decimal
def test_put_decimal_number_updates_dti(
    admin_client, sqlite_session, admin_headers, admin_id: uuid.UUID
):
    from app.modules.shared.config import parameters as params

    _seed_loan_defaults(sqlite_session)
    assert params.get_loan_max_dti_ratio(sqlite_session) == Decimal("0.4")
    resp = admin_client.put(
        "/api/v1/admin/parameters/loan.max_dti_ratio",
        json={"value_json": 0.55},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["key"] == "loan.max_dti_ratio"
    # `Decimal` se serializa como texto exacto (sin artefactos binarios).
    assert Decimal(str(body["data"]["value_json"])) == Decimal("0.55")
    assert body["data"]["updated_at"] is not None
    # Se lee correcto por la fachada (Decimal, sin artefactos binarios).
    assert params.get_loan_max_dti_ratio(sqlite_session) == Decimal("0.55")
    row = (
        sqlite_session.execute(
            sa.text("SELECT updated_by FROM config.parameters WHERE key = :key"),
            {"key": params.LOAN_MAX_DTI_RATIO_KEY},
        )
        .mappings()
        .one()
    )
    assert row["updated_by"] == str(admin_id)


def test_put_decimal_string_and_int_margin(admin_client, sqlite_session, admin_headers):
    from app.modules.shared.config import parameters as params

    _seed_loan_defaults(sqlite_session)
    resp = admin_client.put(
        "/api/v1/admin/parameters/loan.max_dti_ratio",
        json={"value_json": "0.45"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert params.get_loan_max_dti_ratio(sqlite_session) == Decimal("0.45")
    # Un entero tambien vale para una clave decimal (se guarda como numero).
    resp = admin_client.put(
        "/api/v1/admin/parameters/loan.review_margin",
        json={"value_json": 60},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert params.get_loan_review_margin(sqlite_session) == Decimal(60)


def test_get_lists_loan_keys_with_credits_module(admin_client, sqlite_session, admin_headers):
    _seed_loan_defaults(sqlite_session)
    resp = admin_client.get("/api/v1/admin/parameters", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    by_key = {item["key"]: item for item in resp.json()["data"]}
    assert Decimal(str(by_key["loan.max_dti_ratio"]["value_json"])) == Decimal("0.4")
    assert Decimal(str(by_key["loan.review_margin"]["value_json"])) == Decimal(50)
    assert by_key["loan.max_dti_ratio"]["module"] == "credits"


# ------------------------------------------------------------------- Errores
def test_put_decimal_invalid_422_without_write(admin_client, sqlite_session, admin_headers):
    from app.modules.shared.config import parameters as params

    _seed_loan_defaults(sqlite_session)
    url = "/api/v1/admin/parameters/loan.max_dti_ratio"
    assert (
        admin_client.put(url, json={"value_json": "no-numero"}, headers=admin_headers).status_code
        == 422
    )
    assert (
        admin_client.put(url, json={"value_json": -0.5}, headers=admin_headers).status_code == 422
    )
    assert (
        admin_client.put(url, json={"value_json": True}, headers=admin_headers).status_code == 422
    )
    assert admin_client.put(url, json={}, headers=admin_headers).status_code == 422
    assert params.get_loan_max_dti_ratio(sqlite_session) == Decimal("0.4")


def test_put_decimal_non_admin_403(admin_client, sqlite_session, admin_id: uuid.UUID):
    _seed_loan_defaults(sqlite_session)
    url = "/api/v1/admin/parameters/loan.max_dti_ratio"
    for roles in ([], ["operator"], ["analyst"]):
        headers = _bearer(admin_id, roles=roles)
        resp = admin_client.get("/api/v1/admin/parameters", headers=headers)
        assert resp.status_code == 403, roles
        assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"
        resp = admin_client.put(url, json={"value_json": 0.5}, headers=headers)
        assert resp.status_code == 403, roles
        assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"


def test_put_unknown_loan_key_404(admin_client, sqlite_session, admin_headers):
    _seed_loan_defaults(sqlite_session)
    resp = admin_client.put(
        "/api/v1/admin/parameters/loan.no_existe",
        json={"value_json": 1},
        headers=admin_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ------------------------------------------------- Sin regresion en enteros
def test_put_integer_keys_still_integer_only(admin_client, sqlite_session, admin_headers):
    from app.modules.shared.config import parameters as params

    _seed_loan_defaults(sqlite_session)
    url = "/api/v1/admin/parameters/transfer.daily_limit_minor"
    resp = admin_client.put(url, json={"value_json": 600_000}, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert params.get_daily_limit_minor(sqlite_session) == 600_000
    assert (
        admin_client.put(url, json={"value_json": 12.5}, headers=admin_headers).status_code == 422
    )
    assert params.get_daily_limit_minor(sqlite_session) == 600_000
