"""Login con PIN, intentos y bloqueo temporal (E1-T14, HU03 CA-02/CA-03).

- Sin Postgres: `TestClient(app)` + override de `get_db` a SQLite en memoria
  con schemas ATTACH (`identity`/`shared`/`notifications`), patron de
  `tests/test_device_login.py` (TestClient) + `tests/test_identity_otp.py`
  (SQLite ATTACH).
- Decision criptografica (ver `service/pin_login.py`): el `.venv` trae
  `PyJWT` pero NO `bcrypt`/`argon2`/`passlib`/`cryptography`, asi que el
  hasher es PBKDF2-HMAC-SHA256 via `hashlib` (stdlib, formato
  `"pbkdf2-sha256$<iter>$<salt_hex>$<hash_hex>"`, verificacion con
  `hmac.compare_digest`). Sin cripto inventada: PBKDF2 estandar.
- Casos: PIN correcto -> JWT + refresh + sesion en BD (+ 1x
  `auth.login_succeeded` en outbox) y resetea el contador; fallo incrementa
  `failed_attempts`; 5to fallo -> `ACCOUNT_LOCKED` + `locked_until` +
  notificacion `login_alert`; login durante el bloqueo (incluso con PIN
  correcto) -> `ACCOUNT_LOCKED`; bloqueo vencido -> exito (desbloqueo por
  tiempo); inexistente vs mal PIN: cuerpo IDENTICO y costo similar (rama
  ciega con hash ficticio); errores sin `locked_until`/contadores.
- Reglas estaticas (servicio sin `commit`, sin KYC/OTP/activation/onboard,
  reutiliza `create_access_token` y las constantes de emision de
  `device_login`; constantes `MAX_FAILED_ATTEMPTS = 5` documentadas como
  candidatas a `config.parameters`).
"""

from __future__ import annotations

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

SERVICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "service"
    / "pin_login.py"
)
API_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "api"
    / "pin_login.py"
)

PIN = "482917"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@pytest.fixture()
def pin_session():
    """Sesion SQLite aislada (`identity`/`shared`/`notifications` via ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.identity.models as _i  # noqa: F401 (registro)
    import app.modules.notifications.models as _n  # noqa: F401 (registro)
    import app.modules.shared.models as _s  # noqa: F401 (registro)

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("identity", "shared", "notifications"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["identity.users"],
            Base.metadata.tables["identity.credentials"],
            Base.metadata.tables["identity.sessions"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["notifications.notifications"],
            Base.metadata.tables["notifications.notification_templates"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def pin_client(pin_session: Session):
    """TestClient con `get_db` a SQLite."""

    def _override():
        yield pin_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _make_pin_user(pin_session: Session, *, pin: str = PIN):
    """Usuario con credencial PBKDF2 (retorna el `User`)."""
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import pin_login as pin_login_service

    suffix = uuid.uuid4().hex[:8]
    user = identity_repo.create_user(
        pin_session,
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Ada",
        last_name="Lovelace",
        email=f"ada.{suffix}@example.com",
        phone="+51999888777",
    )
    identity_repo.create_credential(
        pin_session, user.id, pin_hash=pin_login_service.hash_pin(pin)
    )
    pin_session.commit()
    return user


def _attempts(pin_session: Session, user_id: uuid.UUID) -> int:
    from app.modules.identity import repository as identity_repo

    pin_session.expire_all()
    row = identity_repo.get_credential(pin_session, user_id)
    assert row is not None
    return int(row.failed_attempts)


def _locked_until(pin_session: Session, user_id: uuid.UUID):
    from app.modules.identity import repository as identity_repo

    pin_session.expire_all()
    row = identity_repo.get_credential(pin_session, user_id)
    assert row is not None
    return row.locked_until


def _login_events(pin_session: Session, user_id: uuid.UUID) -> list:
    from app.modules.shared.models import OutboxEntry

    stmt = sa.select(OutboxEntry).where(
        OutboxEntry.event_type == "auth.login_succeeded",
        OutboxEntry.aggregate_id == user_id,
    )
    return list(pin_session.scalars(stmt).all())


def _lock_notifications(pin_session: Session, user_id: uuid.UUID) -> list:
    from app.modules.notifications.models import Notification

    stmt = sa.select(Notification).where(
        Notification.user_id == user_id,
        Notification.template_code == "login_alert",
    )
    return list(pin_session.scalars(stmt).all())


# ---------------------------------------------------------------- Reglas estaticas
def test_static_rules_pin_constants_no_commit_no_forbidden_imports():
    service = SERVICE_PATH.read_text(encoding="utf-8")
    assert "MAX_FAILED_ATTEMPTS = 5" in service, "umbral configurable documentado (5)"
    assert "LOCKOUT_SECONDS" in service, "duracion del bloqueo documentada"
    assert "auth.max_failed_attempts" in service, "candidata a config.parameters"
    assert "pbkdf2_hmac" in service, "hasher PBKDF2 via hashlib (stdlib)"
    assert "compare_digest" in service, "comparacion en tiempo constante"
    assert ".commit(" not in service, "el servicio hace flush; el endpoint confirma"
    assert "float(" not in service, "sin float"
    assert "create_access_token" in service, "reutiliza el JWT existente"
    assert "jwt.encode(" not in service, "no inventa tokens: usa create_access_token"
    assert "_DUMMY_HASH" in service, "rama ciega contra hash ficticio (anti-timing)"
    top_imports = "\n".join(
        line
        for line in service.splitlines()
        if line.startswith("from app.") or line.startswith("import app.")
    )
    for forbidden in ("kyc_proxy", "otp_service", "activation", "onboard_customer"):
        assert forbidden not in top_imports, f"prohibido tocar {forbidden}"
    assert "device_login" in top_imports, "reutiliza la emision de device_login"
    assert "from app.core.outbox import" in service, "evento solo via outbox perezoso"
    assert "login_alert" in service, "notificacion de bloqueo login_alert"
    log_lines = [line for line in service.splitlines() if "logger." in line]
    assert log_lines, "el servicio debe loguear sin PII"
    for line in log_lines:
        lowered = line.lower()
        assert "pin" not in lowered or "pin_login" in lowered, (
            f"el PIN podria salir en logs: {line.strip()}"
        )

    api = API_PATH.read_text(encoding="utf-8")
    assert "INVALID_CREDENTIALS" in api and "ACCOUNT_LOCKED" in api
    assert ".commit(" in api, "el endpoint confirma exito y fallos (el contador persiste)"
    assert ".rollback(" not in api, (
        "sin rollback ante error de negocio: el intento fallido debe persistir "
        "(desviacion documentada de activation/device_login)"
    )


def test_hash_format_and_constant_time_verify_unit():
    from app.modules.identity.service import pin_login as pin_login_service

    stored = pin_login_service.hash_pin(PIN)
    assert stored.startswith("pbkdf2-sha256$"), "formato con prefijo autodetectable"
    assert stored != PIN and PIN not in stored, "nunca el PIN en claro"
    assert pin_login_service.verify_pin(PIN, stored) is True
    assert pin_login_service.verify_pin("000000", stored) is False
    assert pin_login_service.verify_pin(PIN, "basura") is False
    assert pin_login_service.verify_pin(PIN, "argon2:otro-formato") is False
    assert pin_login_service.verify_pin("", stored) is False


# ---------------------------------------------------------------- Camino feliz
def test_pin_success_returns_tokens_and_resets_counter(
    pin_client: TestClient, pin_session: Session
):
    import hashlib

    from app.core.security import decode_token

    user = _make_pin_user(pin_session)
    from app.modules.identity import repository as identity_repo

    pin_session.expire_all()
    row = identity_repo.get_credential(pin_session, user.id)
    assert row is not None
    row.failed_attempts = 2
    pin_session.commit()

    resp = pin_client.post(
        "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": PIN}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"data", "meta"}
    data = body["data"]
    assert set(data) == {
        "access_token",
        "refresh_token",
        "token_type",
        "session_id",
        "expires_in",
    }
    assert data["token_type"] == "Bearer"
    assert data["expires_in"] > 0
    assert body["meta"]["request_id"]
    assert PIN not in resp.text, "el PIN nunca se refleja"

    claims = decode_token(data["access_token"])
    assert claims["sub"] == str(user.id)

    pin_session.expire_all()
    row = identity_repo.get_session_by_refresh_hash(
        pin_session, hashlib.sha256(data["refresh_token"].encode()).hexdigest()
    )
    assert row is not None, "la sesion queda persistida (hash del refresh)"
    assert str(row.id) == data["session_id"]
    assert row.user_id == user.id
    assert row.revoked_at is None

    assert _attempts(pin_session, user.id) == 0, "exito resetea el contador"
    assert _locked_until(pin_session, user.id) is None

    events = _login_events(pin_session, user.id)
    assert len(events) == 1, "1x auth.login_succeeded via outbox"
    assert events[0].status == "PENDING"
    assert events[0].payload["session_id"] == data["session_id"]


# ---------------------------------------------------------------- Fallos y bloqueo
def test_wrong_pin_increments_counter(pin_client: TestClient, pin_session: Session):
    user = _make_pin_user(pin_session)
    for expected in (1, 2):
        resp = pin_client.post(
            "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": "000000"}
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"
        assert _attempts(pin_session, user.id) == expected
    assert "locked_until" not in resp.text
    assert "failed_attempts" not in resp.text


def test_fifth_failure_locks_and_notifies(
    pin_client: TestClient, pin_session: Session
):
    user = _make_pin_user(pin_session)
    last = None
    for _ in range(5):
        last = pin_client.post(
            "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": "000000"}
        )
    assert last is not None
    assert last.status_code == 423
    assert last.json()["error"]["code"] == "ACCOUNT_LOCKED"
    assert _attempts(pin_session, user.id) == 5
    locked = _locked_until(pin_session, user.id)
    assert locked is not None, "el 5to fallo fija locked_until"
    assert "locked_until" not in last.text, "sin precision quirurgica en el cuerpo"

    notes = _lock_notifications(pin_session, user.id)
    assert len(notes) >= 1, "bloqueo notifica login_alert best-effort"


def test_locked_login_rejected_even_with_correct_pin(
    pin_client: TestClient, pin_session: Session
):
    user = _make_pin_user(pin_session)
    for _ in range(5):
        pin_client.post(
            "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": "000000"}
        )
    resp = pin_client.post(
        "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": PIN}
    )
    assert resp.status_code == 423
    assert resp.json()["error"]["code"] == "ACCOUNT_LOCKED"


def test_time_unlock_allows_login_again(
    pin_client: TestClient, pin_session: Session
):
    from app.modules.identity import repository as identity_repo

    user = _make_pin_user(pin_session)
    for _ in range(5):
        pin_client.post(
            "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": "000000"}
        )
    assert _locked_until(pin_session, user.id) is not None

    # Viaja el reloj: el bloqueo ya vencio (desbloqueo automatico por tiempo).
    pin_session.expire_all()
    row = identity_repo.get_credential(pin_session, user.id)
    assert row is not None
    row.locked_until = _utcnow() - timedelta(seconds=60)
    pin_session.commit()

    resp = pin_client.post(
        "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": PIN}
    )
    assert resp.status_code == 200, resp.text
    assert _attempts(pin_session, user.id) == 0
    assert _locked_until(pin_session, user.id) is None


# ---------------------------------------------------------------- No filtracion
def test_unknown_user_matches_wrong_pin_body(
    pin_client: TestClient, pin_session: Session
):
    user = _make_pin_user(pin_session)
    headers = {"X-Request-Id": "probe-pin"}
    wrong = pin_client.post(
        "/api/v1/auth/login/pin",
        json={"user_ref": str(user.id), "pin": "000000"},
        headers=headers,
    )
    unknown = pin_client.post(
        "/api/v1/auth/login/pin",
        json={"user_ref": str(uuid.uuid4()), "pin": "000000"},
        headers=headers,
    )
    malformed = pin_client.post(
        "/api/v1/auth/login/pin",
        json={"user_ref": "no-es-uuid", "pin": "000000"},
        headers=headers,
    )
    assert wrong.status_code == 401
    assert unknown.status_code == 401
    assert malformed.status_code == 401
    assert wrong.json() == unknown.json() == malformed.json(), (
        "sin distinguir inexistente/malformado de PIN erroneo"
    )
    assert wrong.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_unknown_vs_wrong_pin_timing_similar(
    pin_client: TestClient, pin_session: Session
):
    user = _make_pin_user(pin_session)
    ghost = str(uuid.uuid4())

    def _elapsed(payload: dict) -> float:
        start = time.perf_counter()
        resp = pin_client.post("/api/v1/auth/login/pin", json=payload)
        assert resp.status_code == 401
        return time.perf_counter() - start

    wrong = [_elapsed({"user_ref": str(user.id), "pin": "000000"}) for _ in range(3)]
    unknown = [_elapsed({"user_ref": ghost, "pin": "000000"}) for _ in range(3)]
    mean_wrong = sum(wrong) / len(wrong)
    mean_unknown = sum(unknown) / len(unknown)
    assert mean_wrong > 0 and mean_unknown > 0
    ratio = max(mean_wrong, mean_unknown) / min(mean_wrong, mean_unknown)
    assert ratio < 4, f"timing disparejo (ratio {ratio:.2f}): filtraria existencia"


# ---------------------------------------------------------------- Contrato
def test_openapi_includes_pin_path(pin_client: TestClient):
    spec = pin_client.get("/openapi.json").json()
    assert "/api/v1/auth/login/pin" in spec["paths"]
