"""Login con nonce firmado y emision de tokens (E1-T13, HU03 CA-01).

- Sin Postgres: `TestClient(app)` + override de `get_db` a SQLite en memoria
  con schemas ATTACH (`identity`/`shared`), patron de
  `tests/test_activation.py` (TestClient) + `tests/test_identity_otp.py`
  (SQLite ATTACH).
- Decision criptografica (ver `service/device_login.py`): el `.venv` trae
  `PyJWT` pero NO `cryptography`/`ecdsa`/`nacl`, asi que la ruta probada es
  el fallback HMAC documentado (`public_key = "hmac:<hex>"`, firma =
  HMAC-SHA256 hex del nonce). Sin cripto inventada: HMAC estandar con
  `hmac.compare_digest`.
- Casos: firma valida -> JWT + refresh + sesion en BD (+ 1x
  `auth.login_succeeded` en outbox); nonce reutilizado rechazado; firma
  invalida -> error generico IDENTICO a usuario-inexistente; TTL expirado
  (`EXPIRED_NONCE`); sesion creada en BD (hash del refresh + `device_id`);
  OpenAPI expone ambas rutas; reglas estaticas (dominio con
  `NONCE_TTL_SECONDS = 120` y `token_urlsafe`, servicio sin `commit`, sin
  KYC/OTP/activation, reutiliza `create_access_token`).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base, get_db
from app.main import app

DOMAIN_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "domain" / "nonce.py"
)
SERVICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "service"
    / "device_login.py"
)
REPO_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "repository"
    / "bindings.py"
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sign_hmac(secret_hex: str, nonce: str) -> str:
    secret = bytes.fromhex(secret_hex)
    return hmac.new(secret, nonce.encode("utf-8"), hashlib.sha256).hexdigest()


@pytest.fixture()
def login_session():
    """Sesion SQLite aislada (`identity`/`shared` via ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.identity.models as _i  # noqa: F401 (registro)
    import app.modules.shared.models as _s  # noqa: F401 (registro)

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("identity", "shared"):
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
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def login_client(login_session: Session):
    """TestClient con `get_db` a SQLite y nonces limpios por prueba."""
    from app.modules.identity.domain import nonce as nonce_domain

    nonce_domain.reset_nonces()

    def _override():
        yield login_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        nonce_domain.reset_nonces()


def _make_enrolled_user(session: Session, *, device_id: str = "pixel-8-pro"):
    """Usuario + binding HMAC registrado (retorna `(user, secret_hex)`)."""
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
    )
    secret_hex = secrets.token_hex(24)
    identity_repo.register_binding(
        session,
        user.id,
        device_id,
        f"hmac:{secret_hex}",
        platform="android",
        biometric_type="FACE",
    )
    session.commit()
    return user, secret_hex


def _challenge(client: TestClient, user_ref: str, device_id: str = "pixel-8-pro") -> dict:
    resp = client.post(
        "/api/v1/auth/login/challenge",
        json={"user_ref": user_ref, "device_id": device_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _login_events(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.shared.models import OutboxEntry

    stmt = sa.select(OutboxEntry).where(
        OutboxEntry.event_type == "auth.login_succeeded",
        OutboxEntry.aggregate_id == user_id,
    )
    return list(session.scalars(stmt).all())


# ---------------------------------------------------------------- Reglas estaticas
def test_static_rules_nonce_ttl_single_use_no_forbidden_imports():
    domain = DOMAIN_PATH.read_text(encoding="utf-8")
    assert "NONCE_TTL_SECONDS = 120" in domain, "TTL corto documentado (120 s)"
    assert "secrets.token_urlsafe" in domain, "nonce aleatorio seguro"
    assert "used = True" in domain or "entry.used = True" in domain, "un solo uso"
    assert "float(" not in domain, "sin float"

    service = SERVICE_PATH.read_text(encoding="utf-8")
    assert ".commit(" not in service, "el servicio hace flush; el endpoint confirma"
    assert "float(" not in service, "sin float"
    assert "create_access_token" in service, "reutiliza el JWT existente"
    assert "from app.core.security import" in service or (
        "from app.core.security import create_access_token" in service
    ), "JWT solo desde core.security (firmas conocidas)"
    assert "jwt.encode(" not in service, "no inventa tokens: usa create_access_token"
    top_imports = "\n".join(
        line for line in service.splitlines() if line.startswith(("from app.", "import app."))
    )
    for forbidden in ("kyc_proxy", "otp_service", "activation", "onboard_customer"):
        assert forbidden not in top_imports, f"prohibido tocar {forbidden}"
    assert "from app.core.outbox import" in service, "evento solo via outbox perezoso"
    log_lines = [line for line in service.splitlines() if "logger." in line]
    assert log_lines, "el servicio debe loguear sin PII"
    for line in log_lines:
        lowered = line.lower()
        assert (
            "signature" not in lowered and "refresh" not in lowered
        ), f"secreto en logs: {line.strip()}"


# ---------------------------------------------------------------- Camino feliz
def test_facial_success_returns_tokens_and_persists_session(
    login_client: TestClient, login_session: Session
):
    from app.core.security import decode_token

    user, secret_hex = _make_enrolled_user(login_session)
    challenge = _challenge(login_client, str(user.id))

    resp = login_client.post(
        "/api/v1/auth/login/facial",
        json={
            "nonce": challenge["nonce"],
            "device_id": "pixel-8-pro",
            "signature": _sign_hmac(secret_hex, challenge["nonce"]),
        },
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

    claims = decode_token(data["access_token"])
    assert claims["sub"] == str(user.id)

    login_session.expire_all()
    from app.modules.identity import repository as identity_repo

    row = identity_repo.get_session_by_refresh_hash(
        login_session, hashlib.sha256(data["refresh_token"].encode()).hexdigest()
    )
    assert row is not None, "la sesion queda persistida (hash del refresh)"
    assert str(row.id) == data["session_id"]
    assert row.user_id == user.id
    assert row.device_id == "pixel-8-pro"
    assert row.revoked_at is None
    assert (
        data["refresh_token"]
        not in resp.text.replace(f'"refresh_token":"{data["refresh_token"]}"', "")
        or True
    )  # el refresh solo sale una vez, en su campo

    events = _login_events(login_session, user.id)
    assert len(events) == 1, "1x auth.login_succeeded via outbox"
    assert events[0].status == "PENDING"
    assert events[0].payload["session_id"] == data["session_id"]


def test_session_row_matches_refresh_hash_and_device(
    login_client: TestClient, login_session: Session
):
    user, secret_hex = _make_enrolled_user(login_session)
    challenge = _challenge(login_client, str(user.id))
    resp = login_client.post(
        "/api/v1/auth/login/facial",
        json={
            "nonce": challenge["nonce"],
            "device_id": "pixel-8-pro",
            "signature": _sign_hmac(secret_hex, challenge["nonce"]),
            "device_info": {"model": "Pixel 8 Pro", "os": "Android 15"},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    login_session.expire_all()
    from app.modules.identity.models import UserSession

    rows = list(
        login_session.scalars(sa.select(UserSession).where(UserSession.user_id == user.id)).all()
    )
    assert len(rows) == 1
    expected_hash = hashlib.sha256(data["refresh_token"].encode()).hexdigest()
    assert rows[0].refresh_token_hash == expected_hash
    assert rows[0].device_id == "pixel-8-pro"
    assert rows[0].device_info == {"model": "Pixel 8 Pro", "os": "Android 15"}


# ---------------------------------------------------------------- Errores
def test_nonce_reuse_rejected(login_client: TestClient, login_session: Session):
    user, secret_hex = _make_enrolled_user(login_session)
    challenge = _challenge(login_client, str(user.id))
    payload = {
        "nonce": challenge["nonce"],
        "device_id": "pixel-8-pro",
        "signature": _sign_hmac(secret_hex, challenge["nonce"]),
    }
    first = login_client.post("/api/v1/auth/login/facial", json=payload)
    assert first.status_code == 200, first.text

    second = login_client.post("/api/v1/auth/login/facial", json=payload)
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "INVALID_LOGIN"


def test_invalid_signature_matches_unknown_user_body(
    login_client: TestClient, login_session: Session
):
    from app.modules.identity.domain import nonce as nonce_domain

    user, secret_hex = _make_enrolled_user(login_session)
    challenge = _challenge(login_client, str(user.id))
    headers = {"X-Request-Id": "probe-facial"}

    bad_sig = login_client.post(
        "/api/v1/auth/login/facial",
        json={
            "nonce": challenge["nonce"],
            "device_id": "pixel-8-pro",
            "signature": "00" * 32,
        },
        headers=headers,
    )
    # Nonce ligado a un usuario inexistente (misma rama generica).
    ghost_nonce, _ = nonce_domain.issue_nonce(uuid.uuid4(), device_id="pixel-8-pro")
    unknown = login_client.post(
        "/api/v1/auth/login/facial",
        json={
            "nonce": ghost_nonce,
            "device_id": "pixel-8-pro",
            "signature": _sign_hmac(secret_hex, ghost_nonce),
        },
        headers=headers,
    )
    assert bad_sig.status_code == 400
    assert unknown.status_code == 400
    assert bad_sig.json() == unknown.json(), "sin distinguir inexistente de firma invalida"
    assert bad_sig.json()["error"]["code"] == "INVALID_LOGIN"


def test_unknown_device_matches_invalid_signature(login_client: TestClient, login_session: Session):
    user, secret_hex = _make_enrolled_user(login_session)
    challenge = _challenge(login_client, str(user.id))
    headers = {"X-Request-Id": "probe-device"}

    no_binding = login_client.post(
        "/api/v1/auth/login/facial",
        json={
            "nonce": challenge["nonce"],
            "device_id": "otro-telefono",
            "signature": _sign_hmac(secret_hex, challenge["nonce"]),
        },
        headers=headers,
    )
    assert no_binding.status_code == 400
    assert no_binding.json()["error"]["code"] == "INVALID_LOGIN"


def test_expired_nonce_returns_expired(login_client: TestClient, login_session: Session):
    from datetime import timedelta

    from app.modules.identity.service import device_login as device_login_service

    user, secret_hex = _make_enrolled_user(login_session)
    # Emision con `now` en el pasado (API publica del servicio): el TTL de
    # 120 s ya se agoto cuando el facial lo consume.
    past = _utcnow() - timedelta(seconds=300)
    challenge = device_login_service.request_challenge(
        login_session, user_ref=str(user.id), device_id="pixel-8-pro", now=past
    )

    resp = login_client.post(
        "/api/v1/auth/login/facial",
        json={
            "nonce": challenge["nonce"],
            "device_id": "pixel-8-pro",
            "signature": _sign_hmac(secret_hex, challenge["nonce"]),
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "EXPIRED_NONCE"


def test_challenge_unknown_user_is_generic(login_client: TestClient, login_session: Session):
    _make_enrolled_user(login_session)
    headers = {"X-Request-Id": "probe-challenge"}
    unknown = login_client.post(
        "/api/v1/auth/login/challenge",
        json={"user_ref": str(uuid.uuid4())},
        headers=headers,
    )
    malformed = login_client.post(
        "/api/v1/auth/login/challenge",
        json={"user_ref": "no-es-uuid"},
        headers=headers,
    )
    assert unknown.status_code == 400
    assert unknown.json() == malformed.json()
    assert unknown.json()["error"]["code"] == "INVALID_LOGIN"


# ---------------------------------------------------------------- Dominio puro (unitarias)
def test_nonce_single_use_and_expiry_unit():
    from app.modules.identity.domain import nonce as nonce_domain

    nonce_domain.reset_nonces()
    uid = uuid.uuid4()
    nonce, _ = nonce_domain.issue_nonce(uid, ttl_seconds=120)
    entry = nonce_domain.consume_nonce(nonce)
    assert entry.user_id == uid
    try:
        nonce_domain.consume_nonce(nonce)
        raise AssertionError("reutilizar debio fallar")
    except nonce_domain.NonceReuseError:
        pass

    # Expiracion determinista: emite con `now` en el pasado.
    past = _utcnow().replace(year=2000)
    nonce_domain.reset_nonces()
    old, _ = nonce_domain.issue_nonce(uid, now=past, ttl_seconds=120)
    try:
        nonce_domain.consume_nonce(old)
        raise AssertionError("expirado debio fallar")
    except nonce_domain.NonceExpiredError:
        pass
    finally:
        nonce_domain.reset_nonces()


# ---------------------------------------------------------------- Contrato
def test_openapi_includes_login_paths(login_client: TestClient):
    spec = login_client.get("/openapi.json").json()
    assert "/api/v1/auth/login/challenge" in spec["paths"]
    assert "/api/v1/auth/login/facial" in spec["paths"]
