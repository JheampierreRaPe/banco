"""Tasas y cotizaciones `fx` (E7-T09, HU25).

Cubre: tasa vigente (`GET /fx/rates`) con snapshot persistido en
`fx_rates`; par no soportado/moneda invalida/`base == quote` ->
`422`; proveedor caido/timeout -> `503`/`502` tipados sin persistir;
cotizacion (`POST /fx/quotes`) que congela tasa/spread/montos con
`expires_at` a `fx.quote_ttl_seconds` (default `30`); calculo exacto
de `target_amount_minor` en `Decimal` con `ROUND_HALF_UP` al centavo
(incluye el borde `.5` -> sube); expiracion on-read (`now >=
expires_at` -> `EXPIRED`); idempotencia (misma clave + mismo cuerpo
-> misma cotizacion; distinto cuerpo -> `409`); aislamiento por
titular (ajeno -> `404` neutro); auditoria best-effort
`fx.quote.created` sin PII; sin mover dinero; y frontera (`fx` no
importa modelos de otros modulos).

SQLite en memoria con schemas ATTACH (`fx`, `shared`, `config`,
`audit`, `ledger`); `get_db` y `get_current_user_id` de `fx.api`
se inyectan via `app.dependency_overrides` (auth falsa).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.adapters import fx_rate as fx_adapter
from app.core.db import Base, get_db
from app.main import app
from app.modules.fx import domain as fx_domain
from app.modules.fx import service as fx_service
from app.modules.fx.api import get_current_user_id

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()

CURRENT = {"user": USER_A}

TABLES = [
    "fx.fx_rates",
    "fx.fx_quotes",
    "fx.fx_operations",
    "shared.outbox",
    "shared.idempotency_keys",
    "ledger.journal_entries",
    "ledger.postings",
    "audit.audit_log",
]

SCHEMAS = ("fx", "shared", "config", "audit", "ledger")


def _seed_param(session: Session, key: str, value: str, module: str = "fx") -> None:
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, :module)"
        ),
        {"id": uuid.uuid4().hex, "key": key, "value": value, "module": module},
    )
    session.flush()


@pytest.fixture()
def fx_client():
    """TestClient con BD SQLite aislada y auth falsa conmutable (A/B)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.audit.models as _au
    import app.modules.fx.models as _fx
    import app.modules.ledger.models as _l
    import app.modules.shared.models as _s

    assert _au is not None and _fx is not None and _l is not None and _s is not None

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
    CURRENT["user"] = USER_A
    try:
        _seed_param(session, "fx.quote_ttl_seconds", "30")
        session.commit()

        def _override_db():
            yield session

        def _override_user():
            return CURRENT["user"]

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_user_id] = _override_user
        with TestClient(app) as client:
            yield {"client": client, "session": session}
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        session.close()
        engine.dispose()


def _count(session: Session, table: str) -> int:
    return int(session.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[table])))


def _quote(client: TestClient, body: dict, key: str | None = None):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post("/api/v1/fx/quotes", json=body, headers=headers)


# ------------------------------------------------- tasa vigente (CA-02)
def test_get_rate_returns_live_rate_and_persists_snapshot(fx_client):
    client, session = fx_client["client"], fx_client["session"]
    resp = client.get("/api/v1/fx/rates", params={"base": "PEN", "quote": "USD"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["base"] == "PEN"
    assert data["quote"] == "USD"
    assert Decimal(str(data["rate"])) == Decimal("3.80")
    assert data["source"] == "mock"
    assert data["ttl_seconds"] == 60
    assert data["fetched_at"]

    from app.modules.fx.models import FxRate

    rows = session.scalars(sa.select(FxRate)).all()
    assert len(rows) == 1
    assert (rows[0].base, rows[0].quote) == ("PEN", "USD")
    assert rows[0].rate == Decimal("3.80")
    assert rows[0].source == data["source"]
    assert rows[0].ttl_seconds == data["ttl_seconds"]
    assert not isinstance(rows[0].rate, float)


def test_get_rate_inverse_pair_is_coherent(fx_client):
    client = fx_client["client"]
    resp = client.get("/api/v1/fx/rates", params={"base": "USD", "quote": "PEN"})
    assert resp.status_code == 200, resp.text
    assert Decimal(str(resp.json()["data"]["rate"])) == Decimal("0.26315789")


@pytest.mark.parametrize(
    "base,quote",
    [("PEN", "PEN"), ("PEN", "GBP"), ("GBP", "USD"), ("PE", "USD"), ("PEN", "")],
)
def test_get_rate_invalid_or_unsupported_pair_rejected(fx_client, base: str, quote: str):
    resp = fx_client["client"].get("/api/v1/fx/rates", params={"base": base, "quote": quote})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_get_rate_provider_down_maps_to_503_without_persisting(fx_client, monkeypatch):
    from app.modules.fx.service import quotes as quotes_service

    monkeypatch.setattr(
        quotes_service,
        "create_fx_rate_provider",
        lambda: fx_adapter.MockFxRateProvider(mode="down"),
    )
    resp = fx_client["client"].get("/api/v1/fx/rates", params={"base": "PEN", "quote": "USD"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "FX_RATE_UNAVAILABLE"
    assert _count(fx_client["session"], "fx.fx_rates") == 0


def test_get_rate_provider_timeout_maps_to_502(fx_client, monkeypatch):
    from app.modules.fx.service import quotes as quotes_service

    monkeypatch.setattr(
        quotes_service,
        "create_fx_rate_provider",
        lambda: fx_adapter.MockFxRateProvider(mode="timeout"),
    )
    resp = fx_client["client"].get("/api/v1/fx/rates", params={"base": "PEN", "quote": "USD"})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "FX_RATE_TIMEOUT"


# ------------------------------------------------- cotizacion (CA-01/CA-04)
def test_create_quote_freezes_rate_spread_amounts_and_ttl(fx_client):
    client, session = fx_client["client"], fx_client["session"]
    resp = _quote(client, {"base": "PEN", "quote": "USD", "source_amount_minor": 38000})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["meta"] == {"replayed": False}
    data = body["data"]
    assert uuid.UUID(data["quote_id"])
    assert (data["base"], data["quote"]) == ("PEN", "USD")
    assert Decimal(str(data["rate"])) == Decimal("3.80")
    assert Decimal(str(data["spread"])) == Decimal(0)
    # 38000 PEN minor / 3.80 = 10000 USD minor exactos (sin float).
    assert data["source_amount_minor"] == 38000
    assert data["target_amount_minor"] == 10000
    assert isinstance(data["target_amount_minor"], int)
    assert data["status"] == "ACTIVE"

    from app.modules.fx.models import FxQuote

    row = session.get(FxQuote, uuid.UUID(data["quote_id"]))
    assert row is not None
    assert row.user_id == USER_A
    # `created_at` nace en el default del ORM (microsegundos despues del
    # `moment` usado para `expires_at`): el TTL se verifica con holgura.
    assert abs((row.expires_at - row.created_at).total_seconds() - 30) < 1
    # La tasa quedo congelada: el snapshot de `fx_rates` existe.
    assert _count(session, "fx.fx_rates") == 1


def test_create_quote_reads_configurable_ttl(fx_client):
    client, session = fx_client["client"], fx_client["session"]
    session.execute(
        sa.text("UPDATE config.parameters SET value_json = :value WHERE key = :key"),
        {"value": "45", "key": "fx.quote_ttl_seconds"},
    )
    session.commit()
    data = _quote(client, {"base": "PEN", "quote": "USD", "source_amount_minor": 380}).json()[
        "data"
    ]
    from app.modules.fx.models import FxQuote

    row = session.get(FxQuote, uuid.UUID(data["quote_id"]))
    assert row is not None
    assert abs((row.expires_at - row.created_at).total_seconds() - 45) < 1


def test_target_rounding_half_up_to_the_cent():
    # Borde `.5` exacto: HALF_UP sube (truncar o HALF_EVEN daria 12).
    assert fx_domain.target_for(100, Decimal("8.00")) == 13
    assert fx_domain.target_for(100, Decimal("8.00"), Decimal(0)) == 13
    # Casos exactos sin artefactos binarios.
    assert fx_domain.target_for(38000, Decimal("3.80")) == 10000
    assert fx_domain.target_for(1000, Decimal("3.80")) == 263  # 263.157... -> 263
    assert fx_domain.target_for(1, Decimal("0.02")) == 50
    # El spread reduce el destino (tasa efectiva = rate + spread).
    assert fx_domain.target_for(800, Decimal("8.00"), Decimal("2.00")) == 80


def test_create_quote_applies_stub_rate_with_half_up_rounding(fx_client):
    class _Stub:
        def get_rate(self, *, base: str, quote: str):
            assert (base, quote) == ("PEN", "USD")
            return fx_adapter.FxRatePayload(
                base=base, quote=quote, rate=Decimal("8.00"), source="http", ttl_seconds=60
            )

        def get_rates(self, *, base: str):  # pragma: no cover - no se usa aqui
            raise AssertionError

    view = fx_service.create_quote(
        fx_client["session"],
        USER_A,
        base="PEN",
        quote="USD",
        source_amount_minor=100,
        provider=_Stub(),
    )
    assert view["target_amount_minor"] == 13
    assert view["rate"] == Decimal("8.00")


@pytest.mark.parametrize(
    "body",
    [
        {"base": "PEN", "quote": "PEN", "source_amount_minor": 100},
        {"base": "PEN", "quote": "USD", "source_amount_minor": 0},
        {"base": "PEN", "quote": "USD", "source_amount_minor": -5},
        {"base": "PEN", "quote": "USD", "source_amount_minor": 10.5},
        {"base": "PEN", "quote": "GBP", "source_amount_minor": 100},
        {"base": "USD", "quote": "USD", "source_amount_minor": 100},
        {"base": "PEN", "quote": "USD"},
        {"base": "PEN", "source_amount_minor": 100},
    ],
)
def test_create_quote_invalid_body_rejected(fx_client, body: dict):
    # 422 en ambos niveles: esquema (Pydantic, forma `detail`) o negocio
    # (servicio, `VALIDATION_ERROR`); el contrato solo exige el estado.
    resp = _quote(fx_client["client"], body)
    assert resp.status_code == 422, resp.text


# ------------------------------------------------- expiracion on-read (CA-03)
def test_expired_quote_reported_expired_on_read(fx_client):
    client, session = fx_client["client"], fx_client["session"]
    quote_id = _quote(client, {"base": "PEN", "quote": "USD", "source_amount_minor": 380}).json()[
        "data"
    ]["quote_id"]

    from app.modules.fx.models import FxQuote

    row = session.get(FxQuote, uuid.UUID(quote_id))
    assert row is not None and row.status == "ACTIVE"
    # Simula el paso del tiempo moviendo la ventana completa (el CHECK
    # `expires_at > created_at` impide solo mover el vencimiento atras).
    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=60)
    row.created_at = past
    row.expires_at = past + timedelta(seconds=30)
    session.commit()

    resp = client.get(f"/api/v1/fx/quotes/{quote_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "EXPIRED"
    session.expire_all()
    assert session.get(FxQuote, uuid.UUID(quote_id)).status == "EXPIRED"


def test_active_quote_stays_active_on_read(fx_client):
    client = fx_client["client"]
    quote_id = _quote(client, {"base": "PEN", "quote": "USD", "source_amount_minor": 380}).json()[
        "data"
    ]["quote_id"]
    assert client.get(f"/api/v1/fx/quotes/{quote_id}").json()["data"]["status"] == "ACTIVE"


# ------------------------------------------------- idempotencia (CA-04)
def test_same_key_same_body_replays_without_duplicating(fx_client):
    client, session = fx_client["client"], fx_client["session"]
    body = {"base": "PEN", "quote": "USD", "source_amount_minor": 38000}
    first = _quote(client, body, key="cotiz-1")
    assert first.status_code == 201
    assert first.json()["meta"] == {"replayed": False}
    second = _quote(client, body, key="cotiz-1")
    assert second.status_code == 201
    assert second.json()["meta"] == {"replayed": True}
    assert second.json()["data"]["quote_id"] == first.json()["data"]["quote_id"]
    assert _count(session, "fx.fx_quotes") == 1


def test_same_key_different_body_conflicts(fx_client):
    client, session = fx_client["client"], fx_client["session"]
    body = {"base": "PEN", "quote": "USD", "source_amount_minor": 38000}
    assert _quote(client, body, key="cotiz-2").status_code == 201
    conflict = _quote(
        client, {"base": "PEN", "quote": "USD", "source_amount_minor": 39000}, key="cotiz-2"
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CONFLICT"
    assert _count(session, "fx.fx_quotes") == 1


# ------------------------------------------------- aislamiento (CA-04)
def test_foreign_and_missing_quote_not_found(fx_client):
    client = fx_client["client"]
    quote_id = _quote(client, {"base": "PEN", "quote": "USD", "source_amount_minor": 380}).json()[
        "data"
    ]["quote_id"]

    CURRENT["user"] = USER_B
    try:
        assert client.get(f"/api/v1/fx/quotes/{quote_id}").status_code == 404
    finally:
        CURRENT["user"] = USER_A
    assert client.get(f"/api/v1/fx/quotes/{uuid.uuid4()}").status_code == 404


def test_missing_auth_unauthorized(fx_client):
    client = fx_client["client"]
    app.dependency_overrides.pop(get_current_user_id, None)
    try:
        assert client.get("/api/v1/fx/rates?base=PEN&quote=USD").status_code == 401
        assert (
            _quote(client, {"base": "PEN", "quote": "USD", "source_amount_minor": 1}).status_code
            == 401
        )
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: CURRENT["user"]


# ------------------------------------------------- auditoria y dinero
def test_quote_created_audit_emitted_without_pii(fx_client):
    client, session = fx_client["client"], fx_client["session"]
    quote_id = _quote(client, {"base": "PEN", "quote": "USD", "source_amount_minor": 380}).json()[
        "data"
    ]["quote_id"]
    from app.modules.audit.models import AuditLog

    rows = session.scalars(sa.select(AuditLog).where(AuditLog.action == "fx.quote.created")).all()
    assert len(rows) == 1
    assert str(rows[0].entity_id) == quote_id
    assert rows[0].entity_type == "fx_quote"
    assert str(rows[0].actor_id) == str(USER_A)


def test_quotes_move_no_money(fx_client):
    client, session = fx_client["client"], fx_client["session"]
    assert client.get("/api/v1/fx/rates", params={"base": "PEN", "quote": "USD"}).status_code == 200
    assert (
        _quote(client, {"base": "PEN", "quote": "USD", "source_amount_minor": 380}).status_code
        == 201
    )
    assert _count(session, "ledger.journal_entries") == 0
    assert _count(session, "ledger.postings") == 0


# ------------------------------------------------- frontera
def test_frontera_fx_quotes_no_importa_modelos_ajenos():
    """`fx` (tasas/cotizaciones) no importa modelos/tablas de otros modulos.

    Unica arista permitida: la fachada `app.modules.accounts.service`
    (import perezoso en el alta de bolsillos) + `shared`/`audit`/`core`
    transversales y el adaptador `E7-T08`. Jamas `transactions`/`ledger`
    ni modelos de `accounts`.
    """
    from pathlib import Path

    backend_dir = Path(__file__).resolve().parents[1]
    for rel in (
        "app/modules/fx/service/quotes.py",
        "app/modules/fx/api/__init__.py",
        "app/modules/fx/schemas/__init__.py",
        "app/modules/fx/domain/__init__.py",
    ):
        text = (backend_dir / rel).read_text(encoding="utf-8")
        for forbidden in (
            "app.modules.accounts.models",
            "app.modules.accounts.repository",
            "app.modules.transactions",
            "app.modules.ledger",
            "post_entry",
        ):
            assert forbidden not in text, f"frontera rota en {rel}: {forbidden}"
