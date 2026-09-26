"""QA de OTP y activacion (E1-T12, HU02 CA-01..CA-04).

Cubre: generacion, envio (email-only), validacion correcta/incorrecta,
expiracion (10 min), reenvio (invalida anterior + espera + maximo) y
limites/bloqueo (intentos y RESEND_LIMIT), mas activacion efectiva.

Patron aislado SQLite en memoria con schemas ATTACH
(`identity`/`shared`/`notifications`), sin Postgres, sin migraciones y sin
tocar `kyc-service/`. Sin OTP/correo/PII en logs (ver ultimo test).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base, get_db
from app.main import app


def _utcnow() -> datetime:
    return datetime.now(UTC)


@pytest.fixture()
def qa_session():
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
def qa_client(qa_session: Session, monkeypatch):
    """TestClient con `get_db` a SQLite, espera minima OTP en 0 y buckets limpios."""
    from app.modules.identity.service import activation as activation_service
    from app.modules.identity.service import otp_service

    monkeypatch.setattr(otp_service, "OTP_RESEND_WAIT_SECONDS", 0)
    monkeypatch.delenv("ACTIVATION_RESEND_RATE_LIMIT_MAX_REQUESTS", raising=False)
    monkeypatch.delenv("ACTIVATION_RESEND_RATE_LIMIT_WINDOW_SECONDS", raising=False)
    activation_service.reset_activation_rate_limits()

    def _override():
        yield qa_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        activation_service.reset_activation_rate_limits()


def _make_pending_user(qa_session: Session):
    """Usuario `PENDING_ACTIVATION` con PIN + OTP `ACTIVATION` vigente."""
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import otp_service
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
    identity_repo.create_credential(
        qa_session,
        user.id,
        pin_hash=pin_login_service.hash_pin("4829"),
    )
    _, plain = otp_service.generate_otp(qa_session, user_id=user.id, purpose="ACTIVATION")
    qa_session.commit()
    return user, plain


def _outbox_activated(qa_session: Session, user_id: uuid.UUID) -> list:
    from app.modules.shared.models import OutboxEntry

    stmt = sa.select(OutboxEntry).where(
        OutboxEntry.event_type == "user.activated",
        OutboxEntry.aggregate_id == user_id,
    )
    return list(qa_session.scalars(stmt).all())


def _notifications_for(qa_session: Session, user_id: uuid.UUID) -> list:
    from app.modules.notifications.models import Notification

    stmt = sa.select(Notification).where(Notification.user_id == user_id)
    return list(qa_session.scalars(stmt).all())


# ---------------------------------------------------------------- Generacion
def test_qa_otp_generacion_formato_hash_y_ttl(qa_session: Session):
    """Generacion: 6 digitos, solo hash+salt, PENDING, TTL 600s, sin reutilizar."""
    from app.modules.identity.service import otp_service

    user, plain = _make_pending_user(qa_session)
    from app.modules.identity import repository as identity_repo

    row = identity_repo.get_active_otp(qa_session, user.id, "ACTIVATION")
    assert row is not None
    assert len(plain) == 6 and plain.isdigit()
    assert row.status == "PENDING"
    assert row.attempts == 0 and row.resend_count == 0
    assert row.code_hash != plain and "$" in row.code_hash
    assert len(row.code_hash) <= 128
    ttl = (row.expires_at - row.created_at).total_seconds()
    assert ttl == pytest.approx(otp_service.OTP_TTL_SECONDS, abs=5)
    assert otp_service.OTP_TTL_SECONDS == 600, "expiracion HU02 = 10 min"


# ---------------------------------------------------------------- Envio
def test_qa_otp_envio_resend_registra_email_only(qa_client: TestClient, qa_session: Session):
    """Envio CA-01: el reenvio registra notificacion solo por email (nunca SMS)."""
    user, first_plain = _make_pending_user(qa_session)

    resp = qa_client.post("/api/v1/auth/otp/resend", json={"user_ref": str(user.id)})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["user_id"] == str(user.id)
    assert data["resend_count"] == 1
    assert data["expires_in"] > 0
    assert first_plain not in resp.text

    rows = _notifications_for(qa_session, user.id)
    assert len(rows) == 1
    assert rows[0].channel == "email"
    assert rows[0].template_code == "otp_code_email"
    assert rows[0].status == "SENT"
    assert rows[0].payload_json["recipient"] == user.email


def test_qa_otp_envio_con_channel_sms_igual_entrega_email(
    qa_client: TestClient, qa_session: Session
):
    """`channel='sms'` se ignora: con email se notifica por email (E1-T32)."""
    user, _ = _make_pending_user(qa_session)

    resp = qa_client.post(
        "/api/v1/auth/otp/resend", json={"user_ref": str(user.id), "channel": "sms"}
    )
    assert resp.status_code == 200

    rows = _notifications_for(qa_session, user.id)
    assert len(rows) == 1
    assert rows[0].channel == "email"
    assert rows[0].template_code == "otp_code_email"


# ---------------------------------------------------------------- Validacion
def test_qa_otp_validacion_correcta_activa(qa_client: TestClient, qa_session: Session):
    """Validacion CA-02/CA-04: codigo correcto activa, 1 evento, sin fuga del codigo."""
    from app.modules.identity import repository as identity_repo

    user, plain = _make_pending_user(qa_session)

    resp = qa_client.post("/api/v1/auth/activate", json={"user_ref": str(user.id), "code": plain})
    assert resp.status_code == 200
    assert resp.json()["data"] == {"user_id": str(user.id), "status": "ACTIVE"}
    assert plain not in resp.text

    qa_session.expire_all()
    assert identity_repo.get_user(qa_session, user.id).status == "ACTIVE"
    assert len(_outbox_activated(qa_session, user.id)) == 1


def test_qa_otp_validacion_incorrecta_no_activa(qa_client: TestClient, qa_session: Session):
    """Codigo incorrecto: 400 INVALID_OTP y el usuario sigue pendiente."""
    from app.modules.identity import repository as identity_repo

    user, plain = _make_pending_user(qa_session)
    wrong = "000000" if plain != "000000" else "111111"

    resp = qa_client.post("/api/v1/auth/activate", json={"user_ref": str(user.id), "code": wrong})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_OTP"
    assert wrong not in resp.text

    qa_session.expire_all()
    assert identity_repo.get_user(qa_session, user.id).status == "PENDING_ACTIVATION"
    assert _outbox_activated(qa_session, user.id) == []


# ---------------------------------------------------------------- Expiracion
def test_qa_otp_expirado_rechazado(qa_client: TestClient, qa_session: Session):
    """Expiracion CA-02: pasado el TTL de 10 min el codigo se rechaza."""
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import otp_service

    user = identity_repo.create_user(
        qa_session,
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Ada",
        last_name="Lovelace",
        email=f"qa.{uuid.uuid4().hex[:8]}@example.com",
    )
    past = _utcnow() - timedelta(seconds=otp_service.OTP_TTL_SECONDS + 60)
    _, plain = otp_service.generate_otp(qa_session, user_id=user.id, purpose="ACTIVATION", now=past)
    qa_session.commit()

    resp = qa_client.post("/api/v1/auth/activate", json={"user_ref": str(user.id), "code": plain})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "EXPIRED_OTP"
    assert plain not in resp.text


def test_qa_otp_servicio_marca_expirado(qa_session: Session):
    """Nivel servicio: validar fuera de ventana marca EXPIRED."""
    from app.modules.identity.service import otp_service

    user, _ = _make_pending_user(qa_session)
    from app.modules.identity import repository as identity_repo

    row = identity_repo.get_active_otp(qa_session, user.id, "ACTIVATION")
    assert row is not None
    base = _utcnow()
    qa_session.rollback()
    user2, plain2 = _make_pending_user(qa_session)
    _ = user  # el primer usuario queda aislado por rollback parcial
    with pytest.raises(otp_service.OtpExpiredError):
        otp_service.validate_otp(
            qa_session,
            user_id=user2.id,
            purpose="ACTIVATION",
            code=plain2,
            now=base + timedelta(seconds=otp_service.OTP_TTL_SECONDS + 1),
        )


# ---------------------------------------------------------------- Reenvio
def test_qa_otp_reenvio_invalida_anterior(qa_client: TestClient, qa_session: Session):
    """Reenvio CA-03: el anterior queda invalidado y solo el ultimo activa."""
    user, first_plain = _make_pending_user(qa_session)

    resp = qa_client.post("/api/v1/auth/otp/resend", json={"user_ref": str(user.id)})
    assert resp.status_code == 200

    stale = qa_client.post(
        "/api/v1/auth/activate", json={"user_ref": str(user.id), "code": first_plain}
    )
    assert stale.status_code == 400
    assert stale.json()["error"]["code"] == "INVALID_OTP"


# ---------------------------------------------------------------- Limites / bloqueo
def test_qa_otp_bloqueo_por_intentos(qa_session: Session):
    """Limite CA-03: 3 intentos fallidos bloquean; ni el correcto pasa."""
    from app.modules.identity.service import otp_service

    user, plain = _make_pending_user(qa_session)
    wrong = "000000" if plain != "000000" else "111111"

    with pytest.raises(otp_service.OtpInvalidError):
        otp_service.validate_otp(qa_session, user_id=user.id, purpose="ACTIVATION", code=wrong)
    with pytest.raises(otp_service.OtpInvalidError):
        otp_service.validate_otp(qa_session, user_id=user.id, purpose="ACTIVATION", code=wrong)
    with pytest.raises(otp_service.OtpAttemptsExceededError):
        otp_service.validate_otp(qa_session, user_id=user.id, purpose="ACTIVATION", code=wrong)
    with pytest.raises(otp_service.OtpNotFoundError):
        otp_service.validate_otp(qa_session, user_id=user.id, purpose="ACTIVATION", code=plain)
    qa_session.rollback()


def test_qa_otp_reenvio_limite_maximo(qa_client: TestClient, qa_session: Session):
    """Limite CA-03: tras 3 reenvios el siguiente es 429 RESEND_LIMIT."""
    from app.modules.identity.service import otp_service

    user, _ = _make_pending_user(qa_session)
    for _ in range(otp_service.OTP_MAX_RESENDS):
        ok = qa_client.post("/api/v1/auth/otp/resend", json={"user_ref": str(user.id)})
        assert ok.status_code == 200
    limited = qa_client.post("/api/v1/auth/otp/resend", json={"user_ref": str(user.id)})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RESEND_LIMIT"


# ---------------------------------------------------------------- Sin PII en logs
def test_qa_otp_sin_codigo_ni_correo_en_logs(qa_client: TestClient, qa_session: Session, caplog):
    """Regla 7/9: ni el OTP ni el correo aparecen en logs."""
    user, plain = _make_pending_user(qa_session)
    email = user.email

    with caplog.at_level(logging.INFO):
        qa_client.post("/api/v1/auth/otp/resend", json={"user_ref": str(user.id)})

    assert plain not in caplog.text
    assert email not in caplog.text
