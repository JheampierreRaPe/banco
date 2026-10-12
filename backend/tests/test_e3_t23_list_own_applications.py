"""Listado "Mis creditos" del titular (E3-T23, HU10/HU11, backend).

- Camino feliz: `GET /credits/applications` (sin `status`) devuelve
  solo los expedientes del titular (`sub` del JWT), orden
  `created_at DESC, id`, vista minima + `product_name`,
  `meta.total` correcto.
- Aislamiento: dos titulares ven solo lo suyo; sin expedientes ->
  `data: []` (no `404`).
- Errores: sin `Authorization` -> `401 NOT_AUTHENTICATED`;
  `limit`/`offset` invalidos -> `422`; otro `status` -> `422`.
- Regresion: `?status=IN_REVIEW` (analista, expediente completo) y
  `GET .../{id}` (titular, `404` neutro) intactos.
- Sin PII de mas: el listado no expone ingresos/deuda/score ni los
  loguea.

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron del
repo); `get_db` se inyecta via `app.dependency_overrides` y la auth
usa JWT reales (`create_access_token`). Sin red.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
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
ANALYST = uuid.uuid4()

TABLES = [
    "credits.loan_products",
    "credits.loan_applications",
    "credits.contracts",
]

MINIMAL_KEYS = {
    "id",
    "product_id",
    "product_name",
    "requested_amount_minor",
    "term_months",
    "status",
    "contract_signed",
    "decision_reason",
    "created_at",
    "updated_at",
    "decided_at",
}

SENSITIVE_KEYS = {
    "user_id",
    "bureau_authorized",
    "bureau_authorized_at",
    "score",
    "dti_ratio",
    "total_income_minor",
    "total_debt_minor",
}

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
    """TestClient con catalogo demo y JWT reales (sin overrides de auth)."""
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


def _mk_app(
    session: Session,
    owner: uuid.UUID,
    product_id: str,
    *,
    status: str = "SUBMITTED",
    amount: int = 1_000_000,
    term: int = 12,
    created_ago_seconds: int = 0,
    reason: str | None = None,
):
    """Inserta un expediente directo por repositorio (sin bureau ni reglas)."""
    row = apps_repo.create_application(
        session,
        user_id=owner,
        product_id=product_id,
        requested_amount_minor=amount,
        term_months=term,
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
            decision_reason=reason or f"motivo-{status.lower()}",
        )
    moment = datetime.now(UTC) - timedelta(seconds=created_ago_seconds)
    row.created_at = moment
    row.updated_at = moment
    session.commit()
    return row


def _list(client: TestClient, headers: dict | None, **params):
    return client.get("/api/v1/credits/applications", headers=headers, params=params or None)


def _mk_contracts(session: Session, app_id, statuses=("GENERATED", "GENERATED", "GENERATED")):
    """Juego contractual directo (sin firma biometrica ni notificaciones)."""
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


# ------------------------------------------------ camino feliz
def test_list_own_orders_desc_and_meta_total(app_client):
    client, session = app_client["client"], app_client["session"]
    product = _product_id(session)
    older = _mk_app(
        session, USER, product, status="APPROVED", created_ago_seconds=100, reason="motivo-qa"
    )
    newer = _mk_app(session, USER, product, status="SUBMITTED", created_ago_seconds=10)
    resp = _list(client, _bearer(USER))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"] == {"total": 2, "limit": 50, "offset": 0}
    assert [item["id"] for item in body["data"]] == [str(newer.id), str(older.id)]
    first, second = body["data"]
    assert set(first.keys()) == MINIMAL_KEYS  # CA-02: payload minimo exacto
    assert SENSITIVE_KEYS.isdisjoint(first.keys())
    assert SENSITIVE_KEYS.isdisjoint(second.keys())
    assert first["product_id"] == product
    assert first["product_name"] == "Prestamo personal de libre disponibilidad"
    assert first["requested_amount_minor"] == 1_000_000
    assert first["term_months"] == 12
    assert first["status"] == "SUBMITTED"
    assert first["created_at"] and first["updated_at"]
    assert second["status"] == "APPROVED"
    assert second["decision_reason"] == "motivo-qa"
    assert second["decided_at"]


def test_list_pagination_slices_with_total(app_client):
    client, session = app_client["client"], app_client["session"]
    product = _product_id(session)
    for ago in (30, 20, 10):
        _mk_app(session, USER, product, created_ago_seconds=ago)
    page = _list(client, _bearer(USER), limit=2, offset=1)
    assert page.status_code == 200, page.text
    assert page.json()["meta"] == {"total": 3, "limit": 2, "offset": 1}
    assert len(page.json()["data"]) == 2


# ------------------------------------------------ aislamiento + vacio
def test_list_isolation_between_holders(app_client):
    client, session = app_client["client"], app_client["session"]
    product = _product_id(session)
    mine = _mk_app(session, USER, product, created_ago_seconds=10)
    alien = _mk_app(session, OTHER, product, created_ago_seconds=5)
    as_user = _list(client, _bearer(USER))
    assert as_user.status_code == 200, as_user.text
    assert [item["id"] for item in as_user.json()["data"]] == [str(mine.id)]
    as_other = _list(client, _bearer(OTHER))
    assert as_other.status_code == 200, as_other.text
    assert [item["id"] for item in as_other.json()["data"]] == [str(alien.id)]


def test_list_empty_holder_gets_empty_data(app_client):
    client, session = app_client["client"], app_client["session"]
    _mk_app(session, USER, _product_id(session))
    resp = _list(client, _bearer(OTHER))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []
    assert resp.json()["meta"] == {"total": 0, "limit": 50, "offset": 0}


# ------------------------------------------------ errores
def test_list_without_auth_is_401(app_client):
    client = app_client["client"]
    resp = _list(client, None)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"


def test_list_invalid_pagination_is_422(app_client):
    client = app_client["client"]
    assert _list(client, _bearer(USER), limit=0).status_code == 422
    assert _list(client, _bearer(USER), limit=101).status_code == 422
    assert _list(client, _bearer(USER), offset=-1).status_code == 422


def test_list_unknown_status_is_422(app_client):
    client = app_client["client"]
    resp = _list(client, _bearer(ANALYST, ["analyst"]), status="APPROVED")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ------------------------------------------------ firma del contrato (mejora dueno)
def test_list_contract_signed_false_before_signing(app_client):
    client, session = app_client["client"], app_client["session"]
    product = _product_id(session)
    bare = _mk_app(session, USER, product, status="APPROVED", created_ago_seconds=20)
    generated = _mk_app(session, USER, product, status="APPROVED", created_ago_seconds=10)
    _mk_contracts(session, generated.id)  # juego generado, sin firmar
    resp = _list(client, _bearer(USER))
    assert resp.status_code == 200, resp.text
    by_id = {item["id"]: item for item in resp.json()["data"]}
    assert by_id[str(bare.id)]["status"] == "APPROVED"
    assert by_id[str(bare.id)]["contract_signed"] is False
    assert by_id[str(generated.id)]["contract_signed"] is False


def test_list_contract_signed_true_when_all_signed(app_client):
    client, session = app_client["client"], app_client["session"]
    product = _product_id(session)
    signed = _mk_app(session, USER, product, status="APPROVED", created_ago_seconds=20)
    _mk_contracts(session, signed.id, statuses=("SIGNED", "SIGNED", "SIGNED"))
    partial = _mk_app(session, USER, product, status="APPROVED", created_ago_seconds=10)
    _mk_contracts(session, partial.id, statuses=("SIGNED", "GENERATED", "GENERATED"))
    resp = _list(client, _bearer(USER))
    assert resp.status_code == 200, resp.text
    by_id = {item["id"]: item for item in resp.json()["data"]}
    assert by_id[str(signed.id)]["status"] == "APPROVED"  # el expediente sigue APPROVED
    assert by_id[str(signed.id)]["contract_signed"] is True
    assert by_id[str(partial.id)]["contract_signed"] is False  # mezcla no es firmado


# ------------------------------------------------ regresion (bandeja + detalle)
def test_analyst_tray_keeps_full_shape(app_client):
    client, session = app_client["client"], app_client["session"]
    row = _mk_app(session, USER, _product_id(session), status="IN_REVIEW")
    tray = _list(client, _bearer(ANALYST, ["analyst"]), status="IN_REVIEW")
    assert tray.status_code == 200, tray.text
    assert [item["id"] for item in tray.json()["data"]] == [str(row.id)]
    item = tray.json()["data"][0]
    assert item["user_id"] == str(USER)  # expediente completo intacto
    assert item["score"] == 700
    assert item["total_income_minor"] == 2_000_000

    denied = _list(client, _bearer(USER), status="IN_REVIEW")
    assert denied.status_code == 403  # sin rol analista
    assert denied.json()["error"]["code"] == "NOT_AUTHORIZED"
    anon = _list(client, None, status="IN_REVIEW")
    assert anon.status_code == 401


def test_get_by_id_still_owner_scoped(app_client):
    client, session = app_client["client"], app_client["session"]
    row = _mk_app(session, USER, _product_id(session), status="APPROVED")
    app.dependency_overrides[get_current_user_id] = lambda: USER
    try:
        got = client.get(f"/api/v1/credits/applications/{row.id}", headers=_bearer(USER))
        assert got.status_code == 200
        assert got.json()["data"]["status"] == "APPROVED"
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)
    app.dependency_overrides[get_current_user_id] = lambda: OTHER
    try:
        alien = client.get(f"/api/v1/credits/applications/{row.id}", headers=_bearer(OTHER))
        assert alien.status_code == 404  # neutro: ajeno == inexistente
        assert alien.json()["error"]["code"] == "NOT_FOUND"
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)


# ------------------------------------------------ sin PII en logs
def test_list_does_not_log_pii(app_client, caplog):
    import logging

    client, session = app_client["client"], app_client["session"]
    _mk_app(session, USER, _product_id(session), status="APPROVED")
    with caplog.at_level(logging.DEBUG, logger="app"):
        resp = _list(client, _bearer(USER))
    assert resp.status_code == 200
    assert "2000000" not in caplog.text
    assert "200000" not in caplog.text
