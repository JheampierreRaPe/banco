"""Accion "no reconozco esta operacion" -> bloqueo + alerta (E6-T14, HU22).

Cadena desacoplada por `outbox` (regla de oro 4/8, patron D-04):

- `POST /notifications/{id}/report` (solo el titular; `404` neutro)
  registra el reporte en `notifications` (acuse `security_report_received`
  + `read_at`), emite `security.operation_reported` y audita
  (`notifications.unrecognized_report`). Idempotente por estado.
- `risk` consume el reporte (consumidor `risk-unrecognized-reports`):
  sube la severidad en `risk_profiles`, audita
  (`risk.unrecognized_operation`) y emite `fraud.alert.raised`.
  **No** crea `risk.alerts` (HU20, D-16).
- `identity` consume la alerta (consumidor `identity-channel-lock`):
  revoca TODAS las sesiones del titular (`sessions.revoked_at`) y
  audita (`identity.sessions_revoked`). Nada se borra.

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
from app.core import events as domain_events
from app.core.db import Base, get_db
from app.main import app
from app.modules.identity import repository as identity_repo
from app.modules.identity.events import on_fraud_alert_raised
from app.modules.identity.service import sessions as sessions_service
from app.modules.notifications import service as notifications_service
from app.modules.notifications.api import get_current_user_id as notif_get_current_user_id
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
    """SQLite en memoria con los schemas de la cadena + `config.parameters`."""
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
    """Crea una sesion activa; retorna el refresh en claro (para el 401 posterior)."""
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


def _make_alert(session: Session, uid: uuid.UUID, reference: str):
    """Alerta de seguridad del titular; retorna la fila push."""
    outcome = notifications_service.notify_security_event(
        session,
        user_id=uid,
        event_type="transfer.settled",
        reference=reference,
        device="atm-1",
        at=AT_ISO,
        location="Lima",
    )
    session.commit()
    push_id = uuid.UUID(outcome["channels"]["push"]["notification_id"])
    row = session.get(_notifications_models.Notification, push_id)
    assert row is not None
    return row


def _report_events(session: Session):
    session.expire_all()
    return list(
        session.scalars(
            sa.select(OutboxEntry).where(
                OutboxEntry.event_type == domain_events.SECURITY_OPERATION_REPORTED
            )
        ).all()
    )


def _alert_events(session: Session):
    session.expire_all()
    return list(
        session.scalars(
            sa.select(OutboxEntry).where(OutboxEntry.event_type == domain_events.FRAUD_ALERT_RAISED)
        ).all()
    )


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
    alerts = _alert_events(session)
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


def _active_sessions(session: Session, uid: uuid.UUID):
    session.expire_all()
    return list(
        session.scalars(
            sa.select(_identity_models.UserSession).where(
                _identity_models.UserSession.user_id == uid,
                _identity_models.UserSession.revoked_at.is_(None),
            )
        ).all()
    )


def _audit_actions(session: Session):
    session.expire_all()
    rows = session.scalars(sa.select(_audit_models.AuditLog)).all()
    return sorted({row.action for row in rows})


# ------------------------------------------------- camino feliz (CA-01/CA-02/CA-03)


def test_report_escalates_and_locks_channels(db, api_client):
    engine, session = db
    _make_user(session, USER, "owner")
    session.commit()
    _make_session(session, USER, "a")
    _, clear_b = _make_session(session, USER, "b")
    session.commit()
    alert = _make_alert(session, USER, "op-e6t14")

    resp = api_client.post(f"/api/v1/notifications/{alert.id}/report", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["reported"] is True
    assert body["replayed"] is False
    assert body["notification_id"] == str(alert.id)

    reports = _report_events(session)
    assert len(reports) == 1
    assert reports[0].payload["user_id"] == str(USER)
    assert reports[0].payload["notification_id"] == str(alert.id)

    _drive_report_chain(engine, session, reports[0])

    profile = risk_service.get_or_create_profile(session, USER)
    assert int(profile.score) == risk_service.SCORE_PER_UNRECOGNIZED_REPORT
    assert "risk.unrecognized_operation" in _audit_actions(session)
    assert "identity.sessions_revoked" in _audit_actions(session)
    assert "notifications.unrecognized_report" in _audit_actions(session)

    # CA-03: todas las sesiones previas quedan revocadas...
    assert _active_sessions(session, USER) == []
    # ...el refresh viejo muere (reuso de revocado)...
    with pytest.raises(sessions_service.RefreshReuseError):
        sessions_service.refresh_session(session, refresh_token=clear_b)
    session.rollback()
    # ...pero el titular puede autenticarse de nuevo (nuevo login).
    fresh, _ = _make_session(session, USER, "fresh")
    session.commit()
    assert fresh.revoked_at is None


def test_report_accepts_optional_reason_without_storing_it_raw(db, api_client):
    engine, session = db
    _make_user(session, USER, "owner")
    session.commit()
    alert = _make_alert(session, USER, "op-reason")

    resp = api_client.post(f"/api/v1/notifications/{alert.id}/report", json={"reason": "no fui yo"})
    assert resp.status_code == 200, resp.text
    reports = _report_events(session)
    assert len(reports) == 1
    dumped = json.dumps(reports[0].payload, sort_keys=True)
    assert "no fui yo" not in dumped
    _drive_report_chain(engine, session, reports[0])
    rows = list(session.scalars(sa.select(_audit_models.AuditLog)).all())
    mine = [r for r in rows if r.action == "notifications.unrecognized_report"]
    assert len(mine) == 1
    assert mine[0].after_json.get("reason_present") is True
    assert "no fui yo" not in json.dumps(mine[0].after_json or {}, sort_keys=True)


# ------------------------------------------------- idempotencia (CA-04)


def test_double_report_and_event_replay_do_not_duplicate(db, api_client):
    engine, session = db
    _make_user(session, USER, "owner")
    session.commit()
    _make_session(session, USER, "a")
    session.commit()
    alert = _make_alert(session, USER, "op-idem")

    first = api_client.post(f"/api/v1/notifications/{alert.id}/report", json={})
    assert first.status_code == 200, first.text
    assert first.json()["data"]["replayed"] is False
    second = api_client.post(f"/api/v1/notifications/{alert.id}/report", json={})
    assert second.status_code == 200, second.text
    assert second.json()["data"]["reported"] is True
    assert second.json()["data"]["replayed"] is True
    assert len(_report_events(session)) == 1

    report = _report_events(session)[0]
    factory = _fresh_session_factory(engine)
    assert (
        on_unrecognized_operation_reported(
            SimpleNamespace(id=report.id, payload=dict(report.payload)),
            session_factory=factory,
        )
        is True
    )
    assert (
        on_unrecognized_operation_reported(
            SimpleNamespace(id=report.id, payload=dict(report.payload)),
            session_factory=factory,
        )
        is False
    )
    session.expire_all()
    alerts = _alert_events(session)
    assert len(alerts) == 1
    assert (
        on_fraud_alert_raised(
            SimpleNamespace(id=alerts[0].id, payload=dict(alerts[0].payload)),
            session_factory=factory,
        )
        is True
    )
    assert (
        on_fraud_alert_raised(
            SimpleNamespace(id=alerts[0].id, payload=dict(alerts[0].payload)),
            session_factory=factory,
        )
        is False
    )
    assert len(_alert_events(session)) == 1
    assert _active_sessions(session, USER) == []


# ------------------------------------------------- errores neutros (CA-01)


def test_report_foreign_notification_is_neutral_404_without_effects(db, api_client):
    _make_user(db[1], USER, "owner")
    _make_user(db[1], OTHER, "other")
    db[1].commit()
    _make_session(db[1], USER, "a")
    db[1].commit()
    alert = _make_alert(db[1], USER, "op-ajena")

    _act_as(OTHER)
    try:
        resp = api_client.post(f"/api/v1/notifications/{alert.id}/report", json={})
    finally:
        _act_as(USER)
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert _report_events(db[1]) == []
    assert _alert_events(db[1]) == []
    assert len(_active_sessions(db[1], USER)) == 1


def test_report_missing_notification_is_neutral_404_without_effects(db, api_client):
    _make_user(db[1], USER, "owner")
    db[1].commit()
    resp = api_client.post(f"/api/v1/notifications/{uuid.uuid4()}/report", json={})
    assert resp.status_code == 404, resp.text
    assert _report_events(db[1]) == []


def test_report_without_bearer_is_401(db, api_client):
    app.dependency_overrides.pop(notif_get_current_user_id, None)
    try:
        resp = api_client.post(f"/api/v1/notifications/{uuid.uuid4()}/report", json={})
    finally:
        _act_as(USER)
    assert resp.status_code == 401, resp.text


def test_report_malformed_id_is_422(db, api_client):
    resp = api_client.post("/api/v1/notifications/no-uuid/report", json={})
    assert resp.status_code == 422, resp.text


# ------------------------------------------------- frontera (CA-05)


def test_notifications_never_touches_risk_or_identity_tables():
    from pathlib import Path

    base = Path(__file__).resolve().parents[1]
    for rel in (
        Path("app/modules/notifications/service/__init__.py"),
        Path("app/modules/notifications/api/__init__.py"),
        Path("app/modules/notifications/events/__init__.py"),
    ):
        content = (base / rel).read_text(encoding="utf-8")
        assert "modules.risk" not in content, f"{rel} no debe importar risk"
        assert "modules.identity" not in content, f"{rel} no debe importar identity"
        assert "risk_profiles" not in content, f"{rel} no debe tocar risk_profiles"
        assert "revoke_user_sessions" not in content, f"{rel} no debe revocar sesiones"
    # `service/` no confirma (el llamante decide; el endpoint de `api/`
    # confirma por diseno y `events/` confirma su propia sesion D-04).
    service_src = (base / "app/modules/notifications/service/__init__.py").read_text(
        encoding="utf-8"
    )
    assert ".commit(" not in service_src
    risk_service_src = (base / "app/modules/risk/service/__init__.py").read_text(encoding="utf-8")
    assert "modules.identity" not in risk_service_src
    assert "revoke_user_sessions" not in risk_service_src
    assert "UserSession" not in risk_service_src
    assert "RiskAlert" not in risk_service_src
    assert "risk_models.Alert" not in risk_service_src
    identity_events_src = (base / "app/modules/identity/events/__init__.py").read_text(
        encoding="utf-8"
    )
    assert "modules.risk" not in identity_events_src
    assert "modules.notifications" not in identity_events_src


def test_risk_never_creates_alert_rows(db, api_client):
    engine, session = db
    _make_user(session, USER, "owner")
    session.commit()
    alert = _make_alert(session, USER, "op-sin-alerts")
    resp = api_client.post(f"/api/v1/notifications/{alert.id}/report", json={})
    assert resp.status_code == 200, resp.text
    _drive_report_chain(engine, session, _report_events(session)[0])
    from app.modules.risk import models as risk_models

    assert not hasattr(risk_models, "RiskAlert")
    assert not hasattr(risk_models, "Alert")


def test_event_payloads_and_logs_carry_no_pii(db, api_client, caplog):
    engine, session = db
    _make_user(session, USER, "owner")
    session.commit()
    alert = _make_alert(session, USER, "op-limpia")
    with caplog.at_level(logging.INFO, logger="app.modules.notifications.service"):
        resp = api_client.post(f"/api/v1/notifications/{alert.id}/report", json={})
    assert resp.status_code == 200, resp.text
    _drive_report_chain(engine, session, _report_events(session)[0])
    for entry in _report_events(session) + _alert_events(session):
        assert set(entry.payload) <= {
            "user_id",
            "notification_id",
            "reference",
            "reason",
            "at",
        }, entry.payload
        dumped = json.dumps(entry.payload, sort_keys=True)
        assert "@" not in dumped
        assert "titular-" not in dumped
    for record in caplog.records:
        text = record.getMessage()
        assert "@test.local" not in text
        assert "titular-" not in text


# ------------------------------------------------- contrato y cableado


def test_openapi_exposes_report_route(db, api_client):
    resp = api_client.get("/openapi.json")
    assert resp.status_code == 200, resp.text
    paths = resp.json()["paths"]
    assert "/api/v1/notifications/{notification_id}/report" in paths
    assert "post" in paths["/api/v1/notifications/{notification_id}/report"]


def test_bus_wiring_is_idempotent():
    from app.composition import (
        is_channel_lock_wired,
        is_unrecognized_reports_wired,
        subscribe_channel_lock,
        subscribe_unrecognized_reports,
    )

    subscribe_unrecognized_reports()
    subscribe_channel_lock()
    subscribe_unrecognized_reports()
    subscribe_channel_lock()
    assert is_unrecognized_reports_wired() is True
    assert is_channel_lock_wired() is True
    from app.core import events as domain_events
    from app.core.bus import get_default_bus

    assert get_default_bus().handler_count(domain_events.SECURITY_OPERATION_REPORTED) == 1
    assert get_default_bus().handler_count(domain_events.FRAUD_ALERT_RAISED) == 1
