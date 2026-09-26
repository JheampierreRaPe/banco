"""Login en dispositivo nuevo, paso 1: email + DNI/RUC -> OTP `LOGIN` (E1-T45, HU03/HU04).

- Sin Postgres: `TestClient(app)` + override de `get_db` a SQLite en memoria
  con schemas ATTACH (`identity`/`shared`/`notifications`/`audit`/`config`),
  patron de `tests/test_recovery.py` (TestClient) + `tests/test_pin_reset.py`
  (ATTACH + `hash_document_number`).
- Casos: feliz (email + DNI de la MISMA cuenta `ACTIVE` -> 200 + OTP `LOGIN`
  `PENDING` notificado SOLO por email); rama ciega (email inexistente,
  usuario no `ACTIVE`, documento de OTRA cuenta -> MISMO 200 sin OTP);
  cooldown (reutiliza el `PENDING` sin duplicar ni re-notificar);
  rate-limit por `email+IP` con la ventana reutilizada de verify (-> 429
  `RATE_LIMITED`, antes de la existencia); esquema (422 estandar);
  servicio con documento vacio -> rama ciega 200 (el esquema HTTP filtra
  vacios con 422 antes de llegar al servicio); logs sin email/documento/OTP.
- Reglas estaticas (servicio sin `commit`, sin SMS/sesion/tokens, reutiliza
  `generate_otp`/`resolve_activation_delivery`/`hash_document_number` con
  `compare_digest`, ventana `recovery_verify_*` sin claves nuevas,
  auditoria `auth.device_login_requested` sin PII).
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
    / "device_login_request.py"
)
API_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "api"
    / "device_login_request.py"
)
SCHEMAS_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "schemas"
    / "device_login_request.py"
)

DOC_NUMBER = "12345678"
OTHER_DOC = "87654321"
RUC_NUMBER = "20123456789"


@pytest.fixture()
def device_session():
    """Sesion SQLite aislada (`identity`/`shared`/`notifications`/`audit`/`config`)."""
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
        for schema in ("identity", "shared", "notifications", "audit", "config"):
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
def device_client(device_session: Session, monkeypatch):
    """TestClient con `get_db` a SQLite y buckets de rate-limit limpios."""
    from app.modules.identity.service import recovery as recovery_service

    monkeypatch.delenv("RECOVERY_REQUEST_RATE_LIMIT_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("RECOVERY_REQUEST_RATE_LIMIT_MAX_REQUESTS", raising=False)
    monkeypatch.delenv("RECOVERY_VERIFY_RATE_LIMIT_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("RECOVERY_VERIFY_RATE_LIMIT_MAX_REQUESTS", raising=False)
    recovery_service.reset_recovery_rate_limits()

    def _override():
        yield device_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        recovery_service.reset_recovery_rate_limits()


def _make_device_user(
    session: Session,
    *,
    status: str = "ACTIVE",
    doc_number: str = DOC_NUMBER,
    email: str | None = None,
):
    """Usuario con hash de documento real para el paso 1."""
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service.kyc_onboarding import hash_document_number

    suffix = uuid.uuid4().hex[:8]
    user = identity_repo.create_user(
        session,
        doc_type="DNI",
        doc_number_hash=hash_document_number(doc_number),
        first_name="Ada",
        last_name="Lovelace",
        email=email or f"ada.{suffix}@example.com",
        phone="+51999888777",
        status=status,
    )
    session.commit()
    return user


def _login_otp_rows(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.identity.models import OtpCode

    session.expire_all()
    stmt = (
        sa.select(OtpCode)
        .where(OtpCode.user_id == user_id, OtpCode.purpose == "LOGIN")
        .order_by(OtpCode.created_at.asc())
    )
    return list(session.scalars(stmt).all())


def _all_otp_rows(session: Session) -> list:
    from app.modules.identity.models import OtpCode

    session.expire_all()
    return list(session.scalars(sa.select(OtpCode)).all())


def _notifications_for(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.notifications.models import Notification

    session.expire_all()
    stmt = (
        sa.select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.asc())
    )
    return list(session.scalars(stmt).all())


def _login_code(session: Session, user_id: uuid.UUID) -> str:
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
def test_static_rules_device_login_request_reuses_verify_window_no_new_params():
    service = SERVICE_PATH.read_text(encoding="utf-8")
    assert ".commit(" not in service, "el servicio hace flush; el endpoint confirma"
    assert "float(" not in service, "sin float"
    assert "generate_otp" in service, "emite el OTP via otp_service"
    assert "resolve_activation_delivery" in service, "reutiliza el routing de activation"
    assert "hash_document_number" in service, "misma cuenta via hash HMAC server-side"
    assert "compare_digest" in service, "comparacion del hash en tiempo constante"
    assert "recovery_verify_rate_limit_cfg" in service
    assert "_build_recovery_rate_key" in service
    assert "_check_rate_limit" in service
    assert '"device_login"' in service or "'device_login'" in service
    assert "auth.recovery_verify" in service or "recovery_verify" in service
    assert "auth.device_login_request" not in service.replace(
        "auth.device_login_requested", ""
    ), "sin claves nuevas de rate-limit en config.parameters"
    assert "otp_code_email" in service, "plantilla de email para el codigo"
    assert (
        'purpose="LOGIN"' in service or "purpose='LOGIN'" in service or "LOGIN_PURPOSE" in service
    )
    assert '"LOGIN"' in service or "'LOGIN'" in service
    assert "auth.device_login_requested" in service
    assert "'sms'" not in service and '"sms"' not in service, "solo email, nunca SMS"
    assert "phone=None" in service, "routing forzado a email (sin fallback a SMS)"
    assert "validate_otp" not in service, "el paso 1 solo emite (el paso 2 valida)"
    assert "create_session(" not in service, "el paso 1 no crea sessions"
    assert "create_access_token(" not in service, "el paso 1 no emite JWT"
    assert "register_binding" not in service, "el binding lo crea el paso 2"
    assert "record_access_recovery" not in service, "sin access_recovery en el paso 1"
    assert "outbox_record(" not in service, "sin eventos de dominio (solo auditoria)"
    assert "    from app.modules.notifications.service import" in service
    assert "    from app.modules.audit.service import" in service
    top_imports = "\n".join(
        line for line in service.splitlines() if line.startswith(("from app.", "import app."))
    )
    for forbidden in ("kyc_proxy", "onboard_customer", "pin_login"):
        assert forbidden not in top_imports, f"posible acople indebido: {forbidden}"
    assert "from app.modules.identity.service.kyc_onboarding import" in service
    log_lines = [line for line in service.splitlines() if "logger." in line]
    assert log_lines, "el servicio debe loguear sin PII"
    for line in log_lines:
        lowered = line.lower()
        for forbidden in ("plain", "recipient", "destination", "email", "code_hash", "document"):
            assert forbidden not in lowered, f"posible PII/OTP en log: {line.strip()}"

    api = API_PATH.read_text(encoding="utf-8")
    assert "/auth/login/device/request" in api
    assert "RecoveryRateLimitedError" in api
    assert "RATE_LIMITED" in api
    assert ".commit(" in api, "el endpoint confirma el exito"
    assert ".rollback(" in api, "revierte rate-limit y fallos inesperados"

    schemas = SCHEMAS_PATH.read_text(encoding="utf-8")
    assert "DeviceLoginRequestRequest" in schemas
    assert "EMAIL_PATTERN" in schemas
    assert "DEVICE_LOGIN_DOC_TYPES" in schemas or "DOC_TYPES" in schemas


# ---------------------------------------------------------------- CA-01: feliz
def test_request_same_account_200_with_login_otp_by_email(
    device_client: TestClient, device_session: Session
):
    user = _make_device_user(device_session)

    resp = device_client.post(
        "/api/v1/auth/login/device/request",
        json={"email": user.email, "doc_type": "DNI", "document_number": DOC_NUMBER},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body == {"accepted": True, "ttl_seconds": 600, "resend_wait_seconds": 30}

    rows = _login_otp_rows(device_session, user.id)
    assert len(rows) == 1
    assert rows[0].status == "PENDING" and rows[0].purpose == "LOGIN"
    assert rows[0].destination == user.email
    assert "000000" not in resp.text  # el codigo no sale en la respuesta

    notes = _notifications_for(device_session, user.id)
    assert len(notes) == 1
    assert notes[0].channel == "email", "el OTP de login viaja solo por email"
    assert notes[0].template_code == "otp_code_email"

    audits = _audit_rows(device_session, "auth.device_login_requested")
    assert len(audits) == 1


# ---------------------------------------------------------------- CA-02: rama ciega
def test_blind_branches_same_body_without_otp(device_client: TestClient, device_session: Session):
    user = _make_device_user(device_session)
    known_body = device_client.post(
        "/api/v1/auth/login/device/request",
        json={"email": user.email, "doc_type": "DNI", "document_number": DOC_NUMBER},
    ).json()["data"]

    # Email inexistente.
    ghost = device_client.post(
        "/api/v1/auth/login/device/request",
        json={"email": "nadie@example.com", "doc_type": "DNI", "document_number": DOC_NUMBER},
    )
    assert ghost.status_code == 200
    assert ghost.json()["data"] == known_body, "cuerpo identico: sin enumeracion"

    # Documento de OTRA cuenta.
    other = device_client.post(
        "/api/v1/auth/login/device/request",
        json={"email": user.email, "doc_type": "DNI", "document_number": OTHER_DOC},
    )
    assert other.status_code == 200
    assert other.json()["data"] == known_body

    # Usuario no ACTIVE.
    blocked = _make_device_user(device_session, status="BLOCKED", doc_number="11223344")
    blocked_resp = device_client.post(
        "/api/v1/auth/login/device/request",
        json={"email": blocked.email, "doc_type": "DNI", "document_number": "11223344"},
    )
    assert blocked_resp.status_code == 200
    assert blocked_resp.json()["data"] == known_body

    total = _all_otp_rows(device_session)
    assert len(total) == 1, "la rama ciega no emite OTP (solo la cuenta real tiene uno)"
    assert len(_login_otp_rows(device_session, user.id)) == 1


def test_service_empty_document_is_blind(device_session: Session):
    """El servicio trata el documento vacio como rama ciega (200 identico).

    Nota: por HTTP el esquema filtra vacios con 422 antes de llegar al
    servicio; este caso cubre llamados directos al servicio.
    """
    from app.modules.identity.service import device_login_request as svc

    user = _make_device_user(device_session)
    body = svc.request_device_login(
        device_session, email=user.email, doc_type="DNI", document_number=""
    )
    assert body == {"accepted": True, "ttl_seconds": 600, "resend_wait_seconds": 30}
    assert _login_otp_rows(device_session, user.id) == []


# ---------------------------------------------------------------- CA-04: cooldown + CA-03: rate-limit
def test_request_cooldown_reuses_pending_without_duplicating(
    device_client: TestClient, device_session: Session
):
    user = _make_device_user(device_session)
    first = device_client.post(
        "/api/v1/auth/login/device/request",
        json={"email": user.email, "doc_type": "DNI", "document_number": DOC_NUMBER},
    )
    second = device_client.post(
        "/api/v1/auth/login/device/request",
        json={"email": user.email, "doc_type": "DNI", "document_number": DOC_NUMBER},
    )
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["data"] == second.json()["data"]
    assert len(_login_otp_rows(device_session, user.id)) == 1, "no duplica en cooldown"
    assert len(_notifications_for(device_session, user.id)) == 1, "no re-notifica"


def test_request_rate_limit_429_before_existence(
    device_client: TestClient, device_session: Session, monkeypatch
):
    from app.modules.identity.service import recovery as recovery_service

    monkeypatch.setenv("RECOVERY_VERIFY_RATE_LIMIT_MAX_REQUESTS", "2")
    monkeypatch.setenv("RECOVERY_VERIFY_RATE_LIMIT_WINDOW_SECONDS", "60")
    recovery_service.reset_recovery_rate_limits()

    user = _make_device_user(device_session)
    payload = {"email": user.email, "doc_type": "DNI", "document_number": DOC_NUMBER}
    assert device_client.post("/api/v1/auth/login/device/request", json=payload).status_code == 200
    assert device_client.post("/api/v1/auth/login/device/request", json=payload).status_code == 200
    limited = device_client.post("/api/v1/auth/login/device/request", json=payload)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"

    # El limite corre ANTES de la existencia: un email inexistente con su
    # ventana agotada responde el mismo 429 (sin filtrar existencia).
    ghost = {"email": "otro@example.com", "doc_type": "DNI", "document_number": DOC_NUMBER}
    assert device_client.post("/api/v1/auth/login/device/request", json=ghost).status_code == 200
    assert device_client.post("/api/v1/auth/login/device/request", json=ghost).status_code == 200
    unknown = device_client.post("/api/v1/auth/login/device/request", json=ghost)
    assert unknown.status_code == 429
    assert unknown.json()["error"] == limited.json()["error"]


# ---------------------------------------------------------------- Esquema + logs + OpenAPI
def test_request_malformed_schema_422(device_client: TestClient, device_session: Session):
    user = _make_device_user(device_session)
    cases = [
        {"email": "no-es-email", "doc_type": "DNI", "document_number": DOC_NUMBER},
        {"email": user.email, "doc_type": "CE", "document_number": DOC_NUMBER},
        {"email": user.email, "doc_type": "DNI", "document_number": "123"},
        {"email": user.email, "doc_type": "RUC", "document_number": DOC_NUMBER},
        {"email": user.email, "doc_type": "DNI", "document_number": "1234567a"},
    ]
    for payload in cases:
        resp = device_client.post("/api/v1/auth/login/device/request", json=payload)
        assert resp.status_code == 422, payload


def test_no_pii_or_otp_in_logs(device_client: TestClient, device_session: Session, caplog):
    user = _make_device_user(device_session)
    with caplog.at_level(logging.INFO, logger="app.modules.identity.service.device_login_request"):
        device_client.post(
            "/api/v1/auth/login/device/request",
            json={"email": user.email, "doc_type": "DNI", "document_number": DOC_NUMBER},
        )
        code = _login_code(device_session, user.id)
    assert user.email not in caplog.text, "el email no sale en logs"
    assert DOC_NUMBER not in caplog.text, "el documento no sale en logs"
    assert code not in caplog.text, "el OTP no sale en logs"
    for row in _login_otp_rows(device_session, user.id):
        assert code not in row.code_hash, "solo el hash se persiste"


def test_openapi_exposes_device_login_request_route(device_client: TestClient):
    spec = device_client.get("/openapi.json").json()
    assert "/api/v1/auth/login/device/request" in spec["paths"]
