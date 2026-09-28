"""Evento a `risk` y notificacion ante fallo de autorizacion (E2-T20, HU08).

Sobre SQLite en memoria + schemas ATTACH, sin red, sin `float`, sin PII:
dinero de juguete (UUIDs sinteticos).

Mapeo brief -> test:
- fallo emite `auth.failed_attempt` una vez ........ `test_failed_attempt_*`
- bloqueo preventivo emite/notifica (CA-02) ....... `test_preventive_block_*`
- `risk` registra + notifica al cliente (CA-04) .... `test_consumer_*`
- replay no duplica (emisor y consumidor) ......... `test_replay_*`, `test_duplicate_*`
- concurrencia: dos consumidores, un registro ..... `test_concurrent_*`
- sin PII en payload/cuerpo ....................... `test_no_pii_*`
- fallo de envio -> reintento ..................... `test_delivery_retry_*`
- outbox -> bus -> consumidor end-to-end .......... `test_end_to_end_*`
- cableado del bus ................................ `test_wiring_*`
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

USER_A = uuid.uuid4()
DEVICE_A = "dev-e2t20-1"
SECRET_SIG = "SIG:secreta-manipulada"
PUBLIC_KEY = "pk-test-e2t20"

TABLES = [
    "transactions.transactions",
    "transactions.transaction_status_history",
    "transactions.holds",
    "shared.outbox",
    "shared.processed_events",
    "risk.risk_profiles",
    "notifications.notifications",
    "notifications.notification_templates",
    "audit.audit_log",
]

SCHEMAS = ("transactions", "shared", "risk", "notifications", "audit")


def _make_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.audit.models as _au  # noqa: F401 (registro de tablas)
    import app.modules.notifications.models as _n  # noqa: F401
    import app.modules.risk.models as _r  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401
    import app.modules.transactions.models as _t  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in SCHEMAS:
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    return engine


@pytest.fixture()
def engine():
    eng = _make_engine()
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def session(engine):
    s = Session(engine)
    try:
        yield s
    finally:
        s.close()


def _pending_tx(session: Session, *, user_id=USER_A, device: str | None = DEVICE_A):
    """Transaccion en `PENDING_AUTHORIZATION` con reto biometrico."""
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio

    tx = tx_repo.create_transaction(
        session,
        type="THIRD_PARTY_TRANSFER",
        amount_minor=5_000,
        currency="PEN",
        initiator_user_id=user_id,
        idempotency_key=f"e2t20-{uuid.uuid4()}",
    )
    tx_repo.transition_transaction(session, tx.id, "VALIDATED", reason="entrada valida")
    block, _nonce, _exp = bio.new_challenge_block(device)
    bio.save_block(session, tx, block)
    tx = tx_repo.transition_transaction(
        session, tx.id, "PENDING_AUTHORIZATION", reason=bio.REQUIRED_REASON
    )
    session.commit()
    return tx


def _authorize_fail(session: Session, tx_id, *, signature=SECRET_SIG, **kwargs):
    """`authorize` con biometria que siempre falla (inyectada, sin red)."""
    from app.modules.transactions.service import biometric as bio

    params = {
        "device_id": DEVICE_A,
        "signature": signature,
        "binding_reader": lambda s, u, d: {
            "device_id": d,
            "public_key": PUBLIC_KEY,
            "status": "ACTIVE",
        },
        "signature_verifier": lambda nonce, sig, pk: False,
        "max_attempts_reader": lambda s: 3,
    }
    params.update(kwargs)
    return bio.authorize_transaction(session, tx_id, **params)


def _security_events(session: Session, tx_id):
    from app.modules.shared.models import OutboxEntry

    rows = session.scalars(sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx_id)).all()
    return [r.payload for r in rows if r.event_type == "auth.failed_attempt"]


def _mock_sender():
    from app.adapters.notification_sender import MockNotificationSender

    return MockNotificationSender()


# ---------------------------------------------------------- Emisor (CA-04)


def test_failed_attempt_emits_single_event_with_ids(session: Session):
    from app.modules.transactions.service import biometric as bio

    tx = _pending_tx(session)
    with pytest.raises(bio.NotAuthorizedError):
        _authorize_fail(session, tx.id)
    session.commit()

    events = _security_events(session, tx.id)
    assert len(events) == 1  # un intento fallido -> un evento
    first = events[0]
    assert first["transaction_id"] == str(tx.id)
    assert first["user_id"] == str(USER_A)
    assert first["device_id"] == DEVICE_A
    assert first["attempt"] == 1 and first["blocked"] is False
    assert first["reason"] == "invalid"
    fresh = session.get(tx.__class__, tx.id)
    assert fresh.status == "PENDING_AUTHORIZATION"  # el fallo no mueve dinero
    assert (bio.get_block(fresh) or {})["failed_attempts"] == 1


def test_expired_nonce_emits_expired_event(session: Session):
    from app.modules.transactions.service import biometric as bio

    tx = _pending_tx(session)
    future = datetime.now(UTC) + timedelta(seconds=bio.NONCE_TTL_SECONDS + 60)
    with pytest.raises(bio.ExpiredNonceError):
        _authorize_fail(session, tx.id, now=future)
    session.commit()

    events = _security_events(session, tx.id)
    assert len(events) == 1 and events[0]["reason"] == "expired"
    assert events[0]["blocked"] is False


def test_preventive_block_emits_once_and_rejects_without_money(session: Session):
    from app.modules.transactions.models import TransactionStatusHistory
    from app.modules.transactions.service import biometric as bio

    tx = _pending_tx(session)
    with pytest.raises(bio.NotAuthorizedError):
        _authorize_fail(session, tx.id, signature="mala-1", max_attempts_reader=lambda s: 2)
    session.commit()
    blocked = _authorize_fail(session, tx.id, signature="mala-2", max_attempts_reader=lambda s: 2)
    session.commit()

    assert blocked.status == "REJECTED"  # CA-02: bloqueo preventivo
    assert blocked.failure_reason == bio.PREVENTIVE_BLOCK
    events = _security_events(session, tx.id)
    assert len(events) == 2  # un evento por intento, sin duplicados
    assert events[-1]["blocked"] is True and events[-1]["attempt"] == 2
    assert events[-1]["reason"] == "blocked"
    reasons = session.scalars(
        sa.select(TransactionStatusHistory.reason).where(
            TransactionStatusHistory.transaction_id == tx.id
        )
    ).all()
    assert "bloqueo preventivo tras N intentos fallidos" in reasons


def test_replay_after_block_emits_nothing(session: Session):
    """Tras el bloqueo, reintentar no emite eventos nuevos (idempotencia)."""
    from app.modules.transactions.service import biometric as bio

    tx = _pending_tx(session)
    # max_attempts=1: el primer fallo ya bloquea (retorna REJECTED, no lanza).
    blocked = _authorize_fail(session, tx.id, signature="mala-1", max_attempts_reader=lambda s: 1)
    session.commit()
    assert blocked.status == "REJECTED"
    before = len(_security_events(session, tx.id))
    assert before == 1
    with pytest.raises(bio.BiometricStateError):
        _authorize_fail(session, tx.id)
    session.commit()
    assert len(_security_events(session, tx.id)) == before


# ------------------------------------------------- Consumidor `risk` (CA-04)


def _consume(session: Session, payload: dict, *, event_id=None, sender=None):
    from app.modules.risk import service as risk_service

    return risk_service.handle_auth_failed_attempt(
        session,
        event_id=event_id or uuid.uuid4(),
        payload=payload,
        sender=sender if sender is not None else _mock_sender(),
        now=datetime(2026, 9, 27, 12, 0, tzinfo=UTC),
    )


def _payload(tx_id=None, **overrides):
    base = {
        "transaction_id": str(tx_id or uuid.uuid4()),
        "user_id": str(USER_A),
        "device_id": DEVICE_A,
        "attempt": 1,
        "blocked": False,
        "reason": "invalid",
    }
    base.update(overrides)
    return base


def _notifications(session: Session):
    from app.modules.notifications.models import Notification

    return list(session.scalars(sa.select(Notification).order_by(Notification.created_at)).all())


def _audits(session: Session):
    from app.modules.audit.models import AuditLog

    return list(session.scalars(sa.select(AuditLog).order_by(AuditLog.seq)).all())


def test_consumer_registers_and_notifies_customer(session: Session):
    from app.modules.risk import repository as risk_repo

    outcome = _consume(session, _payload())
    session.commit()

    assert outcome["consumed"] is True and outcome["blocked"] is False
    profile = risk_repo.get_by_user(session, USER_A)
    assert profile is not None and profile.score == 1  # severidad minima
    assert profile.last_device_id == DEVICE_A
    rows = _notifications(session)
    assert len(rows) == 1  # una notificacion al cliente (CA-04)
    assert rows[0].template_code == "auth_failed_attempt" and rows[0].channel == "push"
    assert rows[0].user_id == USER_A and rows[0].status == "SENT"
    audits = _audits(session)
    assert len(audits) == 1 and audits[0].action == "risk.auth_failed_attempt"


def test_consumer_block_notifies_blocked_template(session: Session):
    from app.modules.risk import repository as risk_repo

    outcome = _consume(session, _payload(attempt=3, blocked=True, reason="blocked"))
    session.commit()

    assert outcome["consumed"] is True and outcome["blocked"] is True
    profile = risk_repo.get_by_user(session, USER_A)
    assert profile is not None and profile.score == 5  # 1 + 4 extra (CA-02)
    rows = _notifications(session)
    assert len(rows) == 1 and rows[0].template_code == "auth_blocked"
    audits = _audits(session)
    assert len(audits) == 1 and audits[0].action == "risk.auth_preventive_block"


def test_consumer_duplicate_is_ignored(session: Session):
    from app.modules.risk import repository as risk_repo

    event_id = uuid.uuid4()
    first = _consume(session, _payload(), event_id=event_id)
    session.commit()
    second = _consume(session, _payload(), event_id=event_id)
    session.commit()

    assert first["consumed"] is True and second["consumed"] is False
    assert risk_repo.get_by_user(session, USER_A).score == 1  # sin doble conteo
    assert len(_notifications(session)) == 1  # sin notificacion duplicada
    assert len(_audits(session)) == 1


def test_concurrent_consumers_single_record(engine):
    """Dos consumidores del mismo `event_id` -> un solo registro."""
    from app.modules.risk import service as risk_service

    event_id = uuid.uuid4()
    payload = _payload()
    lock = threading.Lock()
    results = []

    def _run():
        local = Session(engine)
        try:
            with lock:
                outcome = risk_service.handle_auth_failed_attempt(
                    local,
                    event_id=event_id,
                    payload=payload,
                    sender=_mock_sender(),
                )
                local.commit()
                results.append(outcome["consumed"])
        finally:
            local.close()

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [False, True]
    check = Session(engine)
    try:
        assert len(_notifications(check)) == 1
        assert len(_audits(check)) == 1
    finally:
        check.close()


def test_consumer_missing_user_marks_processed(session: Session):
    event_id = uuid.uuid4()
    first = _consume(
        session, {"attempt": 1, "blocked": False, "reason": "invalid"}, event_id=event_id
    )
    session.commit()
    second = _consume(
        session, {"attempt": 1, "blocked": False, "reason": "invalid"}, event_id=event_id
    )
    session.commit()

    # Sin titular no hay perfil ni notificacion, pero se marca procesado
    # (no envenena el worker) y el replay se ignora.
    assert first == {"consumed": True, "reason": "missing-user-id"}
    assert second["consumed"] is False
    assert _notifications(session) == []


# --------------------------------------------------------------- Sin PII


def test_no_pii_in_event_payload_or_body(session: Session):
    from app.modules.transactions.service import biometric as bio

    tx = _pending_tx(session)
    with pytest.raises(bio.NotAuthorizedError):
        _authorize_fail(session, tx.id)
    session.commit()

    raw = str(_security_events(session, tx.id)[0])
    for secret in ("nonce", SECRET_SIG, PUBLIC_KEY, "biometr"):
        assert secret not in raw

    _consume(session, _payload(transaction_id=tx.id))
    session.commit()
    from app.modules.notifications.domain.templates import BASE_TEMPLATES, render_template
    from app.modules.notifications.models import Notification

    row = session.scalars(sa.select(Notification)).one()
    stored = dict(row.payload_json or {})
    template = BASE_TEMPLATES[row.template_code]
    body = render_template(template["body_template"], stored.get("data", {}))
    for secret in ("nonce", SECRET_SIG, PUBLIC_KEY, "biometr"):
        assert secret not in stored and secret not in body
    # El texto al cliente solo lleva el instante, sin identificadores.
    assert "auth_failed_attempt" in stored["template"]


# --------------------------------------------- Fallo de envio -> reintento


def test_failed_delivery_is_requeued_and_retried(session: Session):
    from app.adapters.notification_sender import (
        MockNotificationSender,
        NotificationProviderError,
    )
    from app.modules.notifications import service as notifications_service

    class _DownSender:
        def send(self, *, channel, recipient, subject, body):
            raise NotificationProviderError("proveedor caido")

    row = notifications_service.send(
        session,
        channel="push",
        recipient=str(USER_A),
        template_code="auth_failed_attempt",
        data={"at": datetime(2026, 9, 27, 12, 0, tzinfo=UTC).isoformat()},
        user_id=USER_A,
        sender=_DownSender(),
        max_attempts=2,
        backoff_base_seconds=0,
        sleep_fn=lambda _s: None,
    )
    session.commit()
    assert row.status == "FAILED"  # best-effort: no revierte, queda reimpulsable

    retried = notifications_service.retry_notification(
        session,
        row.id,
        sender=MockNotificationSender(),
        backoff_base_seconds=0,
        sleep_fn=lambda _s: None,
    )
    session.commit()
    assert retried.status == "SENT"


# ------------------------------------------------- End-to-end por outbox


def test_end_to_end_outbox_bus_notifies_once(session: Session, engine):
    """`auth.failed_attempt` via outbox -> bus -> `risk` + notificacion."""
    from functools import partial

    from app.composition import is_risk_security_wired, subscribe_risk_security
    from app.core.bus import EventBus
    from app.modules.risk.events import on_auth_failed_attempt
    from app.modules.shared.jobs.publisher import publish_pending

    tx = _pending_tx(session)
    from app.modules.transactions.service import biometric as bio

    with pytest.raises(bio.NotAuthorizedError):
        _authorize_fail(session, tx.id)
    session.commit()

    bus = EventBus()
    subscribe_risk_security(bus)
    assert is_risk_security_wired(bus) is True
    # Segunda suscripcion no duplica el handler (idempotente).
    subscribe_risk_security(bus)
    assert bus.handler_count("auth.failed_attempt") == 1
    # El handler abre sesion propia: se la ata al motor de prueba (jamas a
    # la BD real) mediante `session_factory` inyectada.
    bus.unsubscribe("auth.failed_attempt", on_auth_failed_attempt)
    bus.subscribe(
        "auth.failed_attempt",
        partial(on_auth_failed_attempt, session_factory=lambda: Session(engine)),
    )

    made = Session(engine)
    try:
        result = publish_pending(made, bus=bus)
        made.commit()
    finally:
        made.close()
    assert result == {"published": 1, "failed": 0, "skipped": 0}

    session.expire_all()
    from app.modules.risk import repository as risk_repo

    assert risk_repo.get_by_user(session, USER_A).score == 1
    rows = _notifications(session)
    assert len(rows) == 1 and rows[0].template_code == "auth_failed_attempt"
    assert len(_audits(session)) == 1


def test_wiring_helpers_on_default_bus():
    from app.composition import is_risk_security_wired, subscribe_risk_security

    handler = subscribe_risk_security()
    try:
        assert is_risk_security_wired() is True
    finally:
        from app.core.bus import get_default_bus

        get_default_bus().unsubscribe("auth.failed_attempt", handler)
