"""Endpoints de activacion y reenvio de OTP (E1-T10, HU02 CA-02/CA-03/CA-04).

- Sin Postgres: `TestClient(app)` + override de `get_db` a SQLite en memoria
  con schemas ATTACH (`identity`/`shared`/`notifications`), patron de
  `tests/test_kyc_proxy.py` (TestClient) + `tests/test_identity_otp.py`
  (SQLite ATTACH).
- Casos: activacion exitosa (`ACTIVE` + 1x `user.activated` en outbox, sin
  duplicar, codigo fuera de la respuesta); codigo invalido/expirado
  (`INVALID_OTP`/`EXPIRED_OTP`, mismo cuerpo que usuario inexistente);
  reenvio OK (nuevo codigo + notificacion registrada via `notifications`);
  limite de reenvios (`RESEND_LIMIT` 429); rate limit (`RATE_LIMITED` 429);
  no filtracion de existencia (cuerpos identicos); OpenAPI expone ambas
  rutas; reglas estaticas (servicio sin `commit`, sin codigo en logs, sin
  tocar otros modulos a nivel top salvo fachadas permitidas).
"""

from __future__ import annotations

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

ACTIVATION_SERVICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "service"
    / "activation.py"
)
ACTIVATION_API_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "api" / "activation.py"
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@pytest.fixture()
def activation_session():
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
            Base.metadata.tables["identity.otp_codes"],
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
def activation_client(activation_session: Session, monkeypatch):
    """TestClient con `get_db` a SQLite, espera minima OTP en 0 y buckets limpios."""
    from app.modules.identity.service import activation as activation_service
    from app.modules.identity.service import otp_service

    monkeypatch.setattr(otp_service, "OTP_RESEND_WAIT_SECONDS", 0)
    monkeypatch.delenv("ACTIVATION_RESEND_RATE_LIMIT_MAX_REQUESTS", raising=False)
    monkeypatch.delenv("ACTIVATION_RESEND_RATE_LIMIT_WINDOW_SECONDS", raising=False)
    activation_service.reset_activation_rate_limits()

    def _override():
        yield activation_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        activation_service.reset_activation_rate_limits()


def _make_pending_user(session: Session, *, destination: str | None = "+51999888777"):
    """Usuario `PENDING_ACTIVATION` + OTP `ACTIVATION` vigente (retorna `(user, plain)`)."""
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import otp_service

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
    _, plain = otp_service.generate_otp(
        session, user_id=user.id, purpose="ACTIVATION", destination=destination
    )
    session.commit()
    return user, plain


def _outbox_activated(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.shared.models import OutboxEntry

    stmt = sa.select(OutboxEntry).where(
        OutboxEntry.event_type == "user.activated",
        OutboxEntry.aggregate_id == user_id,
    )
    return list(session.scalars(stmt).all())


def _notifications_for(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.notifications.models import Notification

    stmt = sa.select(Notification).where(Notification.user_id == user_id)
    return list(session.scalars(stmt).all())


# ---------------------------------------------------------------- Servicio: reglas estaticas
def test_service_rules_no_commit_no_code_in_logs_lazy_notify():
    content = ACTIVATION_SERVICE_PATH.read_text(encoding="utf-8")
    assert ".commit(" not in content, "el servicio hace flush; el endpoint confirma"
    assert "float(" not in content, "sin float"
    assert "    from app.modules.notifications.service import" in content, (
        "notificacion solo via fachada send con import perezoso (E1-T03/E1-T08)"
    )
    top_imports = "\n".join(
        line
        for line in content.splitlines()
        if line.startswith("from app.") or line.startswith("import app.")
    )
    assert "notifications" not in top_imports, "notifications no debe importarse a nivel modulo"
    assert "outbox" not in top_imports, "el evento lo encola otp_service; aqui no se duplica"
    code_imports = [
        line
        for line in content.splitlines()
        if line.startswith("from ") or line.startswith("import ")
    ]
    for forbidden in ("onboard_customer", "kyc_proxy"):
        assert not any(forbidden in line for line in code_imports), (
            f"prohibido importar/tocar {forbidden}"
        )
    log_lines = [line for line in content.splitlines() if "logger." in line]
    assert log_lines, "el servicio debe loguear sin PII"
    for line in log_lines:
        assert "plain" not in line and "code" not in line.replace("template_code", ""), (
            f"codigo en claro en logs: {line.strip()}"
        )
    api_content = ACTIVATION_API_PATH.read_text(encoding="utf-8")
    assert "plain" not in api_content, "el router jamas maneja el codigo en claro"


# ---------------------------------------------------------------- Activacion exitosa
def test_activate_success_sets_active_single_event_no_code_leak(
    activation_client: TestClient, activation_session: Session
):
    user, plain = _make_pending_user(activation_session)

    resp = activation_client.post(
        "/api/v1/auth/activate", json={"user_ref": str(user.id), "code": plain}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"data", "meta"}
    assert body["data"] == {"user_id": str(user.id), "status": "ACTIVE"}
    assert body["meta"]["request_id"]

    activation_session.expire_all()
    from app.modules.identity import repository as identity_repo

    assert identity_repo.get_user(activation_session, user.id).status == "ACTIVE"

    events = _outbox_activated(activation_session, user.id)
    assert len(events) == 1, "validate_otp encola 1x user.activated; el servicio no duplica"
    assert events[0].status == "PENDING"
    assert events[0].payload["user_id"] == str(user.id)

    assert plain not in resp.text, "el codigo OTP no sale en la respuesta"


def test_activate_is_idempotent_without_duplicate_event(
    activation_client: TestClient, activation_session: Session
):
    user, plain = _make_pending_user(activation_session)
    first = activation_client.post(
        "/api/v1/auth/activate", json={"user_ref": str(user.id), "code": plain}
    )
    assert first.status_code == 200
    second = activation_client.post(
        "/api/v1/auth/activate", json={"user_ref": str(user.id), "code": "000000"}
    )
    assert second.status_code == 200
    assert second.json()["data"]["status"] == "ACTIVE"
    assert len(_outbox_activated(activation_session, user.id)) == 1


# ---------------------------------------------------------------- Invalidos / expirados / no filtracion
def test_invalid_code_matches_unknown_user_body(
    activation_client: TestClient, activation_session: Session
):
    user, plain = _make_pending_user(activation_session)
    wrong = "000000" if plain != "000000" else "111111"
    headers = {"X-Request-Id": "probe-identica"}

    bad_code = activation_client.post(
        "/api/v1/auth/activate",
        json={"user_ref": str(user.id), "code": wrong},
        headers=headers,
    )
    unknown = activation_client.post(
        "/api/v1/auth/activate",
        json={"user_ref": str(uuid.uuid4()), "code": wrong},
        headers=headers,
    )
    assert bad_code.status_code == 400
    assert unknown.status_code == 400
    assert bad_code.json() == unknown.json(), "sin distinguir inexistente de codigo invalido"
    assert bad_code.json()["error"]["code"] == "INVALID_OTP"
    assert wrong not in bad_code.text and "hash-" not in bad_code.text


def test_malformed_user_ref_maps_to_generic_invalid(
    activation_client: TestClient, activation_session: Session
):
    _make_pending_user(activation_session)
    resp = activation_client.post(
        "/api/v1/auth/activate", json={"user_ref": "no-es-uuid", "code": "123456"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_OTP"


def test_expired_code_returns_expired_otp(
    activation_client: TestClient, activation_session: Session
):
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import otp_service

    user = identity_repo.create_user(
        activation_session,
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Ada",
        last_name="Lovelace",
    )
    past = _utcnow() - timedelta(seconds=otp_service.OTP_TTL_SECONDS + 60)
    _, plain = otp_service.generate_otp(
        activation_session, user_id=user.id, purpose="ACTIVATION", now=past
    )
    activation_session.commit()

    resp = activation_client.post(
        "/api/v1/auth/activate", json={"user_ref": str(user.id), "code": plain}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "EXPIRED_OTP"
    assert plain not in resp.text


# ---------------------------------------------------------------- Reenvio
def test_resend_ok_registers_notification(
    activation_client: TestClient, activation_session: Session
):
    user, first_plain = _make_pending_user(activation_session)

    resp = activation_client.post("/api/v1/auth/otp/resend", json={"user_ref": str(user.id)})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["user_id"] == str(user.id)
    assert data["resend_count"] == 1
    assert data["expires_in"] > 0
    assert first_plain not in resp.text, "el codigo (viejo o nuevo) no sale en la respuesta"

    rows = _notifications_for(activation_session, user.id)
    assert len(rows) == 1, "reenvio registra la notificacion de entrega"
    assert rows[0].template_code == "otp_code"
    assert rows[0].status == "SENT"

    # El anterior quedo invalidado: ya no activa.
    stale = activation_client.post(
        "/api/v1/auth/activate", json={"user_ref": str(user.id), "code": first_plain}
    )
    assert stale.status_code == 400
    assert stale.json()["error"]["code"] == "INVALID_OTP"


def test_resend_limit_after_max_resends(
    activation_client: TestClient, activation_session: Session
):
    from app.modules.identity.service import otp_service

    user, _ = _make_pending_user(activation_session)
    for _ in range(otp_service.OTP_MAX_RESENDS):
        ok = activation_client.post("/api/v1/auth/otp/resend", json={"user_ref": str(user.id)})
        assert ok.status_code == 200
    limited = activation_client.post(
        "/api/v1/auth/otp/resend", json={"user_ref": str(user.id)}
    )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RESEND_LIMIT"


def test_resend_unknown_user_matches_user_without_pending_otp(
    activation_client: TestClient, activation_session: Session
):
    from app.modules.identity import repository as identity_repo

    headers = {"X-Request-Id": "probe-resend"}
    bare = identity_repo.create_user(
        activation_session,
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Ada",
        last_name="Lovelace",
    )
    activation_session.commit()

    no_pending = activation_client.post(
        "/api/v1/auth/otp/resend", json={"user_ref": str(bare.id)}, headers=headers
    )
    unknown = activation_client.post(
        "/api/v1/auth/otp/resend", json={"user_ref": str(uuid.uuid4())}, headers=headers
    )
    assert no_pending.status_code == 400
    assert no_pending.json() == unknown.json()
    assert no_pending.json()["error"]["code"] == "INVALID_OTP"


def test_resend_rate_limit_returns_429(
    activation_client: TestClient, activation_session: Session, monkeypatch
):
    from app.modules.identity.service import activation as activation_service

    monkeypatch.setenv("ACTIVATION_RESEND_RATE_LIMIT_MAX_REQUESTS", "2")
    monkeypatch.setenv("ACTIVATION_RESEND_RATE_LIMIT_WINDOW_SECONDS", "60")
    activation_service.reset_activation_rate_limits()

    user, _ = _make_pending_user(activation_session)
    assert (
        activation_client.post("/api/v1/auth/otp/resend", json={"user_ref": str(user.id)}).status_code
        == 200
    )
    assert (
        activation_client.post("/api/v1/auth/otp/resend", json={"user_ref": str(user.id)}).status_code
        == 200
    )
    limited = activation_client.post(
        "/api/v1/auth/otp/resend", json={"user_ref": str(user.id)}
    )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


# ---------------------------------------------------------------- Contrato
def test_openapi_includes_activation_paths(activation_client: TestClient):
    spec = activation_client.get("/openapi.json").json()
    assert "/api/v1/auth/activate" in spec["paths"]
    assert "/api/v1/auth/otp/resend" in spec["paths"]
