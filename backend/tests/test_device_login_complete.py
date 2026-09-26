"""Paso 2 del login en dispositivo nuevo: OTP `LOGIN` + PIN -> binding + sesion.

(E1-T46, HU03; consumido por `F-T56`/`F-T57`.)

- Sin Postgres: `TestClient(app)` + override de `get_db` a SQLite en memoria
  con schemas ATTACH (`identity`/`shared`/`notifications`/`audit`), patron de
  `tests/test_pin_reset.py` (TestClient + SQLite ATTACH + notificacion con el
  codigo en claro en el payload).
- Orden atomico (decision documentada en `service/device_login_complete.py`):
  PIN primero, OTP despues. Un OTP valido con PIN erroneo NO se consume (sigue
  `PENDING`, intentos intactos); el fallo solo mueve el lockout del PIN.
- Casos: feliz (`request` + `complete` -> 200 con tokens + `user_ref`, binding
  `ACTIVE`, sesion con `device_id`, OTP `USED`, 1x `auth.login_succeeded` en
  outbox); misma cuenta (documento de otra cuenta -> 401 identico);
  anti-enumeracion (inexistente/no `ACTIVE`/OTP mal-vencido-bloqueado/PIN
  erroneo -> mismo 401 `INVALID_LOGIN`, sin sesion ni binding); atomico (OTP no
  consumido con PIN erroneo); `REVOKED` no revive pero la sesion se abre igual
  (decision del dueno); alta crea `ACTIVE`; rebind reescribe la clave en una
  sola fila; lockout (5 PIN erroneos -> 423, vencido se limpia); best-effort
  (fallo de binding -> 200 + auditoria `failed`); `caplog` sin PII/secretos.
- Reglas estaticas (servicio con `flush` sin `commit`, reutiliza `validate_otp`
  + `verify_pin` + `create_session` + `create_access_token`, sin `float`, sin
  KYC/OTP-emision/activation, clave tal cual `hmac:`/PEM).
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
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "service"
    / "device_login_complete.py"
)
SCHEMAS_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "schemas"
    / "device_login_complete.py"
)
API_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "api"
    / "device_login_complete.py"
)

DOC_NUMBER = "12345678"
WRONG_DOC = "87654321"
WRONG_CODE = "000000"
PIN = "482917"
WRONG_PIN = "000000"
DEVICE_ID = "pixel-9-pro"
DEVICE_KEY = "hmac:9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a39281706f5e4d3c2b1a0"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@pytest.fixture()
def complete_session():
    """Sesion SQLite aislada (`identity`/`shared`/`notifications`/`audit`)."""
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
def complete_client(complete_session: Session):
    """TestClient con `get_db` a SQLite y buckets de rate-limit limpios."""
    from app.modules.identity.service import recovery as recovery_service

    recovery_service.reset_recovery_rate_limits()

    def _override():
        yield complete_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _make_user(
    session: Session,
    *,
    status: str = "ACTIVE",
    doc_number: str = DOC_NUMBER,
    pin: str | None = PIN,
    email: str | None = None,
    biometric: bool = False,
):
    """Usuario elegible (`ACTIVE` + email + hash de documento real + PIN)."""
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import pin_login as pin_login_service
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
    credential = identity_repo.create_credential(
        session,
        user.id,
        pin_hash=pin_login_service.hash_pin(pin) if pin is not None else None,
    )
    if biometric:
        credential.biometric_enabled = True
    session.commit()
    return user


def _request_otp(client: TestClient, email: str, doc: str = DOC_NUMBER):
    resp = client.post(
        "/api/v1/auth/login/device/request",
        json={"email": email, "doc_type": "DNI", "document_number": doc},
    )
    assert resp.status_code == 200, resp.text
    return resp


def _otp_code(session: Session, user_id: uuid.UUID) -> str:
    """Codigo en claro tal como viajo por email (payload de la notificacion)."""
    from app.modules.notifications.models import Notification

    session.expire_all()
    rows = list(
        session.scalars(
            sa.select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.asc())
        ).all()
    )
    assert rows, "se esperaba la notificacion con el OTP por email"
    data = (rows[-1].payload_json or {}).get("data", {})
    assert "code" in data, "el codigo viaja en el payload de la notificacion"
    return str(data["code"])


def _otp_rows(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.identity.models import OtpCode

    session.expire_all()
    return list(
        session.scalars(
            sa.select(OtpCode)
            .where(OtpCode.user_id == user_id, OtpCode.purpose == "LOGIN")
            .order_by(OtpCode.created_at.asc())
        ).all()
    )


def _sessions(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.identity.models import UserSession

    session.expire_all()
    return list(session.scalars(sa.select(UserSession).where(UserSession.user_id == user_id)).all())


def _bindings(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.identity.models import DeviceBinding

    session.expire_all()
    return list(
        session.scalars(sa.select(DeviceBinding).where(DeviceBinding.user_id == user_id)).all()
    )


def _outbox_logins(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.shared.models import OutboxEntry

    session.expire_all()
    return list(
        session.scalars(
            sa.select(OutboxEntry).where(
                OutboxEntry.event_type == "auth.login_succeeded",
                OutboxEntry.aggregate_id == user_id,
            )
        ).all()
    )


def _audit_rows(session: Session, action: str) -> list:
    from app.modules.audit.models import AuditLog

    session.expire_all()
    return list(session.scalars(sa.select(AuditLog).where(AuditLog.action == action)).all())


def _complete(client: TestClient, **overrides):
    body = {
        "email": overrides.pop("email"),
        "doc_type": "DNI",
        "document_number": DOC_NUMBER,
        "code": overrides.pop("code"),
        "pin": overrides.pop("pin", PIN),
        "device_id": DEVICE_ID,
        "device_public_key": DEVICE_KEY,
    }
    body.update(overrides)
    return client.post("/api/v1/auth/login/device/complete", json=body)


def _error_body(resp) -> dict:
    body = dict(resp.json()["error"])
    body.pop("request_id", None)
    return body


# ---------------------------------------------------------------- CA-01: camino feliz
def test_complete_happy_path_request_then_complete(
    complete_client: TestClient, complete_session: Session
):
    user = _make_user(complete_session, biometric=True)
    _request_otp(complete_client, user.email)
    code = _otp_code(complete_session, user.id)

    resp = _complete(complete_client, email=user.email, code=code)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["token_type"] == "Bearer"
    assert data["access_token"] and data["refresh_token"] and data["session_id"]
    assert data["expires_in"] >= 0
    assert data["user_ref"] == str(user.id)
    assert data["biometric_enabled"] is True
    assert code not in resp.text and PIN not in resp.text and DEVICE_KEY not in resp.text

    bindings = _bindings(complete_session, user.id)
    assert len(bindings) == 1
    assert bindings[0].status == "ACTIVE"
    assert bindings[0].device_id == DEVICE_ID
    assert bindings[0].public_key == DEVICE_KEY

    sessions = _sessions(complete_session, user.id)
    assert len(sessions) == 1
    assert sessions[0].device_id == DEVICE_ID
    assert str(sessions[0].id) == data["session_id"]

    rows = _otp_rows(complete_session, user.id)
    assert rows[0].status == "USED", "un solo uso"

    assert len(_outbox_logins(complete_session, user.id)) == 1
    assert len(_audit_rows(complete_session, "auth.device_login")) >= 1


# ---------------------------------------------------------------- CA-02: anti-enumeracion
def test_complete_anti_enumeration_same_401(complete_client: TestClient, complete_session: Session):
    user = _make_user(complete_session)
    _request_otp(complete_client, user.email)
    good_code = _otp_code(complete_session, user.id)
    inactive = _make_user(complete_session, status="BLOCKED", doc_number="11223344")

    def _try(email, doc, code, pin=PIN):
        return _complete(complete_client, email=email, document_number=doc, code=code, pin=pin)

    bodies = {}
    r = _try("nadie@example.com", DOC_NUMBER, WRONG_CODE)
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_LOGIN"
    bodies["unknown_email"] = _error_body(r)

    r = _try(inactive.email, "11223344", WRONG_CODE)
    assert r.status_code == 401
    bodies["inactive"] = _error_body(r)

    r = _try(user.email, WRONG_DOC, WRONG_CODE)
    assert r.status_code == 401, "documento de otra cuenta: mismo 401"
    bodies["wrong_doc"] = _error_body(r)

    r = _try(user.email, DOC_NUMBER, WRONG_CODE)
    assert r.status_code == 401
    bodies["wrong_code"] = _error_body(r)

    rows = _otp_rows(complete_session, user.id)
    rows[0].expires_at = _utcnow() - timedelta(seconds=1)
    complete_session.commit()
    r = _try(user.email, DOC_NUMBER, good_code)
    assert r.status_code == 401, "vencido -> MISMO 401 (colapso anti-oraculo)"
    bodies["expired"] = _error_body(r)

    rows = _otp_rows(complete_session, user.id)
    rows[0].expires_at = _utcnow() + timedelta(minutes=5)
    rows[0].attempts = rows[0].max_attempts
    rows[0].status = "PENDING"
    complete_session.commit()
    r = _try(user.email, DOC_NUMBER, good_code)
    assert r.status_code == 401, "bloqueado -> MISMO 401 (colapso anti-oraculo)"
    bodies["blocked"] = _error_body(r)

    for name, body in bodies.items():
        assert body == bodies["unknown_email"], f"{name} debe responder identico (sin oraculo)"

    assert _sessions(complete_session, user.id) == [], "sin sesion ante fallos"
    assert _bindings(complete_session, user.id) == [], "sin binding ante fallos"
    assert _outbox_logins(complete_session, user.id) == []


def test_complete_valid_otp_wrong_pin_does_not_consume_otp(
    complete_client: TestClient, complete_session: Session
):
    """Atomico (PIN primero): OTP valido + PIN erroneo -> 401 y OTP intacto."""
    user = _make_user(complete_session)
    _request_otp(complete_client, user.email)
    code = _otp_code(complete_session, user.id)

    r = _complete(complete_client, email=user.email, code=code, pin=WRONG_PIN)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_LOGIN"

    rows = _otp_rows(complete_session, user.id)
    assert rows[0].status == "PENDING", "el OTP no se consume con PIN erroneo"
    assert int(rows[0].attempts) == 0, "ni siquiera avanza intentos OTP"
    assert _sessions(complete_session, user.id) == []

    # El mismo OTP sigue sirviendo con el PIN correcto.
    r = _complete(complete_client, email=user.email, code=code)
    assert r.status_code == 200, r.text
    assert _otp_rows(complete_session, user.id)[0].status == "USED"


# ---------------------------------------------------------------- CA-04: REVOKED no revive
def test_complete_revoked_binding_not_reactivated_but_session_opens(
    complete_client: TestClient, complete_session: Session
):
    """Decision del dueno: el binding `REVOKED` no revive, la sesion abre igual."""
    from app.modules.identity import repository as identity_repo

    user = _make_user(complete_session)
    identity_repo.register_binding(complete_session, user.id, DEVICE_ID, "hmac:oldkey")
    row = identity_repo.get_binding(complete_session, user.id, DEVICE_ID)
    row.status = "REVOKED"
    complete_session.commit()

    _request_otp(complete_client, user.email)
    code = _otp_code(complete_session, user.id)
    r = _complete(complete_client, email=user.email, code=code)
    assert r.status_code == 200, r.text

    bindings = _bindings(complete_session, user.id)
    assert len(bindings) == 1, "una sola fila (rebind, sin duplicar)"
    assert bindings[0].status == "REVOKED", "un REVOKED no se reactiva por login"
    assert bindings[0].public_key == DEVICE_KEY, "la clave vigente pisa la obsoleta"
    assert len(_sessions(complete_session, user.id)) == 1, "la sesion se abre igual"


def test_complete_existing_binding_rotates_key_single_row(
    complete_client: TestClient, complete_session: Session
):
    user = _make_user(complete_session)
    _request_otp(complete_client, user.email)
    code = _otp_code(complete_session, user.id)
    r = _complete(complete_client, email=user.email, code=code)
    assert r.status_code == 200, r.text

    _request_otp(complete_client, user.email)
    code2 = _otp_code(complete_session, user.id)
    new_key = "hmac:aaaa1111bbbb2222cccc3333dddd4444eeee5555ffff6666"
    r = _complete(complete_client, email=user.email, code=code2, device_public_key=new_key)
    assert r.status_code == 200, r.text

    bindings = _bindings(complete_session, user.id)
    assert len(bindings) == 1
    assert bindings[0].status == "ACTIVE" and bindings[0].public_key == new_key


# ---------------------------------------------------------------- CA-03: lockout
def test_complete_pin_lockout_then_expired_cleans(
    complete_client: TestClient, complete_session: Session
):
    from app.modules.identity import repository as identity_repo

    user = _make_user(complete_session)
    _request_otp(complete_client, user.email)
    code = _otp_code(complete_session, user.id)

    for i in range(4):
        r = _complete(complete_client, email=user.email, code=code, pin=WRONG_PIN)
        assert r.status_code == 401, f"intento {i + 1}: 401"
    r = _complete(complete_client, email=user.email, code=code, pin=WRONG_PIN)
    assert r.status_code == 423, "5to fallo -> ACCOUNT_LOCKED"
    assert r.json()["error"]["code"] == "ACCOUNT_LOCKED"

    # Bloqueo vigente: gana incluso al OTP + PIN correctos.
    r = _complete(complete_client, email=user.email, code=code)
    assert r.status_code == 423

    # Bloqueo vencido: se limpia solo y el flujo sigue (el OTP sigue PENDING).
    credential = identity_repo.get_credential(complete_session, user.id)
    credential.locked_until = _utcnow() - timedelta(seconds=1)
    complete_session.commit()
    r = _complete(complete_client, email=user.email, code=code)
    assert r.status_code == 200, r.text
    complete_session.expire_all()
    credential = identity_repo.get_credential(complete_session, user.id)
    assert int(credential.failed_attempts) == 0 and credential.locked_until is None


# ---------------------------------------------------------------- CA-05: best-effort + logs
def test_complete_binding_failure_still_opens_session(
    complete_client: TestClient, complete_session: Session, monkeypatch
):
    from app.modules.identity import repository as identity_repo

    user = _make_user(complete_session)
    _request_otp(complete_client, user.email)
    code = _otp_code(complete_session, user.id)

    def _boom(*args, **kwargs):
        raise RuntimeError("binding caido")

    monkeypatch.setattr(identity_repo, "register_binding", _boom)
    r = _complete(complete_client, email=user.email, code=code)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["session_id"]
    assert len(_sessions(complete_session, user.id)) == 1
    assert len(_audit_rows(complete_session, "auth.device_binding")) >= 1


def test_complete_logs_without_pii(complete_client: TestClient, complete_session: Session, caplog):
    user = _make_user(complete_session)
    _request_otp(complete_client, user.email)
    code = _otp_code(complete_session, user.id)

    with caplog.at_level(logging.INFO, logger="app.modules.identity.service.device_login_complete"):
        _complete(complete_client, email=user.email, code=code, pin=WRONG_PIN)
        _complete(complete_client, email=user.email, code=code)

    text = caplog.text
    for secret in (user.email, DOC_NUMBER, code, PIN, WRONG_PIN, DEVICE_KEY):
        assert secret not in text, f"posible PII/secreto en logs: {secret!r}"
    assert "access_token" not in text.lower() and "refresh_token" not in text.lower()


# ---------------------------------------------------------------- H1: device_info via complete
def test_complete_persists_device_info_in_session(
    complete_client: TestClient, complete_session: Session
):
    """H1: `device_info` del `complete` se persiste en `sessions.device_info`.

    Sin el fix el esquema/router ignoraban `device_info` (422 o sesion sin
    el campo); con el fix viaja hasta `create_session`.
    """
    user = _make_user(complete_session)
    _request_otp(complete_client, user.email)
    code = _otp_code(complete_session, user.id)

    info = {"model": "Pixel 9", "os": "Android 15"}
    r = _complete(complete_client, email=user.email, code=code, device_info=info)
    assert r.status_code == 200, r.text

    sessions = _sessions(complete_session, user.id)
    assert len(sessions) == 1
    assert sessions[0].device_info == info


# ---------------------------------------------------------------- CA-06: reglas estaticas
def test_static_rules_complete_service_schemas_api():
    service = SERVICE_PATH.read_text(encoding="utf-8")
    assert ".commit(" not in service, "el servicio hace flush; el endpoint confirma"
    assert "float(" not in service, "sin float"
    assert "validate_otp" in service, "consume el OTP LOGIN (un solo uso)"
    assert "verify_pin" in service, "reusa la verificacion PBKDF2 de pin_login"
    assert "hash_document_number" in service, "misma cuenta via hash HMAC"
    assert "compare_digest" in service, "comparacion en tiempo constante"
    assert "create_session(" in service, "abre sesion en el exito"
    assert "create_access_token(" in service, "emite JWT corto"
    assert "begin_nested" in service, "binding best-effort en savepoint"
    assert "register_binding" in service and "touch_binding" in service
    assert "REVOKED" in service, "documenta que un REVOKED no se reactiva"
    assert "LOGIN_SUCCEEDED_EVENT" in service or "login_succeeded" in service
    assert "auth.device_login" in service
    assert "get_active_otp" in service, "rama ciega con trabajo equivalente"
    top_imports = "\n".join(
        line for line in service.splitlines() if line.startswith(("from app.", "import app."))
    )
    for forbidden in ("onboard_customer", "kyc_proxy", "pin_setup"):
        assert forbidden not in top_imports, f"prohibido tocar {forbidden}"
    log_lines = [line for line in service.splitlines() if "logger." in line]
    assert log_lines, "el servicio debe loguear sin PII"
    for line in log_lines:
        lowered = line.lower()
        for forbidden in (
            "plain",
            "recipient",
            "destination",
            "email",
            "document",
            "code_hash",
            "pin_hash",
            "public_key",
            "token",
        ):
            assert forbidden not in lowered, f"posible PII/OTP/PIN en log: {line.strip()}"

    schemas = SCHEMAS_PATH.read_text(encoding="utf-8")
    assert "user_ref" in schemas and "biometric_enabled" in schemas

    api = API_PATH.read_text(encoding="utf-8")
    assert "INVALID_LOGIN" in api and "ACCOUNT_LOCKED" in api and "RATE_LIMITED" in api
    assert ".commit(" in api, "el endpoint confirma exito y fallos (contadores persisten)"
    assert ".rollback(" in api, "revierte formato debil y fallos inesperados"
