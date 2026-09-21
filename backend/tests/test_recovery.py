"""Recuperacion de acceso por email: OTP + apertura de sesion (E1-T31, HU04 CA-01..CA-04).

- Sin Postgres: `TestClient(app)` + override de `get_db` a SQLite en memoria
  con schemas ATTACH (`identity`/`shared`/`notifications`/`audit`), patron de
  `tests/test_pin_login.py` (TestClient) + `tests/test_activation.py` (ATTACH).
- Casos: `request` con email registrado -> 200 + OTP `RECOVERY` `PENDING`
  notificado SOLO por email; con email no registrado -> 200 con el MISMO
  cuerpo y sin OTP (CA-01, sin enumeracion); `verify` con el OTP correcto ->
  tokens + sesion + `user_ref` + `auth.login_succeeded`, OTP `USED`
  (un solo uso, CA-02); OTP invalido/expirado/bloqueado -> el MISMO 401
  generico `INVALID_RECOVERY_CODE` (colapso anti-oraculo: sin filtrar
  existencia), codigo fuera de respuestas/logs (CA-03);
  dispositivo nuevo -> `device_bindings` (`ACTIVE`) + `access_recovery`
  (`method='OTP'`) en la misma transaccion, y sin clave el acceso procede
  (`device_bound=false`, CA-04); cooldown (reutiliza el `PENDING` sin
  duplicar) + rate-limit por `email+IP` (-> 429 `RATE_LIMITED`), canal
  siempre email (nunca SMS, CA-05); logs sin email/OTP/hash (CA-06).
- Reglas estaticas (servicio sin `commit`, sin SMS, reutiliza `otp_service`/
  `activation`/`device_login`/repositorio, sin tocar otros modulos).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
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

WRONG_CODE = "000000"


def _utcnow() -> datetime:
    return datetime.now(UTC)


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


def _login_events(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.shared.models import OutboxEntry

    session.expire_all()
    stmt = sa.select(OutboxEntry).where(
        OutboxEntry.event_type == "auth.login_succeeded",
        OutboxEntry.aggregate_id == user_id,
    )
    return list(session.scalars(stmt).all())


def _bindings(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.identity.models import DeviceBinding

    session.expire_all()
    stmt = sa.select(DeviceBinding).where(DeviceBinding.user_id == user_id)
    return list(session.scalars(stmt).all())


def _recoveries(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.identity.models import AccessRecovery

    session.expire_all()
    stmt = sa.select(AccessRecovery).where(AccessRecovery.user_id == user_id)
    return list(session.scalars(stmt).all())


def _sessions(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.identity.models import UserSession

    session.expire_all()
    stmt = sa.select(UserSession).where(UserSession.user_id == user_id)
    return list(session.scalars(stmt).all())


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
    assert "generate_otp" in service and "validate_otp" in service, "reutiliza otp_service"
    assert "resolve_activation_delivery" in service, "reutiliza el routing de activation"
    assert "create_access_token" in service, "reutiliza el JWT existente"
    assert "jwt.encode(" not in service, "no inventa tokens"
    assert "record_access_recovery" in service, "registra access_recovery via repositorio"
    assert "register_binding" in service and "touch_binding" in service, "reutiliza bindings"
    assert "REFRESH_TTL_SECONDS" in service, "reutiliza la emision de device_login"
    assert "LOGIN_SUCCEEDED_EVENT" in service, "mismo evento que pin_login/device_login"
    assert "    from app.modules.notifications.service import" in service
    assert "    from app.core.outbox import" in service
    assert "    from app.modules.audit.service import" in service
    assert "auth.recovery_requested" in service and "auth.access_recovered" in service
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
    assert "INVALID_RECOVERY_CODE" in api and "RATE_LIMITED" in api
    assert "EXPIRED_OTP" not in api, "vencido/bloqueado colapsan al 401 generico (sin oraculo)"
    assert "EXPIRED_OTP" not in service, "el servicio no expone el codigo de vencido"
    assert "RecoveryExpiredError" not in api, "sin rama tipada que filtre existencia"
    assert ".commit(" in api, "el endpoint confirma exito y fallos (el contador persiste)"
    assert ".rollback(" in api, "revierte rate-limit de request y fallos inesperados"


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


# ---------------------------------------------------------------- CA-02: verify feliz
def test_verify_correct_code_opens_session_and_consumes_otp(
    recovery_client: TestClient, recovery_session: Session
):
    user = _make_active_user(recovery_session)
    recovery_client.post("/api/v1/auth/recovery/request", json={"email": user.email})
    code = _recovery_code(recovery_session, user.id)

    resp = recovery_client.post(
        "/api/v1/auth/recovery/verify", json={"email": user.email, "code": code}
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["user_ref"] == str(user.id)
    assert data["token_type"] == "Bearer" and data["access_token"] and data["refresh_token"]
    assert data["session_id"] and data["expires_in"] > 0
    assert data["device_bound"] is False, "sin clave no hay binding pero el acceso procede"
    assert code not in resp.text

    rows = _otp_rows(recovery_session, user.id)
    assert rows[0].status == "USED", "un solo uso"
    assert len(_sessions(recovery_session, user.id)) == 1
    assert len(_login_events(recovery_session, user.id)) == 1
    assert len(_audit_rows(recovery_session, "auth.access_recovered")) == 1

    # Reuso del mismo codigo: ya consumido -> 401 generico.
    again = recovery_client.post(
        "/api/v1/auth/recovery/verify", json={"email": user.email, "code": code}
    )
    assert again.status_code == 401
    assert again.json()["error"]["code"] == "INVALID_RECOVERY_CODE"


# ---------------------------------------------------------------- CA-03: errores tipados
def test_verify_invalid_expired_exhausted_no_oracle(
    recovery_client: TestClient, recovery_session: Session
):
    user = _make_active_user(recovery_session)
    recovery_client.post("/api/v1/auth/recovery/request", json={"email": user.email})

    bad = recovery_client.post(
        "/api/v1/auth/recovery/verify", json={"email": user.email, "code": WRONG_CODE}
    )
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "INVALID_RECOVERY_CODE"

    missing = recovery_client.post(
        "/api/v1/auth/recovery/verify",
        json={"email": "fantasma@example.com", "code": WRONG_CODE},
    )
    assert missing.status_code == 401
    assert missing.json()["error"] == bad.json()["error"], "mismo cuerpo: sin enumeracion"

    rows = _otp_rows(recovery_session, user.id)
    assert rows[0].status == "PENDING" and int(rows[0].attempts) == 1, "el intento persiste"

    # Vencido -> MISMO 401 generico (colapso anti-oraculo: indistinguible
    # del email desconocido / codigo incorrecto).
    rows[0].expires_at = _utcnow() - timedelta(seconds=1)
    recovery_session.commit()
    expired = recovery_client.post(
        "/api/v1/auth/recovery/verify", json={"email": user.email, "code": WRONG_CODE}
    )
    assert expired.status_code == 401
    assert expired.json()["error"]["code"] == "INVALID_RECOVERY_CODE"
    assert expired.json()["error"] == bad.json()["error"] == missing.json()["error"]


def test_verify_attempts_exhausted_generic_401_without_oracle(
    recovery_client: TestClient, recovery_session: Session
):
    user = _make_active_user(recovery_session)
    recovery_client.post("/api/v1/auth/recovery/request", json={"email": user.email})

    # max_attempts = 3: los tres intentos responden el MISMO 401 generico
    # (el bloqueo no se anuncia: vencido/bloqueado colapsan al generico
    # para no filtrar existencia).
    first = recovery_client.post(
        "/api/v1/auth/recovery/verify", json={"email": user.email, "code": "111111"}
    )
    assert first.status_code == 401
    second = recovery_client.post(
        "/api/v1/auth/recovery/verify", json={"email": user.email, "code": "222222"}
    )
    assert second.status_code == 401
    locked = recovery_client.post(
        "/api/v1/auth/recovery/verify", json={"email": user.email, "code": "333333"}
    )
    assert locked.status_code == 401
    assert locked.json()["error"]["code"] == "INVALID_RECOVERY_CODE"
    assert locked.json()["error"] == first.json()["error"] == second.json()["error"]

    rows = _otp_rows(recovery_session, user.id)
    assert rows[0].status == "EXPIRED", "el bloqueo persiste en la fila aunque no se anuncie"


def test_verify_non_active_user_generic_401_without_session(
    recovery_client: TestClient, recovery_session: Session
):
    from app.modules.identity.service import otp_service

    user = _make_active_user(recovery_session, status="BLOCKED")
    _, plain = otp_service.generate_otp(
        recovery_session, user_id=user.id, purpose="RECOVERY", destination=user.email
    )
    recovery_session.commit()

    resp = recovery_client.post(
        "/api/v1/auth/recovery/verify", json={"email": user.email, "code": plain}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_RECOVERY_CODE"
    assert _sessions(recovery_session, user.id) == []
    rows = _otp_rows(recovery_session, user.id)
    assert rows[0].status == "PENDING", "no se quema el OTP de un no elegible"


# ---------------------------------------------------------------- CA-04: binding + access_recovery
def test_verify_new_device_registers_binding_and_recovery_row(
    recovery_client: TestClient, recovery_session: Session
):
    user = _make_active_user(recovery_session)
    recovery_client.post("/api/v1/auth/recovery/request", json={"email": user.email})
    code = _recovery_code(recovery_session, user.id)

    resp = recovery_client.post(
        "/api/v1/auth/recovery/verify",
        json={
            "email": user.email,
            "code": code,
            "device_id": "pixel-9",
            "device_public_key": "hmac:" + "ab" * 16,
            "platform": "android",
            "biometric_type": "FACE",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["device_bound"] is True

    binds = _bindings(recovery_session, user.id)
    assert len(binds) == 1
    assert binds[0].status == "ACTIVE" and binds[0].device_id == "pixel-9"

    recs = _recoveries(recovery_session, user.id)
    assert len(recs) == 1
    assert recs[0].method == "OTP" and recs[0].new_credential_set is False
    assert recs[0].device_id == "pixel-9"
    assert recs[0].notified_channels == ["email"]
    assert recs[0].verification_result == {
        "result": "ok",
        "at": recs[0].verification_result["at"],
    }, "sin OTP ni PII en el resultado"

    # Segundo ciclo con el mismo dispositivo: touch (una sola fila), sin re-bind.
    recovery_client.post("/api/v1/auth/recovery/request", json={"email": user.email})
    code2 = _recovery_code(recovery_session, user.id)
    resp2 = recovery_client.post(
        "/api/v1/auth/recovery/verify",
        json={
            "email": user.email,
            "code": code2,
            "device_id": "pixel-9",
            "device_public_key": "hmac:" + "ab" * 16,
        },
    )
    assert resp2.status_code == 200
    assert resp2.json()["data"]["device_bound"] is False, "la clave ya existia igual"
    assert len(_bindings(recovery_session, user.id)) == 1


def test_verify_binding_failure_keeps_session(
    recovery_client: TestClient, recovery_session: Session, monkeypatch
):
    from app.modules.identity import repository as identity_repo

    user = _make_active_user(recovery_session)
    recovery_client.post("/api/v1/auth/recovery/request", json={"email": user.email})
    code = _recovery_code(recovery_session, user.id)

    def _boom(*args, **kwargs):
        raise RuntimeError("binding caido")

    monkeypatch.setattr(identity_repo, "register_binding", _boom)
    resp = recovery_client.post(
        "/api/v1/auth/recovery/verify",
        json={
            "email": user.email,
            "code": code,
            "device_id": "nuevo",
            "device_public_key": "hmac:" + "cd" * 16,
        },
    )
    assert resp.status_code == 200, "best-effort: la sesion sobrevive"
    assert resp.json()["data"]["device_bound"] is False
    assert len(_sessions(recovery_session, user.id)) == 1


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


# ---------------------------------------------------------------- CA-06: sin PII en logs
def test_no_pii_or_otp_in_logs_and_recovery_row(
    recovery_client: TestClient, recovery_session: Session, caplog
):
    user = _make_active_user(recovery_session)
    with caplog.at_level(logging.INFO, logger="app.modules.identity.service.recovery"):
        recovery_client.post("/api/v1/auth/recovery/request", json={"email": user.email})
        code = _recovery_code(recovery_session, user.id)
        recovery_client.post(
            "/api/v1/auth/recovery/verify", json={"email": user.email, "code": code}
        )
    assert user.email not in caplog.text, "el email no sale en logs"
    assert code not in caplog.text, "el OTP no sale en logs"
    for row in _otp_rows(recovery_session, user.id):
        assert code not in row.code_hash, "solo el hash se persiste"
    for rec in _recoveries(recovery_session, user.id):
        text = str(rec.verification_result)
        assert user.email not in text and code not in text, "resultado sin PII ni OTP"


# ---------------------------------------------------------------- Atomicidad
def test_unexpected_failure_after_flush_rolls_back_everything(
    recovery_client: TestClient, recovery_session: Session, monkeypatch
):
    import pytest as _pytest

    from app.modules.identity import repository as identity_repo

    user = _make_active_user(recovery_session)
    recovery_client.post("/api/v1/auth/recovery/request", json={"email": user.email})
    code = _recovery_code(recovery_session, user.id)

    def _boom(*args, **kwargs):
        raise RuntimeError("sesion caida")

    monkeypatch.setattr(identity_repo, "create_session", _boom)
    with _pytest.raises(RuntimeError):
        recovery_client.post(
            "/api/v1/auth/recovery/verify", json={"email": user.email, "code": code}
        )
    rows = _otp_rows(recovery_session, user.id)
    assert rows[0].status == "PENDING", "el consumo del OTP tambien se revierte"
    assert _sessions(recovery_session, user.id) == []
    assert _recoveries(recovery_session, user.id) == []
    assert _login_events(recovery_session, user.id) == []


# ---------------------------------------------------------------- OpenAPI
def test_openapi_exposes_recovery_routes(recovery_client: TestClient):
    spec = recovery_client.get("/openapi.json").json()
    assert "/api/v1/auth/recovery/request" in spec["paths"]
    assert "/api/v1/auth/recovery/verify" in spec["paths"]
