"""Servicio de notificaciones de seguridad + centro (E6-T12, HU22).

- Camino feliz: `notify_security_event` registra DOS filas (`push` +
  `email`) y el push queda `SENT` dentro de la ventana `< 5 s` (medido
  con reloj real y `sleep_fn` inyectable que registra esperas).
- Paralelismo real: los dos canales se entregan en paralelo (rendezvous
  entre senders: si fuera secuencial, el primero expiraria).
- Preferencias: con `push=false` no hay fila push pero si el respaldo
  `email`; con `email=false` el respaldo se garantiza igual (decision
  del dueno documentada en el servicio).
- Multi-canal: sender caido (blando o duro) deja SOLO su fila `FAILED`
  con `error` y no impide la entrega del otro canal.
- Centro: `GET /notifications` aislado por titular; `POST .../read`
  idempotente y `404` neutro; `GET/PUT /preferences` idempotentes.
- Migracion: cadena con un solo head `0044` + `downgrade` reversible
  (en Postgres; se omite sin BD).
- Sin PII/secretos en logs ni en `payload_json`/respuestas.

SQLite en memoria + schemas ATTACH (sin red, sin `float`); HTTP real
(TestClient) con auth falsa por `dependency_overrides`.
"""

from __future__ import annotations

import logging
import threading
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.modules.notifications.models as _notifications
import app.modules.shared.models as _shared
from app.adapters.notification_sender import (
    MockNotificationSender,
    PushNotificationSender,
    SendResult,
)
from app.core.db import Base, get_db
from app.main import app
from app.modules.notifications import service as svc
from app.modules.notifications.api import get_current_user_id as notif_get_current_user_id
from app.modules.notifications.domain.templates import BASE_TEMPLATES, render_template

assert _notifications is not None and _shared is not None

USER = uuid.uuid4()
OTHER = uuid.uuid4()

AT_ISO = "2026-10-13T12:00:00+00:00"

TABLES = [
    "notifications.notifications",
    "notifications.notification_templates",
    "notifications.user_channel_preferences",
    "shared.processed_events",
]

SCHEMAS = ("notifications", "shared", "config")


def _seed_param(session: Session, key: str, value: str, module: str = "notifications") -> None:
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, :module)"
        ),
        {"id": uuid.uuid4().hex, "key": key, "value": value, "module": module},
    )
    session.flush()


@pytest.fixture()
def db():
    """SQLite en memoria con los schemas propios + `processed_events`."""
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


def _notify(session, **kwargs):
    params = {
        "user_id": USER,
        "event_type": "transfer.settled",
        "reference": "op-abc123",
        "device": "Pixel 8",
        "at": AT_ISO,
        "location": "Lima",
        "sender_push": MockNotificationSender(),
        "sender_email": MockNotificationSender(),
    }
    params.update(kwargs)
    return svc.notify_security_event(session, **params)


# ------------------------------------------------- plantillas de seguridad
def test_security_templates_registered_without_pii_placeholders():
    assert BASE_TEMPLATES["security_operation_alert"]["channel"] == "push"
    assert BASE_TEMPLATES["security_operation_email"]["channel"] == "email"
    assert BASE_TEMPLATES["security_report_received"]["channel"] == "push"
    push_body = render_template(
        BASE_TEMPLATES["security_operation_alert"]["body_template"],
        {"operation": "transfer.settled", "at": AT_ISO, "device": "Pixel 8", "location": "Lima"},
    )
    assert "{" not in push_body and "}" not in push_body
    assert "Pixel 8" in push_body and "Lima" in push_body and AT_ISO in push_body
    email_body = render_template(
        BASE_TEMPLATES["security_operation_email"]["body_template"],
        {
            "operation": "transfer.settled",
            "at": AT_ISO,
            "device": "Pixel 8",
            "location": "Lima",
            "reference": "op-abc123",
        },
    )
    assert "op-abc123" in email_body
    ack = render_template(
        BASE_TEMPLATES["security_report_received"]["body_template"],
        {"reference": "rep-1", "at": AT_ISO},
    )
    assert "rep-1" in ack


# ------------------------------------------------- camino feliz < 5 s
def test_happy_path_registers_two_rows_push_sent_within_window(db):
    _, session = db
    sleeps: list[float] = []
    push = MockNotificationSender()
    mail = MockNotificationSender()
    out = svc.notify_security_event(
        session,
        user_id=USER,
        event_type="transfer.settled",
        reference="op-abc123",
        device="Pixel 8",
        at=AT_ISO,
        location="Lima",
        sender_push=push,
        sender_email=mail,
        sleep_fn=lambda s: sleeps.append(s),
    )
    session.commit()
    assert set(out["channels"]) == {"push", "email"}
    assert out["channels"]["push"]["status"] == "SENT"
    assert out["channels"]["email"]["status"] == "SENT"
    assert out["elapsed_seconds"] < out["window_seconds"] <= 5
    assert sleeps == []  # camino feliz sin esperas
    rows = session.scalars(
        sa.select(_notifications.Notification).where(_notifications.Notification.user_id == USER)
    ).all()
    assert len(rows) == 2
    by_channel = {row.channel: row for row in rows}
    assert by_channel["push"].template_code == "security_operation_alert"
    assert by_channel["email"].template_code == "security_operation_email"
    assert by_channel["push"].provider_ref
    # Sin PII sensible en el payload persistido (solo datos de render).
    for row in rows:
        raw = str(row.payload_json)
        for secret in ("4242424242424242", "nonce", "biometr", "firma-secreta"):
            assert secret not in raw
    data = (by_channel["push"].payload_json or {}).get("data", {})
    # Contenido humano persistido: etiqueta legible + fecha local (el
    # codigo crudo vive en `event_type` para correlacion, no en el texto).
    assert data == {
        "operation": "Transferencia realizada",
        "at": "13/10/2026 07:00",
        "device": "Pixel 8",
        "location": "Lima",
    }


def test_channels_deliver_in_parallel_not_sequential(db):
    """Rendezvous: cada sender solo completa si el otro ya empezo."""

    class _RendezvousSender:
        def __init__(self, channel, started, other_started):
            self._channel = channel
            self._started = started
            self._other_started = other_started

        def send(self, *, channel, recipient, subject, body):
            assert channel == self._channel
            self._started.set()
            if not self._other_started.wait(timeout=5):
                raise AssertionError(f"{channel}: el otro canal nunca empezo (secuencial)")
            return SendResult(ok=True, provider_ref=f"rz-{channel}", error=None)

    _, session = db
    push_started, mail_started = threading.Event(), threading.Event()
    out = svc.notify_security_event(
        session,
        user_id=USER,
        event_type="auth.failed_attempt",
        reference="att-7",
        device="Pixel 8",
        at=AT_ISO,
        location="Lima",
        sender_push=_RendezvousSender("push", push_started, mail_started),
        sender_email=_RendezvousSender("email", mail_started, push_started),
    )
    assert out["channels"]["push"]["status"] == "SENT"
    assert out["channels"]["email"]["status"] == "SENT"


def test_location_defaults_without_pii_when_missing(db):
    _, session = db
    out = _notify(session, location=None, device=None, at=None)
    assert out["channels"]["push"]["status"] == "SENT"
    row = session.scalars(
        sa.select(_notifications.Notification).where(_notifications.Notification.channel == "push")
    ).one()
    data = (row.payload_json or {}).get("data", {})
    assert data["location"] == "no disponible"
    assert data["device"] == "no disponible"
    assert data["at"]  # instante aportado por la fachada


def test_invalid_inputs_fail_fast_without_rows(db):
    _, session = db
    with pytest.raises(ValueError):
        _notify(session, user_id="no-uuid")
    with pytest.raises(ValueError):
        _notify(session, event_type="  ")
    with pytest.raises(ValueError):
        _notify(session, reference="")
    assert session.scalars(sa.select(_notifications.Notification)).all() == []


# ------------------------------------------------- preferencias
def test_preferences_defaults_then_persisted(db):
    _, session = db
    assert svc.get_channel_preferences(session, USER) == {
        "push": True,
        "email": True,
        "sms": False,
    }
    saved = svc.set_channel_preferences(session, user_id=USER, push=False, email=True, sms=False)
    assert saved == {"push": False, "email": True, "sms": False}
    session.commit()
    assert svc.get_channel_preferences(session, USER) == saved
    # Idempotente: repetir no duplica filas.
    svc.set_channel_preferences(session, user_id=USER, push=False, email=True, sms=False)
    session.commit()
    rows = session.scalars(sa.select(_notifications.UserChannelPreference)).all()
    assert len(rows) == 1


def test_preferences_reject_non_bool(db):
    _, session = db
    with pytest.raises(TypeError):
        svc.set_channel_preferences(session, user_id=USER, push="si", email=True, sms=False)


def test_push_disabled_skips_push_but_keeps_email_backup(db):
    _, session = db
    svc.set_channel_preferences(session, user_id=USER, push=False, email=True, sms=False)
    out = _notify(session)
    assert set(out["channels"]) == {"email"}
    assert out["channels"]["email"]["status"] == "SENT"


def test_email_disabled_still_guarantees_security_backup(db):
    """Decision del dueno: el email de seguridad no se puede desactivar."""
    _, session = db
    svc.set_channel_preferences(session, user_id=USER, push=True, email=False, sms=False)
    out = _notify(session)
    assert set(out["channels"]) == {"push", "email"}
    assert out["channels"]["email"]["status"] == "SENT"


def test_email_backup_flag_off_respects_opt_out(db):
    _, session = db
    svc.set_channel_preferences(session, user_id=USER, push=True, email=False, sms=False)
    _seed_param(session, svc.SECURITY_EMAIL_BACKUP_KEY, "false")
    session.commit()
    out = _notify(session)
    assert set(out["channels"]) == {"push"}


# ------------------------------------------------- multi-canal tolerante
def test_push_down_only_marks_push_failed(db):
    _, session = db
    out = svc.notify_security_event(
        session,
        user_id=USER,
        event_type="transfer.settled",
        reference="op-down",
        device="Pixel 8",
        at=AT_ISO,
        location="Lima",
        sender_push=PushNotificationSender(mode="down"),
        sender_email=MockNotificationSender(),
    )
    assert out["channels"]["push"]["status"] == "FAILED"
    assert out["channels"]["push"]["error"]
    assert out["channels"]["email"]["status"] == "SENT"
    # La fila push es reimpulsable con `retry_notification`.
    push_id = uuid.UUID(out["channels"]["push"]["notification_id"])
    row = svc.retry_notification(session, push_id, sender=PushNotificationSender(mode="ok"))
    assert row.status == "SENT"


def test_push_hard_outage_does_not_block_email(db):
    _, session = db
    out = svc.notify_security_event(
        session,
        user_id=USER,
        event_type="transfer.settled",
        reference="op-timeout",
        device="Pixel 8",
        at=AT_ISO,
        location="Lima",
        sender_push=PushNotificationSender(mode="timeout"),
        sender_email=MockNotificationSender(),
    )
    assert out["channels"]["push"]["status"] == "FAILED"
    assert out["channels"]["email"]["status"] == "SENT"


def test_email_down_only_marks_email_failed(db):
    _, session = db
    mail = MockNotificationSender()
    mail.set_channel_failure("email", "gmail caido")
    out = _notify(session, sender_email=mail)
    assert out["channels"]["email"]["status"] == "FAILED"
    assert out["channels"]["push"]["status"] == "SENT"


def test_email_hard_raise_does_not_block_push(db):
    _, session = db
    mail = MockNotificationSender()
    mail.set_channel_raise("email", "gmail caido duro")
    out = _notify(session, sender_email=mail)
    assert out["channels"]["email"]["status"] == "FAILED"
    assert out["channels"]["push"]["status"] == "SENT"


# ------------------------------------------------- configuracion
def test_security_config_defaults_and_overrides(db):
    _, session = db
    assert svc.get_security_delivery_window_seconds(session) == 5
    assert svc.is_security_email_backup_enabled(session) is True
    _seed_param(session, svc.SECURITY_DELIVERY_WINDOW_SECONDS_KEY, "8")
    _seed_param(session, svc.SECURITY_EMAIL_BACKUP_KEY, "false")
    session.commit()
    assert svc.get_security_delivery_window_seconds(session) == 8
    assert svc.is_security_email_backup_enabled(session) is False
    session.execute(
        sa.text("UPDATE config.parameters SET value_json = :v WHERE key = :k"),
        {"v": '"roto"', "k": svc.SECURITY_DELIVERY_WINDOW_SECONDS_KEY},
    )
    assert svc.get_security_delivery_window_seconds(session) == 5  # fallback


# ------------------------------------------------- centro (HTTP)
def test_center_lists_only_own_without_pii(api_client, db):
    _, session = db
    _notify(session, reference="op-mia")
    _notify(session, user_id=OTHER, reference="op-ajena")
    session.commit()
    resp = api_client.get("/api/v1/notifications")
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["total"] == 2
    assert len(body["data"]) == 2
    for item in body["data"]:
        assert set(item) == {
            "id",
            "channel",
            "template_code",
            "status",
            "created_at",
            "read_at",
            "device",
            "location",
            "reference",
        }
        assert "payload_json" not in item and "recipient" not in str(item)
    mine = {item["channel"]: item for item in body["data"]}
    assert mine["push"]["device"] == "Pixel 8"
    assert mine["push"]["location"] == "Lima"
    assert mine["push"]["reference"] == "op-mia"
    assert mine["email"]["reference"] == "op-mia"
    only_push = api_client.get("/api/v1/notifications", params={"channel": "push"})
    assert only_push.status_code == 200
    assert only_push.json()["meta"]["total"] == 1
    assert only_push.json()["data"][0]["channel"] == "push"
    bad = api_client.get("/api/v1/notifications", params={"channel": "paloma"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


def test_center_unread_filter_and_pagination(api_client, db):
    _, session = db
    out = _notify(session, reference="op-1")
    session.commit()
    push_id = out["channels"]["push"]["notification_id"]
    marked = api_client.post(f"/api/v1/notifications/{push_id}/read")
    assert marked.status_code == 200
    assert marked.json()["data"]["read_at"] is not None
    unread = api_client.get("/api/v1/notifications", params={"unread": "true"})
    assert unread.json()["meta"]["total"] == 1
    page = api_client.get("/api/v1/notifications", params={"page": 2, "page_size": 1})
    assert page.status_code == 200
    meta = page.json()["meta"]
    assert (meta["page"], meta["page_size"], meta["total"]) == (2, 1, 2)
    assert len(page.json()["data"]) == 1


def test_center_read_is_idempotent_and_neutral_404(api_client, db):
    _, session = db
    out = _notify(session, reference="op-r")
    foreign = _notify(session, user_id=OTHER, reference="op-x")
    session.commit()
    mine = out["channels"]["push"]["notification_id"]
    first = api_client.post(f"/api/v1/notifications/{mine}/read")
    second = api_client.post(f"/api/v1/notifications/{mine}/read")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["data"]["read_at"] == second.json()["data"]["read_at"]
    other_id = foreign["channels"]["push"]["notification_id"]
    resp = api_client.post(f"/api/v1/notifications/{other_id}/read")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    missing = api_client.post(f"/api/v1/notifications/{uuid.uuid4()}/read")
    assert missing.status_code == 404
    malformed = api_client.post("/api/v1/notifications/no-uuid/read")
    assert malformed.status_code == 422


def test_center_preferences_roundtrip(api_client, db):
    _, session = db
    current = api_client.get("/api/v1/notifications/preferences")
    assert current.status_code == 200
    assert current.json()["data"] == {"push": True, "email": True, "sms": False}
    updated = api_client.put(
        "/api/v1/notifications/preferences",
        json={"push": False, "email": True, "sms": False},
    )
    assert updated.status_code == 200
    assert updated.json()["data"] == {"push": False, "email": True, "sms": False}
    again = api_client.put(
        "/api/v1/notifications/preferences",
        json={"push": False, "email": True, "sms": False},
    )
    assert again.status_code == 200
    rows = session.scalars(
        sa.select(_notifications.UserChannelPreference).where(
            _notifications.UserChannelPreference.user_id == USER
        )
    ).all()
    assert len(rows) == 1  # idempotente por user_id
    incomplete = api_client.put("/api/v1/notifications/preferences", json={"push": True})
    assert incomplete.status_code == 422


def test_center_requires_auth_and_isolates_holders(api_client, db):
    _, session = db
    _notify(session, reference="op-mia")
    _notify(session, user_id=OTHER, reference="op-ajena")
    session.commit()
    app.dependency_overrides.pop(notif_get_current_user_id, None)
    try:
        from fastapi.testclient import TestClient

        with TestClient(app) as naked:
            assert naked.get("/api/v1/notifications").status_code == 401
            assert naked.get("/api/v1/notifications/preferences").status_code == 401
    finally:
        app.dependency_overrides[notif_get_current_user_id] = lambda: USER
    _act_as(OTHER)
    assert api_client.get("/api/v1/notifications").json()["meta"]["total"] == 2
    _act_as(USER)
    assert api_client.get("/api/v1/notifications").json()["meta"]["total"] == 2


def test_center_openapi_lists_routes():
    spec = app.openapi()
    paths = spec["paths"]
    assert "/api/v1/notifications" in paths
    assert "/api/v1/notifications/{notification_id}" in paths
    assert "/api/v1/notifications/{notification_id}/read" in paths
    assert "/api/v1/notifications/preferences" in paths


# ------------------------------------------------- consumidor de eventos
def test_consumer_alerts_once_then_ignores_duplicates(db):
    """El consumidor de login solo aporta el correo (A1: sin duplicar el push de E2-T20)."""
    from types import SimpleNamespace

    from app.modules.notifications import events as events_mod

    _, session = db
    push = MockNotificationSender()
    mail = MockNotificationSender()
    entry = SimpleNamespace(
        id=uuid.uuid4(),
        payload={
            "user_id": str(USER),
            "device_id": "Pixel 8",
            "at": AT_ISO,
            "city": "Lima",
            "reference": "att-9",
        },
    )

    calls: list[Session] = []

    def _factory():
        calls.append(session)
        return session

    assert (
        events_mod.on_auth_failed_attempt(
            entry, session_factory=_factory, sender_push=push, sender_email=mail
        )
        is True
    )
    assert (
        events_mod.on_auth_failed_attempt(
            entry, session_factory=_factory, sender_push=push, sender_email=mail
        )
        is False
    )
    rows = session.scalars(
        sa.select(_notifications.Notification).where(_notifications.Notification.user_id == USER)
    ).all()
    # A1: exactamente UN correo de respaldo y NINGUN push (el push de
    # login lo emite `risk`/E2-T20 sobre el mismo evento; este seam no lo
    # duplica). El replay no agrega filas (idempotencia intacta).
    assert len(rows) == 1  # sin duplicar la alerta
    assert rows[0].channel == "email"
    assert rows[0].template_code == "security_operation_email"
    assert rows[0].status == "SENT"
    assert events_mod.SECURITY_CONSUMER == "notifications-security"
    assert len(events_mod.SECURITY_CONSUMER) <= 60


def test_consumer_rejects_event_without_user(db):
    from types import SimpleNamespace

    from app.modules.notifications import events as events_mod

    _, session = db
    entry = SimpleNamespace(id=uuid.uuid4(), payload={"device_id": "Pixel 8"})

    with pytest.raises(ValueError):
        events_mod.on_auth_failed_attempt(entry, session_factory=lambda: session)
    assert session.scalars(sa.select(_notifications.Notification)).all() == []


# ------------------------------------------------- sin PII en logs
def test_no_pii_in_logs(db, caplog):
    _, session = db
    secret_device = "Pixel-secreto-xyz"
    secret_mail = "titular-secreto@example.com"
    push = MockNotificationSender()
    mail = MockNotificationSender()
    with caplog.at_level(logging.INFO, logger="app.modules.notifications.service"):
        _notify(
            session,
            device=secret_device,
            recipient_email=secret_mail,
            sender_push=push,
            sender_email=mail,
        )
    assert secret_device not in caplog.text
    assert secret_mail not in caplog.text
    assert "security_operation_alert" in caplog.text


# ------------------------------------------------- migracion 0045
def test_migration_chain_has_single_head_0044():
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend_dir = Path(__file__).resolve().parent.parent
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    revisions = {rev.revision: rev.down_revision for rev in script.walk_revisions()}
    heads = [rev for rev in revisions if rev not in revisions.values()]
    # Contenido pulido (2026-10-15): `0045_notification_email_content`
    # (textos de las semillas, tras 0044). La cabeza vigente es
    # `0046_services_settlement` (wallet, tras 0045): un solo head.
    assert revisions.get("0046_services_settlement") == "0045_notification_email_content"
    assert revisions.get("0045_notification_email_content") == "0044_notifications_preferences"
    assert revisions.get("0044_notifications_preferences") == "0043_wallet_service_reminder"
    assert revisions.get("0043_wallet_service_reminder") == "0042_wallet_services"
    # E4-T21 (2026-10-15): la cabeza avanza a 0047_wallet_personal_qr
    # (QR personal estatico, tras 0046).
    assert revisions.get("0047_wallet_personal_qr") == "0046_services_settlement"
    assert heads == ["0047_wallet_personal_qr"], f"heads inesperados: {heads}"
    content = (
        backend_dir / "migrations" / "versions" / "0045_notification_email_content.py"
    ).read_text(encoding="utf-8")
    assert "def upgrade" in content and "def downgrade" in content
    assert len("0045_notification_email_content") <= 32
    content_0044 = (
        backend_dir / "migrations" / "versions" / "0044_notifications_preferences.py"
    ).read_text(encoding="utf-8")
    assert "user_channel_preferences" in content_0044
    assert "notifications.security_delivery_window_seconds" in content_0044
    assert "notifications.security_alerts_email_backup" in content_0044


def test_migration_0044_upgrade_downgrade_roundtrip():
    """En Postgres: tabla creada con defaults + `downgrade` reversible."""
    from tests.db_utils import (
        downgrade_in_process,
        make_test_engine,
        resolve_test_database_url,
        run_migrations_in_process,
    )

    try:
        url = resolve_test_database_url()
        engine = make_test_engine(url)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - sin BD se omite
        pytest.skip(f"Postgres de prueba no disponible ({exc})")
        return
    try:
        downgrade_in_process(url, "0043_wallet_service_reminder")
        with engine.connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    sa.text("SELECT tablename FROM pg_tables " "WHERE schemaname = 'notifications'")
                )
            }
        assert "user_channel_preferences" not in tables
        run_migrations_in_process(url)
        with engine.connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    sa.text("SELECT tablename FROM pg_tables " "WHERE schemaname = 'notifications'")
                )
            }
            assert "user_channel_preferences" in tables
            probe = str(uuid.uuid4())
            conn.execute(
                sa.text(
                    "INSERT INTO notifications.user_channel_preferences (user_id) "
                    "VALUES (CAST(:uid AS uuid))"
                ),
                {"uid": probe},
            )
            row = conn.execute(
                sa.text(
                    "SELECT push, email, sms FROM "
                    "notifications.user_channel_preferences WHERE user_id = CAST(:uid AS uuid)"
                ),
                {"uid": probe},
            ).one()
            assert tuple(row) == (True, True, False)  # fila por defecto
            keys = {
                row[0]
                for row in conn.execute(
                    sa.text("SELECT key FROM config.parameters WHERE key LIKE 'notifications.%'")
                )
            }
            assert "notifications.security_delivery_window_seconds" in keys
            assert "notifications.security_alerts_email_backup" in keys
            conn.execute(
                sa.text(
                    "DELETE FROM notifications.user_channel_preferences "
                    "WHERE user_id = CAST(:uid AS uuid)"
                ),
                {"uid": probe},
            )
            conn.commit()
    finally:
        run_migrations_in_process(url)
        engine.dispose()
