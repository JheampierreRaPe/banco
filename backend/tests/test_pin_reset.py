"""Reseteo de PIN con email+DNI+OTP (E1-T34, HU02/HU04 CA-01..CA-05, SCR-005).

- Sin Postgres: `TestClient(app)` + override de `get_db` a SQLite en memoria
  con schemas ATTACH (`identity`/`shared`/`notifications`/`audit`/`config`),
  patron de `tests/test_recovery.py` (TestClient) + `tests/test_activation.py`
  (ATTACH). `config.parameters` se crea solo en los tests de parametros
  (sin la tabla rige la constante: fallback best-effort).
- Casos: feliz (`email + DNI + OTP RECOVERY` correctos -> `{user_ref,
  pin_set: true}` + `pin_hash` PBKDF2 + resets + `access_recovery` con
  `new_credential_set=true`, sin sesion; luego `login/pin` con el PIN nuevo
  abre la unica sesion y el anterior deja de servir); anti-enumeracion
  (email inexistente, DNI incorrecto, sin OTP, codigo incorrecto/vencido/
  bloqueado, no elegible -> MISMO 401 `INVALID_PIN_RESET`); rate-limit por
  `email+IP` (-> 429 `RATE_LIMITED`, antes de la existencia); PIN debil ->
  422 sin consumir el OTP; normalizacion (email con mayusculas/espacios, DNI
  con separadores); parametros (`otp.max_attempts`, `auth.max_failed_attempts`,
  `otp.resend_wait_seconds`, `auth.recovery_verify_*`: sembrado se usa,
  invalido/ausente rige la constante); migracion (`User.email NOT NULL` en
  metadata + insertar sin email falla a nivel BD); sin PII en logs/fila;
  atomicidad ante fallo inesperado; OpenAPI expone la ruta.
- Reglas estaticas (servicio sin `commit`, sin sesion/tokens, reutiliza
  `validate_otp`/`hash_pin`/`hash_document_number`/`record_access_recovery`,
  sin DNI/OTP/PIN en logs).
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import Base, get_db
from app.main import app

SERVICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "service"
    / "pin_reset.py"
)
API_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "api" / "pin_reset.py"
)
SCHEMAS_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "schemas"
    / "pin_reset.py"
)

DOC_NUMBER = "12345678"
RUC_NUMBER = "20123456789"
WRONG_DOC = "87654321"
WRONG_CODE = "000000"
OLD_PIN = "111111"
NEW_PIN = "222222"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@pytest.fixture()
def reset_session():
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
def reset_client(reset_session: Session, monkeypatch):
    """TestClient con `get_db` a SQLite y buckets de rate-limit limpios."""
    from app.modules.identity.service import recovery as recovery_service

    monkeypatch.delenv("RECOVERY_REQUEST_RATE_LIMIT_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("RECOVERY_REQUEST_RATE_LIMIT_MAX_REQUESTS", raising=False)
    monkeypatch.delenv("RECOVERY_VERIFY_RATE_LIMIT_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("RECOVERY_VERIFY_RATE_LIMIT_MAX_REQUESTS", raising=False)
    recovery_service.reset_recovery_rate_limits()

    def _override():
        yield reset_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        recovery_service.reset_recovery_rate_limits()


def _make_reset_user(
    session: Session,
    *,
    status: str = "ACTIVE",
    doc_number: str = DOC_NUMBER,
    doc_type: str = "DNI",
    pin: str | None = OLD_PIN,
    email: str | None = None,
):
    """Usuario elegible (`ACTIVE` + email + hash de documento real) para `/pin-reset`."""
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import pin_login as pin_login_service
    from app.modules.identity.service.kyc_onboarding import hash_document_number

    suffix = uuid.uuid4().hex[:8]
    user = identity_repo.create_user(
        session,
        doc_type=doc_type,
        doc_number_hash=hash_document_number(doc_number),
        first_name="Ada",
        last_name="Lovelace",
        email=email or f"ada.{suffix}@example.com",
        phone="+51999888777",
        status=status,
    )
    identity_repo.create_credential(
        session,
        user.id,
        pin_hash=pin_login_service.hash_pin(pin) if pin is not None else None,
    )
    session.commit()
    return user


def _request_otp(client: TestClient, email: str):
    resp = client.post("/api/v1/auth/recovery/request", json={"email": email})
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
            .where(OtpCode.user_id == user_id, OtpCode.purpose == "RECOVERY")
            .order_by(OtpCode.created_at.asc())
        ).all()
    )


def _recoveries(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.identity.models import AccessRecovery

    session.expire_all()
    return list(
        session.scalars(sa.select(AccessRecovery).where(AccessRecovery.user_id == user_id)).all()
    )


def _sessions(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.identity.models import UserSession

    session.expire_all()
    return list(session.scalars(sa.select(UserSession).where(UserSession.user_id == user_id)).all())


def _audit_rows(session: Session, action: str) -> list:
    from app.modules.audit.models import AuditLog

    session.expire_all()
    return list(session.scalars(sa.select(AuditLog).where(AuditLog.action == action)).all())


def _seed_parameters(session: Session, mapping: dict) -> None:
    """Crea `config.parameters` en SQLite y siembra `mapping` (best-effort)."""
    session.execute(
        sa.text(
            "CREATE TABLE IF NOT EXISTS config.parameters (key TEXT PRIMARY KEY, value_json TEXT)"
        )
    )
    for key, value in mapping.items():
        session.execute(
            sa.text("INSERT OR REPLACE INTO config.parameters (key, value_json) VALUES (:k, :v)"),
            {"k": key, "v": str(value)},
        )
    session.commit()


# ---------------------------------------------------------------- Reglas estaticas
def test_static_rules_pin_reset_no_commit_no_session_no_pii():
    service = SERVICE_PATH.read_text(encoding="utf-8")
    assert ".commit(" not in service, "el servicio hace flush; el endpoint confirma"
    assert "float(" not in service, "sin float"
    assert "validate_otp" in service, "consume el OTP RECOVERY (un solo uso)"
    assert "hash_pin" in service, "fija el PIN con PBKDF2 (sin reinventar cripto)"
    assert "hash_document_number" in service, "valida el DNI contra el hash HMAC server-side"
    assert "compare_digest" in service, "comparacion del hash en tiempo constante"
    assert "record_access_recovery" in service, "registra access_recovery via repositorio"
    assert "new_credential_set=True" in service, "el reseteo es un cambio de credencial real"
    assert "create_session(" not in service, "pin-reset no crea sessions"
    assert "create_access_token(" not in service, "pin-reset no emite JWT"
    assert "from app.core.security import" not in service, "sin import de JWT en pin_reset"
    assert "token_urlsafe" not in service, "sin refresh opaco en pin-reset"
    assert "LOGIN_SUCCEEDED_EVENT" not in service, "pin-reset no enlista login"
    assert "    from app.modules.audit.service import" in service, "auditoria via fachada perezosa"
    assert "auth.pin_reset" in service
    top_imports = "\n".join(
        line for line in service.splitlines() if line.startswith(("from app.", "import app."))
    )
    for forbidden in ("kyc_proxy", "onboard_customer", "device_login", "pin_setup"):
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
            "doc_number",
            "document",
            "code_hash",
            "pin_hash",
            "public_key",
        ):
            assert forbidden not in lowered, f"posible PII/OTP/PIN en log: {line.strip()}"

    schemas = SCHEMAS_PATH.read_text(encoding="utf-8")
    assert "doc_number" in schemas and "pin_set" in schemas

    api = API_PATH.read_text(encoding="utf-8")
    assert "INVALID_PIN_RESET" in api and "RATE_LIMITED" in api
    assert "INVALID_PIN_FORMAT" in api, "PIN debil a nivel servicio -> 422"
    assert ".commit(" in api, "el endpoint confirma exito y fallos (el contador persiste)"
    assert ".rollback(" in api, "revierte PIN debil y fallos inesperados"


# ---------------------------------------------------------------- CA-01: camino feliz
def test_pin_reset_happy_path_then_login_with_new_pin(
    reset_client: TestClient, reset_session: Session
):
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import pin_login as pin_login_service

    user = _make_reset_user(reset_session)
    _request_otp(reset_client, user.email)
    code = _otp_code(reset_session, user.id)

    # Normalizacion (E1-T40): el esquema exige email sin espacios (422 si los
    # trae, igual que `recovery`) y `doc_number` solo digitos con longitud
    # exacta segun `doc_type` (DNI 8 / RUC 11); el servicio tolera
    # mayusculas en el email y el hash HMAC normaliza el documento.
    resp = reset_client.post(
        "/api/v1/auth/pin-reset",
        json={
            "email": user.email.upper(),
            "doc_type": "DNI",
            "doc_number": DOC_NUMBER,
            "code": code,
            "pin": NEW_PIN,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"user_ref": str(user.id), "pin_set": True}
    assert code not in resp.text and NEW_PIN not in resp.text

    credential = identity_repo.get_credential(reset_session, user.id)
    assert pin_login_service.verify_pin(NEW_PIN, credential.pin_hash)
    assert not pin_login_service.verify_pin(OLD_PIN, credential.pin_hash), "el PIN anterior muere"
    assert int(credential.failed_attempts) == 0 and credential.locked_until is None

    rows = _otp_rows(reset_session, user.id)
    assert rows[0].status == "USED", "un solo uso"
    assert _sessions(reset_session, user.id) == [], "pin-reset no crea sessions"

    recs = _recoveries(reset_session, user.id)
    assert len(recs) == 1
    assert recs[0].method == "OTP" and recs[0].new_credential_set is True
    assert recs[0].notified_channels == ["email"]
    assert recs[0].verification_result["result"] == "ok"
    assert len(_audit_rows(reset_session, "auth.pin_reset")) == 1

    # El PIN nuevo abre la unica sesion; el anterior ya no sirve.
    login = reset_client.post(
        "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": NEW_PIN}
    )
    assert login.status_code == 200, login.text
    assert login.json()["data"]["session_id"]
    assert len(_sessions(reset_session, user.id)) == 1

    stale = reset_client.post(
        "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": OLD_PIN}
    )
    assert stale.status_code == 401


def test_pin_reset_resets_lockout_counters(reset_client: TestClient, reset_session: Session):
    from app.modules.identity import repository as identity_repo

    user = _make_reset_user(reset_session)
    credential = identity_repo.get_credential(reset_session, user.id)
    credential.failed_attempts = 4
    reset_session.commit()

    _request_otp(reset_client, user.email)
    code = _otp_code(reset_session, user.id)
    resp = reset_client.post(
        "/api/v1/auth/pin-reset",
        json={"email": user.email, "doc_number": DOC_NUMBER, "code": code, "pin": NEW_PIN},
    )
    assert resp.status_code == 200, resp.text
    reset_session.expire_all()
    credential = identity_repo.get_credential(reset_session, user.id)
    assert int(credential.failed_attempts) == 0 and credential.locked_until is None


# ---------------------------------------------------------------- CA-02: anti-enumeracion
def test_pin_reset_anti_enumeration_same_401(reset_client: TestClient, reset_session: Session):
    user = _make_reset_user(reset_session)
    _request_otp(reset_client, user.email)
    good_code = _otp_code(reset_session, user.id)
    fresh = _make_reset_user(reset_session, doc_number="99999999")

    def _reset(email, doc, code, pin=NEW_PIN):
        return reset_client.post(
            "/api/v1/auth/pin-reset",
            json={"email": email, "doc_number": doc, "code": code, "pin": pin},
        )

    bodies = {}
    r = _reset("nadie@example.com", DOC_NUMBER, WRONG_CODE)
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_PIN_RESET"
    bodies["unknown_email"] = r.json()["error"]

    r = _reset(user.email, WRONG_DOC, WRONG_CODE)
    assert r.status_code == 401
    bodies["wrong_doc"] = r.json()["error"]

    r = _reset(fresh.email, "99999999", WRONG_CODE)
    assert r.status_code == 401, "sin OTP pendiente: mismo 401"
    bodies["no_otp"] = r.json()["error"]

    r = _reset(user.email, DOC_NUMBER, WRONG_CODE)
    assert r.status_code == 401
    bodies["wrong_code"] = r.json()["error"]

    for name, body in bodies.items():
        assert body == bodies["unknown_email"], f"{name} debe responder identico (sin oraculo)"

    # Vencido -> MISMO 401 (colapso anti-oraculo).
    rows = _otp_rows(reset_session, user.id)
    rows[0].expires_at = _utcnow() - timedelta(seconds=1)
    reset_session.commit()
    r = _reset(user.email, DOC_NUMBER, good_code)
    assert r.status_code == 401
    assert r.json()["error"] == bodies["unknown_email"]

    # Bloqueado por intentos -> MISMO 401 (el bloqueo persiste en la fila).
    _request_otp(reset_client, user.email)
    for bad in ("111111", "333333", "444444"):
        probe = _reset(user.email, DOC_NUMBER, bad)
        assert probe.status_code == 401
        assert probe.json()["error"] == bodies["unknown_email"]
    rows = _otp_rows(reset_session, user.id)
    assert rows[-1].status == "EXPIRED", "el bloqueo persiste aunque no se anuncie"
    locked = _reset(user.email, DOC_NUMBER, _otp_code(reset_session, user.id))
    assert locked.status_code == 401, "bloqueado: ni el codigo vigente pasa"
    assert locked.json()["error"] == bodies["unknown_email"]


def test_pin_reset_non_active_user_generic_401_without_burning_otp(
    reset_client: TestClient, reset_session: Session
):
    from app.modules.identity.service import otp_service

    user = _make_reset_user(reset_session, status="BLOCKED")
    _, plain = otp_service.generate_otp(
        reset_session, user_id=user.id, purpose="RECOVERY", destination=user.email
    )
    reset_session.commit()

    resp = reset_client.post(
        "/api/v1/auth/pin-reset",
        json={"email": user.email, "doc_number": DOC_NUMBER, "code": plain, "pin": NEW_PIN},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_PIN_RESET"
    assert _sessions(reset_session, user.id) == []
    rows = _otp_rows(reset_session, user.id)
    assert rows[0].status == "PENDING", "no se quema el OTP de un no elegible"
    assert _recoveries(reset_session, user.id) == []


def test_pin_reset_malformed_schema_422(reset_client: TestClient, reset_session: Session):
    user = _make_reset_user(reset_session)
    for payload in (
        {"email": "no-es-email", "doc_number": DOC_NUMBER, "code": "123456", "pin": NEW_PIN},
        {"email": user.email, "doc_number": DOC_NUMBER, "code": "123456", "pin": "12"},
        {"email": user.email, "doc_number": DOC_NUMBER, "code": "123456", "pin": "1234567"},
        {"email": user.email, "doc_number": DOC_NUMBER, "code": "123456", "pin": "abcd"},
        {"email": user.email, "doc_number": "", "code": "123456", "pin": NEW_PIN},
    ):
        resp = reset_client.post("/api/v1/auth/pin-reset", json=payload)
        assert resp.status_code == 422, payload


def test_pin_reset_weak_pin_422_without_burning_otp(
    reset_client: TestClient, reset_session: Session
):
    user = _make_reset_user(reset_session)
    _request_otp(reset_client, user.email)
    code = _otp_code(reset_session, user.id)

    resp = reset_client.post(
        "/api/v1/auth/pin-reset",
        json={"email": user.email, "doc_number": DOC_NUMBER, "code": code, "pin": "12"},
    )
    assert resp.status_code == 422
    rows = _otp_rows(reset_session, user.id)
    assert rows[0].status == "PENDING" and int(rows[0].attempts) == 0, "el OTP no se consume"

    # El OTP sigue valido: con buen PIN el reseteo procede.
    ok = reset_client.post(
        "/api/v1/auth/pin-reset",
        json={"email": user.email, "doc_number": DOC_NUMBER, "code": code, "pin": NEW_PIN},
    )
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------- E1-T40: doc_type DNI/RUC
def test_pin_reset_ruc_11_digits_success(reset_client: TestClient, reset_session: Session):
    """`doc_type=RUC` con 11 digitos resuelve por el hash del RUC (E1-T40)."""
    user = _make_reset_user(reset_session, doc_number=RUC_NUMBER, doc_type="RUC")
    _request_otp(reset_client, user.email)
    code = _otp_code(reset_session, user.id)

    resp = reset_client.post(
        "/api/v1/auth/pin-reset",
        json={
            "email": user.email,
            "doc_type": "RUC",
            "doc_number": RUC_NUMBER,
            "code": code,
            "pin": NEW_PIN,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"user_ref": str(user.id), "pin_set": True}
    assert code not in resp.text and NEW_PIN not in resp.text and RUC_NUMBER not in resp.text

    recs = _recoveries(reset_session, user.id)
    assert len(recs) == 1 and recs[0].new_credential_set is True


def test_pin_reset_default_doc_type_is_dni_compat(reset_client: TestClient, reset_session: Session):
    """Sin `doc_type` rige `DNI` (compatibilidad F-T43; E1-T40)."""
    user = _make_reset_user(reset_session)
    _request_otp(reset_client, user.email)
    code = _otp_code(reset_session, user.id)

    resp = reset_client.post(
        "/api/v1/auth/pin-reset",
        json={"email": user.email, "doc_number": DOC_NUMBER, "code": code, "pin": NEW_PIN},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"user_ref": str(user.id), "pin_set": True}


def test_pin_reset_unknown_doc_type_422_without_burning_otp(
    reset_client: TestClient, reset_session: Session
):
    """`doc_type` desconocido -> 422 estandar sin consumir el OTP (E1-T40)."""
    user = _make_reset_user(reset_session)
    _request_otp(reset_client, user.email)
    code = _otp_code(reset_session, user.id)

    resp = reset_client.post(
        "/api/v1/auth/pin-reset",
        json={
            "email": user.email,
            "doc_type": "CE",
            "doc_number": DOC_NUMBER,
            "code": code,
            "pin": NEW_PIN,
        },
    )
    assert resp.status_code == 422, resp.text
    rows = _otp_rows(reset_session, user.id)
    assert rows[0].status == "PENDING" and int(rows[0].attempts) == 0, "el OTP no se consume"

    # El OTP sigue valido: con `doc_type` correcto el reseteo procede.
    ok = reset_client.post(
        "/api/v1/auth/pin-reset",
        json={
            "email": user.email,
            "doc_type": "DNI",
            "doc_number": DOC_NUMBER,
            "code": code,
            "pin": NEW_PIN,
        },
    )
    assert ok.status_code == 200, ok.text


def test_pin_reset_doc_length_mismatch_422_without_burning_otp(
    reset_client: TestClient, reset_session: Session
):
    """Longitud no numerica/incorrecta por tipo -> 422 sin tocar el OTP (E1-T40)."""
    user = _make_reset_user(reset_session)
    _request_otp(reset_client, user.email)
    code = _otp_code(reset_session, user.id)

    for payload in (
        {"doc_type": "DNI", "doc_number": RUC_NUMBER},  # 11 digitos con DNI
        {"doc_type": "RUC", "doc_number": DOC_NUMBER},  # 8 digitos con RUC
        {"doc_type": "DNI", "doc_number": "1234567"},  # corto
        {"doc_type": "DNI", "doc_number": "12AB5678"},  # no numerico
        {"doc_type": "DNI", "doc_number": "12.345.678"},  # separadores: solo digitos
    ):
        resp = reset_client.post(
            "/api/v1/auth/pin-reset",
            json={"email": user.email, "code": code, "pin": NEW_PIN, **payload},
        )
        assert resp.status_code == 422, payload
    rows = _otp_rows(reset_session, user.id)
    assert rows[0].status == "PENDING" and int(rows[0].attempts) == 0, "el OTP no se consume"

    ok = reset_client.post(
        "/api/v1/auth/pin-reset",
        json={
            "email": user.email,
            "doc_type": "DNI",
            "doc_number": DOC_NUMBER,
            "code": code,
            "pin": NEW_PIN,
        },
    )
    assert ok.status_code == 200, ok.text


def test_pin_reset_doc_mismatch_still_generic_401(reset_client: TestClient, reset_session: Session):
    """Formato valido pero hash que no coincide -> mismo 401 generico (E1-T40).

    `doc_type` no se cruza con `users.doc_type`: un RUC bien formado que no
    corresponde al usuario responde el `INVALID_PIN_RESET` generico, identico
    al de email inexistente (sin oraculo de campo).
    """
    user = _make_reset_user(reset_session)
    _request_otp(reset_client, user.email)

    ghost = reset_client.post(
        "/api/v1/auth/pin-reset",
        json={
            "email": "nadie-e140@example.com",
            "doc_type": "DNI",
            "doc_number": DOC_NUMBER,
            "code": WRONG_CODE,
            "pin": NEW_PIN,
        },
    )
    assert ghost.status_code == 401
    expected = ghost.json()["error"]

    mismatch = reset_client.post(
        "/api/v1/auth/pin-reset",
        json={
            "email": user.email,
            "doc_type": "RUC",
            "doc_number": RUC_NUMBER,
            "code": WRONG_CODE,
            "pin": NEW_PIN,
        },
    )
    assert mismatch.status_code == 401
    assert mismatch.json()["error"] == expected, "sin oraculo: mismo cuerpo generico"
    assert RUC_NUMBER not in mismatch.text, "el documento no se refleja"


def test_service_doc_validation_defensive_before_state(reset_session: Session):
    """Validacion defensiva del servicio: `ValueError` antes de tocar estado."""
    from app.modules.identity.service import pin_reset as pin_reset_service

    with pytest.raises(ValueError, match="doc_type"):
        pin_reset_service.reset_pin(
            reset_session,
            email="alguien@example.com",
            doc_type="PASSPORT",
            doc_number=DOC_NUMBER,
            code="123456",
            pin=NEW_PIN,
        )
    with pytest.raises(ValueError, match="debe tener 8 digitos"):
        pin_reset_service.reset_pin(
            reset_session,
            email="alguien@example.com",
            doc_type="DNI",
            doc_number="1234567",
            code="123456",
            pin=NEW_PIN,
        )


# ---------------------------------------------------------------- Rate-limit (CA-02/429)
def test_pin_reset_rate_limit_429_before_existence(
    reset_client: TestClient, reset_session: Session, monkeypatch
):
    from app.modules.identity.service import recovery as recovery_service

    monkeypatch.setenv("RECOVERY_VERIFY_RATE_LIMIT_MAX_REQUESTS", "2")
    monkeypatch.setenv("RECOVERY_VERIFY_RATE_LIMIT_WINDOW_SECONDS", "60")
    recovery_service.reset_recovery_rate_limits()

    user = _make_reset_user(reset_session)
    _request_otp(reset_client, user.email)
    payload = {"email": user.email, "doc_number": DOC_NUMBER, "code": "111111", "pin": NEW_PIN}
    assert reset_client.post("/api/v1/auth/pin-reset", json=payload).status_code == 401
    assert reset_client.post("/api/v1/auth/pin-reset", json=payload).status_code == 401
    limited = reset_client.post("/api/v1/auth/pin-reset", json=payload)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"

    # El limite se verifica ANTES de la existencia: un email inexistente con
    # su ventana agotada responde el mismo 429 (sin filtrar existencia).
    ghost = {
        "email": "otro-pinreset@example.com",
        "doc_number": DOC_NUMBER,
        "code": "111111",
        "pin": NEW_PIN,
    }
    assert reset_client.post("/api/v1/auth/pin-reset", json=ghost).status_code == 401
    assert reset_client.post("/api/v1/auth/pin-reset", json=ghost).status_code == 401
    unknown = reset_client.post("/api/v1/auth/pin-reset", json=ghost)
    assert unknown.status_code == 429
    assert unknown.json()["error"] == limited.json()["error"]


# ---------------------------------------------------------------- CA-04: parametros
def test_parameters_otp_max_attempts_seeded_is_used(
    reset_client: TestClient, reset_session: Session
):
    _seed_parameters(reset_session, {"otp.max_attempts": 1})
    user = _make_reset_user(reset_session)
    _request_otp(reset_client, user.email)
    good = _otp_code(reset_session, user.id)

    bad = reset_client.post(
        "/api/v1/auth/pin-reset",
        json={"email": user.email, "doc_number": DOC_NUMBER, "code": WRONG_CODE, "pin": NEW_PIN},
    )
    assert bad.status_code == 401
    rows = _otp_rows(reset_session, user.id)
    assert rows[0].status == "EXPIRED", "con max_attempts=1 el primer fallo bloquea"

    locked = reset_client.post(
        "/api/v1/auth/pin-reset",
        json={"email": user.email, "doc_number": DOC_NUMBER, "code": good, "pin": NEW_PIN},
    )
    assert locked.status_code == 401
    assert locked.json()["error"] == bad.json()["error"]


def test_parameters_invalid_value_falls_back_to_constant(
    reset_client: TestClient, reset_session: Session
):
    _seed_parameters(reset_session, {"otp.max_attempts": "no-numerico"})
    user = _make_reset_user(reset_session)
    _request_otp(reset_client, user.email)
    good = _otp_code(reset_session, user.id)

    for bad_code in ("111111", "333333"):
        probe = reset_client.post(
            "/api/v1/auth/pin-reset",
            json={"email": user.email, "doc_number": DOC_NUMBER, "code": bad_code, "pin": NEW_PIN},
        )
        assert probe.status_code == 401
    rows = _otp_rows(reset_session, user.id)
    assert rows[0].status == "PENDING", "valor invalido -> rige la constante (3)"
    assert int(rows[0].attempts) == 2

    ok = reset_client.post(
        "/api/v1/auth/pin-reset",
        json={"email": user.email, "doc_number": DOC_NUMBER, "code": good, "pin": NEW_PIN},
    )
    assert ok.status_code == 200, ok.text


def test_parameters_lockout_threshold_without_parameters_table_is_default(
    reset_client: TestClient, reset_session: Session
):
    """Sin `config.parameters` rige la constante (`auth.max_failed_attempts=5`)."""
    user = _make_reset_user(reset_session)
    for _ in range(4):
        resp = reset_client.post(
            "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": "000000"}
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"
    locked = reset_client.post(
        "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": "000000"}
    )
    assert locked.status_code == 423
    assert locked.json()["error"]["code"] == "ACCOUNT_LOCKED"


def test_parameters_lockout_threshold_seeded_is_used(
    reset_client: TestClient, reset_session: Session
):
    _seed_parameters(reset_session, {"auth.max_failed_attempts": 2})
    user = _make_reset_user(reset_session)
    first = reset_client.post(
        "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": "000000"}
    )
    assert first.status_code == 401
    locked = reset_client.post(
        "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": "000000"}
    )
    assert locked.status_code == 423, "con max_failed_attempts=2 el 2do fallo bloquea"
    assert locked.json()["error"]["code"] == "ACCOUNT_LOCKED"


def test_parameters_resend_wait_seeded_is_used(reset_session: Session):
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import otp_service

    _seed_parameters(reset_session, {"otp.resend_wait_seconds": 3600})
    user = _make_reset_user(reset_session)
    _, _plain = otp_service.generate_otp(reset_session, user_id=user.id, purpose="ACTIVATION")
    reset_session.commit()
    with pytest.raises(otp_service.OtpResendTooSoonError) as excinfo:
        otp_service.resend_otp(reset_session, user_id=user.id, purpose="ACTIVATION")
    assert excinfo.value.retry_after_seconds > 3000
    assert identity_repo.get_active_otp(reset_session, user.id, "ACTIVATION") is not None


def test_parameters_recovery_verify_window_seeded_is_used(
    reset_client: TestClient, reset_session: Session
):
    # E1-T41 retiro `/auth/recovery/verify`, pero su ventana
    # (`auth.recovery_verify_max_requests`) se conserva: la usa
    # `/auth/pin-reset` via `check_pin_reset_rate_limit`.
    _seed_parameters(reset_session, {"auth.recovery_verify_max_requests": 1})
    user = _make_reset_user(reset_session)
    _request_otp(reset_client, user.email)
    payload = {"email": user.email, "doc_number": DOC_NUMBER, "code": "111111", "pin": NEW_PIN}
    first = reset_client.post("/api/v1/auth/pin-reset", json=payload)
    assert first.status_code == 401
    assert first.json()["error"]["code"] == "INVALID_PIN_RESET"
    limited = reset_client.post("/api/v1/auth/pin-reset", json=payload)
    assert limited.status_code == 429, "con max_requests=1 el 2do pin-reset va a 429"
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


# ---------------------------------------------------------------- CA-03: migracion
def test_migration_contract_email_not_null_at_orm_and_db(reset_session: Session):
    import app.modules.identity.models as _m  # noqa: F401 (registro)

    col = Base.metadata.tables["identity.users"].columns["email"]
    assert col.nullable is False, "ORM User.email debe ser NOT NULL (E1-T34)"
    uq_cols = [
        {c.name for c in con.columns}
        for con in Base.metadata.tables["identity.users"].constraints
        if isinstance(con, sa.UniqueConstraint)
    ]
    assert {"email"} in uq_cols, "el UQ de email se mantiene"

    from app.modules.identity import repository as identity_repo

    with pytest.raises(IntegrityError):
        identity_repo.create_user(
            reset_session,
            doc_type="DNI",
            doc_number_hash="hash-" + uuid.uuid4().hex,
            first_name="Sin",
            last_name="Email",
            email=None,
        )
    reset_session.rollback()


def test_migration_files_chain_and_reversibility():
    from pathlib import Path as _Path

    versions = _Path(__file__).resolve().parents[1] / "migrations" / "versions"
    m18 = (versions / "0018_users_email_not_null.py").read_text(encoding="utf-8")
    assert 'revision = "0018_users_email_not_null"' in m18
    assert 'down_revision = "0017_identity_access_recovery"' in m18
    assert "email IS NULL" in m18, "pre-check defensivo que falla claro ante NULL"
    assert "nullable=False" in m18 and "nullable=True" in m18, "upgrade/downgrade reversibles"
    m19 = (versions / "0019_identity_parameters.py").read_text(encoding="utf-8")
    assert 'revision = "0019_identity_parameters"' in m19
    assert 'down_revision = "0018_users_email_not_null"' in m19
    assert "ON CONFLICT (key) DO NOTHING" in m19, "siembra idempotente"
    for key in (
        "auth.lockout_seconds",
        "auth.max_failed_attempts",
        "otp.resend_wait_seconds",
        "otp.max_attempts",
        "auth.recovery_request_window_seconds",
        "auth.recovery_request_max_requests",
        "auth.recovery_verify_window_seconds",
        "auth.recovery_verify_max_requests",
    ):
        assert f'"{key}"' in m19, f"falta sembrar {key}"
    assert '("otp.ttl_seconds"' not in m19 and '("otp.max_resends"' not in m19, "no duplicar 0001"


# ---------------------------------------------------------------- Sin PII + atomicidad + OpenAPI
def test_no_pii_or_secrets_in_logs_and_recovery_row(
    reset_client: TestClient, reset_session: Session, caplog
):
    user = _make_reset_user(reset_session)
    _request_otp(reset_client, user.email)
    code = _otp_code(reset_session, user.id)
    with caplog.at_level(logging.INFO, logger="app.modules.identity.service.pin_reset"):
        reset_client.post(
            "/api/v1/auth/pin-reset",
            json={"email": user.email, "doc_number": DOC_NUMBER, "code": code, "pin": NEW_PIN},
        )
    assert user.email not in caplog.text, "el email no sale en logs"
    assert DOC_NUMBER not in caplog.text, "el DNI no sale en logs"
    assert code not in caplog.text, "el OTP no sale en logs"
    assert NEW_PIN not in caplog.text, "el PIN no sale en logs"
    for row in _otp_rows(reset_session, user.id):
        assert code not in row.code_hash, "solo el hash se persiste"
    for rec in _recoveries(reset_session, user.id):
        text = str(rec.verification_result)
        assert user.email not in text and code not in text, "resultado sin PII ni OTP"
        assert DOC_NUMBER not in text, "resultado sin DNI"


def test_unexpected_failure_after_flush_rolls_back_everything(
    reset_client: TestClient, reset_session: Session, monkeypatch
):
    import pytest as _pytest

    from app.modules.identity.repository import recovery as recovery_repo

    user = _make_reset_user(reset_session)
    _request_otp(reset_client, user.email)
    code = _otp_code(reset_session, user.id)

    def _boom(*args, **kwargs):
        raise RuntimeError("access_recovery caido")

    monkeypatch.setattr(recovery_repo, "record_access_recovery", _boom)
    with _pytest.raises(RuntimeError):
        reset_client.post(
            "/api/v1/auth/pin-reset",
            json={"email": user.email, "doc_number": DOC_NUMBER, "code": code, "pin": NEW_PIN},
        )
    rows = _otp_rows(reset_session, user.id)
    assert rows[0].status == "PENDING", "el consumo del OTP tambien se revierte"
    assert _sessions(reset_session, user.id) == []
    assert _recoveries(reset_session, user.id) == []
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import pin_login as pin_login_service

    credential = identity_repo.get_credential(reset_session, user.id)
    assert pin_login_service.verify_pin(OLD_PIN, credential.pin_hash), "el PIN anterior sigue"


def test_openapi_exposes_pin_reset_route(reset_client: TestClient):
    spec = reset_client.get("/openapi.json").json()
    assert "/api/v1/auth/pin-reset" in spec["paths"]
    post = spec["paths"]["/api/v1/auth/pin-reset"]["post"]
    assert post["responses"]
