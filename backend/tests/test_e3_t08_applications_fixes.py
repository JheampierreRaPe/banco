"""Regresion de los fixes E3-T08 (validador sprint-2, 2026-10-09).

DEFECTO 1 (MEDIO) — overflow de `dti_ratio` -> HTTP 500: con entradas
validas por esquema (`total_income_minor=1` + deuda alta) el DTI supera
el maximo de `NUMERIC(6,4)` y Postgres lanza `DataError` no capturado
(500). Fix: columna `NUMERIC(12,4)` (migracion `0032`) + acote del
repositorio a `±99999999.9999` (informativo; el dictamen lo decide el
motor E3-T07 con el DTI exacto) + red de seguridad `DataError -> 422`
en el borde API. Sin el fix: en Postgres el `POST` extremo da 500 y en
SQLite persiste el DTI sin acotar (falla la asercion del tope).

DEFECTO 2 (BAJO) — separacion de funciones: el solicitante con rol
analista podia dictaminar su propio expediente. Fix: `CONFLICT` (409)
si `analyst_id == user_id` del expediente. Sin el fix: la auto-revision
responde 200.

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron de
`test_e3_t08_applications.py`); sin red. Sin PII en logs ni en el codigo.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.modules.audit.models as _audit
import app.modules.credits.models as _credits
import app.modules.notifications.models as _notifications
import app.modules.shared.models as _shared
from app.adapters.credit_bureau import BureauScore, MockCreditBureau
from app.core.db import Base, get_db
from app.main import app
from app.modules.credits import repository as repo
from app.modules.credits import service as catalog
from app.modules.credits.api import get_current_user_id
from app.modules.credits.api.applications import get_bureau_provider
from app.modules.credits.repository import applications as apps_repo

USER = uuid.uuid4()
ANALYST = uuid.uuid4()

TABLES = [
    "credits.loan_products",
    "credits.loan_applications",
    "shared.outbox",
    "shared.processed_events",
    "shared.idempotency_keys",
    "notifications.notifications",
    "notifications.notification_templates",
    "audit.audit_log",
]

#: Tope informativo persistible (`NUMERIC(12,4)`, `docs/03b#82`).
DTI_CAP = "99999999.9999"

assert _audit is not None and _credits is not None
assert _notifications is not None and _shared is not None


def _bureau_for(user_id: uuid.UUID, score: int = 780, deudas_minor: int = 0):
    return MockCreditBureau(
        profiles={
            str(user_id): BureauScore(score=score, deudas_minor=deudas_minor, morosidad=False)
        }
    )


def _make_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("credits", "shared", "notifications", "audit"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    return engine


def _engine_with_catalog():
    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    repo.seed_demo_products(session)
    session.commit()
    session.close()
    return engine


@pytest.fixture()
def app_client():
    """TestClient con catalogo demo, auth falsa (USER) y bureau prime."""
    engine = _engine_with_catalog()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:

        def _override_db():
            yield session

        def _override_user():
            return USER

        def _override_bureau():
            return _bureau_for(USER)

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_user_id] = _override_user
        app.dependency_overrides[get_bureau_provider] = _override_bureau
        with TestClient(app) as client:
            yield {"client": client, "session": session}
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        app.dependency_overrides.pop(get_bureau_provider, None)
        session.close()
        engine.dispose()


def _product_id(session: Session, code: str = "PERSONAL_LIBRE") -> str:
    return str(catalog.get_product(session, code=code)["id"])


def _body(session: Session, **overrides) -> dict:
    payload = {
        "product_id": _product_id(session),
        "requested_amount_minor": 1_000_000,
        "term_months": 12,
        "total_income_minor": 2_000_000,
        "total_debt_minor": 100_000,
        "bureau_authorized": True,
    }
    payload.update(overrides)
    return payload


def _post(client: TestClient, payload: dict, key: str):
    return client.post(
        "/api/v1/credits/applications", json=payload, headers={"Idempotency-Key": key}
    )


def _analyst_headers(user_id: uuid.UUID, roles: list[str]) -> dict:
    from app.core.security import create_access_token

    token = create_access_token(subject=str(user_id), roles=roles)
    return {"Authorization": f"Bearer {token}"}


def _submit_in_review(client: TestClient, session: Session, key: str) -> dict:
    """Expediente `IN_REVIEW` (score en banda de revision)."""
    app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER, score=660)
    try:
        resp = _post(client, _body(session), key)
    finally:
        app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER)
    assert resp.status_code == 202, resp.text
    assert resp.json()["data"]["status"] == "IN_REVIEW"
    return resp.json()["data"]


# ------------------------------------------------ DEFECTO 1: DTI extremo nunca 500
def test_extreme_dti_never_500_and_clamped(app_client):
    client, session = app_client["client"], app_client["session"]
    resp = _post(
        client,
        _body(session, total_income_minor=1, total_debt_minor=1_000_000_000_000),
        "k-fix-dti-extreme",
    )
    assert resp.status_code != 500, resp.text  # nunca 500 con entrada valida por esquema
    assert resp.status_code == 201, resp.text  # el motor rechaza por DTI (dictamen final)
    data = resp.json()["data"]
    assert data["status"] == "REJECTED"  # decide el motor, no el acote
    assert data["decision_reason"]
    assert data["dti_ratio"] == DTI_CAP  # informativo acotado, sin excepcion


def test_dti_100_persists_exact(app_client):
    client, session = app_client["client"], app_client["session"]
    resp = _post(
        client,
        _body(session, total_income_minor=100_000, total_debt_minor=10_000_000),
        "k-fix-dti-100",
    )
    assert resp.status_code != 500, resp.text
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "REJECTED"
    assert data["dti_ratio"] == "100.0000"  # cabe en NUMERIC(12,4); antes desbordaba (6,4)


def test_normal_dti_unchanged(app_client):
    client, session = app_client["client"], app_client["session"]
    resp = _post(client, _body(session), "k-fix-dti-normal")
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["dti_ratio"] == "0.0500"  # caso normal intacto


def test_repository_clamps_huge_dti(app_client):
    session = app_client["session"]
    row = apps_repo.create_application(
        session,
        user_id=USER,
        product_id=_product_id(session),
        requested_amount_minor=1_000_000,
        term_months=12,
    )
    apps_repo.set_evaluating(session, row)
    apps_repo.set_decision(
        session,
        row,
        status="REJECTED",
        score=520,
        dti_ratio=Decimal("1000000000000.0000"),
        total_income_minor=1,
        total_debt_minor=1_000_000_000_000,
        decision_reason="rechazado por DTI",
    )
    assert row.dti_ratio == Decimal(DTI_CAP)
    row2 = apps_repo.create_application(
        session,
        user_id=USER,
        product_id=_product_id(session),
        requested_amount_minor=1_000_000,
        term_months=12,
    )
    apps_repo.set_evaluating(session, row2)
    apps_repo.set_decision(
        session,
        row2,
        status="APPROVED",
        score=780,
        dti_ratio=Decimal("0.0500"),
        total_income_minor=2_000_000,
        total_debt_minor=100_000,
        decision_reason="aprobado",
    )
    assert row2.dti_ratio == Decimal("0.0500")  # valor normal pasa intacto


# ------------------------------------------------ DEFECTO 2: separacion de funciones
def test_applicant_with_analyst_role_cannot_review_own(app_client):
    client, session = app_client["client"], app_client["session"]
    created = _submit_in_review(client, session, "k-fix-self-review")
    review = client.post(
        f"/api/v1/credits/applications/{created['id']}/review",
        json={"decision": "APPROVED", "reason": "auto-aprobacion del solicitante"},
        headers=_analyst_headers(USER, ["analyst"]),
    )
    assert review.status_code == 409, review.text
    assert review.json()["error"]["code"] == "CONFLICT"
    got = client.get(f"/api/v1/credits/applications/{created['id']}")
    assert got.status_code == 200
    assert got.json()["data"]["status"] == "IN_REVIEW"  # sigue sin dictamen manual


def test_other_analyst_can_review(app_client):
    client, session = app_client["client"], app_client["session"]
    created = _submit_in_review(client, session, "k-fix-other-analyst")
    review = client.post(
        f"/api/v1/credits/applications/{created['id']}/review",
        json={"decision": "APPROVED", "reason": "verificacion manual superada"},
        headers=_analyst_headers(ANALYST, ["credit_analyst"]),
    )
    assert review.status_code == 200, review.text
    assert review.json()["data"]["status"] == "APPROVED"  # flujo normal intacto
