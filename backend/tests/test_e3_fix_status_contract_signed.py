"""Regresion: `GET /credits/applications/{id}` expone `contract_signed`.

Defecto: "Mis creditos" ya mostraba `Firmado` (listado con
`contract_signed`), pero la pantalla de Estado (`GET .../{id}` ->
`LoanApplicationResult`) no conocia la firma y seguia ofreciendo
"Continuar a la firma" sobre un expediente ya firmado.

- Antes de firmar (sin juego o juego `GENERATED`/parcial):
  `contract_signed is False`.
- Tras firmar (todas las filas `SIGNED`): `contract_signed is True`
  (el `status` sigue `APPROVED`; la firma no cambia el expediente).
- Ajeno/inexistente -> `404 NOT_FOUND` neutro (intacto).
- Bandeja del analista intacta (`user_id` + `score` presentes).

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron del
repo); `get_db` y `get_current_user_id` se inyectan via
`app.dependency_overrides`. Sin red, sin PII en logs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.modules.credits.models as _credits
from app.core.db import Base, get_db
from app.main import app
from app.modules.credits import repository as repo
from app.modules.credits.api import get_current_user_id
from app.modules.credits.repository import applications as apps_repo

USER = uuid.uuid4()
OTHER = uuid.uuid4()

TABLES = [
    "credits.loan_products",
    "credits.loan_applications",
    "credits.contracts",
]

assert _credits is not None


def _make_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("credits",):
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

        app.dependency_overrides[get_db] = _override_db
        with TestClient(app) as client:
            yield {"client": client, "session": session}
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        session.close()
        engine.dispose()


def _bearer(user_id: uuid.UUID) -> dict:
    from app.core.security import create_access_token

    token = create_access_token(subject=str(user_id))
    return {"Authorization": f"Bearer {token}"}


def _product_id(session: Session, code: str = "PERSONAL_LIBRE") -> str:
    from app.modules.credits import service as catalog

    return str(catalog.get_product(session, code=code)["id"])


def _mk_app(session: Session, owner: uuid.UUID, product_id: str, *, status: str = "APPROVED"):
    row = apps_repo.create_application(
        session,
        user_id=owner,
        product_id=product_id,
        requested_amount_minor=1_000_000,
        term_months=12,
    )
    apps_repo.set_evaluating(session, row)
    apps_repo.set_decision(
        session,
        row,
        status=status,
        score=700,
        dti_ratio=Decimal("0.1000"),
        total_income_minor=2_000_000,
        total_debt_minor=200_000,
        decision_reason=f"motivo-{status.lower()}",
        bureau_authorized_at=datetime.now(UTC),
    )
    session.commit()
    return row


def _mk_contracts(session: Session, app_id, statuses=("GENERATED", "GENERATED", "GENERATED")):
    from app.modules.credits.models import Contract

    for doc_type, status in zip(("CONTRATO", "PAGARE", "RESUMEN"), statuses):
        session.add(
            Contract(
                application_id=app_id,
                doc_type=doc_type,
                doc_ref=f"ref-{doc_type.lower()}",
                doc_hash="h" * 64,
                status=status,
            )
        )
    session.commit()


def _get(client: TestClient, app_id, user: uuid.UUID):
    app.dependency_overrides[get_current_user_id] = lambda: user
    try:
        return client.get(f"/api/v1/credits/applications/{app_id}", headers=_bearer(user))
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)


def test_detail_contract_signed_false_before_signing(app_client):
    client, session = app_client["client"], app_client["session"]
    product = _product_id(session)
    bare = _mk_app(session, USER, product)
    generated = _mk_app(session, USER, product)
    _mk_contracts(session, generated.id)  # juego generado, sin firmar

    for row in (bare, generated):
        resp = _get(client, row.id, USER)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["status"] == "APPROVED"
        assert data["contract_signed"] is False


def test_detail_contract_signed_true_when_all_signed(app_client):
    client, session = app_client["client"], app_client["session"]
    product = _product_id(session)
    signed = _mk_app(session, USER, product)
    _mk_contracts(session, signed.id, statuses=("SIGNED", "SIGNED", "SIGNED"))
    partial = _mk_app(session, USER, product)
    _mk_contracts(session, partial.id, statuses=("SIGNED", "GENERATED", "GENERATED"))

    got = _get(client, signed.id, USER)
    assert got.status_code == 200, got.text
    assert got.json()["data"]["status"] == "APPROVED"  # la firma no cambia el estado
    assert got.json()["data"]["contract_signed"] is True

    mixed = _get(client, partial.id, USER)
    assert mixed.status_code == 200, mixed.text
    assert mixed.json()["data"]["contract_signed"] is False  # mezcla no es firmado


def test_detail_still_owner_scoped_and_tray_intact(app_client):
    client, session = app_client["client"], app_client["session"]
    row = _mk_app(session, USER, _product_id(session))

    alien = _get(client, row.id, OTHER)
    assert alien.status_code == 404
    assert alien.json()["error"]["code"] == "NOT_FOUND"

    from app.core.security import create_access_token

    analyst = uuid.uuid4()
    token = create_access_token(subject=str(analyst), roles=["analyst"])
    in_review = _mk_app(session, USER, _product_id(session), status="IN_REVIEW")
    tray = client.get(
        "/api/v1/credits/applications",
        headers={"Authorization": f"Bearer {token}"},
        params={"status": "IN_REVIEW"},
    )
    assert tray.status_code == 200, tray.text
    assert [item["id"] for item in tray.json()["data"]] == [str(in_review.id)]
    item = tray.json()["data"][0]
    assert item["user_id"] == str(USER)
    assert item["score"] == 700
