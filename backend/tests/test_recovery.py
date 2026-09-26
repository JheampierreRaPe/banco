"""Recuperacion de acceso por email: solicitud de OTP (E1-T33, HU04; E1-T41 retira `verify`).

- Sin Postgres: `TestClient(app)` + override de `get_db` a SQLite en memoria
  con schemas ATTACH (`identity`/`shared`/`notifications`/`audit`), patron de
  `tests/test_pin_login.py` (TestClient) + `tests/test_activation.py` (ATTACH).
- Casos: `request` con email registrado -> 200 + OTP `RECOVERY` `PENDING`
  notificado SOLO por email; con email no registrado -> 200 con el MISMO
  cuerpo y sin OTP (CA-01, sin enumeracion); cooldown (reutiliza el
  `PENDING` sin duplicar) + rate-limit por `email+IP` (-> 429
  `RATE_LIMITED`), canal siempre email (nunca SMS); logs sin
  email/OTP/hash. El OTP emitido lo consume `POST /auth/pin-reset`
  (ver `tests/test_pin_reset.py`).
- E1-T41: `POST /auth/recovery/verify` fue RETIRADO (decision del dueno):
  este archivo ya no contiene casos de `verify`; hay una prueba de
  regresion que documenta que la ruta ya no existe (404 y fuera de
  OpenAPI) y que `request` sigue respondiendo.
- Reglas estaticas (servicio sin `commit`, sin SMS, reutiliza `otp_service`/
  `activation`/repositorio, sin tocar otros modulos; sin `verify_recovery`
  ni schemas `RecoveryVerify*`).
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
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "service" / "recovery.py"
)
API_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "api" / "recovery.py"
)
REPO_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "repository"
    / "recovery.py"
)
SCHEMAS_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "schemas" / "recovery.py"
)

WRONG_CODE = "000000"


@pytest.fixture()
def recovery_session():
    """Sesion SQLite aislada (`identity`/`shared`/`notifications`/`audit` via ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.audit.models as _a  # noqa: F401 (registro)
    import app.modules.identity.models as _i  # noqa: F401 (registro)
    import app.modules.notifications.models as _n  # noqa: F401 (registro)
    import app.modules.shared.models as _s  # noqa: F401 (registro)

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("identity", "shared", "notifications", "audit"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["identity.users"],
            Base.metadata.tables["identity.credentials"],
            Base.metadata.tables["identity.otp_codes"],
            Base.metadata.tables["identity.sessions"],
            Base.metadata.tables["identity.device_bindings"],
            Base.metadata.tables["identity.access_recovery"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["notifications.notifications"],
            Base.metadata.tables["notifications.notification_templates"],
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
def recovery_client(recovery_session: Session, monkeypatch):
    """TestClient con `get_db` a SQLite y buckets de rate-limit limpios."""
    from app.modules.identity.service import recovery as recovery_service

    monkeypatch.delenv("RECOVERY_REQUEST_RATE_LIMIT_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("RECOVERY_REQUEST_RATE_LIMIT_MAX_REQUESTS", raising=False)
    monkeypatch.delenv("RECOVERY_VERIFY_RATE_LIMIT_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("RECOVERY_VERIFY_RATE_LIMIT_MAX_REQUESTS", raising=False)
    recovery_service.reset_recovery_rate_limits()

    def _override():
        yield recovery_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        recovery_service.reset_recovery_rate_limits()


def _make_active_user(session: Session, *, status: str = "ACTIVE"):
    """Usuario elegible (`ACTIVE` + email) para recuperacion."""
    from app.modules.identity import repository as identity_repo

    suffix = uuid.uuid4().hex[:8]
    user = identity_repo.create_user(
        session,
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Ada",
        last_name="Lovelace",
        email=f"ada.{suffix}@example.com",
        phone="+51999888777",
        status=status,
    )
    session.commit()
    return user


def _otp_rows(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.identity.models import OtpCode

    session.expire_all()
    stmt = (
        sa.select(OtpCode)
        .where(OtpCode.user_id == user_id, OtpCode.purpose == "RECOVERY")
        .order_by(OtpCode.created_at.asc())
    )
    return list(session.scalars(stmt).all())


def _notifications_for(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.notifications.models import Notification

    session.expire_all()
    stmt = (
        sa.select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.asc())
    )
    return list(session.scalars(stmt).all())


def _recovery_code(session: Session, user_id: uuid.UUID) -> str:
    """Codigo en claro tal como viajo por email (payload de la notificacion)."""
    rows = _notifications_for(session, user_id)
    assert rows, "se esperaba la notificacion con el OTP por email"
    data = (rows[-1].payload_json or {}).get("data", {})
    assert "code" in data, "el codigo viaja en el payload de la notificacion"
    return str(data["code"])


def _audit_rows(session: Session, action: str) -> list:
    from app.modules.audit.models import AuditLog

    session.expire_all()
    stmt = sa.select(AuditLog).where(AuditLog.action == action)
    return list(session.scalars(stmt).all())


# ---------------------------------------------------------------- Reglas estaticas
def test_static_rules_recovery_service_no_commit_no_sms_lazy_facades():
    service = SERVICE_PATH.read_text(encoding="utf-8")
    assert ".commit(" not in service, "el servicio hace flush; el endpoint confirma"
    assert "float(" not in service, "sin float"
    assert "'sms'" not in service and '"sms"' not in service, "recuperacion nunca por SMS"
    assert "phone=None" in service, "routing forzado a email (sin fallback a SMS)"
    assert 'channel="email"' in service or "channel=EMAIL_CHANNEL" in service
    assert "otp_code_email" in service, "plantilla de email para el codigo"
    assert "generate_otp" in service, "emite el OTP via otp_service"
    assert "resolve_activation_delivery" in service, "reutiliza el routing de activation"
    assert "verify_recovery" not in service, "E1-T41: verify retirado del servicio"
    assert "RecoveryInvalidError" not in service, "E1-T41: sin error tipado de verify"
    assert "INVALID_RECOVERY_CODE" not in service, "E1-T41: el codigo lo emitia verify"
    assert "record_access_recovery" not in service, "E1-T41: access_recovery lo registra pin-reset"
    assert "register_binding" not in service, "E1-T41: bindings solo en pin_login"
    assert "touch_binding" not in service, "E1-T41: bindings solo en pin_login"
    assert "validate_otp" not in service, "E1-T41: el consumo del OTP lo hace pin-reset"
    assert "create_access_token(" not in service, "sin JWT en recovery"
    assert "from app.core.security import" not in service, "sin import de JWT en recovery"
    assert "jwt.encode(" not in service, "no inventa tokens"
    assert "create_session(" not in service, "sin sessions en recovery"
    assert "hash_refresh_token(" not in service, "sin refresh en recovery"
    assert "token_urlsafe" not in service, "sin refresh opaco en recovery"
    assert "LOGIN_SUCCEEDED_EVENT" not in service, "sin enlistar login"
    assert "outbox_record(" not in service, "sin outbox"
    assert "    from app.core.outbox import" not in service, "sin outbox"
    assert "    from app.modules.notifications.service import" in service
    assert "    from app.modules.audit.service import" in service
    assert "auth.recovery_requested" in service
    assert "auth.access_recovered" not in service, "E1-T41: esa auditoria era de verify"
    top_imports = "\n".join(
        line for line in service.splitlines() if line.startswith(("from app.", "import app."))
    )
    for forbidden in ("kyc_proxy", "kyc_onboarding", "onboard_customer", "pin_login"):
        assert forbidden not in top_imports, f"prohibido tocar {forbidden}"
    log_lines = [line for line in service.splitlines() if "logger." in line]
    assert log_lines, "el servicio debe loguear sin PII"
    for line in log_lines:
        lowered = line.lower()
        for forbidden in ("plain", "recipient", "destination", "email", "code_hash", "public_key"):
            assert forbidden not in lowered, f"posible PII/OTP en log: {line.strip()}"

    repo = REPO_PATH.read_text(encoding="utf-8")
    assert ".commit(" not in repo, "el repositorio hace flush; el endpoint confirma"

    api = API_PATH.read_text(encoding="utf-8")
    assert "verify_recovery" not in api, "E1-T41: verify retirado del router"
    assert "RecoveryVerify" not in api, "E1-T41: sin schemas de verify en el router"
    assert "INVALID_RECOVERY_CODE" not in api, "E1-T41: ese error lo emitia verify"
    assert "RATE_LIMITED" in api, "request conserva su rate-limit"
    assert ".commit(" in api, "el endpoint confirma el exito"
    assert ".rollback(" in api, "revierte rate-limit y fallos inesperados"

    schemas = SCHEMAS_PATH.read_text(encoding="utf-8")
    assert "RecoveryVerify" not in schemas, "E1-T41: schemas de verify retirados"
    assert "RecoveryRequest" in schemas, "request se conserva"
    assert "EMAIL_PATTERN" in schemas, "el patron de email se conserva"


# ---------------------------------------------------------------- CA-01: request
def test_request_registered_user_200_with_pending_email_otp(
    recovery_client: TestClient, recovery_session: Session
):
    user = _make_active_user(recovery_session)

    resp = recovery_client.post("/api/v1/auth/recovery/request", json={"email": user.email})
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body == {"accepted": True, "ttl_seconds": 600, "resend_wait_seconds": 30}

    rows = _otp_rows(recovery_session, user.id)
    assert len(rows) == 1
    assert rows[0].status == "PENDING" and rows[0].purpose == "RECOVERY"
    assert rows[0].destination == user.email
    assert WRONG_CODE not in resp.text  # el codigo no sale en la respuesta

    notes = _notifications_for(recovery_session, user.id)
    assert len(notes) == 1
    assert notes[0].channel == "email", "el OTP de recuperacion viaja solo por email"
    assert notes[0].template_code == "otp_code_email"

    audits = _audit_rows(recovery_session, "auth.recovery_requested")
    assert len(audits) == 1


def test_request_unknown_email_same_body_without_otp(
    recovery_client: TestClient, recovery_session: Session
):
    known = _make_active_user(recovery_session)
    known_body = recovery_client.post(
        "/api/v1/auth/recovery/request", json={"email": known.email}
    ).json()["data"]

    resp = recovery_client.post(
        "/api/v1/auth/recovery/request", json={"email": "nadie@example.com"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == known_body, "cuerpo identico: sin enumeracion"

    from app.modules.identity.models import OtpCode

    recovery_session.expire_all()
    total = recovery_session.scalars(sa.select(OtpCode)).all()
    assert len(total) == 1, "el email inexistente no genera OTP"
    assert len(_otp_rows(recovery_session, known.id)) == 1, "solo el usuario real tiene OTP"


def test_request_malformed_email_422(recovery_client: TestClient):
    for payload in ({"email": ""}, {"email": "no-es-email"}, {"email": "a@b"}):
        resp = recovery_client.post("/api/v1/auth/recovery/request", json=payload)
        assert resp.status_code == 422, payload


# ---------------------------------------------------------------- E1-T41: retiro de verify
def test_verify_endpoint_retired_404_and_request_still_responds(
    recovery_client: TestClient, recovery_session: Session
):
    """Regresion del retiro (E1-T41): `POST /auth/recovery/verify` ya no
    existe (404) y no aparece en OpenAPI; `POST /auth/recovery/request`
    sigue respondiendo 200."""
    user = _make_active_user(recovery_session)

    resp = recovery_client.post(
        "/api/v1/auth/recovery/verify", json={"email": user.email, "code": "123456"}
    )
    assert resp.status_code == 404, "verify fue retirado: la ruta ya no existe"

    spec = recovery_client.get("/openapi.json").json()
    assert "/api/v1/auth/recovery/request" in spec["paths"]
    assert "/api/v1/auth/recovery/verify" not in spec["paths"]

    ok = recovery_client.post("/api/v1/auth/recovery/request", json={"email": user.email})
    assert ok.status_code == 200
    assert ok.json()["data"] == {
        "accepted": True,
        "ttl_seconds": 600,
        "resend_wait_seconds": 30,
    }


# ---------------------------------------------------------------- CA-05: cooldown + rate-limit + canal
def test_request_cooldown_reuses_pending_without_duplicating(
    recovery_client: TestClient, recovery_session: Session
):
    user = _make_active_user(recovery_session)
    first = recovery_client.post("/api/v1/auth/recovery/request", json={"email": user.email})
    second = recovery_client.post("/api/v1/auth/recovery/request", json={"email": user.email})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["data"] == second.json()["data"]
    assert len(_otp_rows(recovery_session, user.id)) == 1, "no duplica en cooldown"
    assert len(_notifications_for(recovery_session, user.id)) == 1, "no re-notifica"


def test_request_rate_limit_429_without_enumeration(
    recovery_client: TestClient, recovery_session: Session, monkeypatch
):
    from app.modules.identity.service import recovery as recovery_service

    monkeypatch.setenv("RECOVERY_REQUEST_RATE_LIMIT_MAX_REQUESTS", "2")
    monkeypatch.setenv("RECOVERY_REQUEST_RATE_LIMIT_WINDOW_SECONDS", "60")
    recovery_service.reset_recovery_rate_limits()

    user = _make_active_user(recovery_session)
    assert (
        recovery_client.post(
            "/api/v1/auth/recovery/request", json={"email": user.email}
        ).status_code
        == 200
    )
    assert (
        recovery_client.post(
            "/api/v1/auth/recovery/request", json={"email": user.email}
        ).status_code
        == 200
    )
    limited = recovery_client.post("/api/v1/auth/recovery/request", json={"email": user.email})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"

    # El limite se verifica ANTES de la existencia: un email inexistente con
    # su ventana agotada responde el mismo 429 (sin filtrar existencia).
    ghost = "otro@example.com"
    assert (
        recovery_client.post("/api/v1/auth/recovery/request", json={"email": ghost}).status_code
        == 200
    )
    assert (
        recovery_client.post("/api/v1/auth/recovery/request", json={"email": ghost}).status_code
        == 200
    )
    unknown = recovery_client.post("/api/v1/auth/recovery/request", json={"email": ghost})
    assert unknown.status_code == 429
    assert unknown.json()["error"] == limited.json()["error"]


def test_request_without_routable_email_same_body_no_otp_no_sms(
    recovery_client: TestClient, recovery_session: Session, monkeypatch
):
    from app.modules.identity.service import activation as activation_service

    user = _make_active_user(recovery_session)
    monkeypatch.setattr(activation_service, "resolve_activation_delivery", lambda **kwargs: None)
    resp = recovery_client.post("/api/v1/auth/recovery/request", json={"email": user.email})
    assert resp.status_code == 200
    assert resp.json()["data"] == {"accepted": True, "ttl_seconds": 600, "resend_wait_seconds": 30}
    assert _otp_rows(recovery_session, user.id) == [], "sin destino no hay OTP"
    assert _notifications_for(recovery_session, user.id) == [], "nunca cae a SMS"


# ---------------------------------------------------------------- Sin PII en logs
def test_no_pii_or_otp_in_logs(recovery_client: TestClient, recovery_session: Session, caplog):
    user = _make_active_user(recovery_session)
    with caplog.at_level(logging.INFO, logger="app.modules.identity.service.recovery"):
        recovery_client.post("/api/v1/auth/recovery/request", json={"email": user.email})
        code = _recovery_code(recovery_session, user.id)
    assert user.email not in caplog.text, "el email no sale en logs"
    assert code not in caplog.text, "el OTP no sale en logs"
    for row in _otp_rows(recovery_session, user.id):
        assert code not in row.code_hash, "solo el hash se persiste"


# ---------------------------------------------------------------- OpenAPI
def test_openapi_exposes_only_request_route(recovery_client: TestClient):
    spec = recovery_client.get("/openapi.json").json()
    assert "/api/v1/auth/recovery/request" in spec["paths"]
    assert "/api/v1/auth/recovery/verify" not in spec["paths"], "E1-T41: verify retirado"
