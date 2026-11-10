"""Endpoints del simulador de creditos (E3-T03, HU09 CA-01..CA-04).

- Camino feliz: `GET /credits/products` (solo habilitados), `POST
  /credits/simulate` con producto (cuota = motor `E3-T01`) y sin producto
  (TCEA explicita), con y sin desgravamen; export PDF real (`reportlab`
  en el `.venv`).
- Errores: producto inexistente -> 404 `NOT_FOUND`; monto/plazo fuera de
  rango o cuerpo incompleto -> 422 `VALIDATION_ERROR`; export sin backend
  PDF -> 501 `EXPORT_FORMAT_NOT_SUPPORTED`.
- Rendimiento: la simulacion resuelve muy por debajo de 1 s.
- No persistencia: tras simular no aparece fila alguna en `credits`
  (conteo identico, sin tablas nuevas).
- Humo sin Postgres: SQLite en memoria + schema ATTACH (patron del repo);
  `get_db` y `get_current_user_id` (credits) se inyectan via
  `app.dependency_overrides` (contexto de auth falso). Sin red, sin PII.
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_db
from app.main import app
from app.modules.credits import repository as repo
from app.modules.credits import service as catalog
from app.modules.credits.api import get_current_user_id
from app.modules.credits.domain import financial
from app.modules.credits.service import simulation_pdf as sim_pdf

USER = uuid.uuid4()


@pytest.fixture()
def sim_client():
    """TestClient con catalogo demo sembrado y auth falsa."""
    import app.modules.credits.models as _c  # noqa: F401 (registro de tablas)

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS credits")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[Base.metadata.tables["credits.loan_products"]],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        repo.seed_demo_products(session)
        session.commit()

        def _override_db():
            yield session

        def _override_user():
            return USER

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_user_id] = _override_user
        with TestClient(app) as client:
            yield {"client": client, "session": session}
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        session.close()
        engine.dispose()


def _product_id(session: Session, code: str) -> str:
    return str(catalog.get_product(session, code=code)["id"])


# ------------------------------------------------- GET /credits/products
def test_products_lists_only_enabled(sim_client):
    client, session = sim_client["client"], sim_client["session"]
    target = session.query(repo.LoanProduct).filter_by(code="PYME_CAPITAL").one()
    target.enabled = False
    session.commit()

    resp = client.get("/api/v1/credits/products")
    assert resp.status_code == 200
    body = resp.json()
    assert [p["code"] for p in body["data"]] == ["PERSONAL_LIBRE", "VEHICULAR"]
    assert body["meta"] == {"total": 2}
    first = body["data"][0]
    assert first["tcea"] == "0.32500000"
    assert first["tea"] == "0.28000000"
    assert first["insurance_rate"] == "0.00350000"
    assert first["min_amount_minor"] == 100_000
    assert first["allowed_terms"] == [6, 12, 18, 24, 36]


def test_products_requires_auth(sim_client):
    app.dependency_overrides.pop(get_current_user_id, None)
    try:
        with TestClient(app) as anon:
            resp = anon.get("/api/v1/credits/products")
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: USER
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"


# ------------------------------------------------- POST /credits/simulate feliz
def test_simulate_with_product_matches_engine(sim_client):
    client, session = sim_client["client"], sim_client["session"]
    pid = _product_id(session, "PERSONAL_LIBRE")
    resp = client.post(
        "/api/v1/credits/simulate",
        json={"product_id": pid, "amount_minor": 1_000_000, "term_months": 12},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    expected = financial.simulate(1_000_000, Decimal("0.28"), 12, Decimal(0))
    assert data["product"]["code"] == "PERSONAL_LIBRE"
    assert data["amount_minor"] == 1_000_000
    assert data["term_months"] == 12
    assert data["insurance"] is False
    assert data["tea"] == "0.28000000"
    assert data["tcea"] == str(expected.tcea)
    assert data["payment_minor"] == expected.payment_minor  # CA-01: cuota correcta
    assert len(data["schedule"]) == 12  # CA-02: cronograma completo
    row = data["schedule"][0]
    assert set(row) == {
        "installment_no",
        "due_date",
        "principal_minor",
        "interest_minor",
        "insurance_minor",
        "total_minor",
        "balance_minor",
    }
    assert row["due_date"] is None  # efimero: sin fecha de desembolso
    assert sum(r["principal_minor"] for r in data["schedule"]) == 1_000_000
    assert data["schedule"][-1]["balance_minor"] == 0
    assert data["total_paid_minor"] == sum(r["total_minor"] for r in data["schedule"])


def test_simulate_with_insurance_recalculates(sim_client):
    client, session = sim_client["client"], sim_client["session"]
    pid = _product_id(session, "PERSONAL_LIBRE")
    base = {"product_id": pid, "amount_minor": 1_000_000, "term_months": 12}
    plain = client.post("/api/v1/credits/simulate", json=base).json()["data"]
    insured = client.post("/api/v1/credits/simulate", json={**base, "insurance": True}).json()[
        "data"
    ]
    assert insured["insurance"] is True
    assert insured["total_insurance_minor"] > 0  # CA-03: desgravamen recalcula
    assert insured["total_paid_minor"] > plain["total_paid_minor"]
    assert insured["total_principal_minor"] == plain["total_principal_minor"]
    assert Decimal(insured["tcea"]) > Decimal(plain["tcea"])


def test_simulate_free_without_product(sim_client):
    client = sim_client["client"]
    resp = client.post(
        "/api/v1/credits/simulate",
        json={"amount_minor": 500_000, "term_months": 12, "tcea": "0.25"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["product"] is None
    assert data["tea"] == "0.25"
    expected = financial.simulate(500_000, Decimal("0.25"), 12, Decimal(0))
    assert data["payment_minor"] == expected.payment_minor
    assert len(data["schedule"]) == 12


# ------------------------------------------------- errores
def test_simulate_unknown_product_is_404(sim_client):
    client = sim_client["client"]
    resp = client.post(
        "/api/v1/credits/simulate",
        json={
            "product_id": str(uuid.uuid4()),
            "amount_minor": 1_000_000,
            "term_months": 12,
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_simulate_amount_out_of_range_is_422(sim_client):
    client, session = sim_client["client"], sim_client["session"]
    pid = _product_id(session, "VEHICULAR")  # min 1_000_000
    resp = client.post(
        "/api/v1/credits/simulate",
        json={"product_id": pid, "amount_minor": 100_000, "term_months": 12},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_simulate_term_not_allowed_is_422(sim_client):
    client, session = sim_client["client"], sim_client["session"]
    pid = _product_id(session, "PERSONAL_LIBRE")  # 7 en rango 6..36 pero no permitido
    resp = client.post(
        "/api/v1/credits/simulate",
        json={"product_id": pid, "amount_minor": 1_000_000, "term_months": 7},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "body",
    [
        {"amount_minor": 1_000_000, "term_months": 12},  # sin tasa
        {
            "product_id": str(uuid.uuid4()),
            "amount_minor": 1_000_000,
            "term_months": 12,
            "tcea": "0.25",
        },  # ambas
        {"amount_minor": 1_000_000, "term_months": 12, "tcea": "-0.01"},  # negativa
        {"amount_minor": 1_000_000, "term_months": 12, "tcea": "99"},  # tope libre
        {"amount_minor": 1_000_000, "term_months": 12, "tcea": 0.25},  # float
    ],
)
def test_simulate_invalid_body_is_422(sim_client, body):
    resp = sim_client["client"].post("/api/v1/credits/simulate", json=body)
    assert resp.status_code == 422, body


# ------------------------------------------------- export PDF
def test_export_pdf_downloads_schedule(sim_client):
    client, session = sim_client["client"], sim_client["session"]
    pid = _product_id(session, "VEHICULAR")
    resp = client.post(
        "/api/v1/credits/simulate/export",
        json={"product_id": pid, "amount_minor": 2_000_000, "term_months": 12},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")
    assert "attachment" in resp.headers["content-disposition"]


def test_export_pdf_without_backend_is_501(sim_client, monkeypatch):
    monkeypatch.setattr(sim_pdf, "pdf_available", lambda: None)
    client, session = sim_client["client"], sim_client["session"]
    pid = _product_id(session, "VEHICULAR")
    resp = client.post(
        "/api/v1/credits/simulate/export",
        json={"product_id": pid, "amount_minor": 2_000_000, "term_months": 12},
    )
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "EXPORT_FORMAT_NOT_SUPPORTED"
    # simulate sigue sano sin libreria PDF
    ok = client.post(
        "/api/v1/credits/simulate",
        json={"product_id": pid, "amount_minor": 2_000_000, "term_months": 12},
    )
    assert ok.status_code == 200


# ------------------------------------------------- rendimiento y no persistencia
def test_simulate_resolves_well_under_one_second(sim_client):
    client, session = sim_client["client"], sim_client["session"]
    pid = _product_id(session, "PYME_CAPITAL")
    body = {"product_id": pid, "amount_minor": 5_000_000, "term_months": 48}
    start = time.perf_counter()
    resp = client.post("/api/v1/credits/simulate", json=body)
    elapsed = time.perf_counter() - start
    assert resp.status_code == 200
    assert elapsed < 1.0


def test_simulate_does_not_persist(sim_client):
    client, session = sim_client["client"], sim_client["session"]
    before = session.query(repo.LoanProduct).count()
    pid = _product_id(session, "PERSONAL_LIBRE")
    for insurance in (False, True):
        resp = client.post(
            "/api/v1/credits/simulate",
            json={
                "product_id": pid,
                "amount_minor": 1_000_000,
                "term_months": 12,
                "insurance": insurance,
            },
        )
        assert resp.status_code == 200
    session.rollback()
    assert session.query(repo.LoanProduct).count() == before
    tables = [
        row[0]
        for row in session.execute(
            text("SELECT name FROM credits.sqlite_master WHERE type='table' ORDER BY 1")
        )
    ]
    assert tables == ["loan_products"]
