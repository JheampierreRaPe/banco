"""QA del ciclo solicitud + evaluacion crediticia (E3-T11, HU10).

Cubre los cuatro desenlaces (APROBADO, RECHAZADO por reglas, EN_REVISION
por score limite y EN_REVISION por bureau caido), consentimiento
obligatorio, campos obligatorios, DTI/topes/score en los limites, bureau
caido con reintentos (adaptador) + `IN_REVIEW` sin perdida, estado
visible, persistencia de `decision_reason`, aprobacion separada del
analista (el solicitante no dictamina lo propio), idempotencia del `POST`
(una sola notificacion; cuerpo distinto -> 409), anti-enumeracion (404
neutro) y sin PII en logs. Aislamiento: no se crea `loans` ni se mueve
dinero (solo `loan_applications` + `outbox` + notificacion + auditoria).

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron de
`test_e3_t08_applications.py`); `get_db`, `get_current_user_id`,
`get_bureau_provider` se inyectan via `app.dependency_overrides`. Sin red
(salvo `httpx.MockTransport` para los reintentos del adaptador). Semilla
determinista: perfiles fijos del `MockCreditBureau` por usuario.

NO toca producto: solo lee/escribe via HTTP + repositorio propio. Si un
caso falla por defecto real de E3-T06/T07/T08, el fallo queda como
evidencia (no se parchea aqui).
"""

from __future__ import annotations

import logging
import time
import uuid

import httpx
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
from app.adapters.credit_bureau import (
    BureauScore,
    BureauUnavailableError,
    CreditBureauSettings,
    HttpCreditBureau,
    MockCreditBureau,
)
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

assert _audit is not None and _credits is not None
assert _notifications is not None and _shared is not None


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


def _all_outbox_types(session: Session) -> list[str]:
    table = Base.metadata.tables["shared.outbox"]
    session.expire_all()
    return [row[0] for row in session.execute(sa.select(table.c.event_type)).all()]


def _notification_count(session: Session) -> int:
    table = Base.metadata.tables["notifications.notifications"]
    session.expire_all()
    return int(session.scalar(sa.select(sa.func.count()).select_from(table)) or 0)


def _notification_templates(session: Session) -> list:
    table = Base.metadata.tables["notifications.notifications"]
    session.expire_all()
    return [
        row[0]
        for row in session.execute(
            sa.select(table.c.template_code).order_by(table.c.created_at)
        ).all()
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


def _submit_with_score(client, session, score: int, key: str) -> dict:
    app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER, score=score)
    try:
        resp = _post(client, _body(session), key=key)
    finally:
        app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER)
    return resp


# ------------------------------------------- desenlace 1: aprobado
def test_qa_aprobado_con_consentimiento(app_client):
    client, session = app_client["client"], app_client["session"]
    start = time.perf_counter()
    resp = _post(client, _body(session), key="qa-aprobado")
    elapsed = time.perf_counter() - start
    assert resp.status_code == 201, resp.text  # CA-03: dictamen final
    assert elapsed < 300  # SLO pre-calificacion <= 5 min (holgado en sincrono)
    data = resp.json()["data"]
    assert resp.json()["meta"] == {"replayed": False}
    assert data["status"] == "APPROVED"
    assert data["decision_reason"]  # CA: justificacion persistida
    assert data["decided_at"]
    assert data["bureau_authorized"] is True
    assert data["bureau_authorized_at"]  # consentimiento estampado por el servidor
    assert data["score"] == 780
    assert data["dti_ratio"] == "0.0500"
    assert data["total_income_minor"] == 2_000_000
    assert data["total_debt_minor"] == 100_000

    got = client.get(f"/api/v1/credits/applications/{data['id']}")
    assert got.status_code == 200  # CA-04: estado visible para el titular
    assert got.json()["data"]["status"] == "APPROVED"
    assert got.json()["data"]["decision_reason"] == data["decision_reason"]

    assert _outbox_types(session, data["id"]) == [
        "credit.application.submitted",
        "credit.approved",
    ]
    assert _notification_count(session) == 1
    assert _notification_templates(session) == ["credit_application_approved"]
    assert "credit.application.decided" in _audit_actions(session)


# ------------------------------------------- desenlace 2: rechazados por reglas
def test_qa_rechazado_por_dti(app_client):
    client, session = app_client["client"], app_client["session"]
    resp = _post(
        client,
        _body(session, total_income_minor=1_000_000, total_debt_minor=600_000),
        key="qa-rech-dti",
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "REJECTED"
    assert "dti" in data["decision_reason"].lower()
    assert _outbox_types(session, data["id"]) == [
        "credit.application.submitted",
        "credit.rejected",
    ]
    assert _notification_templates(session) == ["credit_application_rejected"]


def test_qa_rechazado_por_tope_de_monto_regla(app_client):
    client, session = app_client["client"], app_client["session"]
    vehicular = _product_id(session, code="VEHICULAR")  # rango 1M..20M: pasa producto
    resp = _post(
        client,
        _body(session, product_id=vehicular, requested_amount_minor=6_000_000),
        key="qa-rech-tope",
    )
    assert resp.status_code == 201, resp.text  # tope de regla (5M generico), no 422
    data = resp.json()["data"]
    assert data["status"] == "REJECTED"
    assert "tope" in data["decision_reason"].lower()


def test_qa_rechazado_por_tope_de_deuda(app_client):
    client, session = app_client["client"], app_client["session"]
    resp = _post(
        client,
        _body(session, total_income_minor=100_000_000, total_debt_minor=10_000_001),
        key="qa-rech-deuda",
    )
    assert resp.status_code == 201, resp.text  # DTI 0.10 pasa; frena el tope de deuda 10M
    data = resp.json()["data"]
    assert data["status"] == "REJECTED"
    assert "deuda" in data["decision_reason"].lower()


def test_qa_rechazado_por_score_bajo(app_client):
    client, session = app_client["client"], app_client["session"]
    resp = _submit_with_score(client, session, 520, "qa-rech-score")
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "REJECTED"
    assert "score" in data["decision_reason"].lower()


# ------------------------------------------- desenlace 3: en revision por score
def test_qa_en_revision_por_score_limite(app_client):
    client, session = app_client["client"], app_client["session"]
    resp = _submit_with_score(client, session, 660, "qa-rev-score")
    assert resp.status_code == 202, resp.text  # banda [650, 700): se difiere, no error
    data = resp.json()["data"]
    assert data["status"] == "IN_REVIEW"
    assert data["score"] == 660
    assert data["decision_reason"]
    assert _outbox_types(session, data["id"]) == [
        "credit.application.submitted",
        "credit.review.required",
    ]
    assert _notification_templates(session) == ["credit_review_required"]

    got = client.get(f"/api/v1/credits/applications/{data['id']}")
    assert got.status_code == 200
    assert got.json()["data"]["decision_reason"] == data["decision_reason"]

    tray = client.get(
        "/api/v1/credits/applications?status=IN_REVIEW",
        headers=_analyst_headers(ANALYST, ["analyst"]),
    )
    assert tray.status_code == 200
    assert data["id"] in [item["id"] for item in tray.json()["data"]]


def test_qa_score_en_los_limites(app_client):
    client, session = app_client["client"], app_client["session"]
    at_min = _submit_with_score(client, session, 650, "qa-score-min")
    assert at_min.status_code == 202, at_min.text  # score == min_score -> banda: revision
    assert at_min.json()["data"]["status"] == "IN_REVIEW"
    at_bound = _submit_with_score(client, session, 700, "qa-score-bound")
    assert at_bound.status_code == 201, at_bound.text  # score == min+margen -> aprobado
    assert at_bound.json()["data"]["status"] == "APPROVED"


def test_qa_dti_en_el_limite_no_rechaza_por_dti(app_client):
    client, session = app_client["client"], app_client["session"]
    resp = _post(
        client,
        _body(session, total_income_minor=1_000_000, total_debt_minor=400_000),
        key="qa-dti-limite",
    )
    assert resp.status_code == 201, resp.text  # DTI == max (0.4): no supera -> no frena
    data = resp.json()["data"]
    assert data["dti_ratio"] == "0.4000"
    assert data["status"] == "APPROVED"
    assert (
        "dti" not in data["decision_reason"].lower()
        or "aprobado" in data["decision_reason"].lower()
    )


# ------------------------------------------- desenlace 4: bureau caido
def test_qa_en_revision_por_bureau_caido_down(app_client):
    client, session = app_client["client"], app_client["session"]
    app.dependency_overrides[get_bureau_provider] = lambda: MockCreditBureau(mode="down")
    try:
        resp = _post(client, _body(session), key="qa-bureau-down")
    finally:
        app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER)
    assert resp.status_code == 202, resp.text  # caida persistente: NO 500, se conserva
    data = resp.json()["data"]
    assert data["status"] == "IN_REVIEW"
    assert data["score"] is None
    assert "bureau" in data["decision_reason"].lower()  # justificacion para auditoria
    assert _outbox_types(session, data["id"])[-1] == "credit.review.required"
    assert _notification_count(session) == 1
    got = client.get(f"/api/v1/credits/applications/{data['id']}")
    assert got.status_code == 200  # la solicitud no se pierde
    assert got.json()["data"]["status"] == "IN_REVIEW"


def test_qa_en_revision_por_bureau_timeout(app_client):
    client, session = app_client["client"], app_client["session"]
    app.dependency_overrides[get_bureau_provider] = lambda: MockCreditBureau(mode="timeout")
    try:
        resp = _post(client, _body(session), key="qa-bureau-timeout")
    finally:
        app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER)
    assert resp.status_code == 202, resp.text
    assert resp.json()["data"]["status"] == "IN_REVIEW"


def test_qa_bureau_reintenta_antes_de_fallar():
    attempts = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(500, json={"error": "caido"})
        return httpx.Response(200, json={"score": 780, "deudas": 0, "morosidad": False})

    settings = CreditBureauSettings(
        provider="http",
        base_url="http://bureau.test",
        max_retries=2,
        backoff_base_seconds=0.0,
        breaker_failures=10,
        breaker_cooldown_seconds=30.0,
    )
    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport, base_url="http://bureau.test")
    provider = HttpCreditBureau(settings=settings, client=client, sleeper=lambda _: None)
    result = provider.get_score(subject_ref="qa-sujeto", income_minor=100, debt_minor=0)
    assert result.score == 780
    assert attempts["n"] == 3  # 1 intento + 2 reintentos con backoff


def test_qa_bureau_caida_persistente_error_tipado():
    attempts = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, json={"error": "caido"})

    settings = CreditBureauSettings(
        provider="http",
        base_url="http://bureau.test",
        max_retries=2,
        backoff_base_seconds=0.0,
        breaker_failures=10,
        breaker_cooldown_seconds=30.0,
    )
    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport, base_url="http://bureau.test")
    provider = HttpCreditBureau(settings=settings, client=client, sleeper=lambda _: None)
    with pytest.raises(BureauUnavailableError):
        provider.get_score(subject_ref="qa-sujeto", income_minor=100, debt_minor=0)
    assert attempts["n"] == 3  # agota los reintentos antes de rendirse


# ------------------------------------------- consentimiento + validacion
def test_qa_sin_consentimiento_422_y_sin_bureau(app_client):
    client, session = app_client["client"], app_client["session"]
    counting = _CountingBureau(
        profiles={str(USER): BureauScore(score=780, deudas_minor=0, morosidad=False)}
    )
    app.dependency_overrides[get_bureau_provider] = lambda: counting
    try:
        resp = _post(client, _body(session, bureau_authorized=False), key="qa-noconsent")
    finally:
        app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER)
    assert resp.status_code == 422  # CA-01: sin consentimiento no se evalua
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert counting.calls == 0  # el adaptador jamas fue llamado
    assert _app_count(session) == 0
    assert _notification_count(session) == 0


def test_qa_campos_faltantes_422(app_client):
    client, session = app_client["client"], app_client["session"]
    payload = _body(session)
    del payload["product_id"]
    resp = _post(client, payload, key="qa-missing")
    assert resp.status_code == 422  # CA-02: no pasa al motor
    assert _app_count(session) == 0


def test_qa_sin_idempotency_key_422(app_client):
    client, session = app_client["client"], app_client["session"]
    resp = _post(client, _body(session), key=None)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _app_count(session) == 0


def test_qa_monto_fuera_de_producto_422(app_client):
    client, session = app_client["client"], app_client["session"]
    resp = _post(client, _body(session, requested_amount_minor=1), key="qa-rango")
    assert resp.status_code == 422  # bajo el minimo de PERSONAL_LIBRE
    resp = _post(client, _body(session, term_months=7), key="qa-plazo")
    assert resp.status_code == 422  # plazo no permitido por el producto


# ------------------------------------------- aprobacion separada
def test_qa_aprobacion_separada_analista_aprueba(app_client):
    client, session = app_client["client"], app_client["session"]
    created = _submit_with_score(client, session, 660, "qa-sep-apr").json()["data"]
    assert created["status"] == "IN_REVIEW"
    review = client.post(
        f"/api/v1/credits/applications/{created['id']}/review",
        json={"decision": "APPROVED", "reason": "verificacion manual superada"},
        headers=_analyst_headers(ANALYST, ["credit_analyst"]),
    )
    assert review.status_code == 200, review.text
    solved = review.json()["data"]
    assert solved["status"] == "APPROVED"  # la solicitud no se auto-aprueba
    assert solved["decision_reason"] == "verificacion manual superada"
    assert _outbox_types(session, created["id"])[-1] == "credit.approved"
    assert "credit.application.reviewed" in _audit_actions(session)
    got = client.get(f"/api/v1/credits/applications/{created['id']}")
    assert got.json()["data"]["status"] == "APPROVED"  # dictamen final persiste
    tray = client.get(
        "/api/v1/credits/applications?status=IN_REVIEW",
        headers=_analyst_headers(ANALYST, ["analyst"]),
    )
    assert tray.json()["data"] == []


def test_qa_aprobacion_separada_analista_rechaza(app_client):
    client, session = app_client["client"], app_client["session"]
    created = _submit_with_score(client, session, 660, "qa-sep-rej").json()["data"]
    review = client.post(
        f"/api/v1/credits/applications/{created['id']}/review",
        json={"decision": "REJECTED", "reason": "ingresos no verificables"},
        headers=_analyst_headers(ANALYST, ["CREDIT_ANALYST"]),
    )
    assert review.status_code == 200, review.text
    assert review.json()["data"]["status"] == "REJECTED"
    assert _outbox_types(session, created["id"])[-1] == "credit.rejected"


def test_qa_solicitante_no_dictamina_lo_propio(app_client):
    client, session = app_client["client"], app_client["session"]
    created = _submit_with_score(client, session, 660, "qa-sep-propia").json()["data"]
    review = client.post(
        f"/api/v1/credits/applications/{created['id']}/review",
        json={"decision": "APPROVED", "reason": "auto-aprobacion"},
        headers=_analyst_headers(USER, ["analyst"]),
    )
    assert review.status_code == 409  # separacion de funciones aunque traiga rol
    assert review.json()["error"]["code"] == "CONFLICT"
    got = client.get(f"/api/v1/credits/applications/{created['id']}")
    assert got.json()["data"]["status"] == "IN_REVIEW"


def test_qa_revision_exige_rol_y_no_reevalua_decidido(app_client):
    client, session = app_client["client"], app_client["session"]
    pending = _submit_with_score(client, session, 660, "qa-sep-rol").json()["data"]
    url = f"/api/v1/credits/applications/{pending['id']}/review"
    assert client.post(url, json={"decision": "APPROVED", "reason": "x"}).status_code == 401
    plain = client.post(
        url, json={"decision": "APPROVED", "reason": "x"}, headers=_analyst_headers(USER, [])
    )
    assert plain.status_code == 403
    assert plain.json()["error"]["code"] == "NOT_AUTHORIZED"
    decided = _post(client, _body(session), key="qa-sep-dec").json()["data"]
    assert decided["status"] == "APPROVED"
    again = client.post(
        f"/api/v1/credits/applications/{decided['id']}/review",
        json={"decision": "REJECTED", "reason": "reevaluar"},
        headers=_analyst_headers(ANALYST, ["analyst"]),
    )
    assert again.status_code == 409  # decidido no se reevalua
    assert again.json()["error"]["code"] == "CONFLICT"


# ------------------------------------------- idempotencia + enumeracion + logs
def test_qa_idempotencia_un_solo_expediente_una_notificacion(app_client):
    client, session = app_client["client"], app_client["session"]
    payload = _body(session)
    first = _post(client, payload, key="qa-idem")
    second = _post(client, payload, key="qa-idem")
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    assert second.json()["meta"] == {"replayed": True}
    assert _app_count(session) == 1
    assert _notification_count(session) == 1


def test_qa_idempotencia_cuerpo_distinto_409(app_client):
    client, session = app_client["client"], app_client["session"]
    first = _post(client, _body(session), key="qa-idem-conf")
    assert first.status_code == 201
    second = _post(client, _body(session, requested_amount_minor=2_000_000), key="qa-idem-conf")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert _app_count(session) == 1


def test_qa_expediente_ajeno_404_neutro(app_client):
    client, session = app_client["client"], app_client["session"]
    created = _post(client, _body(session), key="qa-ajeno").json()["data"]
    app.dependency_overrides[get_current_user_id] = lambda: OTHER
    try:
        resp = client.get(f"/api/v1/credits/applications/{created['id']}")
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: USER
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    ghost = client.get(f"/api/v1/credits/applications/{uuid.uuid4()}")
    assert ghost.status_code == 404  # mismo cuerpo: sin oraculo de existencia
    assert ghost.json()["error"] == resp.json()["error"]


def test_qa_sin_pii_en_logs(app_client, caplog):
    client, session = app_client["client"], app_client["session"]
    income, debt = 7_654_321, 1_234_567
    with caplog.at_level(logging.INFO, logger="app"):
        resp = _post(
            client,
            _body(session, total_income_minor=income, total_debt_minor=debt),
            key="qa-nopii",
        )
    assert resp.status_code == 201
    assert str(income) not in caplog.text
    assert str(debt) not in caplog.text


def test_qa_aislamiento_sin_loans_ni_dinero(app_client):
    client, session = app_client["client"], app_client["session"]
    _post(client, _body(session), key="qa-ais-1")
    _post(
        client,
        _body(session, total_income_minor=1_000_000, total_debt_minor=600_000),
        key="qa-ais-2",
    )
    app.dependency_overrides[get_bureau_provider] = lambda: MockCreditBureau(mode="down")
    try:
        _post(client, _body(session), key="qa-ais-3")
    finally:
        app.dependency_overrides[get_bureau_provider] = lambda: _bureau_for(USER)
    assert _app_count(session) == 3
    loan_tables = session.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'loan%'")
    ).all()
    names = sorted(row[0] for row in loan_tables)
    assert "loans" not in names  # el desembolso es Sprint 3: aqui no se crea prestamo
    assert "loan_schedules" not in names
    assert "loan_payments" not in names
    for event_type in _all_outbox_types(session):
        assert event_type.startswith("credit.")  # ningun evento mueve dinero
