"""Adaptador de notificaciones + fachada send (E1-T09, HU02 CA-01).

- Parte A (sin BD): modelos/migracion segun `03b#13/#15`, contrato del adapter
  mock (multicanal, fallo configurable por canal), render sin PII en logs,
  backoff puro, firma estable de `send`, sin `commit`/`float`/negocio ajeno.
- Parte B (SQLite en memoria + schema ATTACH): envio mock exitoso por canal;
  fallo de un canal + reintento exitoso (sin perder el mensaje); registro de
  estado QUEUED/SENT/FAILED en `notifications`.
- Parte C (Postgres `db_session`): humo de integracion; se omite si no hay BD.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

NO_SLEEP = lambda _seconds: None  # backoff sin espera en tests

DATA_BY_TEMPLATE = {
    "otp_code": {"code": "123456", "ttl_minutes": 10},
    "account_activated": {"account_masked": "***-1234"},
    "login_alert": {"device": "Pixel-test", "at": "2026-09-17T00:00:00Z"},
}
RECIPIENT_BY_CHANNEL = {
    "sms": "+51999999999",
    "email": "cliente@example.com",
    "push": "device-token-abc",
}
CHANNEL_BY_TEMPLATE = {"otp_code": "sms", "account_activated": "email", "login_alert": "push"}


# ---------------------------------------------------------------- Parte A: modelo
def test_tables_registered_with_schema_columns_constraints_indexes():
    import app.modules.notifications.models as m  # noqa: F401 (registro)

    for key in ("notifications.notifications", "notifications.notification_templates"):
        assert key in Base.metadata.tables, f"falta tabla {key}"

    templates = Base.metadata.tables["notifications.notification_templates"]
    assert templates.schema == "notifications"
    cols = {c.name: c for c in templates.columns}
    assert set(cols) == {"id", "code", "channel", "subject", "body_template", "enabled"}
    assert isinstance(cols["code"].type, sa.String) and cols["code"].type.length == 50
    assert isinstance(cols["subject"].type, sa.String) and cols["subject"].type.length == 150
    assert isinstance(cols["body_template"].type, sa.Text)
    assert {c.name for c in templates.constraints if isinstance(c, sa.UniqueConstraint)} == {
        "uq_notification_templates_code"
    }
    checks = {c.name for c in templates.constraints if isinstance(c, sa.CheckConstraint)}
    assert "ck_notification_templates_channel" in checks

    notifications = Base.metadata.tables["notifications.notifications"]
    assert notifications.schema == "notifications"
    cols = {c.name: c for c in notifications.columns}
    assert set(cols) == {
        "id",
        "user_id",
        "channel",
        "template_code",
        "payload_json",
        "status",
        "provider_ref",
        "error",
        "created_at",
        "sent_at",
        "read_at",
    }
    assert isinstance(cols["channel"].type, sa.String) and cols["channel"].type.length == 10
    assert isinstance(cols["status"].type, sa.String) and cols["status"].type.length == 12
    assert isinstance(cols["provider_ref"].type, sa.String)
    assert cols["provider_ref"].type.length == 120
    assert isinstance(cols["payload_json"].type, sa.JSON)
    assert isinstance(cols["created_at"].type, sa.DateTime)
    checks = {c.name for c in notifications.constraints if isinstance(c, sa.CheckConstraint)}
    assert "ck_notifications_channel" in checks
    assert "ck_notifications_status" in checks
    idx = {i.name for i in notifications.indexes}
    assert "ix_notifications_user_created" in idx
    assert "ix_notifications_status" in idx


def test_no_foreign_keys_to_other_schemas():
    for key in ("notifications.notifications", "notifications.notification_templates"):
        table = Base.metadata.tables[key]
        assert not list(table.foreign_keys), f"{key} no debe tener FK fisicas"
    notifications = Base.metadata.tables["notifications.notifications"]
    assert not notifications.columns["user_id"].foreign_keys, "user_id es UUID logico sin FK"


def test_migration_0011_exists_and_matches_models():
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0011_notifications.py"
    assert path.exists(), "falta migracion 0011_notifications.py"
    content = path.read_text(encoding="utf-8")
    for token in (
        'revision = "0011_notifications"',
        'down_revision = "0010_accounts_movements"',
        '"notification_templates"',
        '"notifications"',
        '"notifications"',
        "ck_notifications_channel",
        "ck_notifications_status",
        "ix_notifications_user_created",
        "ix_notifications_status",
        "otp_code",
        "account_activated",
        "login_alert",
    ):
        assert token in content, f"migracion sin {token}"
    for foreign in ("ledger.", "transactions.", "accounts.", "identity.", "shared."):
        assert foreign not in content, f"la migracion no debe tocar {foreign}"


def test_mock_adapter_contract_and_per_channel_failure():
    from app.adapters.notification_sender import MockNotificationSender

    sender = MockNotificationSender()
    for channel in ("push", "email", "sms"):
        result = sender.send(channel=channel, recipient="dest", subject=None, body="hola")
        assert result.ok and result.provider_ref, f"{channel} debe enviar"
    assert len(sender.calls) == 3

    sender.set_channel_failure("sms", "smsc caido")
    failed = sender.send(channel="sms", recipient="dest", subject=None, body="hola")
    assert not failed.ok and failed.error == "smsc caido"
    # Caida parcial: los demas canales siguen enviando.
    assert sender.send(channel="email", recipient="d", subject=None, body="hola").ok
    sender.clear_channel_failure("sms")
    assert sender.send(channel="sms", recipient="dest", subject=None, body="hola").ok

    with pytest.raises(ValueError):
        sender.send(channel="fax", recipient="d", subject=None, body="hola")


def test_mock_adapter_raise_mode_simulates_hard_outage():
    from app.adapters.notification_sender import MockNotificationSender, NotificationProviderError

    sender = MockNotificationSender()
    sender.set_channel_raise("push", "push caido")
    with pytest.raises(NotificationProviderError):
        sender.send(channel="push", recipient="d", subject=None, body="hola")
    sender.clear_channel_failure("push")
    assert sender.send(channel="push", recipient="d", subject=None, body="hola").ok


def test_render_validates_and_logs_without_pii(caplog):
    from app.modules.notifications.domain.templates import BASE_TEMPLATES, render_template

    assert set(BASE_TEMPLATES) >= {"otp_code", "account_activated", "login_alert"}
    for code, data in DATA_BY_TEMPLATE.items():
        body = render_template(BASE_TEMPLATES[code]["body_template"], data)
        assert "{" not in body and "}" not in body, f"{code} deja placeholders sin rendir"
    with pytest.raises(ValueError):
        render_template("hola {falta}", {})
    with pytest.raises(TypeError):
        render_template("hola {x}", {"x": {"pii": "estructurada"}})
    with pytest.raises(TypeError):
        render_template("hola {x}", ["no-dict"])


def test_send_logs_without_pii(caplog):
    from app.modules.notifications import service as svc

    class _FakeSession:
        def add(self, row):
            row.id = uuid.uuid4()

        def flush(self):
            pass

        def scalar(self, _stmt):
            return None  # sin plantillas en BD: la fachada usa la semilla base

    class _Sender:
        def send(self, *, channel, recipient, subject, body):
            from app.adapters.notification_sender import SendResult

            return SendResult(ok=True, provider_ref="mock-x")

    import app.modules.notifications.repository as repo

    real_save = repo.save_notification
    real_sent = repo.mark_sent
    try:
        repo.save_notification = lambda s, **kw: type(
            "N",
            (),
            {
                "id": uuid.uuid4(),
                **{k: v for k, v in kw.items() if k != "payload"},
                "payload_json": kw.get("payload"),
                "channel": kw.get("channel"),
                "template_code": kw.get("template_code"),
                "status": "QUEUED",
            },
        )()
        repo.mark_sent = lambda s, i, ref, **kw: None
        with caplog.at_level(logging.INFO):
            svc.send(
                _FakeSession(),  # type: ignore[arg-type]
                channel="sms",
                recipient="+51999999999",
                template_code="otp_code",
                data={"code": "123456", "ttl_minutes": 10},
                sender=_Sender(),  # type: ignore[arg-type]
                sleep_fn=NO_SLEEP,
            )
    finally:
        repo.save_notification = real_save
        repo.mark_sent = real_sent
    assert "+51999999999" not in caplog.text
    assert "123456" not in caplog.text
    assert "otp_code" in caplog.text  # solo el codigo de plantilla se loguea


def test_send_signature_is_stable_contract():
    import inspect

    from app.modules.notifications.service import retry_notification, send

    sig = inspect.signature(send)
    params = list(sig.parameters.values())
    assert params[0].name == "session"
    rest = {p.name for p in params[1:]}
    assert {"channel", "recipient", "template_code", "data", "sender"} <= rest
    for name in ("channel", "recipient", "template_code", "data"):
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert callable(retry_notification)


def test_backoff_is_exponential():
    from app.modules.notifications.service import compute_backoff_seconds

    assert compute_backoff_seconds(1, 1) == 1
    assert compute_backoff_seconds(2, 1) == 2
    assert compute_backoff_seconds(3, 1) == 4
    with pytest.raises(ValueError):
        compute_backoff_seconds(0, 1)


def test_modules_have_no_commit_float_nor_cross_module_access():
    base = Path(__file__).resolve().parents[1]
    for rel in (
        Path("app/adapters/notification_sender.py"),
        Path("app/modules/notifications/models/__init__.py"),
        Path("app/modules/notifications/repository/__init__.py"),
        Path("app/modules/notifications/service/__init__.py"),
        Path("app/modules/notifications/domain/templates.py"),
    ):
        content = (base / rel).read_text(encoding="utf-8")
        assert ".commit(" not in content, f"{rel} no debe hacer commit"
        assert "float(" not in content, f"{rel} sin atajos float"
    service = (base / "app/modules/notifications/service/__init__.py").read_text(encoding="utf-8")
    assert "time.sleep" not in service or "sleep_fn" in service, "la espera debe ser inyectable"
    for rel, banned in (
        (Path("app/adapters/notification_sender.py"), ("identity", "ledger", "transactions")),
        (Path("app/modules/notifications/service/__init__.py"), ("identity", "ledger")),
    ):
        content = (base / rel).read_text(encoding="utf-8")
        for mod in banned:
            assert f"modules.{mod}" not in content, f"{rel} no debe importar {mod}"


# ---------------------------------------------------------------- Parte B: SQLite
@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schema `notifications` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.notifications.models as m  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS notifications")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
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


def _send_ok(session: Session, template_code: str, sender=None):
    from app.modules.notifications.service import send

    return send(
        session,
        channel=CHANNEL_BY_TEMPLATE[template_code],
        recipient=RECIPIENT_BY_CHANNEL[CHANNEL_BY_TEMPLATE[template_code]],
        template_code=template_code,
        data=dict(DATA_BY_TEMPLATE[template_code]),
        sender=sender,
        sleep_fn=NO_SLEEP,
    )


def test_successful_send_per_channel(sqlite_session: Session):
    from app.adapters.notification_sender import MockNotificationSender

    sender = MockNotificationSender()
    for code in ("otp_code", "account_activated", "login_alert"):
        row = _send_ok(sqlite_session, code, sender=sender)
        assert row.status == "SENT"
        assert row.provider_ref and row.provider_ref.startswith("mock-")
        assert row.sent_at is not None and row.error is None
        assert row.template_code == code
    assert (
        sqlite_session.scalar(
            sa.select(sa.func.count()).select_from(
                Base.metadata.tables["notifications.notifications"]
            )
        )
        == 3
    )


def test_channel_failure_then_retry_succeeds_without_losing_message(sqlite_session: Session):
    from app.adapters.notification_sender import MockNotificationSender
    from app.modules.notifications import service as svc

    sender = MockNotificationSender()
    sender.set_channel_failure("sms", "smsc caido")
    row = _send_ok(sqlite_session, "otp_code", sender=sender)
    notification_id = row.id
    assert row.status == "FAILED"
    assert row.error == "smsc caido"
    assert row.provider_ref is None  # sin ref: el proveedor nunca acepto

    # Los demas canales no se ven afectados por la caida del sms.
    email_row = _send_ok(sqlite_session, "account_activated", sender=sender)
    assert email_row.status == "SENT"

    # El proveedor se recupera: el reintento posterior envia el mensaje guardado.
    sender.clear_channel_failure("sms")
    retried = svc.retry_notification(sqlite_session, notification_id, sender, sleep_fn=NO_SLEEP)
    assert retried.status == "SENT"
    assert retried.provider_ref and retried.error is None


def test_hard_outage_marks_failed_and_retry_recovers(sqlite_session: Session):
    from app.adapters.notification_sender import MockNotificationSender
    from app.modules.notifications import service as svc

    sender = MockNotificationSender()
    sender.set_channel_raise("push", "push caido")
    row = _send_ok(sqlite_session, "login_alert", sender=sender)
    assert row.status == "FAILED"
    sender.clear_channel_failure("push")
    retried = svc.retry_notification(sqlite_session, row.id, sender, sleep_fn=NO_SLEEP)
    assert retried.status == "SENT"


def test_sent_notification_is_not_reforwarded(sqlite_session: Session):
    from app.adapters.notification_sender import MockNotificationSender
    from app.modules.notifications import service as svc

    sender = MockNotificationSender()
    row = _send_ok(sqlite_session, "otp_code", sender=sender)
    calls_before = len(sender.calls)
    again = svc.retry_notification(sqlite_session, row.id, sender, sleep_fn=NO_SLEEP)
    assert again.status == "SENT" and len(sender.calls) == calls_before


def test_notification_status_recorded(sqlite_session: Session):
    from app.modules.notifications import repository as repo
    from app.modules.notifications.models import Notification

    row = repo.save_notification(
        sqlite_session, channel="sms", template_code="otp_code", payload={"a": 1}
    )
    assert row.status == "QUEUED"
    stored = sqlite_session.get(Notification, row.id)
    assert stored is not None and stored.status == "QUEUED"
    repo.mark_sent(sqlite_session, row.id, "mock-x")
    assert sqlite_session.get(Notification, row.id).status == "SENT"
    repo.mark_failed(sqlite_session, row.id, "boom")
    failed = sqlite_session.get(Notification, row.id)
    assert failed.status == "FAILED" and failed.error == "boom"
    assert repo.list_pending(sqlite_session) == [] or all(
        r.status == "QUEUED" for r in repo.list_pending(sqlite_session)
    )


def test_render_failure_leaves_no_residual_rows(sqlite_session: Session):
    from app.modules.notifications.service import send

    before = sqlite_session.scalar(
        sa.select(sa.func.count()).select_from(Base.metadata.tables["notifications.notifications"])
    )
    with pytest.raises(ValueError):
        send(
            sqlite_session,
            channel="sms",
            recipient="+51999999999",
            template_code="otp_code",
            data={},  # falta {code} y {ttl_minutes}
            sleep_fn=NO_SLEEP,
        )
    after = sqlite_session.scalar(
        sa.select(sa.func.count()).select_from(Base.metadata.tables["notifications.notifications"])
    )
    assert before == after == 0


# ---------------------------------------------------------------- Parte C: Postgres
def test_integration_postgres_notifications_smoke(db_session: Session):
    from app.adapters.notification_sender import MockNotificationSender
    from app.modules.notifications import service as svc
    from app.modules.notifications.models import Notification

    sender = MockNotificationSender()
    row = svc.send(
        db_session,
        channel="sms",
        recipient="+51999999999",
        template_code="otp_code",
        data={"code": "123456", "ttl_minutes": 10},
        sender=sender,
        sleep_fn=NO_SLEEP,
    )
    assert row.status == "SENT"
    assert db_session.get(Notification, row.id) is not None
