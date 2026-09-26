"""Precheck de email del paso "Continuar" del KYC (E1-T40, HU01 CA-01/CA-02).

- Sin Postgres: `TestClient(app)` + override de `get_db` a SQLite en memoria
  con schema ATTACH (`identity`), patron de `tests/test_pin_reset.py`.
  Buckets del rate-limit KYC limpios por fixture
  (`kyc_proxy.reset_rate_limits`).
- Casos: email nuevo -> `200 {"data": {"available": true}}` sin proveedor
  ni escritura; email registrado (con mayusculas/espacios) ->
  `409 DUPLICATE_EMAIL` neutro (`"El correo ya esta registrado"`, sin eco
  del email); malformado/vacio -> `422`; ventana excedida -> `429
  RATE_LIMITED` verificado ANTES de la existencia; `caplog` sin el email;
  OpenAPI expone la ruta.
- Reglas estaticas (servicio solo lectura via `get_by_email`, normaliza
  `strip().lower()`, sin PII en logs; ruta con rate-limit previo).
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base, get_db
from app.main import app

SERVICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "service"
    / "email_check.py"
)
SCHEMAS_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "schemas"
    / "email_check.py"
)
API_PATH = Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "api" / "kyc.py"


@pytest.fixture()
def email_session():
    """Sesion SQLite aislada (solo `identity.users`, el precheck es read-only)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.identity.models as _i  # noqa: F401 (registro)

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS identity")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["identity.users"],
            Base.metadata.tables["identity.credentials"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def email_client(email_session: Session):
    """TestClient con `get_db` a SQLite y buckets del rate-limit KYC limpios."""
    from app.modules.identity.service import kyc_proxy

    kyc_proxy.reset_rate_limits()

    def _override():
        yield email_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        kyc_proxy.reset_rate_limits()


def _make_email_user(email_session: Session, *, email: str | None = None):
    """Usuario con email normalizado (el precheck resuelve por `get_by_email`)."""
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service.kyc_onboarding import hash_document_number

    suffix = uuid.uuid4().hex[:8]
    user = identity_repo.create_user(
        email_session,
        doc_type="DNI",
        doc_number_hash=hash_document_number(f"DNI{suffix}"),
        first_name="Ada",
        last_name="Lovelace",
        email=(email or f"ada.{suffix}@example.com").strip().lower(),
        phone="+51999888777",
        status="ACTIVE",
    )
    email_session.commit()
    return user


def _count_users(email_session: Session) -> int:
    import sqlalchemy as sa

    from app.modules.identity.models import User

    email_session.expire_all()
    return int(email_session.scalar(sa.select(sa.func.count()).select_from(User)) or 0)


# ---------------------------------------------------------------- Reglas estaticas
def test_static_rules_email_check_readonly_no_pii():
    service = SERVICE_PATH.read_text(encoding="utf-8")
    assert "get_by_email" in service, "resuelve por email via repositorio"
    assert ".commit(" not in service and ".flush(" not in service, "solo lectura"
    assert "strip().lower()" in service or "strip()" in service, "normaliza el email"
    for forbidden in ("kyc_proxy", "create_kyc_provider", "lookup_document"):
        assert forbidden not in service, f"sin proveedor externo: {forbidden}"
    log_lines = [line for line in service.splitlines() if "logger." in line]
    assert log_lines, "el servicio debe loguear sin PII"
    for line in log_lines:
        lowered = line.lower()
        for forbidden in ("email=", "recipient", "destination", "plain"):
            assert forbidden not in lowered, f"posible PII en log: {line.strip()}"
    # Fix MENOR (hash de email sin sal): el log degrada a solo el booleano
    # `has_match`; ni hash sin sal ni pseudonimo correlacionable del email.
    # (Se inspecciona codigo ejecutable: imports + lineas `logger.`, no la
    # prosa del docstring que documenta el retiro.)
    assert "hashlib" not in service, "sin SHA-256 del email en el servicio"
    assert "_short_email_hash" not in service, "helper de hash retirado"
    assert "hash_token" not in service, "sin hash correlacionable en el servicio"
    for line in log_lines:
        assert "hash" not in line.lower(), f"sin hash en log: {line.strip()}"

    schemas = SCHEMAS_PATH.read_text(encoding="utf-8")
    assert "EMAIL_PATTERN" in schemas and "available" in schemas
    assert "max_length=320" in schemas

    api = API_PATH.read_text(encoding="utf-8")
    assert "/auth/kyc/email/check" in api
    assert "DUPLICATE_EMAIL" in api
    assert "El correo ya esta registrado" in api


# ---------------------------------------------------------------- CA-01: feliz y duplicado
def test_email_check_available_true_for_new_email(email_client: TestClient, email_session: Session):
    before = _count_users(email_session)
    resp = email_client.post("/api/v1/auth/kyc/email/check", json={"email": "nuevo@example.com"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"available": True}
    assert "request_id" in resp.json()["meta"]
    assert _count_users(email_session) == before, "sin escritura ni proveedor"


def test_email_check_duplicate_with_case_and_spaces(
    email_client: TestClient, email_session: Session, caplog
):
    user = _make_email_user(email_session, email="duplicado@example.com")
    before = _count_users(email_session)
    with caplog.at_level(logging.INFO, logger="app.modules.identity.service.email_check"):
        resp = email_client.post(
            "/api/v1/auth/kyc/email/check",
            json={"email": "  DUPLICADO@Example.COM  "},
        )
    assert resp.status_code == 409, resp.text
    body = resp.json()["error"]
    assert body["code"] == "DUPLICATE_EMAIL"
    assert body["message"] == "El correo ya esta registrado"
    assert user.email not in resp.text, "mensaje neutro sin eco del email"
    assert "DUPLICADO" not in resp.text
    assert _count_users(email_session) == before, "sin escritura"
    assert user.email not in caplog.text, "el email no sale en logs"
    assert "DUPLICADO" not in caplog.text
    # Regresion MENOR: el log solo lleva el booleano, sin hash derivado.
    assert "has_match=True" in caplog.text
    assert "email_hash" not in caplog.text
    assert "hash=" not in caplog.text.lower()


# ---------------------------------------------------------------- CA-02: errores
def test_email_check_malformed_422(email_client: TestClient):
    for payload in (
        {"email": "no-es-email"},
        {"email": ""},
        {"email": "sin-arroba ni dominio"},
    ):
        resp = email_client.post("/api/v1/auth/kyc/email/check", json=payload)
        assert resp.status_code == 422, payload


def test_email_check_rate_limit_429_before_existence(
    email_client: TestClient, email_session: Session, monkeypatch
):
    from app.modules.identity.service import kyc_proxy

    monkeypatch.setenv("KYC_RATE_LIMIT_MAX_REQUESTS", "2")
    monkeypatch.setenv("KYC_RATE_LIMIT_WINDOW_SECONDS", "60")
    kyc_proxy.reset_rate_limits()

    user = _make_email_user(email_session, email="limite@example.com")
    payload = {"email": user.email}
    assert email_client.post("/api/v1/auth/kyc/email/check", json=payload).status_code == 409
    assert email_client.post("/api/v1/auth/kyc/email/check", json=payload).status_code == 409
    limited = email_client.post("/api/v1/auth/kyc/email/check", json=payload)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"

    # El limite se verifica ANTES de la existencia: con la ventana agotada
    # (misma IP), hasta un email inexistente responde el mismo 429 generico
    # (sin filtrar existencia).
    ghost = {"email": "fantasma-rl@example.com"}
    unknown = email_client.post("/api/v1/auth/kyc/email/check", json=ghost)
    assert unknown.status_code == 429
    assert unknown.json()["error"] == limited.json()["error"]


def test_openapi_exposes_email_check_route(email_client: TestClient):
    spec = email_client.get("/openapi.json").json()
    assert "/api/v1/auth/kyc/email/check" in spec["paths"]
    post = spec["paths"]["/api/v1/auth/kyc/email/check"]["post"]
    assert post["responses"]
