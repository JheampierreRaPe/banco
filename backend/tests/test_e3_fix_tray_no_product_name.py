"""Regresion H-01 (BAJO, contrato): la bandeja no serializa `product_name`.

Causa (`backend/app/modules/credits/api/applications.py:194` +
`schemas/applications.py:106`): `GET /credits/applications` usa
`OwnApplicationData` (con `product_name: str | None = None`) como
`response_model` para ambos modos; el servicio de la bandeja (`to_view`)
no emite ese campo, asi que la bandeja (`?status=IN_REVIEW`) serializaba
un `"product_name": null` que antes no existia (el modelo anterior era
`ApplicationData`, sin ese campo).

Fix: `response_model_exclude_unset=True` en el endpoint (no
`exclude_none`: ese borraria tambien los `null` legitimos del titular,
como `decided_at`, y romperia su contrato minimo exacto). Con
`exclude_unset`, la bandeja omite `product_name` (no viene en el dict
del servicio) y el titular lo conserva (su dict siempre trae la clave,
incluso en `None` si faltara el producto).

Sin el fix, `test_tray_has_no_product_name` falla
(`"product_name": null` presente). Con el fix, ambas pasan.

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron del repo);
auth con JWT reales (`create_access_token`). Sin red.
"""

from __future__ import annotations

import uuid
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
from app.modules.credits.repository import applications as apps_repo

USER = uuid.uuid4()
ANALYST = uuid.uuid4()

TABLES = [
    "credits.loan_products",
    "credits.loan_applications",
    "credits.contracts",  # el listado del titular deriva `contract_signed` de aqui
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
        session.close()
        engine.dispose()


def _bearer(user_id: uuid.UUID, roles: list[str] | None = None) -> dict:
    from app.core.security import create_access_token

    token = create_access_token(subject=str(user_id), roles=roles)
    return {"Authorization": f"Bearer {token}"}


def _product_id(session: Session, code: str = "PERSONAL_LIBRE") -> str:
    from app.modules.credits import service as catalog

    return str(catalog.get_product(session, code=code)["id"])


def _mk_app(session: Session, owner: uuid.UUID, product_id: str, *, status: str):
    row = apps_repo.create_application(
        session,
        user_id=owner,
        product_id=product_id,
        requested_amount_minor=1_000_000,
        term_months=12,
    )
    if status != "SUBMITTED":
        apps_repo.set_evaluating(session, row)
    if status in ("IN_REVIEW", "APPROVED", "REJECTED"):
        apps_repo.set_decision(
            session,
            row,
            status=status,
            score=700,
            dti_ratio=Decimal("0.1000"),
            total_income_minor=2_000_000,
            total_debt_minor=200_000,
            decision_reason=f"motivo-{status.lower()}",
        )
    session.commit()
    return row


def test_tray_has_no_product_name(app_client):
    """La bandeja del analista es byte-identica a antes: sin `product_name`."""
    client, session = app_client["client"], app_client["session"]
    row = _mk_app(session, USER, _product_id(session), status="IN_REVIEW")
    resp = client.get(
        "/api/v1/credits/applications",
        headers=_bearer(ANALYST, ["analyst"]),
        params={"status": "IN_REVIEW"},
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"][0]
    assert item["id"] == str(row.id)
    assert "product_name" not in item  # falla sin el fix: null presente
    # Expediente completo intacto (no se recorto nada mas).
    assert item["user_id"] == str(USER)
    assert item["score"] == 700
    assert item["total_income_minor"] == 2_000_000


def test_owner_list_keeps_product_name(app_client):
    """El listado del titular si lleva `product_name` (contrato E3-T23)."""
    client, session = app_client["client"], app_client["session"]
    _mk_app(session, USER, _product_id(session), status="SUBMITTED")
    resp = client.get("/api/v1/credits/applications", headers=_bearer(USER))
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"][0]
    assert item["product_name"] == "Prestamo personal de libre disponibilidad"
    assert item["contract_signed"] is False  # sin juego contractual: no firmado
