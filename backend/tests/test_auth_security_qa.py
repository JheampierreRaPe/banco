"""QA de autenticacion y sesion (E1-T18, HU03 CA-01..CA-04).

Suite de seguridad backend (solo `backend/tests/`, sin migraciones, sin
`kyc-service/`):

- Fuerza bruta / bloqueo (CA-03): 5 intentos -> `423 ACCOUNT_LOCKED` +
  `locked_until` + notificacion `login_alert`; el bloqueo gana al PIN
  correcto; desbloqueo automatico por tiempo; errores sin enumeracion ni
  contadores (`INVALID_CREDENTIALS` identico para inexistente/malformado).
- PIN y biometria (CA-02): el login PIN expone `biometric_enabled` real
  (no constante); el facial exige consentimiento (`400 INVALID_LOGIN`
  generico sin el); firma valida abre sesion; nonce de un solo uso
  (reuso -> `INVALID_LOGIN`); nonce vencido -> `EXPIRED_NONCE`; firma
  invalida indistinguible de usuario inexistente.
- Sesiones (CA-04): refresh valido rota (viejo muere, nuevo sirve);
  refresh desconocido -> `INVALID_REFRESH`; vencido -> `REFRESH_EXPIRED`
  y cierra la sesion; reuso de revocado -> `REFRESH_REUSED` y revoca
  TODA la cadena; logout revoca (idempotente); inactividad excedida ->
  `SESSION_INACTIVE`; el refresh solo se persiste hasheado (SHA-256).
- Rendimiento (CA-01): login PIN correcto < 1.5 s.
- Higiene: PIN/refresh/firma/nonce jamas en logs ni en auditoria; el
  refresh solo sale en su campo de la respuesta.

Patron: `TestClient(app)` + override de `get_db` a SQLite en memoria
con schemas ATTACH (`identity`/`shared`/`audit`/`notifications`), como
`tests/test_pin_login.py` + `tests/test_sessions.py` +
`tests/test_device_login.py`. Sin PII/secretos en logs.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
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

PIN = "482917"
WRONG_PIN = "000000"

PIN_SERVICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "service"
    / "pin_login.py"
)
SESSIONS_SERVICE_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "service" / "sessions.py"
)
DEVICE_SERVICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "service"
    / "device_login.py"
)
NONCE_DOMAIN_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "domain" / "nonce.py"
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sign_hmac(secret_hex: str, nonce: str) -> str:
    secret = bytes.fromhex(secret_hex)
    return hmac.new(secret, nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def _hash_refresh(refresh: str) -> str:
    return hashlib.sha256(refresh.encode()).hexdigest()


@pytest.fixture()
def qa_session():
    """SQLite aislada (`identity`/`shared`/`audit`/`notifications` via ATTACH)."""
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
        for schema in ("identity", "shared", "audit", "notifications"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["identity.users"],
            Base.metadata.tables["identity.credentials"],
            Base.metadata.tables["identity.device_bindings"],
            Base.metadata.tables["identity.sessions"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["audit.audit_log"],
            Base.metadata.tables["notifications.notifications"],
            Base.metadata.tables["notifications.notification_templates"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        from app.modules.identity.domain import nonce as nonce_domain

        nonce_domain.reset_nonces()
        yield session
    finally:
        try:
            from app.modules.identity.domain import nonce as nonce_domain

            nonce_domain.reset_nonces()
        finally:
            session.close()
            engine.dispose()


@pytest.fixture()
def qa_client(qa_session: Session):
    """TestClient con `get_db` a SQLite."""

    def _override():
        yield qa_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _make_pin_user(qa_session: Session, *, pin: str = PIN):
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import pin_login as pin_login_service

    suffix = uuid.uuid4().hex[:8]
    user = identity_repo.create_user(
        qa_session,
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Ada",
        last_name="Lovelace",
        email=f"qa.{suffix}@example.com",
        phone="+51999888777",
    )
    identity_repo.create_credential(qa_session, user.id, pin_hash=pin_login_service.hash_pin(pin))
    qa_session.commit()
    return user


def _make_enrolled_user(
    qa_session: Session, *, device_id: str = "pixel-8-pro", consent: bool = True
):
    from app.modules.identity import repository as identity_repo

    suffix = uuid.uuid4().hex[:8]
    user = identity_repo.create_user(
        qa_session,
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Ada",
        last_name="Lovelace",
        email=f"qa.{suffix}@example.com",
        phone="+51999888777",
    )
    credential = identity_repo.create_credential(qa_session, user.id)
    if consent:
        credential.biometric_enabled = True
        qa_session.flush()
    secret_hex = secrets.token_hex(24)
    identity_repo.register_binding(
        qa_session,
        user.id,
        device_id,
        f"hmac:{secret_hex}",
        platform="android",
        biometric_type="FACE",
    )
    qa_session.commit()
    return user, secret_hex


def _challenge(client: TestClient, user_ref: str, device_id: str = "pixel-8-pro") -> dict:
    resp = client.post(
        "/api/v1/auth/login/challenge",
        json={"user_ref": user_ref, "device_id": device_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _open_session(
    qa_session: Session,
    user_id: uuid.UUID,
    *,
    ttl_seconds: int = 3600,
    created_ago_seconds: int = 0,
    device_id: str = "pixel-8-pro",
) -> str:
    from app.modules.identity import repository as identity_repo

    refresh = secrets.token_urlsafe(32)
    moment = _utcnow()
    row = identity_repo.create_session(
        qa_session,
        user_id,
        identity_repo.hash_refresh_token(refresh),
        moment + timedelta(seconds=ttl_seconds),
        device_id=device_id,
    )
    qa_session.commit()
    if created_ago_seconds:
        row.created_at = moment - timedelta(seconds=created_ago_seconds)
        qa_session.commit()
    qa_session.expire_all()
    return refresh


def _audit_dump(qa_session: Session) -> str:
    import json

    from app.modules.audit.models import AuditLog

    rows = list(qa_session.scalars(sa.select(AuditLog).order_by(AuditLog.seq.asc())).all())
    return "\n".join(
        json.dumps(
            {"action": r.action, "after": r.after_json, "before": r.before_json},
            sort_keys=True,
            default=str,
        )
        for r in rows
    )


# ---------------------------------------------------------------- Reglas estaticas
def test_qa_static_security_constants():
    pin_service = PIN_SERVICE_PATH.read_text(encoding="utf-8")
    assert "MAX_FAILED_ATTEMPTS = 5" in pin_service, "bloqueo tras 5 intentos"
    assert "LOCKOUT_SECONDS" in pin_service, "duracion del bloqueo documentada"
    assert ".commit(" not in pin_service, "flush sin commit en pin_login"
    assert "compare_digest" in pin_service, "comparacion en tiempo constante"
    assert "float(" not in pin_service, "sin float"

    sessions_service = SESSIONS_SERVICE_PATH.read_text(encoding="utf-8")
    assert "INACTIVITY_SECONDS = 180" in sessions_service, "inactividad 3 min"
    assert ".commit(" not in sessions_service, "flush sin commit en sessions"
    assert "hash_refresh_token" in sessions_service, "solo hash del refresh"
    assert "float(" not in sessions_service, "sin float"

    nonce_domain = NONCE_DOMAIN_PATH.read_text(encoding="utf-8")
    assert "NONCE_TTL_SECONDS = 120" in nonce_domain, "nonce con TTL corto"
    assert "used = True" in nonce_domain or "entry.used = True" in nonce_domain

    device_service = DEVICE_SERVICE_PATH.read_text(encoding="utf-8")
    assert "biometric_enabled" in device_service, "facial exige consentimiento"
    for content, name in (
        (pin_service, "pin_login"),
        (sessions_service, "sessions"),
        (device_service, "device_login"),
    ):
        for line in content.splitlines():
            if "logger." in line:
                lowered = line.lower()
                assert "refresh_token" not in lowered, f"secreto en logs ({name})"
                assert "signature" not in lowered, f"secreto en logs ({name})"


# ---------------------------------------------------------------- Fuerza bruta / bloqueo (CA-03)
def test_qa_bruteforce_five_attempts_trigger_lockout(qa_client: TestClient, qa_session: Session):
    from app.modules.identity import repository as identity_repo
    from app.modules.notifications.models import Notification

    user = _make_pin_user(qa_session)
    last = None
    for _ in range(4):
        resp = qa_client.post(
            "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": WRONG_PIN}
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"
    last = qa_client.post(
        "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": WRONG_PIN}
    )
    assert last.status_code == 423, last.text
    assert last.json()["error"]["code"] == "ACCOUNT_LOCKED"

    qa_session.expire_all()
    row = identity_repo.get_credential(qa_session, user.id)
    assert row is not None
    assert int(row.failed_attempts) == 5, "el 5to fallo fija el contador"
    assert row.locked_until is not None, "el 5to fallo fija locked_until"
    assert (
        PIN not in last.text
        and WRONG_PIN not in last.text.replace('"pin":"000000"', "")
        or PIN not in last.text
    )
    assert "locked_until" not in last.text, "sin precision quirurgica"
    assert "failed_attempts" not in last.text, "sin contadores en el cuerpo"

    notes = list(
        qa_session.scalars(
            sa.select(Notification).where(
                Notification.user_id == user.id,
                Notification.template_code == "login_alert",
            )
        ).all()
    )
    assert len(notes) >= 1, "el bloqueo notifica login_alert"


def test_qa_lockout_rejects_correct_pin_and_time_unlock(qa_client: TestClient, qa_session: Session):
    from app.modules.identity import repository as identity_repo

    user = _make_pin_user(qa_session)
    for _ in range(5):
        qa_client.post("/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": WRONG_PIN})
    locked = qa_client.post("/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": PIN})
    assert locked.status_code == 423, "el bloqueo gana al PIN correcto"
    assert locked.json()["error"]["code"] == "ACCOUNT_LOCKED"

    qa_session.expire_all()
    row = identity_repo.get_credential(qa_session, user.id)
    assert row is not None
    row.locked_until = _utcnow() - timedelta(seconds=60)
    qa_session.commit()

    ok_resp = qa_client.post("/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": PIN})
    assert ok_resp.status_code == 200, ok_resp.text
    qa_session.expire_all()
    row = identity_repo.get_credential(qa_session, user.id)
    assert row is not None
    assert int(row.failed_attempts) == 0, "el exito resetea el contador"
    assert row.locked_until is None, "desbloqueo automatico por tiempo"


def test_qa_pin_errors_do_not_enumerate_or_leak(qa_client: TestClient, qa_session: Session):
    user = _make_pin_user(qa_session)
    headers = {"X-Request-Id": "qa-probe"}
    wrong = qa_client.post(
        "/api/v1/auth/login/pin",
        json={"user_ref": str(user.id), "pin": WRONG_PIN},
        headers=headers,
    )
    unknown = qa_client.post(
        "/api/v1/auth/login/pin",
        json={"user_ref": str(uuid.uuid4()), "pin": WRONG_PIN},
        headers=headers,
    )
    malformed = qa_client.post(
        "/api/v1/auth/login/pin",
        json={"user_ref": "no-es-uuid", "pin": WRONG_PIN},
        headers=headers,
    )
    assert wrong.status_code == 401
    assert wrong.json() == unknown.json() == malformed.json()
    assert wrong.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert PIN not in wrong.text, "el PIN nunca se refleja"


def test_qa_pin_login_latency_under_threshold(qa_client: TestClient, qa_session: Session):
    user = _make_pin_user(qa_session)
    start = time.perf_counter()
    resp = qa_client.post("/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": PIN})
    elapsed = time.perf_counter() - start
    assert resp.status_code == 200, resp.text
    assert elapsed < 1.5, f"login PIN supero CA-01 (1.5 s): {elapsed:.3f} s"


# ---------------------------------------------------------------- PIN y biometria (CA-02)
def test_qa_pin_exposes_biometric_flag_from_db(qa_client: TestClient, qa_session: Session):
    from app.modules.identity import repository as identity_repo

    user = _make_pin_user(qa_session)
    first = qa_client.post("/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": PIN})
    assert first.status_code == 200, first.text
    assert first.json()["data"]["biometric_enabled"] is False

    qa_session.expire_all()
    row = identity_repo.get_credential(qa_session, user.id)
    assert row is not None
    row.biometric_enabled = True
    qa_session.commit()

    second = qa_client.post("/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": PIN})
    assert second.status_code == 200, second.text
    assert second.json()["data"]["biometric_enabled"] is True, "flag real, no constante"


def test_qa_facial_requires_biometric_consent(qa_client: TestClient, qa_session: Session):
    from app.modules.identity.models import UserSession

    user, secret_hex = _make_enrolled_user(qa_session, consent=False)
    challenge = _challenge(qa_client, str(user.id))
    denied = qa_client.post(
        "/api/v1/auth/login/facial",
        json={
            "nonce": challenge["nonce"],
            "device_id": "pixel-8-pro",
            "signature": _sign_hmac(secret_hex, challenge["nonce"]),
        },
    )
    assert denied.status_code == 400
    assert denied.json()["error"]["code"] == "INVALID_LOGIN", "sin consentimiento"
    qa_session.expire_all()
    rows = list(
        qa_session.scalars(sa.select(UserSession).where(UserSession.user_id == user.id)).all()
    )
    assert rows == [], "sin consentimiento no se abre sesion"


def test_qa_facial_success_and_single_use_nonce(qa_client: TestClient, qa_session: Session):
    from app.core.security import decode_token

    user, secret_hex = _make_enrolled_user(qa_session, consent=True)
    challenge = _challenge(qa_client, str(user.id))
    payload = {
        "nonce": challenge["nonce"],
        "device_id": "pixel-8-pro",
        "signature": _sign_hmac(secret_hex, challenge["nonce"]),
    }
    first = qa_client.post("/api/v1/auth/login/facial", json=payload)
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert decode_token(data["access_token"])["sub"] == str(user.id)

    reuse = qa_client.post("/api/v1/auth/login/facial", json=payload)
    assert reuse.status_code == 400, "el nonce es de un solo uso"
    assert reuse.json()["error"]["code"] == "INVALID_LOGIN"


def test_qa_nonce_expired_and_unknown_rejected(qa_client: TestClient, qa_session: Session):
    from app.modules.identity.domain import nonce as nonce_domain

    # Unidad: TTL agotado y reuso a nivel dominio.
    uid = uuid.uuid4()
    nonce, _ = nonce_domain.issue_nonce(uid, device_id="pixel-8-pro", ttl_seconds=60)
    nonce_domain.consume_nonce(nonce)
    try:
        nonce_domain.consume_nonce(nonce)
        raise AssertionError("el reuso debio fallar")
    except nonce_domain.NonceReuseError:
        pass
    short_nonce, _ = nonce_domain.issue_nonce(uid, device_id="pixel-8-pro", ttl_seconds=1)
    future = _utcnow() + timedelta(seconds=120)
    try:
        nonce_domain.consume_nonce(short_nonce, now=future)
        raise AssertionError("el nonce vencido debio fallar")
    except nonce_domain.NonceExpiredError:
        pass

    # API: nonce vencido -> EXPIRED_NONCE.
    user, secret_hex = _make_enrolled_user(qa_session, consent=True)
    challenge = _challenge(qa_client, str(user.id))
    entry = nonce_domain._NONCES[challenge["nonce"]]
    entry.expires_at = _utcnow() - timedelta(seconds=1)
    expired = qa_client.post(
        "/api/v1/auth/login/facial",
        json={
            "nonce": challenge["nonce"],
            "device_id": "pixel-8-pro",
            "signature": _sign_hmac(secret_hex, challenge["nonce"]),
        },
    )
    assert expired.status_code == 400
    assert expired.json()["error"]["code"] == "EXPIRED_NONCE"


def test_qa_facial_errors_do_not_enumerate(qa_client: TestClient, qa_session: Session):
    from app.modules.identity.domain import nonce as nonce_domain

    user, secret_hex = _make_enrolled_user(qa_session, consent=True)
    challenge = _challenge(qa_client, str(user.id))
    headers = {"X-Request-Id": "qa-facial"}
    bad_sig = qa_client.post(
        "/api/v1/auth/login/facial",
        json={
            "nonce": challenge["nonce"],
            "device_id": "pixel-8-pro",
            "signature": "00" * 32,
        },
        headers=headers,
    )
    ghost_nonce, _ = nonce_domain.issue_nonce(uuid.uuid4(), device_id="pixel-8-pro")
    unknown = qa_client.post(
        "/api/v1/auth/login/facial",
        json={
            "nonce": ghost_nonce,
            "device_id": "pixel-8-pro",
            "signature": _sign_hmac(secret_hex, ghost_nonce),
        },
        headers=headers,
    )
    assert bad_sig.status_code == 400 and unknown.status_code == 400
    assert bad_sig.json() == unknown.json(), "firma invalida == inexistente"
    assert bad_sig.json()["error"]["code"] == "INVALID_LOGIN"


# ---------------------------------------------------------------- Sesiones (CA-04)
def test_qa_refresh_rotates_and_old_dies(qa_client: TestClient, qa_session: Session):
    from app.modules.identity import repository as identity_repo

    user = _make_pin_user(qa_session)
    old_refresh = _open_session(qa_session, user.id)
    resp = qa_client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert resp.status_code == 200, resp.text
    new_refresh = resp.json()["data"]["refresh_token"]
    assert new_refresh != old_refresh

    qa_session.expire_all()
    old_row = identity_repo.get_session_by_refresh_hash(qa_session, _hash_refresh(old_refresh))
    assert old_row is not None and old_row.revoked_at is not None, "el presentado muere"
    new_row = identity_repo.get_session_by_refresh_hash(qa_session, _hash_refresh(new_refresh))
    assert new_row is not None and new_row.revoked_at is None, "el nuevo esta vigente"

    second = qa_client.post("/api/v1/auth/refresh", json={"refresh_token": new_refresh})
    assert second.status_code == 200, second.text


def test_qa_refresh_invalid_and_expired(qa_client: TestClient, qa_session: Session):
    from app.modules.identity import repository as identity_repo

    user = _make_pin_user(qa_session)
    unknown = qa_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": secrets.token_urlsafe(32)}
    )
    assert unknown.status_code == 401
    assert unknown.json()["error"]["code"] == "INVALID_REFRESH"

    expired_refresh = _open_session(qa_session, user.id, ttl_seconds=-60)
    expired = qa_client.post("/api/v1/auth/refresh", json={"refresh_token": expired_refresh})
    assert expired.status_code == 401
    assert expired.json()["error"]["code"] == "REFRESH_EXPIRED"
    qa_session.expire_all()
    row = identity_repo.get_session_by_refresh_hash(qa_session, _hash_refresh(expired_refresh))
    assert row is not None and row.revoked_at is not None, "vencido cierra la sesion"


def test_qa_refresh_reuse_revokes_chain(qa_client: TestClient, qa_session: Session):
    from app.modules.identity import repository as identity_repo

    user = _make_pin_user(qa_session)
    old_refresh = _open_session(qa_session, user.id)
    first = qa_client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert first.status_code == 200, first.text
    successor = first.json()["data"]["refresh_token"]

    reuse = qa_client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert reuse.status_code == 401
    assert reuse.json()["error"]["code"] == "REFRESH_REUSED"

    qa_session.expire_all()
    dead = identity_repo.get_session_by_refresh_hash(qa_session, _hash_refresh(successor))
    assert dead is not None and dead.revoked_at is not None, "el sucesor tambien muere"

    after = qa_client.post("/api/v1/auth/refresh", json={"refresh_token": successor})
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "REFRESH_REUSED"


def test_qa_logout_revokes_and_idempotent(qa_client: TestClient, qa_session: Session):
    from app.modules.identity import repository as identity_repo

    user = _make_pin_user(qa_session)
    refresh = _open_session(qa_session, user.id)

    unknown = qa_client.post(
        "/api/v1/auth/logout", json={"refresh_token": secrets.token_urlsafe(32)}
    )
    assert unknown.status_code == 200, "logout idempotente ante desconocido"
    assert unknown.json()["data"]["revoked"] is False

    first = qa_client.post("/api/v1/auth/logout", json={"refresh_token": refresh})
    assert first.status_code == 200, first.text
    assert first.json()["data"]["revoked"] is True
    qa_session.expire_all()
    row = identity_repo.get_session_by_refresh_hash(qa_session, _hash_refresh(refresh))
    assert row is not None and row.revoked_at is not None, "logout revoca"

    denied = qa_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert denied.status_code == 401, "refresh posterior al logout se rechaza"

    second = qa_client.post("/api/v1/auth/logout", json={"refresh_token": refresh})
    assert second.status_code == 200, "doble logout no falla"
    assert second.json()["data"]["revoked"] is False


def test_qa_inactivity_closes_session(qa_client: TestClient, qa_session: Session):
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import sessions as sessions_service

    user = _make_pin_user(qa_session)
    idle_refresh = _open_session(
        qa_session, user.id, created_ago_seconds=sessions_service.INACTIVITY_SECONDS + 60
    )
    idle = qa_client.post("/api/v1/auth/refresh", json={"refresh_token": idle_refresh})
    assert idle.status_code == 401
    assert idle.json()["error"]["code"] == "SESSION_INACTIVE"
    qa_session.expire_all()
    row = identity_repo.get_session_by_refresh_hash(qa_session, _hash_refresh(idle_refresh))
    assert row is not None and row.revoked_at is not None, "inactividad cierra la sesion"

    fresh_refresh = _open_session(
        qa_session, user.id, created_ago_seconds=sessions_service.INACTIVITY_SECONDS - 60
    )
    fresh = qa_client.post("/api/v1/auth/refresh", json={"refresh_token": fresh_refresh})
    assert fresh.status_code == 200, fresh.text


def test_qa_refresh_never_stored_in_clear_and_not_in_audit_or_logs(
    qa_client: TestClient, qa_session: Session, caplog
):
    from app.modules.identity.models import UserSession

    user = _make_pin_user(qa_session)
    refresh = _open_session(qa_session, user.id)
    with caplog.at_level(logging.DEBUG):
        resp = qa_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200, resp.text
    assert refresh not in caplog.text, "el refresh en claro jamas va a logs"

    qa_session.expire_all()
    rows = list(
        qa_session.scalars(sa.select(UserSession).where(UserSession.user_id == user.id)).all()
    )
    assert rows, "hay sesiones"
    for row in rows:
        assert row.refresh_token_hash != refresh, "solo el hash se persiste"
        assert len(row.refresh_token_hash) == 64, "SHA-256 hex"

    dump = _audit_dump(qa_session)
    assert refresh not in dump, "el refresh en claro jamas va a auditoria"
    assert PIN not in dump, "el PIN en claro jamas va a auditoria"
    assert "pbkdf2" not in dump, "ni el hash del PIN va a auditoria"
