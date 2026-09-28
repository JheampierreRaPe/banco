"""Fachada admin de parametros `GET/PUT /admin/parameters` (E2-T26, HU08/HU20).

Cubre (brief `docs/tasks/E2-T26.md#pruebas-requeridas`):

- Camino feliz: `GET` lista (`key`/valor/`module`/`updated_at`); `PUT`
  cambia umbral/limite y la fachada lo refleja sin reiniciar (en caliente).
- Errores: sin token -> `401`; sin rol admin -> `403`; clave inexistente
  -> `404`; valor negativo/booleano -> `422` sin escribir.
- Idempotencia/concurrencia: `PUT` repetido no duplica; escrituras
  intercaladas resuelven ultimo-gana con auditoria de cada una.
- Auditoria: `updated_by` (UUID del admin) y `updated_at` por cambio, sin
  PII ni valores en logs.

Parte A: reglas estaticas (frontera + delegacion a la fachada duena).
Parte B: SQLite en memoria + schema ATTACH (`config`) + `TestClient`.
Parte C: Postgres de prueba (`db_session`/`db_client`, rollback; se omite
sin BD).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

BACKEND_DIR = Path(__file__).resolve().parents[1]
SERVICE_PATH = BACKEND_DIR / "app" / "modules" / "admin" / "service" / "parameters.py"
API_PATH = BACKEND_DIR / "app" / "modules" / "admin" / "api" / "parameters.py"
SCHEMAS_PATH = BACKEND_DIR / "app" / "modules" / "admin" / "schemas" / "parameters.py"


# ---------------------------------------------------------- Parte A: estatica
def test_static_rules_facade_delegation_and_boundaries():
    """`admin` expone via la fachada duena; no reimplementa ni escribe directo."""
    service = SERVICE_PATH.read_text(encoding="utf-8")
    assert "set_parameter" in service, "escritura solo via la fachada"
    assert "get_biometric_threshold_minor" in service, "lectura via la fachada"
    assert "get_daily_limit_minor" in service, "lectura via la fachada"
    assert "get_max_failed_attempts" in service, "lectura via la fachada"
    for forbidden in (
        "UPDATE config.parameters",
        "INSERT INTO config",
        "DELETE FROM config",
        "value_json =",
    ):
        assert forbidden not in service, f"admin no escribe directo: {forbidden}"
    for token in (
        "app.modules.identity",
        "app.modules.accounts",
        "app.modules.transactions",
        "app.modules.risk",
    ):
        assert token not in service, f"frontera rota: {token}"
    assert "config.parameters" in service, "solo SELECT acotado de metadatos"
    assert "float(" not in service, "dinero entero, sin float"

    api = API_PATH.read_text(encoding="utf-8")
    assert "/admin/parameters" in api, "ruta del catalogo (05#6.8)"
    assert "get_admin_context" in api, "guard de rol admin"
    assert "NOT_AUTHENTICATED" in api and "NOT_AUTHORIZED" in api
    assert "NOT_FOUND" in api and "VALIDATION_ERROR" in api
    assert "decode_token" in api, "Bearer via core.security"
    for token in (
        "app.modules.identity",
        "app.modules.accounts",
        "app.modules.transactions",
    ):
        assert token not in api, f"frontera rota: {token}"

    schemas = SCHEMAS_PATH.read_text(encoding="utf-8")
    assert "ParameterUpdateRequest" in schemas and "ParametersListResponse" in schemas
    assert "float(" not in schemas, "dinero entero, sin float"


# --------------------------------------------------------- Parte B: SQLite
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
    module: str = "transactions",
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


def _seed_defaults(session: Session) -> None:
    from app.modules.shared.config import parameters as params

    _seed_param(session, params.BIOMETRIC_THRESHOLD_KEY, "1000000")
    _seed_param(session, params.DAILY_LIMIT_KEY, "500000")
    _seed_param(
        session,
        "transfer.daily_limit_STANDARD_minor",
        "500000",
        description="Limite diario STANDARD",
    )
    _seed_param(session, params.AUTH_MAX_FAILED_ATTEMPTS_KEY, "5", module="identity")


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


def _seeded_client(admin_client, sqlite_session: Session):
    _seed_defaults(sqlite_session)
    return admin_client


# Camino feliz: listado
def test_get_list_admin_ok(admin_client, sqlite_session, admin_headers):
    _seeded_client(admin_client, sqlite_session)
    resp = admin_client.get("/api/v1/admin/parameters", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"data", "meta"}
    assert body["meta"]["total"] == 4
    assert "request_id" in body["meta"]
    by_key = {item["key"]: item for item in body["data"]}
    assert by_key["transfer.biometric_threshold_minor"]["value_json"] == 1_000_000
    assert by_key["transfer.daily_limit_STANDARD_minor"]["value_json"] == 500_000
    assert by_key["auth.max_failed_attempts"]["value_json"] == 5
    for item in body["data"]:
        assert item["module"] in ("transactions", "identity")
        assert item["updated_at"] is not None, "CA-01: updated_at visible"


def test_get_filter_module_and_prefix(admin_client, sqlite_session, admin_headers):
    _seeded_client(admin_client, sqlite_session)
    resp = admin_client.get(
        "/api/v1/admin/parameters", params={"module": "identity"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert [i["key"] for i in resp.json()["data"]] == ["auth.max_failed_attempts"]
    resp = admin_client.get(
        "/api/v1/admin/parameters",
        params={"prefix": "transfer.daily_limit_"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert {i["key"] for i in resp.json()["data"]} == {
        "transfer.daily_limit_minor",
        "transfer.daily_limit_STANDARD_minor",
    }


def test_get_filter_no_match_returns_empty_200(admin_client, sqlite_session, admin_headers):
    _seeded_client(admin_client, sqlite_session)
    resp = admin_client.get(
        "/api/v1/admin/parameters", params={"module": "fx"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []


# Camino feliz: edicion en caliente
def test_put_valid_reflected_hot_without_restart(
    admin_client, sqlite_session, admin_headers, admin_id: uuid.UUID
):
    from app.modules.shared.config import parameters as params

    _seeded_client(admin_client, sqlite_session)
    assert params.get_biometric_threshold_minor(sqlite_session) == 1_000_000
    resp = admin_client.put(
        "/api/v1/admin/parameters/transfer.biometric_threshold_minor",
        json={"value_json": 2_000_000},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["key"] == "transfer.biometric_threshold_minor"
    assert body["data"]["value_json"] == 2_000_000
    assert body["data"]["updated_at"] is not None
    # En caliente: la fachada refleja el cambio sin reiniciar (CA-02).
    assert params.get_biometric_threshold_minor(sqlite_session) == 2_000_000
    row = (
        sqlite_session.execute(
            sa.text("SELECT updated_by, updated_at FROM config.parameters WHERE key = :key"),
            {"key": params.BIOMETRIC_THRESHOLD_KEY},
        )
        .mappings()
        .one()
    )
    assert row["updated_by"] == str(admin_id), "CA-03: auditoria del actor"
    assert row["updated_at"] is not None, "CA-03: auditoria temporal"


def test_put_repeated_same_value_is_idempotent(admin_client, sqlite_session, admin_headers):
    from app.modules.shared.config import parameters as params

    _seeded_client(admin_client, sqlite_session)
    url = "/api/v1/admin/parameters/transfer.daily_limit_minor"
    for _ in range(2):
        resp = admin_client.put(url, json={"value_json": 600_000}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
    count = sqlite_session.execute(
        sa.text("SELECT COUNT(*) FROM config.parameters WHERE key = :key"),
        {"key": params.DAILY_LIMIT_KEY},
    ).scalar()
    assert count == 1
    # Perfil sin clave propia ("VIP") cae al generico actualizado (E2-T18).
    assert params.get_daily_limit_minor(sqlite_session, "VIP") == 600_000


# Errores de auth/autorizacion
def test_get_without_token_401(admin_client, sqlite_session):
    _seeded_client(admin_client, sqlite_session)
    resp = admin_client.get("/api/v1/admin/parameters")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"


def test_put_without_token_401(admin_client, sqlite_session):
    _seeded_client(admin_client, sqlite_session)
    resp = admin_client.put(
        "/api/v1/admin/parameters/transfer.daily_limit_minor",
        json={"value_json": 1},
    )
    assert resp.status_code == 401


def test_non_admin_forbidden_403(admin_client, sqlite_session, admin_id: uuid.UUID):
    _seeded_client(admin_client, sqlite_session)
    for roles in ([], ["operator"], ["viewer"]):
        resp = admin_client.get("/api/v1/admin/parameters", headers=_bearer(admin_id, roles=roles))
        assert resp.status_code == 403, roles
        assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"
        resp = admin_client.put(
            "/api/v1/admin/parameters/transfer.daily_limit_minor",
            json={"value_json": 1},
            headers=_bearer(admin_id, roles=roles),
        )
        assert resp.status_code == 403, roles


def test_invalid_token_401(admin_client, sqlite_session):
    _seeded_client(admin_client, sqlite_session)
    resp = admin_client.get(
        "/api/v1/admin/parameters", headers={"Authorization": "Bearer no-es-jwt"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"


# Errores de negocio
def test_put_unknown_key_404(admin_client, sqlite_session, admin_headers):
    _seeded_client(admin_client, sqlite_session)
    resp = admin_client.put(
        "/api/v1/admin/parameters/transfer.no_existe_minor",
        json={"value_json": 1},
        headers=admin_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_put_negative_422_without_write(admin_client, sqlite_session, admin_headers):
    from app.modules.shared.config import parameters as params

    _seeded_client(admin_client, sqlite_session)
    resp = admin_client.put(
        "/api/v1/admin/parameters/transfer.daily_limit_minor",
        json={"value_json": -1},
        headers=admin_headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert params.get_daily_limit_minor(sqlite_session) == 500_000
    resp = admin_client.put(
        "/api/v1/admin/parameters/auth.max_failed_attempts",
        json={"value_json": 0},
        headers=admin_headers,
    )
    assert resp.status_code == 422, resp.text
    assert params.get_max_failed_attempts(sqlite_session) == 5


def test_put_bool_and_malformed_422_without_write(admin_client, sqlite_session, admin_headers):
    from app.modules.shared.config import parameters as params

    _seeded_client(admin_client, sqlite_session)
    url = "/api/v1/admin/parameters/transfer.daily_limit_minor"
    assert (
        admin_client.put(url, json={"value_json": True}, headers=admin_headers).status_code == 422
    )
    assert (
        admin_client.put(url, json={"value_json": 12.5}, headers=admin_headers).status_code == 422
    )
    assert (
        admin_client.put(url, json={"value_json": "no-numero"}, headers=admin_headers).status_code
        == 422
    )
    assert admin_client.put(url, json={}, headers=admin_headers).status_code == 422
    assert params.get_daily_limit_minor(sqlite_session) == 500_000


# Idempotencia/concurrencia + auditoria sin PII
def test_interleaved_writes_last_wins_single_row(sqlite_session: Session):
    """Dos escrituras intercaladas: una fila, ultimo valor, ambas auditadas."""
    from app.modules.admin.service import parameters as admin_params
    from app.modules.shared.config import parameters as params

    _seed_defaults(sqlite_session)
    other = Session(bind=sqlite_session.bind, autoflush=False, expire_on_commit=False)
    try:
        actor_a, actor_b = uuid.uuid4(), uuid.uuid4()
        admin_params.update_parameter(
            sqlite_session, key=params.DAILY_LIMIT_KEY, value=600_000, updated_by=actor_a
        )
        admin_params.update_parameter(
            other, key=params.DAILY_LIMIT_KEY, value=610_000, updated_by=actor_b
        )
        count = sqlite_session.execute(
            sa.text("SELECT COUNT(*) FROM config.parameters WHERE key = :key"),
            {"key": params.DAILY_LIMIT_KEY},
        ).scalar()
        assert count == 1
        # Perfil sin clave propia ("VIP") cae al generico (ultimo escritor).
        assert params.get_daily_limit_minor(sqlite_session, "VIP") == 610_000
    finally:
        other.close()


def test_audit_trail_without_pii_or_values_in_logs(
    admin_client, sqlite_session, admin_headers, admin_id: uuid.UUID, caplog
):
    from app.modules.shared.config import parameters as params

    _seeded_client(admin_client, sqlite_session)
    with caplog.at_level(logging.INFO, logger="app.modules.admin"):
        resp = admin_client.put(
            "/api/v1/admin/parameters/transfer.biometric_threshold_minor",
            json={"value_json": 2_000_000},
            headers=admin_headers,
        )
    assert resp.status_code == 200, resp.text
    assert "transfer.biometric_threshold_minor" in caplog.text, "clave en el log"
    assert "2000000" not in caplog.text, "el valor jamas se loguea"
    row = (
        sqlite_session.execute(
            sa.text("SELECT updated_by, updated_at FROM config.parameters WHERE key = :key"),
            {"key": params.BIOMETRIC_THRESHOLD_KEY},
        )
        .mappings()
        .one()
    )
    assert row["updated_by"] == str(admin_id)
    assert row["updated_at"] is not None


def test_list_without_table_falls_back_without_raising():
    """Sin `config.parameters` el listado best-effort no lanza (defaults)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from app.modules.admin.service import parameters as admin_params

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        items = admin_params.list_parameters(session)
        assert {item["key"] for item in items} >= {
            "transfer.biometric_threshold_minor",
            "transfer.daily_limit_minor",
            "auth.max_failed_attempts",
        }
    finally:
        session.close()
        engine.dispose()


def test_openapi_exposes_admin_parameters(admin_client):
    spec = admin_client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    assert "/api/v1/admin/parameters" in paths
    assert "/api/v1/admin/parameters/{key}" in paths
    assert "get" in paths["/api/v1/admin/parameters"]
    assert "put" in paths["/api/v1/admin/parameters/{key}"]


# -------------------------------------------------------- Parte C: Postgres
def test_postgres_list_and_hot_change_with_audit(db_client, db_session: Session):
    """Semillas reales + cambio en caliente + auditoria (rollback por prueba)."""
    from app.modules.shared.config import parameters as params

    admin_id = uuid.uuid4()
    headers = _bearer(admin_id, roles=["ADMIN"])
    resp = db_client.get("/api/v1/admin/parameters", headers=headers)
    assert resp.status_code == 200, resp.text
    by_key = {item["key"]: item for item in resp.json()["data"]}
    assert by_key["transfer.biometric_threshold_minor"]["value_json"] == 1_000_000

    resp = db_client.put(
        "/api/v1/admin/parameters/transfer.biometric_threshold_minor",
        json={"value_json": 1_500_000},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert params.get_biometric_threshold_minor(db_session) == 1_500_000
    row = (
        db_session.execute(
            sa.text("SELECT updated_by FROM config.parameters WHERE key = :key"),
            {"key": params.BIOMETRIC_THRESHOLD_KEY},
        )
        .mappings()
        .one()
    )
    # En Postgres `updated_by` es UUID nativo (en SQLite, texto).
    assert str(row["updated_by"]) == str(admin_id)


def test_postgres_non_admin_403_and_unknown_404(db_client):
    headers = _bearer(uuid.uuid4(), roles=["operator"])
    assert db_client.get("/api/v1/admin/parameters", headers=headers).status_code == 403
    admin_headers = _bearer(uuid.uuid4(), roles=["admin"])
    resp = db_client.put(
        "/api/v1/admin/parameters/transfer.no_existe_minor",
        json={"value_json": 1},
        headers=admin_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
