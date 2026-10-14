"""QA de entrega y reaccion a "no reconozco" (E6-T16, HU22 CA-01..CA-04).

Suite de cierre: verifica de punta a punta —independientemente y SIN
solapar— lo que `E6-T12` (fachada/preferencias/centro), `E6-T13`
(adaptadores) y `E6-T14` ("no reconozco"/bloqueo) ya cubren por pieza:

- CA-01: alerta push + correo de respaldo entregados en `< 5 s` con
  senders mock, reloj (`now`) y espera (`sleep_fn`) inyectables.
- CA-02: contenido con dispositivo/hora/ubicacion aproximada y sin PII;
  la `reference` (no PII) solo viaja en el correo.
- CA-04: caida del correo (viceversa no cubierto por E6-T12) no impide
  el push, y `retry_notification` recupera el correo.
- Preferencias via HTTP + fachada: `push=false` deja solo el respaldo;
  `email=false` no desactiva el respaldo; `sms=true` nunca abre canal.
- CA-03: jornada completa notificar -> centro -> reportar -> cadena
  `risk`/`identity`: sesiones revocadas, `fraud.alert.raised` unico,
  replay posterior sin nuevos efectos, `404` neutro e indistinguible
  (ajena === inexistente), login nuevo funciona y refresh viejo muere.
- Barrido de PII en la jornada completa (respuestas, `payload_json`,
  eventos, auditoria, logs).

SQLite en memoria + schemas ATTACH (sin red, sin `float`); HTTP real
(TestClient) con auth falsa por `dependency_overrides`.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.modules.audit.models as _audit_models
import app.modules.identity.models as _identity_models
import app.modules.notifications.models as _notifications_models
import app.modules.risk.models as _risk_models
import app.modules.shared.models as _shared_models
from app.adapters.notification_sender import MockNotificationSender
from app.core import events as domain_events
from app.core.db import Base, get_db
from app.main import app
from app.modules.identity import repository as identity_repo
from app.modules.identity.events import on_fraud_alert_raised
from app.modules.identity.service import sessions as sessions_service
from app.modules.notifications import service as svc
from app.modules.notifications.api import get_current_user_id as notif_get_current_user_id
from app.modules.notifications.domain.templates import BASE_TEMPLATES, render_template
from app.modules.notifications.models import Notification
from app.modules.notifications.service import notify_security_event
from app.modules.risk import service as risk_service
from app.modules.risk.events import on_unrecognized_operation_reported
from app.modules.shared.models import OutboxEntry

assert _audit_models is not None
assert _identity_models is not None
assert _notifications_models is not None
assert _risk_models is not None
assert _shared_models is not None

USER = uuid.uuid4()
OTHER = uuid.uuid4()

AT_ISO = "2026-10-13T12:00:00+00:00"
NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)

TABLES = [
    "notifications.notifications",
    "notifications.notification_templates",
    "notifications.user_channel_preferences",
    "shared.outbox",
    "shared.processed_events",
    "risk.risk_profiles",
    "audit.audit_log",
    "identity.users",
    "identity.sessions",
]

SCHEMAS = ("notifications", "shared", "risk", "audit", "identity", "config")


@pytest.fixture()
def db():
    """SQLite en memoria con los schemas de la jornada + `config.parameters`."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in SCHEMAS:
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE config.parameters (id TEXT PRIMARY KEY, "
                "key TEXT NOT NULL UNIQUE, value_json TEXT NOT NULL, "
                "module TEXT NOT NULL, description TEXT, updated_by TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
        )
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        session.commit()
        yield engine, session
        session.rollback()
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def api_client(db, monkeypatch):
    """TestClient con BD SQLite y auth falsa como el titular."""
    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "0")
    _, session = db

    def _override_db():
        yield session

    def _override_user():
        return USER

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[notif_get_current_user_id] = _override_user
    try:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(notif_get_current_user_id, None)


def _act_as(user_id: uuid.UUID):
    """Cambia el usuario autenticado del TestClient."""
    app.dependency_overrides[notif_get_current_user_id] = lambda: user_id


def _fresh_session_factory(engine):
    def _factory():
        return Session(bind=engine, autoflush=False, expire_on_commit=False)

    return _factory


def _make_user(session: Session, uid: uuid.UUID, tag: str) -> None:
    identity_repo.create_user(
        session,
        doc_type="DNI",
        doc_number_hash=f"doctest-{tag}-{uid.hex[:12]}",
        first_name="Titular",
        last_name=f"Prueba {tag}",
        email=f"titular-{tag}-{uid.hex[:8]}@test.local",
    )
    session.flush()


def _make_session(session: Session, uid: uuid.UUID, tag: str):
    """Sesion activa; retorna (fila, refresh en claro para el 401 posterior)."""
    clear = f"refresh-{tag}-{uuid.uuid4().hex}"
    row = identity_repo.create_session(
        session,
        uid,
        identity_repo.hash_refresh_token(clear),
        datetime.now(UTC) + timedelta(hours=2),
        device_id=f"dev-{tag}",
    )
    session.flush()
    return row, clear


def _drive_report_chain(engine, session, report_entry):
    """Ejecuta los dos consumidores D-04 como lo haria el worker."""
    factory = _fresh_session_factory(engine)
    assert (
        on_unrecognized_operation_reported(
            SimpleNamespace(id=report_entry.id, payload=dict(report_entry.payload)),
            session_factory=factory,
        )
        is True
    )
    session.expire_all()
    alerts = list(
        session.scalars(
            sa.select(OutboxEntry).where(OutboxEntry.event_type == domain_events.FRAUD_ALERT_RAISED)
        ).all()
    )
    assert len(alerts) == 1
    assert (
        on_fraud_alert_raised(
            SimpleNamespace(id=alerts[0].id, payload=dict(alerts[0].payload)),
            session_factory=factory,
        )
        is True
    )
    session.expire_all()
    return alerts[0]


def _outbox(session: Session, event_type: str):
    session.expire_all()
    return list(
        session.scalars(sa.select(OutboxEntry).where(OutboxEntry.event_type == event_type)).all()
    )


# ------------------------------------------------- CA-01: entrega < 5 s (push + correo)
def test_qa_security_alert_delivered_within_window_both_channels(db):
    """Camino feliz con reloj y espera inyectables: push + correo < 5 s."""
    _, session = db
    sleeps: list[float] = []
    push = MockNotificationSender()
    mail = MockNotificationSender()
    out = notify_security_event(
        session,
        user_id=USER,
        event_type="transfer.settled",
        reference="op-qa16",
        device="Pixel 8",
        at=AT_ISO,
        location="Lima",
        sender_push=push,
        sender_email=mail,
        max_attempts=1,
        sleep_fn=lambda s: sleeps.append(s),
        now=NOW,
    )
    session.commit()
    assert out["email_backup"] is True
    assert set(out["channels"]) == {"push", "email"}
    assert out["channels"]["push"]["status"] == "SENT"
    assert out["channels"]["email"]["status"] == "SENT"
    assert out["channels"]["push"]["provider_ref"]
    assert out["channels"]["email"]["provider_ref"]
    assert out["channels"]["push"]["provider_ref"] != out["channels"]["email"]["provider_ref"]
    assert out["elapsed_seconds"] < out["window_seconds"] <= 5
    assert sleeps == []  # sin esperas reales ni inyectadas en el camino feliz


# ------------------------------------------------- CA-02: contenido sin PII
def test_qa_alert_content_has_device_time_location_without_pii(db):
    """Dispositivo/hora/ubicacion en ambos cuerpos; `reference` solo en el correo."""
    _, session = db
    notify_security_event(
        session,
        user_id=USER,
        event_type="transfer.settled",
        reference="op-qa16-contenido",
        device="Pixel 8",
        at=AT_ISO,
        location="Lima",
        sender_push=MockNotificationSender(),
        sender_email=MockNotificationSender(),
        now=NOW,
    )
    session.commit()
    rows = {
        row.channel: row
        for row in session.scalars(
            sa.select(Notification).where(Notification.user_id == USER)
        ).all()
    }
    push_data = (rows["push"].payload_json or {}).get("data", {})
    email_data = (rows["email"].payload_json or {}).get("data", {})
    assert push_data == {
        "operation": "transfer.settled",
        "at": AT_ISO,
        "device": "Pixel 8",
        "location": "Lima",
    }
    assert email_data["reference"] == "op-qa16-contenido"
    assert "reference" not in push_data
    push_body = render_template(
        BASE_TEMPLATES["security_operation_alert"]["body_template"], push_data
    )
    email_body = render_template(
        BASE_TEMPLATES["security_operation_email"]["body_template"], email_data
    )
    for body in (push_body, email_body):
        assert "Pixel 8" in body and AT_ISO in body and "Lima" in body
        assert "{" not in body and "}" not in body
    assert "op-qa16-contenido" in email_body
    assert "op-qa16-contenido" not in push_body
    for row in rows.values():
        raw = json.dumps(row.payload_json or {}, sort_keys=True)
        for pii in ("@test.local", "titular-", "424242", "-12.04,-77.03", "nonce", "biometr"):
            assert pii not in raw


def test_qa_missing_device_location_falls_back_to_approximate(db):
    """Sin dispositivo/ubicacion: etiqueta aproximada, jamas coordenadas ni vacio."""
    _, session = db
    out = notify_security_event(
        session,
        user_id=USER,
        event_type="auth.failed_attempt",
        reference="op-qa16-sitio",
        device=None,
        at=None,
        location="",
        sender_push=MockNotificationSender(),
        sender_email=MockNotificationSender(),
        now=NOW,
    )
    assert out["channels"]["push"]["status"] == "SENT"
    row = session.scalars(sa.select(Notification).where(Notification.channel == "push")).one()
    data = (row.payload_json or {}).get("data", {})
    assert data["device"] == svc.UNKNOWN_DEVICE
    assert data["location"] == svc.UNKNOWN_LOCATION
    assert data["at"]  # instante aportado por la fachada
    body = render_template(BASE_TEMPLATES["security_operation_alert"]["body_template"], data)
    assert svc.UNKNOWN_LOCATION in body


# ------------------------------------------------- CA-04: viceversa + reimpulso del correo
def test_qa_email_outage_does_not_block_push_and_retry_recovers(db):
    """Caida dura del correo (viceversa de E6-T12): push SENT, correo FAILED y retry lo recupera."""
    _, session = db
    mail = MockNotificationSender()
    mail.set_channel_raise("email", "gmail caido duro")
    out = notify_security_event(
        session,
        user_id=USER,
        event_type="transfer.settled",
        reference="op-qa16-caida",
        device="Pixel 8",
        at=AT_ISO,
        location="Lima",
        sender_push=MockNotificationSender(),
        sender_email=mail,
        now=NOW,
    )
    assert out["channels"]["push"]["status"] == "SENT"
    assert out["channels"]["email"]["status"] == "FAILED"
    assert out["channels"]["email"]["error"]
    email_id = uuid.UUID(out["channels"]["email"]["notification_id"])
    row = svc.retry_notification(session, email_id, sender=MockNotificationSender())
    assert row.status == "SENT"
    assert row.provider_ref
    session.commit()
    push_id = uuid.UUID(out["channels"]["push"]["notification_id"])
    assert session.get(Notification, push_id).status == "SENT"  # intacto
    assert session.get(Notification, email_id).status == "SENT"  # recuperado


# ------------------------------------------------- preferencias E2E (HTTP + fachada)
def test_qa_preferences_via_http_drive_security_dispatch(db, api_client):
    """`PUT /preferences` + fachada: push=false -> solo respaldo; email=false -> respaldo igual."""
    _, session = db
    resp = api_client.put(
        "/api/v1/notifications/preferences",
        json={"push": False, "email": True, "sms": False},
    )
    assert resp.status_code == 200
    out = notify_security_event(
        session,
        user_id=USER,
        event_type="transfer.settled",
        reference="op-qa16-pref1",
        device="Pixel 8",
        at=AT_ISO,
        location="Lima",
        sender_push=MockNotificationSender(),
        sender_email=MockNotificationSender(),
        now=NOW,
    )
    assert set(out["channels"]) == {"email"}
    resp = api_client.put(
        "/api/v1/notifications/preferences",
        json={"push": True, "email": False, "sms": False},
    )
    assert resp.status_code == 200
    out = notify_security_event(
        session,
        user_id=USER,
        event_type="transfer.settled",
        reference="op-qa16-pref2",
        device="Pixel 8",
        at=AT_ISO,
        location="Lima",
        sender_push=MockNotificationSender(),
        sender_email=MockNotificationSender(),
        now=NOW,
    )
    assert set(out["channels"]) == {"push", "email"}  # respaldo no desactivable
    assert out["channels"]["email"]["status"] == "SENT"


def test_qa_sms_opt_in_never_opens_sms_channel(db, api_client):
    """`sms=true` no participa en la ruta de seguridad (solo push + correo)."""
    _, session = db
    resp = api_client.put(
        "/api/v1/notifications/preferences",
        json={"push": True, "email": True, "sms": True},
    )
    assert resp.status_code == 200
    out = notify_security_event(
        session,
        user_id=USER,
        event_type="transfer.settled",
        reference="op-qa16-sms",
        device="Pixel 8",
        at=AT_ISO,
        location="Lima",
        sender_push=MockNotificationSender(),
        sender_email=MockNotificationSender(),
        now=NOW,
    )
    assert set(out["channels"]) == {"push", "email"}
    assert "sms" not in out["channels"]


# ------------------------------------------------- CA-03: jornada "no reconozco" de punta a punta
def test_qa_unrecognized_journey_blocks_channels_and_alerts_risk_once(db, api_client):
    """Notificar -> centro -> reportar -> cadena: bloqueo + alerta unicos + acuse visible."""
    engine, session = db
    _make_user(session, USER, "owner")
    session.commit()
    _make_session(session, USER, "a")
    _, clear_b = _make_session(session, USER, "b")
    session.commit()

    out = notify_security_event(
        session,
        user_id=USER,
        event_type="transfer.settled",
        reference="op-qa16-jornada",
        device="Pixel 8",
        at=AT_ISO,
        location="Lima",
        sender_push=MockNotificationSender(),
        sender_email=MockNotificationSender(),
        now=NOW,
    )
    session.commit()
    push_id = out["channels"]["push"]["notification_id"]

    listed = api_client.get("/api/v1/notifications")
    assert listed.status_code == 200
    assert str(push_id) in {item["id"] for item in listed.json()["data"]}

    resp = api_client.post(f"/api/v1/notifications/{push_id}/report", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {
        "reported": True,
        "replayed": False,
        "notification_id": str(push_id),
        "confirmation_id": resp.json()["data"]["confirmation_id"],
        "reference": "op-qa16-jornada",
    }
    reports = _outbox(session, domain_events.SECURITY_OPERATION_REPORTED)
    assert len(reports) == 1
    assert reports[0].payload["user_id"] == str(USER)

    _drive_report_chain(engine, session, reports[0])

    profile = risk_service.get_or_create_profile(session, USER)
    assert int(profile.score) == risk_service.SCORE_PER_UNRECOGNIZED_REPORT
    assert len(_outbox(session, domain_events.FRAUD_ALERT_RAISED)) == 1
    actions = sorted(
        {row.action for row in session.scalars(sa.select(_audit_models.AuditLog)).all()}
    )
    assert "notifications.unrecognized_report" in actions
    assert "risk.unrecognized_operation" in actions
    assert "identity.sessions_revoked" in actions
    assert not hasattr(_risk_models, "RiskAlert")  # D-16: sin `risk.alerts`
    assert not hasattr(_risk_models, "Alert")

    session.expire_all()
    assert (
        session.scalars(
            sa.select(_identity_models.UserSession).where(
                _identity_models.UserSession.user_id == USER,
                _identity_models.UserSession.revoked_at.is_(None),
            )
        ).all()
        == []
    )
    with pytest.raises(sessions_service.RefreshReuseError):
        sessions_service.refresh_session(session, refresh_token=clear_b)
    session.rollback()
    fresh, _ = _make_session(session, USER, "fresh")
    session.commit()
    assert fresh.revoked_at is None  # el titular puede volver a entrar

    listed = api_client.get("/api/v1/notifications")
    assert listed.status_code == 200
    assert "security_report_received" in {
        item["template_code"] for item in listed.json()["data"]
    }  # acuse en el centro


def test_qa_second_report_after_chain_is_replay_without_new_effects(db, api_client):
    """Replay DESPUES de la cadena: sin segundo evento, sin subir el riesgo, sin nuevo acuse."""
    engine, session = db
    _make_user(session, USER, "owner")
    session.commit()
    _make_session(session, USER, "a")
    session.commit()
    out = notify_security_event(
        session,
        user_id=USER,
        event_type="transfer.settled",
        reference="op-qa16-replay",
        device="Pixel 8",
        at=AT_ISO,
        location="Lima",
        sender_push=MockNotificationSender(),
        sender_email=MockNotificationSender(),
        now=NOW,
    )
    session.commit()
    push_id = out["channels"]["push"]["notification_id"]

    first = api_client.post(f"/api/v1/notifications/{push_id}/report", json={})
    assert first.json()["data"]["replayed"] is False
    _drive_report_chain(
        engine, session, _outbox(session, domain_events.SECURITY_OPERATION_REPORTED)[0]
    )
    score_after_chain = int(risk_service.get_or_create_profile(session, USER).score)
    ack_count = len(
        session.scalars(
            sa.select(Notification).where(Notification.template_code == "security_report_received")
        ).all()
    )

    second = api_client.post(f"/api/v1/notifications/{push_id}/report", json={})
    assert second.status_code == 200
    assert second.json()["data"]["reported"] is True
    assert second.json()["data"]["replayed"] is True
    assert len(_outbox(session, domain_events.SECURITY_OPERATION_REPORTED)) == 1
    assert len(_outbox(session, domain_events.FRAUD_ALERT_RAISED)) == 1
    assert int(risk_service.get_or_create_profile(session, USER).score) == score_after_chain
    assert (
        len(
            session.scalars(
                sa.select(Notification).where(
                    Notification.template_code == "security_report_received"
                )
            ).all()
        )
        == ack_count
    )


def test_qa_foreign_and_missing_reports_share_neutral_404(db, api_client):
    """Ajena e inexistente responden el mismo `404` neutro, sin efectos ni fugas."""
    _, session = db
    _make_user(session, USER, "owner")
    _make_user(session, OTHER, "other")
    session.commit()
    _make_session(session, USER, "a")
    session.commit()
    out = notify_security_event(
        session,
        user_id=USER,
        event_type="transfer.settled",
        reference="op-qa16-neutro",
        device="Pixel 8",
        at=AT_ISO,
        location="Lima",
        sender_push=MockNotificationSender(),
        sender_email=MockNotificationSender(),
        now=NOW,
    )
    session.commit()
    push_id = out["channels"]["push"]["notification_id"]

    _act_as(OTHER)
    try:
        foreign = api_client.post(f"/api/v1/notifications/{push_id}/report", json={})
    finally:
        _act_as(USER)
    missing = api_client.post(f"/api/v1/notifications/{uuid.uuid4()}/report", json={})
    assert foreign.status_code == 404
    assert missing.status_code == 404
    assert foreign.json()["error"]["code"] == "NOT_FOUND"
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    assert push_id not in foreign.text  # el id ajeno no se refleja
    assert _outbox(session, domain_events.SECURITY_OPERATION_REPORTED) == []
    assert _outbox(session, domain_events.FRAUD_ALERT_RAISED) == []
    assert session.scalars(sa.select(_audit_models.AuditLog)).all() == []
    session.expire_all()
    assert (
        len(
            session.scalars(
                sa.select(_identity_models.UserSession).where(
                    _identity_models.UserSession.user_id == USER,
                    _identity_models.UserSession.revoked_at.is_(None),
                )
            ).all()
        )
        == 1
    )  # sin bloqueo colateral


# ------------------------------------------------- barrido de PII en la jornada completa
def test_qa_full_journey_carries_no_pii(db, api_client, caplog):
    """Respuestas, payloads, eventos, auditoria y logs de notificar->reportar->cadena sin PII."""
    engine, session = db
    _make_user(session, USER, "owner")
    session.commit()
    secret_markers = ("@test.local", "titular-owner", "4242424242424242", "nonce", "biometr")
    with caplog.at_level(logging.INFO):
        out = notify_security_event(
            session,
            user_id=USER,
            event_type="transfer.settled",
            reference="op-qa16-limpia",
            device="Pixel 8",
            at=AT_ISO,
            location="Lima",
            sender_push=MockNotificationSender(),
            sender_email=MockNotificationSender(),
            now=NOW,
        )
        session.commit()
        push_id = out["channels"]["push"]["notification_id"]
        listed = api_client.get("/api/v1/notifications")
        read = api_client.post(f"/api/v1/notifications/{push_id}/read")
        report = api_client.post(
            f"/api/v1/notifications/{push_id}/report", json={"reason": "no fui yo"}
        )
    assert listed.status_code == 200 and read.status_code == 200 and report.status_code == 200
    _drive_report_chain(
        engine, session, _outbox(session, domain_events.SECURITY_OPERATION_REPORTED)[0]
    )

    dumped_payloads = json.dumps(
        [row.payload_json for row in session.scalars(sa.select(Notification)).all()],
        sort_keys=True,
        default=str,
    )
    assert "@" not in dumped_payloads  # destinatario por defecto = `user_id`, sin correo
    for payload in (
        listed.text,
        read.text,
        report.text,
        dumped_payloads,
        json.dumps(
            [
                entry.payload
                for entry in _outbox(session, domain_events.SECURITY_OPERATION_REPORTED)
            ],
            sort_keys=True,
        ),
        json.dumps(
            [entry.payload for entry in _outbox(session, domain_events.FRAUD_ALERT_RAISED)],
            sort_keys=True,
        ),
        json.dumps(
            [row.after_json for row in session.scalars(sa.select(_audit_models.AuditLog)).all()],
            sort_keys=True,
            default=str,
        ),
        caplog.text,
    ):
        for marker in secret_markers + ("no fui yo",):
            assert marker not in payload
    for entry in _outbox(session, domain_events.FRAUD_ALERT_RAISED):
        assert set(entry.payload) <= {"user_id", "notification_id", "reference", "reason", "at"}
