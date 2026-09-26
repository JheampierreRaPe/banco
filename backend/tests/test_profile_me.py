"""Perfil autenticado minimo `GET /me` para saludo/avatar (E1-T44, HU01/HU02).

- Sin Postgres: `TestClient(app)` + override de `get_db` a SQLite en memoria
  con schema ATTACH (`identity`), patron de `tests/test_biometric_consent.py`.
- Casos: `GET /me` con Bearer valido devuelve **solo**
  `first_name`/`last_name`/`business_name` reales de la BD del `user_id` del
  token (se compara contra el fixture, no contra constantes); RUC juridica
  trae nombres `""` + razon social; el resto trae `business_name` null; sin
  cabecera/`Bearer` invalido/`sub` no UUID -> 401 `NOT_AUTHENTICATED`;
  `sub` valido sin usuario -> 404 `NOT_FOUND` neutro.
- Seguridad: el JSON no contiene `email`/`phone`/`doc_number_hash`/
  `doc_number_masked`; `caplog` sin PII (nombres, email, documento, token).
- Reglas estaticas: servicio de solo lectura (sin `flush`/`commit`), sin
  tocar `accounts`, Bearer via `core.security`, sin migracion.
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
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "service" / "profile.py"
)
API_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "api" / "profile.py"
)
SCHEMAS_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "schemas" / "profile.py"
)


@pytest.fixture()
def profile_session():
    """Sesion SQLite aislada (schema `identity` via ATTACH)."""
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
def profile_client(profile_session: Session):
    """TestClient con `get_db` a SQLite."""

    def _override():
        yield profile_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _make_user(
    profile_session: Session,
    *,
    doc_type: str = "DNI",
    first_name: str = "Ana",
    last_name: str = "Lopez",
    business_name: str | None = None,
):
    """Usuario `ACTIVE` con email/telefono/documento realisticos."""
    from app.modules.identity import repository as identity_repo

    suffix = uuid.uuid4().hex[:8]
    user = identity_repo.create_user(
        profile_session,
        doc_type=doc_type,
        doc_number_hash="hash-" + uuid.uuid4().hex,
        doc_number_masked="****5678",
        first_name=first_name,
        last_name=last_name,
        business_name=business_name,
        email=f"titular.{suffix}@example.com",
        phone="+51999888777",
        status="ACTIVE",
    )
    profile_session.commit()
    return user


def _bearer(user_id: uuid.UUID) -> dict:
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(subject=str(user_id))}"}


def _db_values(profile_session: Session, user_id: uuid.UUID) -> dict:
    from app.modules.identity import repository as identity_repo

    profile_session.expire_all()
    user = identity_repo.get_user(profile_session, user_id)
    assert user is not None
    return {
        "first_name": user.first_name,
        "last_name": user.last_name,
        "business_name": user.business_name,
    }


# ---------------------------------------------------------------- Reglas estaticas
def test_static_rules_profile_readonly_no_forbidden():
    service = SERVICE_PATH.read_text(encoding="utf-8")
    assert "get_profile" in service, "caso de uso tipado"
    assert "to_profile" in service, "proyeccion pura testeable"
    assert "ProfileUserNotFoundError" in service, "excepcion tipada -> 404"
    assert ".commit(" not in service, "solo lectura: el servicio no confirma"
    assert ".flush(" not in service, "solo lectura: el servicio no persiste"
    assert "float(" not in service, "sin float"
    top_imports = "\n".join(
        line for line in service.splitlines() if line.startswith(("from app.", "import app."))
    )
    assert "accounts" not in top_imports, "sin tocar otros modulos"

    api = API_PATH.read_text(encoding="utf-8")
    assert '"/me"' in api or "'/me'" in api, "ruta canonica del catalogo"
    assert "decode_token" in api, "Bearer via core.security"
    assert "NOT_AUTHENTICATED" in api and "NOT_FOUND" in api
    assert "ProfileResponse" in api, "contrato 05#4 via response_model"
    assert "from app.modules.accounts" not in api, "sin importar accounts"

    schemas = SCHEMAS_PATH.read_text(encoding="utf-8")
    assert "ProfileData" in schemas
    assert "ProfileResponse" in schemas
    assert "first_name" in schemas and "last_name" in schemas and "business_name" in schemas
    for forbidden in ("email", "phone", "doc_number_hash", "doc_number_masked"):
        assert forbidden not in schemas, f"PII en el contrato: {forbidden}"


# ---------------------------------------------------------------- Camino feliz
def test_me_returns_real_names_from_db(profile_client: TestClient, profile_session: Session):
    user = _make_user(profile_session, first_name="Ana", last_name="Lopez")
    resp = profile_client.get("/api/v1/me", headers=_bearer(user.id))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"data", "meta"}
    assert set(body["data"]) == {"first_name", "last_name", "business_name"}
    assert body["data"] == _db_values(profile_session, user.id)
    assert body["data"]["business_name"] is None
    assert body["meta"]["request_id"]


def test_me_ruc_juridica_business_name(profile_client: TestClient, profile_session: Session):
    user = _make_user(
        profile_session,
        doc_type="RUC",
        first_name="",
        last_name="",
        business_name="ACME SAC",
    )
    resp = profile_client.get("/api/v1/me", headers=_bearer(user.id))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"] == _db_values(profile_session, user.id)
    assert body["data"] == {"first_name": "", "last_name": "", "business_name": "ACME SAC"}


def test_me_ruc_natural_business_name_null(profile_client: TestClient, profile_session: Session):
    user = _make_user(
        profile_session,
        doc_type="RUC",
        first_name="Juan",
        last_name="Perez",
        business_name=None,
    )
    resp = profile_client.get("/api/v1/me", headers=_bearer(user.id))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == _db_values(profile_session, user.id)
    assert resp.json()["data"]["business_name"] is None


def test_to_profile_pure_and_not_found_typed(profile_session: Session):
    from app.modules.identity.service import profile as profile_service

    user = _make_user(profile_session, first_name="Ana", last_name="Lopez")
    data = profile_service.to_profile(user)
    assert (data.first_name, data.last_name, data.business_name) == ("Ana", "Lopez", None)

    with pytest.raises(profile_service.ProfileUserNotFoundError):
        profile_service.get_profile(profile_session, user_id=uuid.uuid4())


# ---------------------------------------------------------------- Auth y 404
def test_me_requires_bearer(profile_client: TestClient, profile_session: Session):
    _make_user(profile_session)

    missing = profile_client.get("/api/v1/me")
    assert missing.status_code == 401, missing.text
    assert missing.json()["error"]["code"] == "NOT_AUTHENTICATED"

    garbage = profile_client.get("/api/v1/me", headers={"Authorization": "Bearer basura"})
    assert garbage.status_code == 401, garbage.text
    assert garbage.json()["error"]["code"] == "NOT_AUTHENTICATED"

    no_scheme = profile_client.get("/api/v1/me", headers={"Authorization": "Token abc"})
    assert no_scheme.status_code == 401, no_scheme.text
    assert no_scheme.json()["error"]["code"] == "NOT_AUTHENTICATED"

    from app.core.security import create_access_token

    bad_sub = create_access_token(subject="not-a-uuid")
    bad = profile_client.get("/api/v1/me", headers={"Authorization": f"Bearer {bad_sub}"})
    assert bad.status_code == 401, bad.text
    assert bad.json()["error"]["code"] == "NOT_AUTHENTICATED"


def test_me_unknown_user_404_neutral(profile_client: TestClient, profile_session: Session):
    ghost = uuid.uuid4()
    resp = profile_client.get("/api/v1/me", headers=_bearer(ghost))
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert str(ghost) not in resp.text, "el 404 neutro no filtra datos"


# ---------------------------------------------------------------- Seguridad: sin PII
def test_me_response_without_pii(profile_client: TestClient, profile_session: Session):
    user = _make_user(profile_session, first_name="Ana", last_name="Lopez")
    resp = profile_client.get("/api/v1/me", headers=_bearer(user.id))
    assert resp.status_code == 200, resp.text
    for forbidden in ("email", "phone", "doc_number_hash", "doc_number_masked", "status"):
        assert forbidden not in resp.json()["data"], f"PII en la respuesta: {forbidden}"
    blob = resp.text
    assert "@example.com" not in blob, "el correo no sale en el cuerpo"
    assert "+51999888777" not in blob, "el telefono no sale en el cuerpo"


def test_profile_logs_without_pii(profile_client: TestClient, profile_session: Session, caplog):
    user = _make_user(profile_session, first_name="Anacleta", last_name="Quispehuaman")
    token = _bearer(user.id)["Authorization"].removeprefix("Bearer ").strip()
    with caplog.at_level(logging.INFO, logger="app.modules.identity.service.profile"):
        profile_client.get("/api/v1/me", headers=_bearer(user.id))
        profile_client.get("/api/v1/me")
    blob = "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records)
    for forbidden in (
        "Anacleta",
        "Quispehuaman",
        "@example.com",
        "hash-",
        "****5678",
        "+51999888777",
        token,
    ):
        assert forbidden not in blob, f"PII/secreto en logs: {forbidden}"
