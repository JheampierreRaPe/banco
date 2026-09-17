"""Gestion de sesiones: refresh rotativo, revocacion y logout (E1-T15, HU03 CA-04).

- Sin Postgres: `TestClient(app)` + override de `get_db` a SQLite en memoria
  con schemas ATTACH (`identity`/`shared`), patron de
  `tests/test_device_login.py` (TestClient) + `tests/test_identity_otp.py`
  (SQLite ATTACH).
- Decisiones (ver `service/sessions.py`): inactividad deslizante desde
  `session.created_at` con default `INACTIVITY_SECONDS = 180` (candidato a
  `config.parameters: session.inactivity_seconds`); revocacion efectiva e
  inmediata en la tabla `sessions.revoked_at` (el `.venv` NO trae cliente
  `redis`/`fakeredis`: Redis como lista negra es mejora futura).
- Casos: refresh valido rota (viejo invalidado, nuevo funciona); refresh
  expirado cierra la sesion; reuso de revocado revoca TODA la cadena +
  error; logout revoca (refresh posterior rechazado); inactividad excedida
  cierra la sesion; reglas estaticas (servicio sin `commit`, solo hash del
  refresh en BD, sin Redis, OpenAPI expone ambas rutas).
"""

from __future__ import annotations

import hashlib
import secrets
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
    / "sessions.py"
)
API_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "api"
    / "sessions.py"
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@pytest.fixture()
def sessions_session():
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
def sessions_client(sessions_session: Session):
    """TestClient con `get_db` a SQLite."""

    def _override():
        yield sessions_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _make_user(sessions_session: Session):
    """Usuario minimo (retorna `User`)."""
    from app.modules.identity import repository as identity_repo

    suffix = uuid.uuid4().hex[:8]
    user = identity_repo.create_user(
        sessions_session,
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Ada",
        last_name="Lovelace",
        email=f"ada.{suffix}@example.com",
        phone="+51999888777",
    )
    sessions_session.commit()
    return user


def _open_session(
    sessions_session: Session,
    user_id: uuid.UUID,
    *,
    created_ago_seconds: int = 0,
    ttl_seconds: int = 30 * 24 * 3600,
    device_id: str = "pixel-8-pro",
) -> tuple[str, uuid.UUID]:
    """Crea una sesion via repositorio (retorna `(refresh_claro, session_id)`).

    `created_ago_seconds` retrocede `created_at` para simular inactividad.
    """
    from app.modules.identity import repository as identity_repo

    refresh = secrets.token_urlsafe(32)
    moment = _utcnow()
    row = identity_repo.create_session(
        sessions_session,
        user_id,
        identity_repo.hash_refresh_token(refresh),
        moment + timedelta(seconds=ttl_seconds),
        device_id=device_id,
    )
    sessions_session.commit()
    if created_ago_seconds:
        row.created_at = moment - timedelta(seconds=created_ago_seconds)
        sessions_session.commit()
    sessions_session.expire_all()
    return refresh, row.id


def _hash(refresh: str) -> str:
    return hashlib.sha256(refresh.encode()).hexdigest()


# ---------------------------------------------------------------- Reglas estaticas
def test_static_rules_no_commit_hash_only_no_redis_documented():
    service = SERVICE_PATH.read_text(encoding="utf-8")
    assert ".commit(" not in service, "el servicio hace flush; el endpoint confirma"
    assert "float(" not in service, "sin float"
    assert "INACTIVITY_SECONDS = 180" in service, "inactividad default 180 s documentado"
    assert "session.inactivity_seconds" in service, "clave config.parameters documentada"
    assert "create_access_token" in service, "reutiliza el JWT existente"
    assert "jwt.encode(" not in service, "no inventa tokens: usa create_access_token"
    assert "import redis" not in service and "from redis" not in service, (
        "sin Redis en el repo: la revocacion vive en la tabla"
    )
    assert "hash_refresh_token" in service, "solo el hash del refresh se persiste"
    log_lines = [line for line in service.splitlines() if "logger." in line]
    assert log_lines, "el servicio debe loguear sin PII"
    for line in log_lines:
        lowered = line.lower()
        assert "refresh_token" not in lowered and "signature" not in lowered, (
            f"secreto en logs: {line.strip()}"
        )

    api = API_PATH.read_text(encoding="utf-8")
    assert ".commit(" in api, "el endpoint confirma la transaccion"
    assert '"/auth/refresh"' in api and '"/auth/logout"' in api, "ambas rutas"


def test_no_redis_client_in_venv():
    import importlib.util

    assert importlib.util.find_spec("redis") is None, "sin redis: revocacion en tabla"
    assert importlib.util.find_spec("fakeredis") is None


# ---------------------------------------------------------------- Camino feliz: rotacion
def test_refresh_valid_rotates_old_invalid_new_works(
    sessions_client: TestClient, sessions_session: Session
):
    from app.core.security import decode_token
    from app.modules.identity import repository as identity_repo

    user = _make_user(sessions_session)
    old_refresh, _ = _open_session(sessions_session, user.id)

    resp = sessions_client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
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
    assert data["refresh_token"] != old_refresh
    assert data["expires_in"] > 0
    assert body["meta"]["request_id"]
    assert decode_token(data["access_token"])["sub"] == str(user.id)

    sessions_session.expire_all()
    old_row = identity_repo.get_session_by_refresh_hash(sessions_session, _hash(old_refresh))
    assert old_row is not None and old_row.revoked_at is not None, (
        "el refresh presentado queda revocado"
    )
    new_row = identity_repo.get_session_by_refresh_hash(
        sessions_session, _hash(data["refresh_token"])
    )
    assert new_row is not None and new_row.revoked_at is None, (
        "el refresh nuevo esta vigente"
    )
    assert new_row.user_id == user.id and new_row.device_id == "pixel-8-pro"

    # El nuevo refresh funciona (segunda rotacion).
    second = sessions_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]}
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["refresh_token"] != data["refresh_token"]


def test_refresh_token_never_stored_in_clear(
    sessions_client: TestClient, sessions_session: Session
):
    from app.modules.identity.models import UserSession

    user = _make_user(sessions_session)
    refresh, _ = _open_session(sessions_session, user.id)
    sessions_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})

    sessions_session.expire_all()
    rows = list(
        sessions_session.scalars(
            sa.select(UserSession).where(UserSession.user_id == user.id)
        ).all()
    )
    assert rows, "hay sesiones"
    for row in rows:
        assert row.refresh_token_hash != refresh, "solo el hash se persiste"
        assert len(row.refresh_token_hash) == 64, "SHA-256 hex"


# ---------------------------------------------------------------- Expirado
def test_expired_refresh_rejected_and_session_closed(
    sessions_client: TestClient, sessions_session: Session
):
    from app.modules.identity import repository as identity_repo

    user = _make_user(sessions_session)
    refresh, _ = _open_session(sessions_session, user.id, ttl_seconds=-60)

    resp = sessions_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "REFRESH_EXPIRED"

    sessions_session.expire_all()
    row = identity_repo.get_session_by_refresh_hash(sessions_session, _hash(refresh))
    assert row is not None and row.revoked_at is not None, "vencido cierra la sesion"


def test_unknown_refresh_is_invalid(
    sessions_client: TestClient, sessions_session: Session
):
    _make_user(sessions_session)
    resp = sessions_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": secrets.token_urlsafe(32)}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_REFRESH"


# ---------------------------------------------------------------- Reuso = posible robo
def test_reuse_of_revoked_refresh_revokes_chain(
    sessions_client: TestClient, sessions_session: Session
):
    from app.modules.identity import repository as identity_repo

    user = _make_user(sessions_session)
    old_refresh, _ = _open_session(sessions_session, user.id)

    first = sessions_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": old_refresh}
    )
    assert first.status_code == 200, first.text
    new_refresh = first.json()["data"]["refresh_token"]

    # Reuso del viejo: posible robo -> toda la cadena revocada + error.
    reuse = sessions_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": old_refresh}
    )
    assert reuse.status_code == 401
    assert reuse.json()["error"]["code"] == "REFRESH_REUSED"

    sessions_session.expire_all()
    successor = identity_repo.get_session_by_refresh_hash(
        sessions_session, _hash(new_refresh)
    )
    assert successor is not None and successor.revoked_at is not None, (
        "el sucesor legitimo tambien muere ante el reuso"
    )

    # El sucesor ya no sirve (cadena muerta).
    after = sessions_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": new_refresh}
    )
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "REFRESH_REUSED"


# ---------------------------------------------------------------- Logout
def test_logout_revokes_subsequent_refresh_rejected(
    sessions_client: TestClient, sessions_session: Session
):
    from app.modules.identity import repository as identity_repo

    user = _make_user(sessions_session)
    refresh, _ = _open_session(sessions_session, user.id)

    resp = sessions_client.post("/api/v1/auth/logout", json={"refresh_token": refresh})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["revoked"] is True
    assert data["session_id"]

    sessions_session.expire_all()
    row = identity_repo.get_session_by_refresh_hash(sessions_session, _hash(refresh))
    assert row is not None and row.revoked_at is not None, "logout revoca la sesion"

    after = sessions_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": refresh}
    )
    assert after.status_code == 401, "refresh posterior al logout se rechaza"


def test_logout_idempotent_unknown_or_twice(
    sessions_client: TestClient, sessions_session: Session
):
    user = _make_user(sessions_session)
    refresh, _ = _open_session(sessions_session, user.id)

    unknown = sessions_client.post(
        "/api/v1/auth/logout", json={"refresh_token": secrets.token_urlsafe(32)}
    )
    assert unknown.status_code == 200, "logout idempotente ante desconocido"
    assert unknown.json()["data"]["revoked"] is False

    first = sessions_client.post("/api/v1/auth/logout", json={"refresh_token": refresh})
    assert first.status_code == 200
    assert first.json()["data"]["revoked"] is True

    second = sessions_client.post("/api/v1/auth/logout", json={"refresh_token": refresh})
    assert second.status_code == 200, "doble logout no falla"
    assert second.json()["data"]["revoked"] is False


# ---------------------------------------------------------------- Inactividad
def test_inactivity_closes_session(
    sessions_client: TestClient, sessions_session: Session
):
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import sessions as sessions_service

    user = _make_user(sessions_session)
    # Sesion emitida hace mas que la ventana de inactividad.
    refresh, _ = _open_session(
        sessions_session,
        user.id,
        created_ago_seconds=sessions_service.INACTIVITY_SECONDS + 60,
    )

    resp = sessions_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "SESSION_INACTIVE"

    sessions_session.expire_all()
    row = identity_repo.get_session_by_refresh_hash(sessions_session, _hash(refresh))
    assert row is not None and row.revoked_at is not None, (
        "inactividad excedida cierra la sesion"
    )


def test_inactivity_within_window_allows_refresh(
    sessions_client: TestClient, sessions_session: Session
):
    from app.modules.identity.service import sessions as sessions_service

    user = _make_user(sessions_session)
    refresh, _ = _open_session(
        sessions_session,
        user.id,
        created_ago_seconds=sessions_service.INACTIVITY_SECONDS - 60,
    )

    resp = sessions_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200, resp.text


def test_inactivity_unit_boundary():
    from datetime import timedelta

    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import sessions as sessions_service

    assert sessions_service.INACTIVITY_SECONDS == 180
    # Servicio puro: vencimiento determinista con `now` explicito.
    from sqlalchemy import create_engine
    from sqlalchemy import event as _event
    from sqlalchemy.orm import Session as _Session
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

    _event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["identity.users"],
            Base.metadata.tables["identity.credentials"],
            Base.metadata.tables["identity.sessions"],
            Base.metadata.tables["shared.outbox"],
        ],
    )
    db = _Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        user = identity_repo.create_user(
            db,
            doc_type="DNI",
            doc_number_hash="hash-" + uuid.uuid4().hex,
            first_name="Ada",
            last_name="Lovelace",
        )
        refresh = secrets.token_urlsafe(32)
        moment = _utcnow()
        row = identity_repo.create_session(
            db,
            user.id,
            identity_repo.hash_refresh_token(refresh),
            moment + timedelta(seconds=3600),
        )
        db.commit()
        # Envejecer created_at mas alla de la ventana + margen.
        row.created_at = moment - timedelta(
            seconds=sessions_service.INACTIVITY_SECONDS + 10
        )
        db.commit()
        db.expire_all()
        try:
            sessions_service.refresh_session(db, refresh_token=refresh, now=moment)
            raise AssertionError("inactividad debio cerrar la sesion")
        except sessions_service.SessionInactiveError:
            pass
        db.rollback()
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------- Contrato
def test_openapi_includes_session_paths(sessions_client: TestClient):
    spec = sessions_client.get("/openapi.json").json()
    assert "/api/v1/auth/refresh" in spec["paths"]
    assert "/api/v1/auth/logout" in spec["paths"]
