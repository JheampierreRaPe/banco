"""Fix A1/A2/A3/B2/B3/B4 (HU22, sprint-3): alertas de seguridad E2E.

Regresion del cableado multicanal (cierra `docs/18` G80 y CA-01 E2E):

- A1: una transferencia REAL por el motor (`execute_transfer`, ruta
  atomica propia) emite `transfer.settled` con `user_id`/`device_id` y el
  consumidor `on_sensitive_business_event` genera DOS filas (push +
  correo) con dispositivo/ubicacion/hora. Sin el fix: el payload no trae
  `user_id` (el consumidor omite) y el evento no esta suscrito (0 filas).
- A1/login: el mismo `auth.failed_attempt` consumido por `risk` (push
  E2-T20) + `notifications` (respaldo) deja exactamente UN push + UN
  correo (sin duplicar). Sin el fix: o falta el correo o hay dos push.
- A3: con un sender que valida como el Gmail real, el respaldo queda
  `SENT` via el resolutor cableado (`holder_contact`) y `FAILED` sin el.
- A2: el centro expone `device`/`location`/`reference` + detalle
  read-only `GET /notifications/{id}` (titular, `404` neutro).
- B2: el push se marca `SENT` sin esperar al correo lento.
- B3: el replay de un reporte viejo (mas de 50 confirmaciones despues)
  sigue siendo replay (marca transaccional + escaneo sin limite).
- B4: reportar una notificacion NO `security_*` -> `ValueError`/422.

SQLite en memoria + schemas ATTACH (sin red, sin `float`); senders mock
inyectados. Sin PII en aserciones.
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.modules.audit.models as _audit_models
import app.modules.identity.models as _identity_models
import app.modules.ledger.models as _ledger_models
import app.modules.notifications.models as _notifications_models
import app.modules.risk.models as _risk_models
import app.modules.shared.models as _shared_models
import app.modules.transactions.models as _transactions_models
from app.adapters.notification_sender import MockNotificationSender
from app.core.db import Base, get_db
from app.main import app
from app.modules.notifications import service as svc
from app.modules.notifications.api import get_current_user_id as notif_get_current_user_id

assert _audit_models is not None
assert _identity_models is not None
assert _ledger_models is not None
assert _notifications_models is not None
assert _risk_models is not None
assert _shared_models is not None
assert _transactions_models is not None

USER = uuid.uuid4()
OTHER = uuid.uuid4()
DEVICE = "Pixel 8"
AT_ISO = "2026-10-14T12:00:00+00:00"

TABLES = [
    "notifications.notifications",
    "notifications.notification_templates",
    "notifications.user_channel_preferences",
    "shared.outbox",
    "shared.processed_events",
    "transactions.transactions",
    "transactions.transaction_status_history",
    "transactions.holds",
    "ledger.ledger_accounts",
    "ledger.journal_entries",
    "ledger.postings",
    "ledger.ledger_balances",
    "risk.risk_profiles",
    "audit.audit_log",
    "identity.users",
]

SCHEMAS = (
    "notifications",
    "shared",
    "transactions",
    "ledger",
    "risk",
    "audit",
    "identity",
    "config",
)


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


def _fresh_session_factory(engine):
    def _factory():
        return Session(bind=engine, autoflush=False, expire_on_commit=False)

    return _factory


class _StubPort:
    """`BalancePort` con fondos (la alerta no depende del saldo real)."""

    class _View:
        available_minor = 10_000_000
        currency = "PEN"

    def lock_and_get(self, session, account_id):
        return self._View()

    def apply_delta(self, session, account_id, delta_minor, currency):
        return None


def _notifications_of(session, uid):
    return list(
        session.scalars(
            sa.select(_notifications_models.Notification)
            .where(_notifications_models.Notification.user_id == uid)
            .order_by(
                _notifications_models.Notification.created_at,
                _notifications_models.Notification.id,
            )
        ).all()
    )


# ------------------------------------------------- A1: cableado suscrito
def test_a1_security_alerts_wired_without_duplicates():
    from app.composition import is_security_alerts_wired, subscribe_security_alerts
    from app.core.bus import EventBus
    from app.modules.notifications import events as events_mod

    bus = EventBus()
    subscribe_security_alerts(bus)
    subscribe_security_alerts(bus)  # idempotente
    assert is_security_alerts_wired(bus) is True
    from app.core import events as domain_events

    assert bus.handler_count(domain_events.AUTH_FAILED_ATTEMPT) == 1
    for event_type in events_mod.SENSITIVE_BUSINESS_EVENTS:
        assert bus.handler_count(event_type) == 1, event_type
    assert set(events_mod.SENSITIVE_BUSINESS_EVENTS) == {
        "transfer.settled",
        "credit.disbursed",
        "loan.installment.paid",
        "qr.payment.confirmed",
        "service.paid",
        "topup.completed",
    }


# ------------------------------------------------- A1: transferencia real -> push + correo
def test_a1_real_transfer_settled_alerts_push_and_email(db):
    """`execute_transfer` real + consumidor real: 2 filas, un solo aviso."""
    from app.modules.notifications import events as events_mod
    from app.modules.transactions.service import execute_transfer

    engine, session = db
    tx = execute_transfer(
        session,
        source_account_id=uuid.uuid4(),
        target_account_id=uuid.uuid4(),
        amount_minor=1_000,
        currency="PEN",
        fee_minor=0,
        tx_type="OWN_TRANSFER",
        idempotency_key=f"fix-a1-{uuid.uuid4()}",
        initiator_user_id=USER,
        settle=True,
        atomic=True,
        balance_port=_StubPort(),
        device_id=DEVICE,
    )
    assert tx.status == "SETTLED"
    session.commit()
    session.expire_all()
    entries = list(
        session.scalars(
            sa.select(_shared_models.OutboxEntry).where(
                _shared_models.OutboxEntry.event_type == "transfer.settled",
                _shared_models.OutboxEntry.aggregate_id == tx.id,
            )
        ).all()
    )
    assert len(entries) == 1
    assert entries[0].payload.get("user_id") == str(USER)  # el emisor lo aporta
    assert entries[0].payload.get("device_id") == DEVICE

    factory = _fresh_session_factory(engine)
    push = MockNotificationSender()
    mail = MockNotificationSender()
    entry = entries[0]
    assert (
        events_mod.on_sensitive_business_event(
            SimpleNamespace(id=entry.id, event_type=entry.event_type, payload=dict(entry.payload)),
            session_factory=factory,
            sender_push=push,
            sender_email=mail,
        )
        is True
    )
    session.expire_all()
    rows = _notifications_of(session, USER)
    # Exactamente UN aviso multicanal: una fila push + una fila correo.
    assert len(rows) == 2
    by_channel = {row.channel: row for row in rows}
    assert set(by_channel) == {"push", "email"}
    assert by_channel["push"].template_code == "security_operation_alert"
    assert by_channel["email"].template_code == "security_operation_email"
    assert by_channel["push"].status == "SENT"
    assert by_channel["email"].status == "SENT"
    push_data = (by_channel["push"].payload_json or {}).get("data", {})
    assert push_data["device"] == DEVICE  # dispositivo real del emisor
    assert push_data["location"] == svc.UNKNOWN_LOCATION  # fallback aproximado
    assert push_data["at"]  # hora aportada por la fachada
    email_payload = by_channel["email"].payload_json or {}
    assert email_payload.get("reference") == str(tx.id)  # correlacion E6-T14
    # Replay del worker: sin duplicar.
    assert (
        events_mod.on_sensitive_business_event(
            SimpleNamespace(id=entry.id, event_type=entry.event_type, payload=dict(entry.payload)),
            session_factory=factory,
            sender_push=push,
            sender_email=mail,
        )
        is False
    )
    session.expire_all()
    assert len(_notifications_of(session, USER)) == 2


# ------------------------------------------------- A1/login: un solo push + correo de respaldo
def test_a1_login_produces_single_push_plus_email_backup(db):
    """`risk` (push E2-T20) + `notifications` (correo): 1 push + 1 email."""
    from app.modules.notifications import events as events_mod
    from app.modules.risk.events import on_auth_failed_attempt as risk_consumer

    engine, session = db
    entry_id = uuid.uuid4()
    payload = {
        "transaction_id": str(uuid.uuid4()),
        "user_id": str(USER),
        "device_id": DEVICE,
        "attempt": 1,
        "blocked": False,
        "reason": "invalid",
    }
    factory = _fresh_session_factory(engine)
    entry = SimpleNamespace(id=entry_id, payload=dict(payload))
    assert risk_consumer(entry, session_factory=factory, sender=MockNotificationSender()) is True
    assert (
        events_mod.on_auth_failed_attempt(
            entry,
            session_factory=factory,
            sender_push=MockNotificationSender(),
            sender_email=MockNotificationSender(),
        )
        is True
    )
    session.expire_all()
    rows = _notifications_of(session, USER)
    pushes = [row for row in rows if row.channel == "push"]
    emails = [row for row in rows if row.channel == "email"]
    # Exactamente UN aviso por evento: un push (E2-T20) + un correo (respaldo).
    assert len(pushes) == 1 and pushes[0].template_code == "auth_failed_attempt"
    assert len(emails) == 1 and emails[0].template_code == "security_operation_email"
    assert len(rows) == 2
    # Replay de ambos consumidores: sin nuevos efectos.
    assert risk_consumer(entry, session_factory=factory, sender=MockNotificationSender()) is False
    assert (
        events_mod.on_auth_failed_attempt(
            entry,
            session_factory=factory,
            sender_push=MockNotificationSender(),
            sender_email=MockNotificationSender(),
        )
        is False
    )
    session.expire_all()
    assert len(_notifications_of(session, USER)) == 2


# ------------------------------------------------- A3: respaldo con correo real resoluble
class _GmailLikeSender:
    """Mimetiza `GmailNotificationSender._validate_recipient` (sin red)."""

    def __init__(self):
        self.calls = []

    def send(self, *, channel, recipient, subject, body):
        from app.adapters.notification_sender import SendResult

        self.calls.append({"channel": channel, "recipient": recipient})
        to = str(recipient or "").strip()
        if channel != "email" or "@" not in to or "." not in to.split("@")[-1]:
            return SendResult(ok=False, error="recipient debe ser un email valido")
        return SendResult(ok=True, provider_ref="gmail-like-ref")


def test_a3_email_backup_resolves_holder_email(db):
    """Con resolutor: `SENT` al correo del titular; sin el: `FAILED` (gmail)."""
    from app.modules.identity import repository as identity_repo

    _, session = db
    holder = identity_repo.create_user(
        session,
        doc_type="DNI",
        doc_number_hash=f"doctest-a3-{USER.hex[:12]}",
        first_name="Titular",
        last_name="Atres",
        email="titular-a3@test.local",
    )
    session.flush()
    holder_id = holder.id
    # Sin resolutor: el fallback `str(user_id)` no es email -> FAILED real.
    svc.set_security_email_resolver(None)
    try:
        out = svc.notify_security_event(
            session,
            user_id=holder_id,
            event_type="transfer.settled",
            reference="op-a3",
            device=DEVICE,
            at=AT_ISO,
            location="Lima",
            sender_push=MockNotificationSender(),
            sender_email=_GmailLikeSender(),
        )
        assert out["channels"]["push"]["status"] == "SENT"
        assert out["channels"]["email"]["status"] == "FAILED"
        # Con el resolutor cableado (fachada `holder_contact`, D-03): SENT.
        from app.composition import wire_security_email_resolver

        assert wire_security_email_resolver() is not None
        out = svc.notify_security_event(
            session,
            user_id=holder_id,
            event_type="transfer.settled",
            reference="op-a3b",
            device=DEVICE,
            at=AT_ISO,
            location="Lima",
            sender_push=MockNotificationSender(),
            sender_email=_GmailLikeSender(),
        )
        assert out["channels"]["email"]["status"] == "SENT"
        session.expire_all()
        mails = [
            row
            for row in _notifications_of(session, holder_id)
            if row.channel == "email" and row.status == "SENT"
        ]
        assert mails and (mails[0].payload_json or {}).get("recipient") == "titular-a3@test.local"
    finally:
        svc.set_security_email_resolver(None)


# ------------------------------------------------- A2: centro con device/location/reference + detalle
def test_a2_center_exposes_device_location_reference_and_detail(db, api_client):
    _, session = db
    out = svc.notify_security_event(
        session,
        user_id=USER,
        event_type="transfer.settled",
        reference="op-a2",
        device=DEVICE,
        at=AT_ISO,
        location="Lima",
        sender_push=MockNotificationSender(),
        sender_email=MockNotificationSender(),
    )
    session.commit()
    push_id = out["channels"]["push"]["notification_id"]
    resp = api_client.get("/api/v1/notifications")
    assert resp.status_code == 200
    items = {item["channel"]: item for item in resp.json()["data"]}
    assert items["push"]["device"] == DEVICE
    assert items["push"]["location"] == "Lima"
    assert items["push"]["reference"] == "op-a2"
    assert "payload_json" not in resp.text and "recipient" not in resp.text
    detail = api_client.get(f"/api/v1/notifications/{push_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["id"] == push_id
    assert detail.json()["data"]["device"] == DEVICE
    assert api_client.get(f"/api/v1/notifications/{uuid.uuid4()}").status_code == 404
    assert api_client.get("/api/v1/notifications/no-uuid").status_code == 422
    # Aislamiento: otro titular ve 404 neutro (no el detalle ajeno).
    app.dependency_overrides[notif_get_current_user_id] = lambda: OTHER
    try:
        assert api_client.get(f"/api/v1/notifications/{push_id}").status_code == 404
    finally:
        app.dependency_overrides[notif_get_current_user_id] = lambda: USER


# ------------------------------------------------- B2: marca por canal sin acoplar latencia
def test_b2_push_marked_without_waiting_for_slow_email(db):
    from app.modules.notifications import repository as repo

    _, session = db
    marks: list[tuple[str, float]] = []
    real_mark_sent = repo.mark_sent

    def _recording_mark_sent(session_inner, notification_id, provider_ref, *, now=None):
        row = session_inner.get(_notifications_models.Notification, notification_id)
        marks.append((row.channel, time.monotonic()))
        return real_mark_sent(session_inner, notification_id, provider_ref, now=now)

    class _SlowMail(MockNotificationSender):
        def send(self, *, channel, recipient, subject, body):
            time.sleep(0.5)
            return super().send(channel=channel, recipient=recipient, subject=subject, body=body)

    import app.modules.notifications.service as service_mod

    monkeypatched = False
    original = service_mod.repo.mark_sent
    service_mod.repo.mark_sent = _recording_mark_sent
    try:
        out = svc.notify_security_event(
            session,
            user_id=USER,
            event_type="transfer.settled",
            reference="op-b2",
            device=DEVICE,
            at=AT_ISO,
            location="Lima",
            sender_push=MockNotificationSender(),
            sender_email=_SlowMail(),
        )
    finally:
        service_mod.repo.mark_sent = original
        monkeypatched = True
    assert monkeypatched
    assert out["channels"]["push"]["status"] == "SENT"
    assert out["channels"]["email"]["status"] == "SENT"
    by_channel = dict(marks)
    # El push se marco mucho antes de que terminara el correo lento.
    assert by_channel["push"] + 0.45 < by_channel["email"]


# ------------------------------------------------- B3: replay robusto aun con > 50 confirmaciones
def test_b3_old_report_replay_after_many_confirmations(db):
    from app.modules.notifications import repository as repo

    _, session = db
    first = svc.notify_security_event(
        session,
        user_id=USER,
        event_type="transfer.settled",
        reference="op-b3-vieja",
        device=DEVICE,
        at=AT_ISO,
        location="Lima",
        sender_push=MockNotificationSender(),
        sender_email=MockNotificationSender(),
    )
    session.commit()
    old_push_id = uuid.UUID(first["channels"]["push"]["notification_id"])
    assert (
        svc.report_unrecognized_operation(
            session, notification_id=old_push_id, user_id=USER, sender=MockNotificationSender()
        )["replayed"]
        is False
    )
    session.commit()
    # 55 reportes posteriores (mas que el viejo limite de 50).
    for i in range(55):
        nxt = svc.notify_security_event(
            session,
            user_id=USER,
            event_type="transfer.settled",
            reference=f"op-b3-{i}",
            device=DEVICE,
            at=AT_ISO,
            location="Lima",
            sender_push=MockNotificationSender(),
            sender_email=MockNotificationSender(),
        )
        session.commit()
        svc.report_unrecognized_operation(
            session,
            notification_id=uuid.UUID(nxt["channels"]["push"]["notification_id"]),
            user_id=USER,
            sender=MockNotificationSender(),
        )
        session.commit()
    # Re-reportar la alerta vieja: replay, sin nuevos efectos.
    outcome = svc.report_unrecognized_operation(
        session, notification_id=old_push_id, user_id=USER, sender=MockNotificationSender()
    )
    assert outcome == {
        "reported": True,
        "replayed": True,
        "notification_id": str(old_push_id),
        "confirmation_id": outcome["confirmation_id"],
    }
    session.commit()
    acks = list(
        session.scalars(
            sa.select(_notifications_models.Notification).where(
                _notifications_models.Notification.template_code == "security_report_received"
            )
        ).all()
    )
    assert len(acks) == 56
    linked = [
        row
        for row in acks
        if str((row.payload_json or {}).get("report_for") or "") == str(old_push_id)
    ]
    assert len(linked) == 1
    assert repo.get_notification(session, uuid.UUID(outcome["confirmation_id"])) is not None


# ------------------------------------------------- B4: solo notificaciones de seguridad
def test_b4_report_rejects_non_security_notification(db, api_client):
    _, session = db
    row = svc.send(
        session,
        channel="push",
        recipient=str(USER),
        template_code="login_alert",
        data={"device": DEVICE, "at": AT_ISO},
        user_id=USER,
        sender=MockNotificationSender(),
    )
    session.commit()
    with pytest.raises(ValueError, match="seguridad"):
        svc.report_unrecognized_operation(
            session, notification_id=row.id, user_id=USER, sender=MockNotificationSender()
        )
    session.rollback()
    resp = api_client.post(f"/api/v1/notifications/{row.id}/report", json={})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
