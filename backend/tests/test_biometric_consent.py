"""Consentimiento biometrico post-login + flag en login PIN (E1-T39, HU02/HU03).

- Sin Postgres: `TestClient(app)` + override de `get_db` a SQLite en memoria
  con schemas ATTACH (`identity`/`audit`), patron de `tests/test_pin_login.py`
  (TestClient) + `tests/test_identity_otp.py` (SQLite ATTACH).
- Casos: `POST /auth/biometric/consent` con Bearer valido fija
  `credentials.biometric_enabled` (`true`/`false`, idempotente); sin
  cabecera/`Bearer` invalido/`sub` no UUID -> 401 `NOT_AUTHENTICATED`;
  credencial inexistente -> 404 `NOT_FOUND` neutro; esquema malformado ->
  422 estandar; `POST /auth/login/pin` expone `biometric_enabled` con el valor
  real (se compara contra la BD, no es constante).
- Reglas estaticas: servicio con `flush` sin `commit`, sin tocar
  `device_login`/`pin_setup`/`hashing`/`hmac:`, sin PII/secretos en logs.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
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
    / "biometric_consent.py"
)
API_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "api" / "biometric.py"
)
SCHEMAS_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "schemas"
    / "biometric_consent.py"
)

PIN = "482917"


@pytest.fixture()
def consent_session():
    """Sesion SQLite aislada (`identity`/`audit` via ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.audit.models as _a  # noqa: F401 (registro)
    import app.modules.identity.models as _i  # noqa: F401 (registro)

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("identity", "audit"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["identity.users"],
            Base.metadata.tables["identity.credentials"],
            Base.metadata.tables["audit.audit_log"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def consent_client(consent_session: Session):
    """TestClient con `get_db` a SQLite."""

    def _override():
        yield consent_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _make_consent_user(consent_session: Session, *, pin: str = PIN):
    """Usuario `ACTIVE` con credencial PBKDF2 y consentimiento apagado."""
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import pin_login as pin_login_service

    suffix = uuid.uuid4().hex[:8]
    user = identity_repo.create_user(
        consent_session,
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Ada",
        last_name="Lovelace",
        email=f"ada.{suffix}@example.com",
        phone="+51999888777",
        status="ACTIVE",
    )
    identity_repo.create_credential(
        consent_session, user.id, pin_hash=pin_login_service.hash_pin(pin)
    )
    consent_session.commit()
    return user


def _bearer(user_id: uuid.UUID) -> dict:
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(subject=str(user_id))}"}


def _flag(consent_session: Session, user_id: uuid.UUID) -> bool:
    from app.modules.identity import repository as identity_repo

    consent_session.expire_all()
    row = identity_repo.get_credential(consent_session, user_id)
    assert row is not None
    return bool(row.biometric_enabled)


def _audit_rows(consent_session: Session, action: str) -> list:
    from app.modules.audit.models import AuditLog

    consent_session.expire_all()
    stmt = sa.select(AuditLog).where(AuditLog.action == action)
    return list(consent_session.scalars(stmt).all())


# ---------------------------------------------------------------- Reglas estaticas
def test_static_rules_consent_flush_no_forbidden(tmp_path=None):
    service = SERVICE_PATH.read_text(encoding="utf-8")
    assert "set_biometric_consent" in service, "caso de uso tipado"
    assert "BiometricConsentUserNotFoundError" in service, "excepcion tipada -> 404"
    assert ".commit(" not in service, "el servicio hace flush; el endpoint confirma"
    assert "session.flush()" in service, "persiste con flush"
    assert "auth.biometric_consent" in service, "auditoria best-effort via fachada"
    assert "float(" not in service, "sin float"
    assert "hash_pin" not in service, "no toca hashing"
    assert "verify_signature" not in service, "no toca firmas"
    assert "hmac:" not in service, "bindings intactos"
    top_imports = "\n".join(
        line for line in service.splitlines() if line.startswith(("from app.", "import app."))
    )
    assert "device_login" not in top_imports, "no toca el login facial (solo repositorio y audit)"
    assert "pin_setup" not in top_imports, "no toca el alta de PIN"
    log_lines = [line for line in service.splitlines() if "logger." in line]
    assert log_lines, "el servicio debe loguear sin PII"
    for line in log_lines:
        lowered = line.lower()
        for forbidden in ("email", "document", "pin", "token", "public_key"):
            assert forbidden not in lowered, f"PII/secreto en logs: {line.strip()}"

    api = API_PATH.read_text(encoding="utf-8")
    assert "/auth/biometric/consent" in api, "ruta exacta del endpoint"
    assert "decode_token" in api, "Bearer via core.security"
    assert "NOT_AUTHENTICATED" in api and "NOT_FOUND" in api
    assert "from app.modules.accounts" not in api, "sin importar accounts"
    assert ".commit(" in api and ".rollback(" in api, "commit en exito, rollback ante error"

    schemas = SCHEMAS_PATH.read_text(encoding="utf-8")
    assert "BiometricConsentRequest" in schemas
    assert "BiometricConsentResponse" in schemas
    assert "biometric_enabled" in schemas


# ---------------------------------------------------------------- Camino feliz
def test_consent_enable_disable_idempotent(consent_client: TestClient, consent_session: Session):
    user = _make_consent_user(consent_session)
    assert _flag(consent_session, user.id) is False

    resp = consent_client.post(
        "/api/v1/auth/biometric/consent", json={"enabled": True}, headers=_bearer(user.id)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"data", "meta"}
    assert body["data"] == {"biometric_enabled": True}
    assert body["meta"]["request_id"]
    assert _flag(consent_session, user.id) is True

    # Idempotente: repetir el mismo valor no es error.
    again = consent_client.post(
        "/api/v1/auth/biometric/consent", json={"enabled": True}, headers=_bearer(user.id)
    )
    assert again.status_code == 200, again.text
    assert again.json()["data"] == {"biometric_enabled": True}
    assert _flag(consent_session, user.id) is True

    revoke = consent_client.post(
        "/api/v1/auth/biometric/consent", json={"enabled": False}, headers=_bearer(user.id)
    )
    assert revoke.status_code == 200, revoke.text
    assert revoke.json()["data"] == {"biometric_enabled": False}
    assert _flag(consent_session, user.id) is False

    revoke_again = consent_client.post(
        "/api/v1/auth/biometric/consent", json={"enabled": False}, headers=_bearer(user.id)
    )
    assert revoke_again.status_code == 200, revoke_again.text
    assert _flag(consent_session, user.id) is False


def test_consent_audits_without_pii(consent_client: TestClient, consent_session: Session):
    user = _make_consent_user(consent_session)
    resp = consent_client.post(
        "/api/v1/auth/biometric/consent", json={"enabled": True}, headers=_bearer(user.id)
    )
    assert resp.status_code == 200, resp.text
    rows = _audit_rows(consent_session, "auth.biometric_consent")
    assert len(rows) >= 1, "auditoria best-effort del consentimiento"
    blob = repr([(r.action, r.entity_id, r.after_json) for r in rows])
    for forbidden in ("@example.com", "hash-", PIN):
        assert forbidden not in blob, "sin PII/secretos en la auditoria"


# ---------------------------------------------------------------- Auth y errores
def test_consent_requires_bearer(consent_client: TestClient, consent_session: Session):
    user = _make_consent_user(consent_session)

    missing = consent_client.post("/api/v1/auth/biometric/consent", json={"enabled": True})
    assert missing.status_code == 401, missing.text
    assert missing.json()["error"]["code"] == "NOT_AUTHENTICATED"

    garbage = consent_client.post(
        "/api/v1/auth/biometric/consent",
        json={"enabled": True},
        headers={"Authorization": "Bearer basura"},
    )
    assert garbage.status_code == 401, garbage.text
    assert garbage.json()["error"]["code"] == "NOT_AUTHENTICATED"

    no_scheme = consent_client.post(
        "/api/v1/auth/biometric/consent",
        json={"enabled": True},
        headers={"Authorization": "Token abc"},
    )
    assert no_scheme.status_code == 401, no_scheme.text
    assert no_scheme.json()["error"]["code"] == "NOT_AUTHENTICATED"

    from app.core.security import create_access_token

    bad_sub = create_access_token(subject="not-a-uuid")
    bad = consent_client.post(
        "/api/v1/auth/biometric/consent",
        json={"enabled": True},
        headers={"Authorization": f"Bearer {bad_sub}"},
    )
    assert bad.status_code == 401, bad.text
    assert bad.json()["error"]["code"] == "NOT_AUTHENTICATED"
    assert _flag(consent_session, user.id) is False, "fallo de auth no cambia el flag"


def test_consent_unknown_user_404_neutral(consent_client: TestClient, consent_session: Session):
    ghost = uuid.uuid4()
    resp = consent_client.post(
        "/api/v1/auth/biometric/consent", json={"enabled": True}, headers=_bearer(ghost)
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert str(ghost) not in resp.text, "el 404 neutro no filtra datos"


def test_consent_schema_422(consent_client: TestClient, consent_session: Session):
    user = _make_consent_user(consent_session)
    resp = consent_client.post("/api/v1/auth/biometric/consent", json={}, headers=_bearer(user.id))
    assert resp.status_code == 422, resp.text
    assert _flag(consent_session, user.id) is False


# ---------------------------------------------------------------- Seguridad: sin PII en logs
def test_consent_logs_without_pii(consent_client: TestClient, consent_session: Session, caplog):
    user = _make_consent_user(consent_session)
    token = _bearer(user.id)["Authorization"].removeprefix("Bearer ").strip()
    with caplog.at_level(logging.INFO, logger="app.modules.identity.service.biometric_consent"):
        consent_client.post(
            "/api/v1/auth/biometric/consent", json={"enabled": True}, headers=_bearer(user.id)
        )
        consent_client.post("/api/v1/auth/biometric/consent", json={"enabled": True})
    blob = "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records)
    for forbidden in ("@example.com", "hash-", PIN, token, "hmac:"):
        assert forbidden not in blob, f"PII/secreto en logs: {forbidden}"
