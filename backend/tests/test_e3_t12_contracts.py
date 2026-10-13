"""Juego contractual: contrato + pagare + hoja resumen (E3-T12, HU11).

- Camino feliz: expediente `APPROVED` -> `POST .../contract` (201) ->
  3 filas `contracts` `GENERATED` con `doc_hash`; `GET` metadatos y
  descarga PDF (`%PDF`) de cada documento.
- Errores: expediente `REJECTED`/`IN_REVIEW` -> `409`; ajeno o
  inexistente -> `404` neutro; sin `Idempotency-Key` -> `422`;
  misma clave con otro expediente -> `409`; sin backend PDF -> `501`
  (sin romper la generacion/persistencia).
- Integridad: mismas condiciones -> mismo `doc_hash`; un campo
  alterado -> hash distinto.
- Idempotencia: doble `POST` (misma u otra clave) -> un juego y un
  solo evento `credit.contract.generated`.
- Aislamiento: 0 filas en `loans`/`loan_schedules` (ni existen las
  tablas), 0 `transactions`/`postings` nuevas.
- Sin PII en logs.

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron del
repo); `get_db` y `get_current_user_id` (credits) se inyectan via
`app.dependency_overrides`. Sin red.
"""

from __future__ import annotations

import re
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
from app.modules.credits.service import applications as apps_service
from app.modules.credits.service import documents as docs_service

USER = uuid.uuid4()
OTHER = uuid.uuid4()

TABLES = [
    "credits.loan_products",
    "credits.loan_applications",
    "credits.contracts",
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
    """TestClient con catalogo demo y auth falsa (USER)."""
    engine = _engine_with_catalog()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:

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


def _product_id(session: Session, code: str = "PERSONAL_LIBRE") -> str:
    return str(catalog.get_product(session, code=code)["id"])


def _approved_app_id(
    session: Session, user: uuid.UUID = USER, *, score: int = 780, commit: bool = True
) -> str:
    view = apps_service.submit_application(
        session,
        user,
        product_id=_product_id(session),
        requested_amount_minor=1_000_000,
        term_months=12,
        total_income_minor=2_000_000,
        total_debt_minor=100_000,
        bureau_authorized=True,
        bureau_provider=_bureau_for(user, score=score),
    )
    assert view["status"] == "APPROVED"
    if commit:
        session.commit()
    return view["id"]


def _in_review_app_id(session: Session, user: uuid.UUID = USER) -> str:
    from app.adapters.credit_bureau import BUREAU_UNAVAILABLE, CreditBureauError

    class _Down(MockCreditBureau):
        def get_score(self, **kwargs):
            raise CreditBureauError(BUREAU_UNAVAILABLE, "bureau caido")

    view = apps_service.submit_application(
        session,
        user,
        product_id=_product_id(session),
        requested_amount_minor=1_000_000,
        term_months=12,
        total_income_minor=2_000_000,
        total_debt_minor=100_000,
        bureau_authorized=True,
        bureau_provider=_Down(profiles={}),
    )
    assert view["status"] == "IN_REVIEW"
    session.commit()
    return view["id"]


def _post_contract(client: TestClient, app_id: str, key: str | None = "k-contract"):
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(f"/api/v1/credits/applications/{app_id}/contract", headers=headers)


def _contract_count(session: Session) -> int:
    table = Base.metadata.tables["credits.contracts"]
    session.expire_all()
    return int(session.scalar(sa.select(sa.func.count()).select_from(table)) or 0)


def _contract_events(session: Session) -> list[str]:
    table = Base.metadata.tables["shared.outbox"]
    session.expire_all()
    return [
        row[0]
        for row in session.execute(
            sa.select(table.c.event_type)
            .where(table.c.event_type == "credit.contract.generated")
            .order_by(table.c.created_at)
        ).all()
    ]


# ------------------------------------------------ camino feliz
def test_generate_and_download_all_docs(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _approved_app_id(session)

    resp = _post_contract(client, app_id, key="k-happy-1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["meta"] == {"replayed": False}
    data = body["data"]
    assert data["application_id"] == app_id
    assert data["status"] == "GENERATED"
    docs = {doc["doc"]: doc for doc in data["documents"]}
    assert set(docs) == {"contrato", "pagare", "resumen"}
    for doc in data["documents"]:
        assert doc["status"] == "GENERATED"
        assert doc["doc_ref"].endswith(".pdf") and app_id in doc["doc_ref"]
        assert re.fullmatch(r"[0-9a-f]{64}", doc["doc_hash"])
    assert len({doc["doc_hash"] for doc in data["documents"]}) == 3
    assert _contract_count(session) == 3

    got = client.get(f"/api/v1/credits/applications/{app_id}/contract")
    assert got.status_code == 200, got.text
    assert got.json()["data"] == data  # CA-01: metadatos con doc_ref/doc_hash

    for slug in ("contrato", "pagare", "resumen"):
        pdf = client.get(f"/api/v1/credits/applications/{app_id}/contract?format=pdf&doc={slug}")
        assert pdf.status_code == 200, pdf.text
        assert pdf.headers["content-type"] == "application/pdf"
        assert "attachment" in pdf.headers["content-disposition"]
        assert pdf.content.startswith(b"%PDF")  # PDF valido

    assert _contract_events(session) == ["credit.contract.generated"]


# ------------------------------------------------ idempotencia
def test_double_post_single_set_single_event(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _approved_app_id(session)

    first = _post_contract(client, app_id, key="k-idem-1")
    assert first.status_code == 201, first.text
    second = _post_contract(client, app_id, key="k-idem-1")
    assert second.status_code == 200, second.text
    assert second.json()["meta"] == {"replayed": True}
    assert second.json()["data"] == first.json()["data"]
    assert _contract_count(session) == 3
    assert _contract_events(session) == ["credit.contract.generated"]


def test_second_key_reuses_existing_set(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _approved_app_id(session)

    first = _post_contract(client, app_id, key="k-natural-1")
    assert first.status_code == 201, first.text
    # Idempotente por naturaleza (UQ por expediente): otra clave no duplica.
    replay = _post_contract(client, app_id, key="k-natural-2")
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"] == first.json()["data"]
    assert _contract_count(session) == 3
    assert _contract_events(session) == ["credit.contract.generated"]


def test_same_key_other_application_conflicts(app_client):
    client, session = app_client["client"], app_client["session"]
    app_a = _approved_app_id(session)
    app_b = _approved_app_id(session)
    assert _post_contract(client, app_a, key="k-shared").status_code == 201
    resp = _post_contract(client, app_b, key="k-shared")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# ------------------------------------------------ errores
def test_not_approved_conflicts(app_client):
    client, session = app_client["client"], app_client["session"]
    review_id = _in_review_app_id(session)
    resp = _post_contract(client, review_id, key="k-review-1")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CONFLICT"

    rejected = apps_service.submit_application(
        session,
        USER,
        product_id=_product_id(session),
        requested_amount_minor=1_000_000,
        term_months=12,
        total_income_minor=2_000_000,
        total_debt_minor=100_000,
        bureau_authorized=True,
        bureau_provider=_bureau_for(USER, score=100),
    )
    assert rejected["status"] == "REJECTED"
    session.commit()
    resp = _post_contract(client, rejected["id"], key="k-rejected-1")
    assert resp.status_code == 409, resp.text
    assert _contract_count(session) == 0


def test_foreign_or_missing_neutral_404(app_client):
    client, session = app_client["client"], app_client["session"]
    foreign_id = _approved_app_id(session, user=OTHER)
    for target in (foreign_id, str(uuid.uuid4())):
        resp = _post_contract(client, target, key="k-404")
        assert resp.status_code == 404, (target, resp.text)
        assert resp.json()["error"]["code"] == "NOT_FOUND"
        get = client.get(f"/api/v1/credits/applications/{target}/contract")
        assert get.status_code == 404, (target, get.text)


def test_get_before_generation_404(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _approved_app_id(session)
    resp = client.get(f"/api/v1/credits/applications/{app_id}/contract")
    assert resp.status_code == 404, resp.text


def test_missing_idempotency_key_422(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _approved_app_id(session)
    resp = _post_contract(client, app_id, key=None)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _contract_count(session) == 0


def test_invalid_format_or_doc_422(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _approved_app_id(session)
    assert _post_contract(client, app_id, key="k-422").status_code == 201
    bad_format = client.get(f"/api/v1/credits/applications/{app_id}/contract?format=docx")
    assert bad_format.status_code == 422, bad_format.text
    bad_doc = client.get(f"/api/v1/credits/applications/{app_id}/contract?format=pdf&doc=escritura")
    assert bad_doc.status_code == 422, bad_doc.text


def test_pdf_unavailable_501_keeps_persistence(app_client, monkeypatch):
    client, session = app_client["client"], app_client["session"]
    app_id = _approved_app_id(session)
    assert _post_contract(client, app_id, key="k-501").status_code == 201
    monkeypatch.setattr(docs_service, "pdf_available", lambda: None)
    pdf = client.get(f"/api/v1/credits/applications/{app_id}/contract?format=pdf&doc=contrato")
    assert pdf.status_code == 501, pdf.text
    assert pdf.json()["error"]["code"] == "EXPORT_FORMAT_NOT_SUPPORTED"
    # La generacion/persistencia sigue intacta (patron E3-T03/E2-T10).
    meta = client.get(f"/api/v1/credits/applications/{app_id}/contract")
    assert meta.status_code == 200
    assert len(meta.json()["data"]["documents"]) == 3


# ------------------------------------------------ integridad del hash
def test_doc_hash_reproducible_and_sensitive(app_client):
    _, session = app_client["client"], app_client["session"]
    app_id = _approved_app_id(session)
    from app.modules.credits.repository import applications as apps_repo

    row = apps_repo.get_application(session, app_id)
    assert row is not None
    ctx = docs_service.build_context(session, row)
    for doc_type in ("CONTRATO", "PAGARE", "RESUMEN"):
        first = docs_service.doc_hash_for(docs_service.canonical_for(doc_type, ctx))
        again = docs_service.doc_hash_for(docs_service.canonical_for(doc_type, ctx))
        assert first == again  # reproducible
        tampered = dict(ctx, requested_amount_minor=ctx["requested_amount_minor"] + 1)
        other = docs_service.doc_hash_for(docs_service.canonical_for(doc_type, tampered))
        assert other != first  # sensible a cualquier cambio


# ------------------------------------------------ aislamiento (no mueve dinero)
def test_generation_creates_no_loans_nor_money(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _approved_app_id(session)
    assert _post_contract(client, app_id, key="k-iso").status_code == 201
    session.expire_all()
    names = {
        row[0]
        for row in session.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='table'")
        ).all()
    }
    assert "loans" not in names
    assert "loan_schedules" not in names
    assert "loan_payments" not in names
    assert "transactions" not in names
    assert "postings" not in names
    assert "journal_entries" not in names
    outbox = Base.metadata.tables["shared.outbox"]
    types = {row[0] for row in session.execute(sa.select(outbox.c.event_type)).all()}
    assert types <= {
        "credit.application.submitted",
        "credit.approved",
        "credit.contract.generated",
    }


# ------------------------------------------------ sin PII en logs
def test_no_pii_in_logs(app_client, caplog):
    import logging

    client, session = app_client["client"], app_client["session"]
    app_id = _approved_app_id(session)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.modules.credits"):
        resp = _post_contract(client, app_id, key="k-logs")
    assert resp.status_code == 201, resp.text
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert "2000000" not in blob
    assert "1000000" not in blob
    assert "%PDF" not in blob
