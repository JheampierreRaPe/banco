"""QA de exactitud del simulador de creditos (E3-T05, HU09 CA-01..CA-04).

Verifica la exactitud numerica del motor puro (`E3-T01`) y del endpoint
`POST /credits/simulate` (`E3-T03`) contra casos de referencia externos
(tabla financiera / Excel `PAGO` con tasa mensual equivalente
`i = (1 + TEA) ** (1/12) - 1`), sin introducir dependencias de red:

- CA-01 (cuota): casos de referencia con tolerancia <= 1 centimo, mas
  verificacion independiente por biseccion (algoritmo distinto a la
  formula cerrada del motor: busca la cuota fija que amortiza el saldo
  a cero periodo a periodo).
- CA-02 (cronograma): invariantes `sum(principal) == P`, saldo final 0,
  `len == term_months`, `principal + interes + seguro == total` por fila,
  interes del primer periodo contra calculo independiente, casos borde
  (tasa 0, plazo 1).
- CA-03 (desgravamen): con tasa 0 el seguro es 0 y la cuota no cambia;
  con tasa > 0 el seguro es >= 0, el total se recalcula y la TCEA sube.
- CA-04 (descarga): export PDF responde 200 (`application/pdf`) o 501
  `EXPORT_FORMAT_NOT_SUPPORTED` sin romper `simulate`.
- TCEA (TIR): sin seguro coincide con la TEA (ruido de redondeo); el VPN
  del flujo a la TCEA reportada cuadra a <= 1 centimo por cuota.
- Endpoint: producto y libre, errores `VALIDATION_ERROR`/`NOT_FOUND`,
  la simulacion no persiste filas en `credits`, responde < 1 s.

Dinero entero en centimos (`int`), tasas `Decimal` exactas: jamas `float`.
Sin PII, sin red, fixtures deterministas (SQLite en memoria + catalogo
demo, patron de `test_e3_t03_simulate_api.py`).
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal, localcontext

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
from app.modules.credits.domain.financial import (
    FinancialValidationError,
    build_schedule,
    compute_tcea,
    french_payment,
    simulate,
)
from app.modules.credits.service import simulation_pdf as sim_pdf

USER = uuid.uuid4()
_PREC = 28
CENT = Decimal(1)


def _q_half_up(value: Decimal) -> int:
    """Redondeo half-up a centimo entero (rutina propia de la QA)."""
    with localcontext() as ctx:
        ctx.prec = _PREC
        return int(value.quantize(CENT))


def _monthly_rate(tea: Decimal) -> Decimal:
    """Tasa mensual equivalente, calculada de forma independiente."""
    with localcontext() as ctx:
        ctx.prec = _PREC
        if tea == 0:
            return Decimal(0)
        return +((Decimal(1) + tea) ** (Decimal(1) / Decimal(12))) - Decimal(1)


def _payment_by_bisection(principal: int, tea: Decimal, term: int) -> int:
    """Cuota fija por biseccion: amortiza el saldo a cero (sin formula cerrada).

    Algoritmo independiente al del motor: prueba cuotas candidatas y simula
    periodo a periodo (`interes = round(saldo * i)`, `saldo -= cuota -
    interes`) SIN rescate en la ultima cuota; la cuota es suficiente cuando
    el saldo final queda <= 0. Biseccion sobre el predicado de suficiencia
    hasta la cuota minima.
    """
    i = _monthly_rate(tea)
    if tea == 0:
        return _q_half_up(Decimal(principal) / Decimal(term))

    def _sufficient(payment: int) -> bool:
        balance = principal
        for _ in range(term):
            balance -= payment - _q_half_up(Decimal(balance) * i)
        return balance <= 0

    lo, hi = -1, principal * 2
    assert not _sufficient(lo) and _sufficient(hi)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _sufficient(mid):
            hi = mid
        else:
            lo = mid
    return hi


# Casos de referencia externos (tabla financiera / Excel `PAGO` con `i`
# mensual equivalente; redondeo half-up a centimo). Tolerancia: <= 1 centimo.
REFERENCE_CASES = (
    # (principal_minor, tea, term_months, cuota_esperada_minor)
    (1_000_000, Decimal("0.20"), 12, 91857),  # S/10,000, TEA 20%, 12 m
    (120_000, Decimal("0.12"), 12, 10627),  # S/1,200, TEA 12%, 12 m
    (500_000, Decimal("0.25"), 12, 46923),  # S/5,000, TEA 25%, 12 m
    (1_000_000, Decimal("0.28"), 12, 95016),  # S/10,000, TEA 28%, 12 m
)


def _assert_schedule_invariants(principal: int, term: int, rows) -> None:
    assert len(rows) == term
    assert [r.installment_no for r in rows] == list(range(1, term + 1))
    assert sum(r.principal_minor for r in rows) == principal
    assert rows[-1].balance_minor == 0
    assert all(r.balance_minor >= 0 for r in rows)
    for row in rows:
        for field in (
            "principal_minor",
            "interest_minor",
            "insurance_minor",
            "total_minor",
            "balance_minor",
        ):
            assert isinstance(getattr(row, field), int), field
            assert not isinstance(getattr(row, field), bool), field
        assert row.principal_minor + row.interest_minor + row.insurance_minor == row.total_minor


# ------------------------------------------------- CA-01: cuota exacta
@pytest.mark.parametrize("principal,tea,term,expected", REFERENCE_CASES)
def test_reference_quota_within_one_cent(principal, tea, term, expected):
    assert abs(french_payment(principal, tea, term) - expected) <= 1


@pytest.mark.parametrize("principal,tea,term,expected", REFERENCE_CASES)
def test_reference_quota_matches_independent_bisection(principal, tea, term, expected):
    assert abs(_payment_by_bisection(principal, tea, term) - expected) <= 1
    assert (
        abs(french_payment(principal, tea, term) - _payment_by_bisection(principal, tea, term)) <= 1
    )


def test_zero_rate_quota_splits_principal_evenly():
    assert french_payment(100_000, Decimal(0), 3) == 33333


def test_single_month_quota_is_principal_plus_interest():
    payment = french_payment(100_000, Decimal("0.15"), 1)
    i = _monthly_rate(Decimal("0.15"))
    assert payment == 100_000 + _q_half_up(Decimal(100_000) * i)


def test_money_never_float():
    with pytest.raises(FinancialValidationError):
        french_payment(1_000_000, 0.20, 12)
    with pytest.raises(FinancialValidationError):
        build_schedule(1_000_000.0, Decimal("0.20"), 12)


# ------------------------------------------------- CA-02: cronograma
@pytest.mark.parametrize(
    "principal,tea,term",
    [
        (1_000_000, Decimal("0.20"), 12),
        (120_000, Decimal("0.12"), 12),
        (500_000, Decimal("0.25"), 24),
        (250_000, Decimal("0.10"), 6),
        (100_000, Decimal(0), 3),
        (100_000, Decimal("0.15"), 1),
    ],
)
def test_schedule_invariants(principal, tea, term):
    rows = build_schedule(principal, tea, term)
    _assert_schedule_invariants(principal, term, rows)


def test_first_period_interest_matches_independent_computation():
    principal, tea, term = 1_000_000, Decimal("0.20"), 12
    rows = build_schedule(principal, tea, term)
    assert rows[0].interest_minor == _q_half_up(Decimal(principal) * _monthly_rate(tea))


def test_zero_rate_schedule_has_no_interest_and_absorbs_remainder():
    rows = build_schedule(100_000, Decimal(0), 3)
    assert [r.total_minor for r in rows] == [33333, 33333, 33334]
    assert all(r.interest_minor == 0 for r in rows)
    _assert_schedule_invariants(100_000, 3, rows)


def test_single_installment_schedule():
    rows = build_schedule(100_000, Decimal("0.15"), 1)
    assert len(rows) == 1
    assert rows[0].principal_minor == 100_000
    assert rows[0].balance_minor == 0
    assert rows[0].total_minor == rows[0].principal_minor + rows[0].interest_minor


def test_simulate_totals_reconcile_with_schedule():
    sim = simulate(1_000_000, Decimal("0.20"), 12)
    rows = list(sim.schedule)
    assert sim.total_principal_minor == sum(r.principal_minor for r in rows) == 1_000_000
    assert sim.total_interest_minor == sum(r.interest_minor for r in rows)
    assert sim.total_insurance_minor == sum(r.insurance_minor for r in rows)
    assert sim.total_paid_minor == sum(r.total_minor for r in rows)
    assert sim.total_principal_minor + sim.total_interest_minor + sim.total_insurance_minor == (
        sim.total_paid_minor
    )
    assert sim.payment_minor == french_payment(1_000_000, Decimal("0.20"), 12)


# ------------------------------------------------- CA-03: desgravamen
def test_zero_insurance_leaves_payment_unchanged():
    base = build_schedule(1_000_000, Decimal("0.20"), 12)
    insured = build_schedule(1_000_000, Decimal("0.20"), 12, Decimal(0))
    assert all(r.insurance_minor == 0 for r in base)
    assert [(r.insurance_minor, r.total_minor) for r in insured] == [
        (r.insurance_minor, r.total_minor) for r in base
    ]


def test_insurance_is_nonnegative_and_recalculates_total():
    insured = build_schedule(1_000_000, Decimal("0.20"), 12, Decimal("0.0005"))
    base = build_schedule(1_000_000, Decimal("0.20"), 12)
    assert all(r.insurance_minor >= 0 for r in insured)
    assert sum(r.insurance_minor for r in insured) > 0
    assert sum(r.total_minor for r in insured) > sum(r.total_minor for r in base)
    assert sum(r.principal_minor for r in insured) == 1_000_000
    _assert_schedule_invariants(1_000_000, 12, insured)


def test_insurance_rate_applies_on_opening_balance():
    insured = build_schedule(1_000_000, Decimal("0.20"), 12, Decimal("0.0005"))
    assert insured[0].insurance_minor == _q_half_up(Decimal(1_000_000) * Decimal("0.0005"))


# ------------------------------------------------- TCEA (TIR)
def test_tcea_without_insurance_matches_tea():
    sim = simulate(1_000_000, Decimal("0.20"), 12)
    assert sim.tea == Decimal("0.20")
    assert abs(sim.tcea - Decimal("0.20")) < Decimal("0.0005")


def test_tcea_with_insurance_exceeds_plain_tcea():
    plain = simulate(1_000_000, Decimal("0.20"), 12)
    insured = simulate(1_000_000, Decimal("0.20"), 12, Decimal("0.0005"))
    assert insured.tcea > plain.tcea
    assert insured.total_paid_minor > plain.total_paid_minor


def test_tcea_single_period_known_value():
    # Flujo [-100, 110]: TIR mensual 10% -> TCEA = 1.1^12 - 1 ~= 2.138428.
    assert abs(compute_tcea([-100, 110]) - Decimal("2.138428")) < Decimal("0.000001")


def test_tcea_zero_cost_flow_is_zero():
    assert compute_tcea([-100_000, 33333, 33333, 33334]) == Decimal(0)


@pytest.mark.parametrize(
    "principal,tea,term,insurance",
    [
        (1_000_000, Decimal("0.20"), 12, Decimal(0)),
        (1_000_000, Decimal("0.20"), 12, Decimal("0.0005")),
        (500_000, Decimal("0.25"), 24, Decimal("0.0004")),
        (100_000, Decimal(0), 3, Decimal(0)),
    ],
)
def test_reported_tcea_zeroes_npv_within_one_cent_per_installment(principal, tea, term, insurance):
    """Chequeo independiente de la TIR: el VPN a la TCEA reportada ~= 0."""
    sim = simulate(principal, tea, term, insurance)
    flows = [-principal] + [r.total_minor for r in sim.schedule]
    i_m = _monthly_rate(sim.tcea)
    with localcontext() as ctx:
        ctx.prec = _PREC
        npv = Decimal(0)
        discount = Decimal(1)
        for flow in flows:
            npv += Decimal(flow) / discount
            discount *= Decimal(1) + i_m
    assert abs(npv) <= term  # <= 1 centimo por cuota (ruido de redondeo)


# ------------------------------------------------- Endpoint (CA-01..CA-04)
@pytest.fixture()
def sim_client():
    """TestClient con catalogo demo sembrado y auth falsa (sin red, sin PII)."""
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


def test_products_lists_only_enabled(sim_client):
    client = sim_client["client"]
    resp = client.get("/api/v1/credits/products")
    assert resp.status_code == 200
    body = resp.json()
    assert [p["code"] for p in body["data"]] == [
        "PERSONAL_LIBRE",
        "PYME_CAPITAL",
        "VEHICULAR",
    ]
    assert body["meta"] == {"total": 3}
    first = body["data"][0]
    assert first["tcea"] == "0.32500000"
    assert first["tea"] == "0.28000000"
    assert isinstance(first["min_amount_minor"], int)


def test_simulate_with_product_matches_reference(sim_client):
    client, session = sim_client["client"], sim_client["session"]
    pid = _product_id(session, "VEHICULAR")  # TEA 0.165
    resp = client.post(
        "/api/v1/credits/simulate",
        json={"product_id": pid, "amount_minor": 2_000_000, "term_months": 12},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    expected = financial.simulate(2_000_000, Decimal("0.165"), 12, Decimal(0))
    assert data["product"]["code"] == "VEHICULAR"
    assert data["tea"] == "0.16500000"
    assert data["payment_minor"] == expected.payment_minor  # CA-01
    assert len(data["schedule"]) == 12  # CA-02
    assert data["schedule"][-1]["balance_minor"] == 0
    assert sum(r["principal_minor"] for r in data["schedule"]) == 2_000_000
    assert all(
        r["principal_minor"] + r["interest_minor"] + r["insurance_minor"] == r["total_minor"]
        for r in data["schedule"]
    )
    assert data["total_paid_minor"] == sum(r["total_minor"] for r in data["schedule"])
    assert all(r["due_date"] is None for r in data["schedule"])  # efimero


def test_simulate_free_matches_reference_case(sim_client):
    client = sim_client["client"]
    resp = client.post(
        "/api/v1/credits/simulate",
        json={"amount_minor": 500_000, "term_months": 12, "tcea": "0.25"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["product"] is None
    assert data["tea"] == "0.25"
    assert abs(data["payment_minor"] - 46923) <= 1  # caso de referencia S/5,000 TEA 25%
    assert len(data["schedule"]) == 12
    assert sum(r["principal_minor"] for r in data["schedule"]) == 500_000


def test_simulate_with_insurance_via_api(sim_client):
    client, session = sim_client["client"], sim_client["session"]
    pid = _product_id(session, "PERSONAL_LIBRE")
    base = {"product_id": pid, "amount_minor": 1_000_000, "term_months": 12}
    plain = client.post("/api/v1/credits/simulate", json=base).json()["data"]
    insured = client.post("/api/v1/credits/simulate", json={**base, "insurance": True}).json()[
        "data"
    ]
    assert plain["total_insurance_minor"] == 0  # CA-03 sin desgravamen
    assert all(r["insurance_minor"] == 0 for r in plain["schedule"])
    assert insured["total_insurance_minor"] > 0  # CA-03 con desgravamen
    assert insured["total_paid_minor"] > plain["total_paid_minor"]
    assert insured["total_principal_minor"] == plain["total_principal_minor"] == 1_000_000
    assert Decimal(insured["tcea"]) > Decimal(plain["tcea"])


def test_simulate_unknown_product_is_404(sim_client):
    resp = sim_client["client"].post(
        "/api/v1/credits/simulate",
        json={"product_id": str(uuid.uuid4()), "amount_minor": 1_000_000, "term_months": 12},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    "body",
    [
        {"amount_minor": 1_000_000, "term_months": 12},  # sin fuente de tasa
        {
            "product_id": str(uuid.uuid4()),
            "amount_minor": 1_000_000,
            "term_months": 12,
            "tcea": "0.25",
        },  # ambas fuentes
        {"amount_minor": 1_000_000, "term_months": 12, "tcea": 0.25},  # float prohibido
    ],
)
def test_simulate_schema_violation_is_422_with_detail(sim_client, body):
    """422 de esquema Pydantic: el handler global responde `{"detail": [...]}`."""
    resp = sim_client["client"].post("/api/v1/credits/simulate", json=body)
    assert resp.status_code == 422, body
    assert isinstance(resp.json().get("detail"), list)


@pytest.mark.parametrize(
    "body",
    [
        {"amount_minor": 1_000_000, "term_months": 12, "tcea": "-0.01"},  # negativa
        {"amount_minor": 1_000_000, "term_months": 12, "tcea": "99"},  # tope libre 500%
        {"amount_minor": 0, "term_months": 12, "tcea": "0.25"},  # monto 0
        {"amount_minor": 500_000, "term_months": 0, "tcea": "0.25"},  # plazo 0
    ],
)
def test_simulate_invalid_free_params_is_validation_error(sim_client, body):
    resp = sim_client["client"].post("/api/v1/credits/simulate", json=body)
    assert resp.status_code == 422, body
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_simulate_amount_out_of_product_range_is_422(sim_client):
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
    pid = _product_id(session, "PERSONAL_LIBRE")  # 7 en rango pero no permitido
    resp = client.post(
        "/api/v1/credits/simulate",
        json={"product_id": pid, "amount_minor": 1_000_000, "term_months": 7},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_export_pdf_is_200_or_501_without_breaking_simulate(sim_client, monkeypatch):
    client, session = sim_client["client"], sim_client["session"]
    pid = _product_id(session, "VEHICULAR")
    body = {"product_id": pid, "amount_minor": 2_000_000, "term_months": 12}
    resp = client.post("/api/v1/credits/simulate/export", json=body)
    assert resp.status_code in (200, 501)  # CA-04
    if resp.status_code == 200:
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")
    else:
        assert resp.json()["error"]["code"] == "EXPORT_FORMAT_NOT_SUPPORTED"
    monkeypatch.setattr(sim_pdf, "pdf_available", lambda: None)
    missing = client.post("/api/v1/credits/simulate/export", json=body)
    assert missing.status_code == 501
    assert missing.json()["error"]["code"] == "EXPORT_FORMAT_NOT_SUPPORTED"
    ok = client.post("/api/v1/credits/simulate", json=body)
    assert ok.status_code == 200  # simulate sigue sano sin libreria PDF


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


def test_simulate_requires_auth(sim_client):
    app.dependency_overrides.pop(get_current_user_id, None)
    try:
        with TestClient(app) as anon:
            resp = anon.post(
                "/api/v1/credits/simulate",
                json={"amount_minor": 500_000, "term_months": 12, "tcea": "0.25"},
            )
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: USER
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"
