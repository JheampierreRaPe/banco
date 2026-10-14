"""Catalogo `billers` + adaptador de servicios mock (E4-T16, HU16 CA-01/CA-03 base).

- Camino feliz: `GET /services/catalog` lista los `billers` demo
  (`200`, filtrable por `category`/`q`); `POST
  /services/subscriptions` registra un servicio a recordar (`201`,
  idempotente por naturaleza con `replayed`).
- Adaptador: `mock` determinista sin red (misma entrada -> misma
  salida; busqueda por nombre/suministro); seleccion `mock|http` por
  entorno (valor basura -> `mock`); `http` via `httpx.MockTransport`
  (exito/timeout/caida -> error tipado tras reintentos); sin red real.
- Errores: `biller` inexistente -> `404` neutro; filtro/`supply_ref`
  invalidos -> `422`; proveedor caido/timeout -> `503`/`502`.
- Sin PII en respuestas/logs (el suministro nunca en claro en logs);
  dinero en centimos sin `float` (el catalogo no mueve dinero).
- Estructural: `alembic heads` = 1 (`0042_wallet_services`,
  `down_revision="0041_wallet_qr_payment_guard"`) y `downgrade`
  reversible.

SQLite en memoria + schemas ATTACH (sin red).
"""

from __future__ import annotations

import logging
import uuid

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.modules.wallet.models as _wallet
from app.adapters import service_provider as provider_adapter
from app.adapters.service_provider import (
    SERVICE_PROVIDER_TIMEOUT,
    SERVICE_PROVIDER_UNAVAILABLE,
    BillerMatch,
    HttpServiceProviderGateway,
    MockServiceProviderGateway,
    ServiceProviderSettings,
    ServiceProviderTimeoutError,
    ServiceProviderUnavailableError,
    ServiceProviderValidationError,
    SupplyValidation,
    create_service_provider,
)
from app.core.db import Base, get_db
from app.main import app
from app.modules.wallet import service as wallet_service
from app.modules.wallet.api import get_current_user_id
from app.modules.wallet.repository import services as services_repo

assert _wallet is not None

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
LUZ_ID = "c1111111-1111-4111-8111-111111111111"
AGUA_ID = "c2222222-2222-4222-8222-222222222222"
SOL_ID = "c5555555-5555-4555-8555-555555555555"
SUPPLY = "10293847"

TABLES = [
    "wallet.billers",
    "wallet.service_payments",
    "wallet.topups",
    "wallet.service_subscriptions",
]

SCHEMAS = ("wallet",)


@pytest.fixture()
def sqlite_session():
    """SQLite con `billers` demo + suscripciones vacias."""
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
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        services_repo.ensure_demo_billers(session)
        session.commit()
        yield session
        session.rollback()
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def api_client(sqlite_session, monkeypatch):
    """TestClient con BD SQLite y auth falsa como titular A."""
    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    monkeypatch.setenv("SERVICE_PROVIDER", "mock")
    monkeypatch.setenv("SERVICE_PROVIDER_MOCK_MODE", "success")
    session = sqlite_session

    def _override_db():
        yield session

    def _override_user():
        return USER_A

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
    """Cambia el usuario autenticado del TestClient (aislamiento)."""
    app.dependency_overrides[get_current_user_id] = lambda: user_id


# ------------------------------------------------------- catalogo: camino feliz
def test_catalog_lists_demo_billers(api_client):
    resp = api_client.get("/api/v1/services/catalog")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]) == 6
    first = body["data"][0]
    assert set(first) == {"biller_id", "category", "name", "required_fields"}
    uuid.UUID(first["biller_id"])
    by_id = {item["biller_id"]: item for item in body["data"]}
    assert by_id[LUZ_ID]["required_fields"] == ["supply_ref"]
    assert by_id[SOL_ID]["required_fields"] == ["phone"]
    assert "request_id" in body["meta"]


def test_catalog_filter_category(api_client):
    resp = api_client.get("/api/v1/services/catalog", params={"category": "telefonia"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 2
    assert {item["category"] for item in data} == {"TELEFONIA"}


def test_catalog_filter_q_by_name(api_client):
    resp = api_client.get("/api/v1/services/catalog", params={"q": "LUZ"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["biller_id"] == LUZ_ID


def test_catalog_unknown_category_returns_empty(api_client):
    resp = api_client.get("/api/v1/services/catalog", params={"category": "CABLE"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []


def test_catalog_extensible_without_migration(api_client, sqlite_session):
    """Agregar un `biller` es insertar una fila (sin cambios estructurales)."""
    sqlite_session.add(
        _wallet.Biller(
            category="CABLE",
            name="Cable Vision Demo",
            required_fields=["supply_ref"],
            enabled=True,
        )
    )
    sqlite_session.commit()
    resp = api_client.get("/api/v1/services/catalog", params={"category": "CABLE"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["name"] == "Cable Vision Demo"


def test_catalog_excludes_disabled(api_client, sqlite_session):
    row = services_repo.get_biller(sqlite_session, LUZ_ID)
    assert row is not None
    row.enabled = False
    sqlite_session.commit()
    resp = api_client.get("/api/v1/services/catalog")
    assert resp.status_code == 200, resp.text
    assert LUZ_ID not in {item["biller_id"] for item in resp.json()["data"]}


# ------------------------------------------------------- catalogo: errores
def test_catalog_invalid_category_filter_is_422(api_client):
    resp = api_client.get("/api/v1/services/catalog", params={"category": "luz!!"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_catalog_q_too_long_is_422(api_client):
    resp = api_client.get("/api/v1/services/catalog", params={"q": "x" * 81})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_catalog_requires_auth(api_client):
    app.dependency_overrides.pop(get_current_user_id, None)
    try:
        resp = api_client.get("/api/v1/services/catalog", headers={"Authorization": "Bearer malo"})
        assert resp.status_code == 401, resp.text
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: USER_A


# ------------------------------------------------------- suscripciones: camino feliz
def test_subscription_register_returns_201_with_defaults(api_client, sqlite_session):
    resp = api_client.post(
        "/api/v1/services/subscriptions",
        json={"biller_id": LUZ_ID, "supply_ref": SUPPLY},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    data = body["data"]
    assert set(data) == {
        "subscription_id",
        "biller_id",
        "supply_ref",
        "notify_days_before",
        "status",
    }
    assert data["biller_id"] == LUZ_ID
    assert data["supply_ref"] == SUPPLY
    assert data["notify_days_before"] == 3
    assert data["status"] == "ACTIVE"
    assert body["meta"]["replayed"] is False
    rows = services_repo.list_subscriptions(sqlite_session, USER_A)
    assert len(rows) == 1


def test_subscription_replay_returns_existing_without_duplicating(api_client, sqlite_session):
    first = api_client.post(
        "/api/v1/services/subscriptions",
        json={"biller_id": LUZ_ID, "supply_ref": SUPPLY},
    )
    assert first.status_code == 201, first.text
    second = api_client.post(
        "/api/v1/services/subscriptions",
        json={"biller_id": LUZ_ID, "supply_ref": SUPPLY, "notify_days_before": 7},
    )
    assert second.status_code == 201, second.text
    assert second.json()["data"]["subscription_id"] == first.json()["data"]["subscription_id"]
    assert second.json()["meta"]["replayed"] is True
    assert len(services_repo.list_subscriptions(sqlite_session, USER_A)) == 1


def test_subscription_isolated_by_holder(api_client, sqlite_session):
    resp = api_client.post(
        "/api/v1/services/subscriptions",
        json={"biller_id": AGUA_ID, "supply_ref": "20998877"},
    )
    assert resp.status_code == 201, resp.text
    _act_as(USER_B)
    try:
        mine = api_client.post(
            "/api/v1/services/subscriptions",
            json={"biller_id": AGUA_ID, "supply_ref": "20998877"},
        )
        assert mine.status_code == 201, mine.text
        assert mine.json()["data"]["subscription_id"] != resp.json()["data"]["subscription_id"]
    finally:
        _act_as(USER_A)


# ------------------------------------------------------- suscripciones: errores
def test_subscription_unknown_biller_is_404_neutral(api_client):
    resp = api_client.post(
        "/api/v1/services/subscriptions",
        json={"biller_id": str(uuid.uuid4()), "supply_ref": SUPPLY},
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert SUPPLY not in body["error"]["message"]


def test_subscription_malformed_biller_id_is_422(api_client):
    resp = api_client.post(
        "/api/v1/services/subscriptions",
        json={"biller_id": "no-uuid", "supply_ref": SUPPLY},
    )
    assert resp.status_code == 422, resp.text


def test_subscription_empty_supply_is_422(api_client):
    resp = api_client.post(
        "/api/v1/services/subscriptions",
        json={"biller_id": LUZ_ID, "supply_ref": "   "},
    )
    assert resp.status_code == 422, resp.text


def test_subscription_notify_days_out_of_range_is_422(api_client):
    for days in (0, 31):
        resp = api_client.post(
            "/api/v1/services/subscriptions",
            json={"biller_id": LUZ_ID, "supply_ref": SUPPLY, "notify_days_before": days},
        )
        assert resp.status_code == 422, resp.text


def test_subscription_requires_auth(api_client):
    app.dependency_overrides.pop(get_current_user_id, None)
    try:
        resp = api_client.post(
            "/api/v1/services/subscriptions",
            json={"biller_id": LUZ_ID, "supply_ref": SUPPLY},
            headers={"Authorization": "Bearer malo"},
        )
        assert resp.status_code == 401, resp.text
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: USER_A


def test_subscription_provider_down_is_503(api_client, monkeypatch):
    monkeypatch.setenv("SERVICE_PROVIDER_MOCK_MODE", "down")
    resp = api_client.post(
        "/api/v1/services/subscriptions",
        json={"biller_id": LUZ_ID, "supply_ref": SUPPLY},
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_PROVIDER_UNAVAILABLE"


def test_subscription_provider_timeout_is_502(api_client, monkeypatch):
    monkeypatch.setenv("SERVICE_PROVIDER_MOCK_MODE", "timeout")
    resp = api_client.post(
        "/api/v1/services/subscriptions",
        json={"biller_id": LUZ_ID, "supply_ref": SUPPLY},
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_PROVIDER_TIMEOUT"


# ------------------------------------------------------- adaptador: mock determinista
def test_mock_validate_is_deterministic():
    gateway = MockServiceProviderGateway()
    first = gateway.validate_supply(biller_id=LUZ_ID, supply_ref=SUPPLY)
    second = MockServiceProviderGateway().validate_supply(biller_id=LUZ_ID, supply_ref=SUPPLY)
    assert first == second
    assert isinstance(first, SupplyValidation)
    assert first.valid is True
    assert first.biller_id == LUZ_ID
    assert first.amount_due_minor > 0
    assert isinstance(first.amount_due_minor, int)
    assert first.currency == "PEN"
    # Enmascarado: el suministro en claro no viaja en el resultado.
    assert SUPPLY not in first.holder_masked


def test_mock_lookup_by_name_and_supply_number():
    gateway = MockServiceProviderGateway()
    by_name = gateway.lookup_biller("luz")
    assert [m.biller_id for m in by_name] == [LUZ_ID]
    by_supply = gateway.lookup_biller("10293847")
    assert LUZ_ID in [m.biller_id for m in by_supply]
    by_operator = gateway.lookup_biller("Recarga Movil")
    assert {m.biller_id for m in by_operator} == {
        "c5555555-5555-4555-8555-555555555555",
        "c6666666-6666-4666-8666-666666666666",
    }
    assert gateway.lookup_biller("") and len(gateway.lookup_biller("")) == 6
    assert all(isinstance(m, BillerMatch) for m in by_name)


def test_mock_rejects_unknown_biller_and_malformed_supply():
    gateway = MockServiceProviderGateway()
    with pytest.raises(ServiceProviderValidationError):
        gateway.validate_supply(biller_id=str(uuid.uuid4()), supply_ref=SUPPLY)
    with pytest.raises(ServiceProviderValidationError):
        gateway.validate_supply(biller_id=LUZ_ID, supply_ref="!!")
    with pytest.raises(ServiceProviderValidationError):
        gateway.lookup_biller("x" * 81)


def test_mock_down_and_timeout_modes():
    with pytest.raises(ServiceProviderUnavailableError) as exc_down:
        MockServiceProviderGateway(mode="down").validate_supply(biller_id=LUZ_ID, supply_ref=SUPPLY)
    assert exc_down.value.code == SERVICE_PROVIDER_UNAVAILABLE
    with pytest.raises(ServiceProviderTimeoutError) as exc_timeout:
        MockServiceProviderGateway(mode="timeout").validate_supply(
            biller_id=LUZ_ID, supply_ref=SUPPLY
        )
    assert exc_timeout.value.code == SERVICE_PROVIDER_TIMEOUT
    with pytest.raises(ValueError):
        MockServiceProviderGateway(mode="basura")


def test_mock_rejected_supply_is_not_valid():
    gateway = MockServiceProviderGateway(rejected=[SUPPLY])
    result = gateway.validate_supply(biller_id=LUZ_ID, supply_ref=SUPPLY)
    assert result.valid is False


# ------------------------------------------------------- adaptador: fabrica por entorno
def test_factory_selects_by_env_and_garbage_falls_back_to_mock(monkeypatch):
    monkeypatch.setenv("SERVICE_PROVIDER", "mock")
    assert isinstance(create_service_provider(), MockServiceProviderGateway)
    monkeypatch.setenv("SERVICE_PROVIDER", "http")
    assert isinstance(create_service_provider(), HttpServiceProviderGateway)
    monkeypatch.setenv("SERVICE_PROVIDER", "basura-no-existe")
    assert isinstance(create_service_provider(), MockServiceProviderGateway)
    assert isinstance(create_service_provider("http"), HttpServiceProviderGateway)
    assert isinstance(create_service_provider("mock"), MockServiceProviderGateway)


def test_settings_defaults_are_safe():
    settings = ServiceProviderSettings.from_env({})
    assert settings.provider == "mock"
    assert settings.mock_mode == "success"


# ------------------------------------------------------- adaptador: http sin red real
def _http(handler, **overrides) -> HttpServiceProviderGateway:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://provider.test")
    base = {
        "provider": "http",
        "base_url": "http://provider.test",
        "api_key": "k",
        "timeout_seconds": 5.0,
        "max_retries": 1,
        "backoff_base_seconds": 0.0,
        "breaker_failures": 5,
        "breaker_cooldown_seconds": 30.0,
    }
    base.update(overrides)
    return HttpServiceProviderGateway(settings=ServiceProviderSettings(**base), client=client)


def test_http_validate_success():
    def _ok(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/services/supply/validate"
        assert request.headers["Authorization"] == "Bearer k"
        return httpx.Response(
            200,
            json={
                "valid": True,
                "holder_masked": "******47",
                "amount_due_minor": 5200,
                "currency": "PEN",
            },
        )

    result = _http(_ok).validate_supply(biller_id=LUZ_ID, supply_ref=SUPPLY)
    assert result == SupplyValidation(
        valid=True,
        biller_id=LUZ_ID,
        holder_masked="******47",
        amount_due_minor=5200,
        currency="PEN",
    )


def test_http_timeout_becomes_typed_error():
    def _slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulado", request=request)

    with pytest.raises(ServiceProviderTimeoutError) as exc:
        _http(_slow).validate_supply(biller_id=LUZ_ID, supply_ref=SUPPLY)
    assert exc.value.code == SERVICE_PROVIDER_TIMEOUT


def test_http_server_error_becomes_unavailable():
    def _down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "caido"})

    with pytest.raises(ServiceProviderUnavailableError) as exc:
        _http(_down).validate_supply(biller_id=LUZ_ID, supply_ref=SUPPLY)
    assert exc.value.code == SERVICE_PROVIDER_UNAVAILABLE


def test_http_malformed_body_becomes_unavailable():
    def _weird(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rara": True})

    with pytest.raises(ServiceProviderUnavailableError):
        _http(_weird).validate_supply(biller_id=LUZ_ID, supply_ref=SUPPLY)


def test_http_validates_locally_without_calling_provider():
    def _must_not_run(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no debe llamar al proveedor con formato invalido")

    with pytest.raises(ServiceProviderValidationError):
        _http(_must_not_run).validate_supply(biller_id=LUZ_ID, supply_ref="!!")


def test_circuit_opens_after_consecutive_failures():
    def _down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    gateway = _http(_down, max_retries=0, breaker_failures=1, breaker_cooldown_seconds=60.0)
    with pytest.raises(ServiceProviderUnavailableError):
        gateway.validate_supply(biller_id=LUZ_ID, supply_ref=SUPPLY)
    assert gateway.circuit_open is True
    with pytest.raises(ServiceProviderUnavailableError):
        gateway.validate_supply(biller_id=LUZ_ID, supply_ref=SUPPLY)


# ------------------------------------------------------- sin PII / frontera
def test_supply_never_leaks_in_logs(sqlite_session, caplog):
    gateway = MockServiceProviderGateway()
    with caplog.at_level(logging.INFO, logger="app.adapters.service_provider"):
        gateway.validate_supply(biller_id=LUZ_ID, supply_ref=SUPPLY)
    for record in caplog.records:
        assert SUPPLY not in record.getMessage()
    with caplog.at_level(logging.INFO, logger="app.modules.wallet.service.services"):
        wallet_service.register_subscription(
            sqlite_session,
            USER_A,
            biller_id=uuid.UUID(LUZ_ID),
            supply_ref="55667788",
            gateway=MockServiceProviderGateway(),
        )
    for record in caplog.records:
        assert "55667788" not in record.getMessage()


def test_module_boundary_only_wallet_tables():
    from app.modules.wallet import models

    tables = {table.name for table in models.Base.metadata.tables.values()}
    assert {"billers", "service_payments", "topups", "service_subscriptions"} <= tables
    assert provider_adapter.__name__ == "app.adapters.service_provider"


# ------------------------------------------------------- migracion 0042
def test_migration_chain_has_single_head_0042():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from tests.db_utils import BACKEND_DIR

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.config_file_name = None
    script = ScriptDirectory.from_config(cfg)
    revisions = {rev.revision: rev.down_revision for rev in script.walk_revisions()}
    assert revisions.get("0042_wallet_services") == "0041_wallet_qr_payment_guard"
    heads = [rev for rev in script.get_heads() if rev not in ()]
    assert heads == ["0044_notifications_preferences"], f"heads inesperados: {heads}"
    content = (BACKEND_DIR / "migrations" / "versions" / "0042_wallet_services.py").read_text(
        encoding="utf-8"
    )
    assert "def downgrade" in content
    assert 'down_revision = "0041_wallet_qr_payment_guard"' in content
