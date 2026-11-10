"""Total contable consolidado por moneda (E1-T43, HU05).

- `GET /api/v1/accounts/totals` (Bearer): suma EN EL SERVIDOR por moneda
  desde la proyeccion `account_balances` (misma fuente que `GET /accounts`);
  el cliente solo muestra el monto (cliente delgado).
- Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron de
  `tests/test_accounts_endpoints.py`); `get_db` y `get_current_user_id`
  se inyectan via `app.dependency_overrides` (contexto de auth falso).
  Los casos 401 usan el `get_current_user_id` real (sin override) con
  JWT de `app.core.security.create_access_token`.
- Casos: feliz (dos PEN -> 170_000 y cuadre con `GET /accounts`),
  multicurrency (PEN + USD), vacio (200 con `[]`), aislamiento por
  `user_id`, auth 401 (sin cabecera/`Bearer` basura/`sub` no UUID),
  dinero entero sin `float`, funcion pura y reglas estaticas
  (ruta `/accounts/totals` ANTES de `/accounts/{account_id}`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_db
from app.main import app
from app.modules.accounts.api import get_current_user_id

API_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "accounts" / "api" / "__init__.py"
)
SERVICE_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "accounts" / "service" / "__init__.py"
)
SCHEMAS_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "accounts" / "schemas" / "__init__.py"
)

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
USER_EMPTY = uuid.uuid4()
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
        # a1: disponible 42_000 + retenido 8_000 = contable 50_000.
        repo.apply_delta(session, a1.id, available_delta=50_000, held_delta=0, currency="PEN")
        repo.apply_delta(session, a1.id, available_delta=-8_000, held_delta=8_000, currency="PEN")
        # a2: 120_000 + 0 = 120_000; consolidado PEN 170_000.
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


# ---------------------------------------------------------------- Reglas estaticas
def test_static_rules_totals_route_before_path_param():
    api = API_PATH.read_text(encoding="utf-8")
    assert '"/accounts/totals"' in api, "ruta exacta del endpoint"
    assert api.index('"/accounts/totals"') < api.index(
        '"/accounts/{account_id}"'
    ), "totals debe declararse ANTES de /accounts/{account_id} (si no, 422)"
    assert "get_accounts_totals" in api, "el router delega al service (05#1)"
    assert "get_current_user_id" in api, "reutiliza el patron Bearer existente"

    service = SERVICE_PATH.read_text(encoding="utf-8")
    assert "PRIMARY_CURRENCY" in service and '"PEN"' in service, "constante del modulo"
    assert "sum_totals_by_currency" in service, "funcion pura de agregacion"
    assert "get_accounts_totals" in service, "caso de uso de totales"
    assert "list_accounts" in service, "reutiliza la misma fuente que GET /accounts"
    assert (
        ".commit(" not in service.split("def get_accounts_totals")[1].split("def ")[0]
    ), "solo lectura: sin commit en get_accounts_totals"

    schemas = SCHEMAS_PATH.read_text(encoding="utf-8")
    for name in ("CurrencyTotal", "AccountTotals", "AccountTotalsResponse"):
        assert name in schemas, f"esquema {name} declarado"
    assert (
        ": float" not in schemas and "float(" not in schemas
    ), "dinero entero en centimos, nunca float como tipo"


# ---------------------------------------------------------------- Camino feliz
def test_totals_happy_path_pen(seeded_client):
    client: TestClient = seeded_client["client"]
    resp = client.get("/api/v1/accounts/totals")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"data", "meta"}
    data = body["data"]
    assert set(data) == {"as_of", "primary_currency", "primary_total_minor", "totals"}
    assert data["primary_currency"] == "PEN"
    assert data["primary_total_minor"] == 170_000
    assert body["meta"]["total"] == 1  # numero de monedas, no de cuentas
    by_ccy = {t["currency"]: t for t in data["totals"]}
    pen = by_ccy["PEN"]
    assert (pen["available_minor"], pen["held_minor"], pen["total_minor"]) == (
        162_000,
        8_000,
        170_000,
    )
    assert pen["total_minor"] == pen["available_minor"] + pen["held_minor"]
    assert data["as_of"], "as_of presente"
    datetime.fromisoformat(data["as_of"])  # ISO-8601 parseable


def test_totals_match_list_source(seeded_client):
    """La suma es del servidor y cuadra con GET /accounts (misma sesion/semilla)."""
    client: TestClient = seeded_client["client"]
    listed = client.get("/api/v1/accounts").json()["data"]
    expected = sum(i["balance_minor"] for i in listed if i["currency"] == "PEN")
    totals = client.get("/api/v1/accounts/totals").json()["data"]
    by_ccy = {t["currency"]: t for t in totals["totals"]}
    assert by_ccy["PEN"]["total_minor"] == expected == 170_000
    assert totals["primary_total_minor"] == expected


def test_totals_multicurrency(seeded_client):
    from app.modules.accounts import repository as repo

    session: Session = seeded_client["session"]
    client: TestClient = seeded_client["client"]
    usd = repo.create_account(
        session,
        user_id=USER_A,
        account_number="00110009999",
        type="AHORRO",
        currency="USD",
    )
    repo.apply_delta(session, usd.id, available_delta=1_000, held_delta=200, currency="USD")
    session.commit()

    body = client.get("/api/v1/accounts/totals").json()["data"]
    assert body["primary_currency"] == "PEN"
    assert body["primary_total_minor"] == 170_000  # solo el consolidado PEN
    by_ccy = {t["currency"]: t for t in body["totals"]}
    assert set(by_ccy) == {"PEN", "USD"}
    assert by_ccy["USD"]["total_minor"] == 1_200
    assert by_ccy["USD"]["total_minor"] == (
        by_ccy["USD"]["available_minor"] + by_ccy["USD"]["held_minor"]
    )


def test_totals_empty_user(seeded_client):
    client: TestClient = seeded_client["client"]
    app.dependency_overrides[get_current_user_id] = lambda: USER_EMPTY
    try:
        resp = client.get("/api/v1/accounts/totals")
        assert resp.status_code == 200, resp.text  # no 404
        data = resp.json()["data"]
        assert data["totals"] == []
        assert data["primary_total_minor"] == 0
        assert data["primary_currency"] == "PEN"
        assert data["as_of"], "as_of presente aun sin cuentas"
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: USER_A


def test_totals_isolation_other_user_excluded(seeded_client):
    client: TestClient = seeded_client["client"]
    body = client.get("/api/v1/accounts/totals").json()["data"]
    assert body["primary_total_minor"] == 170_000  # sin los 9_999 de USER_B
    raw = client.get("/api/v1/accounts/totals").text
    assert FULL_B1 not in raw, "sin PII de otro usuario en la respuesta"


def test_totals_never_exposes_full_account_number(seeded_client):
    client: TestClient = seeded_client["client"]
    raw = client.get("/api/v1/accounts/totals").text
    assert FULL_A1 not in raw and FULL_A2 not in raw


def test_totals_money_integers_no_float(seeded_client):
    client: TestClient = seeded_client["client"]
    body = client.get("/api/v1/accounts/totals").json()["data"]
    assert isinstance(body["primary_total_minor"], int) and not isinstance(
        body["primary_total_minor"], bool
    )
    for entry in body["totals"]:
        for field in ("available_minor", "held_minor", "total_minor"):
            assert isinstance(entry[field], int) and not isinstance(entry[field], bool), field
            assert entry[field] >= 0


# ---------------------------------------------------------------- Dominio puro
def test_sum_totals_by_currency_pure():
    from app.modules.accounts.schemas import AccountSummary
    from app.modules.accounts.service import sum_totals_by_currency

    assert sum_totals_by_currency([]) == []
    items = [
        AccountSummary(
            id=uuid.uuid4(),
            account_number_masked="****1234",
            type="AHORRO",
            currency="PEN",
            available_minor=42_000,
            held_minor=8_000,
            balance_minor=50_000,
        ),
        AccountSummary(
            id=uuid.uuid4(),
            account_number_masked="****5678",
            type="CORRIENTE",
            currency="PEN",
            available_minor=120_000,
            held_minor=0,
            balance_minor=120_000,
        ),
    ]
    (pen,) = sum_totals_by_currency(items)
    assert (pen.available_minor, pen.held_minor, pen.total_minor) == (162_000, 8_000, 170_000)


def test_sum_totals_by_currency_rejects_negative():
    import pydantic

    from app.modules.accounts.service import sum_totals_by_currency

    with pytest.raises((ValueError, pydantic.ValidationError)):
        from app.modules.accounts.schemas import AccountSummary

        sum_totals_by_currency(
            [
                AccountSummary.model_construct(
                    id=uuid.uuid4(),
                    account_number_masked="****1234",
                    type="AHORRO",
                    currency="PEN",
                    available_minor=-1,
                    held_minor=0,
                    balance_minor=-1,
                )
            ]
        )


# ---------------------------------------------------------------- Auth
def test_totals_requires_bearer(seeded_client):
    from app.core.security import create_access_token

    client: TestClient = seeded_client["client"]
    app.dependency_overrides.pop(get_current_user_id, None)
    try:
        missing = client.get("/api/v1/accounts/totals")
        assert missing.status_code == 401, missing.text
        assert missing.json()["error"]["code"] == "NOT_AUTHENTICATED"

        garbage = client.get("/api/v1/accounts/totals", headers={"Authorization": "Bearer basura"})
        assert garbage.status_code == 401, garbage.text
        assert garbage.json()["error"]["code"] == "NOT_AUTHENTICATED"

        bad_sub = create_access_token(subject="not-a-uuid")
        bad = client.get("/api/v1/accounts/totals", headers={"Authorization": f"Bearer {bad_sub}"})
        assert bad.status_code == 401, bad.text
        assert bad.json()["error"]["code"] == "NOT_AUTHENTICATED"
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: USER_A
