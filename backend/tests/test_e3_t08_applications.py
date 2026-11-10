"""Caso de uso de solicitud de credito + expediente + notificacion (E3-T08, HU10).

- Camino feliz: solicitud con consentimiento -> dictamen (`APPROVED` /
  `REJECTED` / `IN_REVIEW`); `GET` estado del titular.
- Errores: sin consentimiento -> `422`; campos faltantes -> `422`;
  monto/plazo fuera del producto -> `422`; producto inexistente ->
  `404`; expediente ajeno -> `404` neutro; reevaluar/decidido ->
  `409`; sin `Idempotency-Key` -> `422`; misma clave + distinto cuerpo
  -> `409`.
- Bureau caido (`down`/`timeout` del mock E3-T06) -> `IN_REVIEW` + notificacion.
- Aprobacion separada: `IN_REVIEW` -> revision del analista (rol) ->
  `APPROVED`/`REJECTED` auditado; sin rol -> `403`.
- Idempotencia: doble `POST` con la misma clave -> un solo expediente y
  una sola notificacion.
- Sin PII en logs (documento/ingresos/deuda ausentes del `caplog`).

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron del
repo); `get_db`, `get_current_user_id` (credits), `get_bureau_provider`
y el rol analista se inyectan via `app.dependency_overrides`. Sin red.
"""

from __future__ import annotations

import logging
import uuid

import pytest
import sqlalchemy as sa
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

USER = uuid.uuid4()
OTHER = uuid.uuid4()
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


def _bureau_for(user_id: uuid.UUID, score: int = 780, deudas_minor: int = 0):
    return MockCreditBureau(
        profiles={
            str(user_id): BureauScore(score=score, deudas_minor=deudas_minor, morosidad=False)
        }
    )


class _CountingBureau(MockCreditBureau):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0

    def get_score(self, **kwargs):
        self.calls += 1
        return super().get_score(**kwargs)


assert _audit is not None and _credits is not None
assert _notifications is not None and _shared is not None


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


def _post(client: TestClient, payload: dict, key: str | None = "k-default"):
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post("/api/v1/credits/applications", json=payload, headers=headers)


def _outbox_types(session: Session, application_id: str) -> list[str]:
    table = Base.metadata.tables["shared.outbox"]
    stmt = (
        sa.select(table.c.event_type)
        .where(table.c.aggregate_id == uuid.UUID(str(application_id)))
        .order_by(table.c.created_at)
    )
    session.expire_all()
    return [row[0] for row in session.execute(stmt).all()]


def _notification_count(session: Session) -> int:
    table = Base.metadata.tables["notifications.notifications"]
    session.expire_all()
    return int(session.scalar(sa.select(sa.func.count()).select_from(table)) or 0)


def _notification_statuses(session: Session) -> list[str]:
    table = Base.metadata.tables["notifications.notifications"]
    session.expire_all()
    return [
        row[0]
        for row in session.execute(sa.select(table.c.status).order_by(table.c.created_at)).all()
    ]


def _audit_actions(session: Session) -> list[str]:
    session.expire_all()
    return [
        row[0]
        for row in session.execute(sa.text("SELECT action FROM audit.audit_log ORDER BY seq")).all()
    ]


def _app_count(session: Session) -> int:
    table = Base.metadata.tables["credits.loan_applications"]
    session.expire_all()
    return int(session.scalar(sa.select(sa.func.count()).select_from(table)) or 0)


def _analyst_headers(user_id: uuid.UUID, roles: list[str] | None = None) -> dict:
    from app.core.security import create_access_token

    token = create_access_token(subject=str(user_id), roles=roles)
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------ camino feliz
def test_submit_approved_and_get_state(app_client):
    client, session = app_client["client"], app_client["session"]
    resp = _post(client, _body(session), key="k-approved-1")
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert resp.json()["meta"] == {"replayed": False}
    assert data["status"] == "APPROVED"  # CA-03: dictamen con justificacion
    assert data["decision_reason"]
    assert data["decided_at"]
    assert data["bureau_authorized"] is True
    assert data["bureau_authorized_at"]
    assert data["score"] == 780
    assert data["dti_ratio"] == "0.0500"
    assert data["total_income_minor"] == 2_000_000
    assert data["total_debt_minor"] == 100_000

    got = client.get(f"/api/v1/credits/applications/{data['id']}")
    assert got.status_code == 200  # CA-04: estado visible
    assert got.json()["data"]["status"] == "APPROVED"

    assert _outbox_types(session, data["id"]) == [
        "credit.application.submitted",
        "credit.approved",
    ]
    assert _notification_count(session) == 1  # una notificacion
    assert _notification_statuses(session) == ["SENT"]
    assert "credit.application.decided" in _audit_actions(session)


def test_submit_rejected(app_client):
    client, session = app_client["client"], app_client["session"]
    app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER, score=520)
    try:
        resp = _post(client, _body(session), key="k-rejected-1")
    finally:
        app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER)
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "REJECTED"
    assert data["decision_reason"]
    assert _outbox_types(session, data["id"]) == [
        "credit.application.submitted",
        "credit.rejected",
    ]
    assert _notification_count(session) == 1


def test_submit_in_review_then_analyst_approves(app_client):
    client, session = app_client["client"], app_client["session"]
    app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER, score=660)
    try:
        resp = _post(client, _body(session), key="k-review-1")
    finally:
        app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER)
    assert resp.status_code == 202, resp.text  # 202: se difiere a revision
    data = resp.json()["data"]
    assert data["status"] == "IN_REVIEW"
    assert data["decision_reason"]
    assert _outbox_types(session, data["id"]) == [
        "credit.application.submitted",
        "credit.review.required",
    ]

    review = client.post(
        f"/api/v1/credits/applications/{data['id']}/review",
        json={"decision": "APPROVED", "reason": "verificacion manual superada"},
        headers=_analyst_headers(ANALYST, ["credit_analyst"]),
    )
    assert review.status_code == 200, review.text
    solved = review.json()["data"]
    assert solved["status"] == "APPROVED"  # aprobacion separada de la solicitud
    assert solved["decision_reason"] == "verificacion manual superada"
    assert _outbox_types(session, data["id"])[-1] == "credit.approved"
    assert "credit.application.reviewed" in _audit_actions(session)

    tray = client.get(
        "/api/v1/credits/applications?status=IN_REVIEW",
        headers=_analyst_headers(ANALYST, ["analyst"]),
    )
    assert tray.status_code == 200
    assert tray.json()["data"] == []  # ya resuelto: bandeja vacia


def test_review_rejects_with_reason(app_client):
    client, session = app_client["client"], app_client["session"]
    app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER, score=660)
    try:
        created = _post(client, _body(session), key="k-review-2").json()["data"]
    finally:
        app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER)
    review = client.post(
        f"/api/v1/credits/applications/{created['id']}/review",
        json={"decision": "REJECTED", "reason": "ingresos no verificables"},
        headers=_analyst_headers(ANALYST, ["CREDIT_ANALYST"]),
    )
    assert review.status_code == 200, review.text
    assert review.json()["data"]["status"] == "REJECTED"


# ------------------------------------------------ errores de validacion
def test_submit_without_consent_is_422_and_never_evaluates(app_client):
    client, session = app_client["client"], app_client["session"]
    counting = _CountingBureau(
        profiles={str(USER): BureauScore(score=780, deudas_minor=0, morosidad=False)}
    )
    app.dependency_overrides[get_bureau_provider] = lambda: counting
    try:
        resp = _post(client, _body(session, bureau_authorized=False), key="k-noconsent")
    finally:
        app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER)
    assert resp.status_code == 422  # CA-01: sin consentimiento no hay evaluacion
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert counting.calls == 0  # el bureau jamas se consulta
    assert _app_count(session) == 0  # ni siquiera se crea el expediente
    assert _notification_count(session) == 0


def test_submit_missing_fields_is_422(app_client):
    client, session = app_client["client"], app_client["session"]
    payload = _body(session)
    del payload["product_id"]
    resp = _post(client, payload, key="k-missing")
    assert resp.status_code == 422  # CA-02: faltantes no pasan al motor
    assert _app_count(session) == 0


def test_submit_out_of_product_range_is_422(app_client):
    client, session = app_client["client"], app_client["session"]
    resp = _post(client, _body(session, requested_amount_minor=1), key="k-range")
    assert resp.status_code == 422  # bajo el minimo de PERSONAL_LIBRE (100_000)
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    resp = _post(client, _body(session, term_months=7), key="k-term")
    assert resp.status_code == 422  # plazo no permitido por el producto


def test_submit_unknown_product_is_404(app_client):
    client, session = app_client["client"], app_client["session"]
    resp = _post(client, _body(session, product_id=str(uuid.uuid4())), key="k-unknown-product")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_get_foreign_application_is_404_neutral(app_client):
    client, session = app_client["client"], app_client["session"]
    created = _post(client, _body(session), key="k-foreign").json()["data"]
    app.dependency_overrides[get_current_user_id] = lambda: OTHER
    try:
        resp = client.get(f"/api/v1/credits/applications/{created['id']}")
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: USER
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    ghost = client.get(f"/api/v1/credits/applications/{uuid.uuid4()}")
    assert ghost.status_code == 404  # mismo cuerpo: sin oraculo
    assert ghost.json()["error"]["code"] == "NOT_FOUND"


def test_review_decided_is_409(app_client):
    client, session = app_client["client"], app_client["session"]
    created = _post(client, _body(session), key="k-decided").json()["data"]
    assert created["status"] == "APPROVED"
    review = client.post(
        f"/api/v1/credits/applications/{created['id']}/review",
        json={"decision": "REJECTED", "reason": "intento de reevaluar"},
        headers=_analyst_headers(ANALYST, ["analyst"]),
    )
    assert review.status_code == 409  # no se reevalua un decidido
    assert review.json()["error"]["code"] == "CONFLICT"


def test_review_empty_reason_is_422(app_client):
    client, session = app_client["client"], app_client["session"]
    app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER, score=660)
    try:
        created = _post(client, _body(session), key="k-empty-reason").json()["data"]
    finally:
        app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER)
    review = client.post(
        f"/api/v1/credits/applications/{created['id']}/review",
        json={"decision": "APPROVED", "reason": ""},
        headers=_analyst_headers(ANALYST, ["analyst"]),
    )
    assert review.status_code == 422


def test_review_requires_analyst_role(app_client):
    client, session = app_client["client"], app_client["session"]
    app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER, score=660)
    try:
        created = _post(client, _body(session), key="k-role").json()["data"]
    finally:
        app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER)
    url = f"/api/v1/credits/applications/{created['id']}/review"
    payload = {"decision": "APPROVED", "reason": "x"}
    anon = client.post(url, json=payload)
    assert anon.status_code == 401  # sin token
    plain = client.post(url, json=payload, headers=_analyst_headers(USER, []))
    assert plain.status_code == 403  # token valido sin rol analista
    assert plain.json()["error"]["code"] == "NOT_AUTHORIZED"
    tray = client.get("/api/v1/credits/applications?status=IN_REVIEW")
    assert tray.status_code == 401


# ------------------------------------------------ bureau caido
def test_bureau_down_goes_in_review(app_client):
    client, session = app_client["client"], app_client["session"]
    app.dependency_overrides[get_bureau_provider] = lambda: MockCreditBureau(mode="down")
    try:
        resp = _post(client, _body(session), key="k-down")
    finally:
        app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER)
    assert resp.status_code == 202, resp.text  # NO error 500: se conserva en revision
    data = resp.json()["data"]
    assert data["status"] == "IN_REVIEW"
    assert data["score"] is None
    assert data["decision_reason"]
    assert _outbox_types(session, data["id"])[-1] == "credit.review.required"
    assert _notification_count(session) == 1  # se notifica igual


def test_bureau_timeout_goes_in_review(app_client):
    client, session = app_client["client"], app_client["session"]
    app.dependency_overrides[get_bureau_provider] = lambda: MockCreditBureau(mode="timeout")
    try:
        resp = _post(client, _body(session), key="k-timeout")
    finally:
        app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER)
    assert resp.status_code == 202, resp.text
    assert resp.json()["data"]["status"] == "IN_REVIEW"


# ------------------------------------------------ idempotencia
def test_idempotent_double_post_creates_one(app_client):
    client, session = app_client["client"], app_client["session"]
    payload = _body(session)
    first = _post(client, payload, key="k-idem-1")
    second = _post(client, payload, key="k-idem-1")
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    assert second.json()["meta"] == {"replayed": True}
    assert _app_count(session) == 1  # un solo expediente
    assert _notification_count(session) == 1  # una sola notificacion


def test_idempotency_conflict_on_different_body(app_client):
    client, session = app_client["client"], app_client["session"]
    first = _post(client, _body(session), key="k-idem-2")
    assert first.status_code == 201
    second = _post(client, _body(session, requested_amount_minor=2_000_000), key="k-idem-2")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert _app_count(session) == 1


def test_missing_idempotency_key_is_422(app_client):
    client, session = app_client["client"], app_client["session"]
    resp = _post(client, _body(session), key=None)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _app_count(session) == 0


# ------------------------------------------------ sin PII en logs
def test_no_pii_in_logs(app_client, caplog):
    client, session = app_client["client"], app_client["session"]
    income, debt = 7_654_321, 1_234_567
    with caplog.at_level(logging.INFO, logger="app"):
        resp = _post(
            client,
            _body(session, total_income_minor=income, total_debt_minor=debt),
            key="k-nopii",
        )
    assert resp.status_code == 201
    assert str(income) not in caplog.text
    assert str(debt) not in caplog.text
