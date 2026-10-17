"""Contenido humano de los correos de notificacion (pulido 2026-10-15).

Regresion del feedback "los correos llegan pero habria que pulir el
contenido":

- `security_operation_email` (y su push gemelo): la operacion se muestra
  como etiqueta humana ("Transferencia realizada", no `transfer.settled`)
  y el instante como fecha local legible (`13/10/2026 07:00`, no ISO con
  microsegundos); el asunto incluye la etiqueta.
- `credit_contract_signed`, `account_activated`, `otp_code_email`,
  `login_alert`: tildes, claridad y tono consistente sin romper sus
  placeholders ni el contrato de `notifications.send`.
- Garantia PII: el codigo tecnico crudo solo vive en el `payload`
  (`event_type`) para correlacion, nunca en el texto visible.

SQLite en memoria + schemas ATTACH (sin red, sin `float`); Postgres
(`db_session`) solo para la coincidencia semilla<->BASE y el roundtrip
de la `0045` (se omite sin BD).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.modules.notifications.models as _notifications
import app.modules.shared.models as _shared
from app.adapters.notification_sender import SendResult
from app.core.db import Base
from app.modules.notifications import service as svc
from app.modules.notifications.domain.templates import (
    BASE_TEMPLATES,
    format_at_legible,
    humanize_operation,
    render_subject,
    render_template,
)

assert _notifications is not None and _shared is not None

USER = uuid.uuid4()
AT_ISO = "2026-10-13T12:00:00+00:00"
AT_MICRO = "2026-10-15T19:30:45.123456+00:00"

TABLES = [
    "notifications.notifications",
    "notifications.notification_templates",
    "notifications.user_channel_preferences",
    "shared.processed_events",
]
SCHEMAS = ("notifications", "shared", "config")


@pytest.fixture()
def db():
    """SQLite en memoria con los schemas propios (sin red)."""
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


class _CapturingSender:
    """Sender en memoria que registra asunto/cuerpo entregados (sin red)."""

    def __init__(self):
        self.sent: list[dict] = []

    def send(self, *, channel, recipient, subject, body):
        self.sent.append(
            {"channel": channel, "recipient": recipient, "subject": subject, "body": body}
        )
        return SendResult(ok=True, provider_ref=f"cap-{channel}")


# --------------------------------------- etiquetas humanas de operacion
def test_humanize_operation_maps_known_codes():
    assert humanize_operation("transfer.settled") == "Transferencia realizada"
    assert humanize_operation("credit.disbursed") == "Desembolso de crédito"
    assert humanize_operation("loan.installment.paid") == "Pago de cuota de crédito"
    assert humanize_operation("qr.payment.confirmed") == "Pago con QR"
    assert humanize_operation("service.paid") == "Pago de servicio"
    assert humanize_operation("topup.completed") == "Recarga completada"
    assert humanize_operation("auth.failed_attempt") == "Intento de inicio de sesión"


def test_humanize_operation_fallback_is_legible():
    assert humanize_operation("misterio.raro_x") == "misterio raro x"
    assert humanize_operation("") == "Operación de tu cuenta"
    assert humanize_operation(None) == "Operación de tu cuenta"
    assert "." not in humanize_operation("a.b.c")


# --------------------------------------- fecha local legible
def test_format_at_legible_converts_iso_to_lima():
    # 12:00 UTC == 07:00 en Lima (UTC-5, sin horario de verano).
    assert format_at_legible(AT_ISO) == "13/10/2026 07:00"
    # Sin microsegundos crudos aunque el emisor los aporte.
    assert format_at_legible(AT_MICRO) == "15/10/2026 14:30"
    assert format_at_legible("2026-09-17T00:00:00Z") == "16/09/2026 19:00"
    assert format_at_legible(datetime(2026, 10, 15, 19, 30, tzinfo=UTC)) == "15/10/2026 14:30"
    # Naive = UTC (mismo criterio que el resto del backend con SQLite).
    naive = datetime(2026, 10, 15, 19, 30)  # noqa: DTZ001 - el caso naive es intencional
    assert format_at_legible(naive) == "15/10/2026 14:30"


def test_format_at_legible_never_empty():
    assert format_at_legible(None)  # instante aportado por la fachada
    assert format_at_legible("no-es-fecha") == "no-es-fecha"


# --------------------------------------- correo de seguridad humanizado
def test_security_email_subject_and_body_are_humanized(db):
    _, session = db
    push = _CapturingSender()
    mail = _CapturingSender()
    out = svc.notify_security_event(
        session,
        user_id=USER,
        event_type="transfer.settled",
        reference="op-pulido-1",
        device="Pixel 8",
        at=AT_MICRO,
        location="Lima",
        sender_push=push,
        sender_email=mail,
    )
    assert out["channels"]["push"]["status"] == "SENT"
    assert out["channels"]["email"]["status"] == "SENT"
    subject = mail.sent[0]["subject"]
    body = mail.sent[0]["body"]
    assert subject == "Nueva operación en tu cuenta: Transferencia realizada"
    assert "Transferencia realizada" in body
    assert "15/10/2026 14:30" in body
    assert "transfer.settled" not in subject and "transfer.settled" not in body
    assert "19:30:45.123456" not in body and "T19:30" not in body
    assert "{" not in body and "}" not in body
    assert "op-pulido-1" in body  # la referencia no PII sigue en el correo
    # Tildes y tono consistente.
    for word in ("operación", "Ubicación", "Dispositivo"):
        assert word in body
    # El push gemelo tambien llega humanizado.
    push_body = push.sent[0]["body"]
    assert "Transferencia realizada" in push_body
    assert "transfer.settled" not in push_body
    assert "op-pulido-1" not in push_body  # la referencia solo viaja en el correo
    # El codigo crudo se conserva para correlacion (payload + respuesta).
    assert out["event_type"] == "transfer.settled"
    row = session.scalars(
        sa.select(_notifications.Notification).where(_notifications.Notification.channel == "email")
    ).one()
    assert (row.payload_json or {}).get("event_type") == "transfer.settled"


def test_security_bodies_have_no_pii(db):
    _, session = db
    push = _CapturingSender()
    mail = _CapturingSender()
    svc.notify_security_event(
        session,
        user_id=USER,
        event_type="auth.failed_attempt",
        reference="op-pulido-pii",
        device="Pixel 8",
        at=AT_ISO,
        location="Lima",
        sender_push=push,
        sender_email=mail,
    )
    for sent in (*push.sent, *mail.sent):
        text = f"{sent['subject']} {sent['body']}"
        for pii in ("4242424242424242", "-12.04,-77.03", "nonce", "biometr", "@test.local"):
            assert pii not in text
    assert "Intento de inicio de sesión" in mail.sent[0]["body"]


def test_security_email_unknown_operation_falls_back_legibly(db):
    _, session = db
    mail = _CapturingSender()
    svc.notify_security_event(
        session,
        user_id=USER,
        event_type="misterio.raro_x",
        reference="op-pulido-raro",
        device="Pixel 8",
        at=AT_ISO,
        location="Lima",
        sender_push=_CapturingSender(),
        sender_email=mail,
    )
    assert "misterio raro x" in mail.sent[0]["body"]
    assert "misterio.raro_x" not in mail.sent[0]["body"]
    assert "misterio raro x" in mail.sent[0]["subject"]


def test_security_email_without_device_location_reads_clean(db):
    _, session = db
    push = _CapturingSender()
    mail = _CapturingSender()
    svc.notify_security_event(
        session,
        user_id=USER,
        event_type="topup.completed",
        reference="op-pulido-sitio",
        device=None,
        at=None,
        location="",
        sender_push=push,
        sender_email=mail,
    )
    body = mail.sent[0]["body"]
    assert "Recarga completada" in body
    assert "no disponible" in body
    assert "None" not in body
    row = session.scalars(
        sa.select(_notifications.Notification).where(_notifications.Notification.channel == "push")
    ).one()
    data = (row.payload_json or {}).get("data", {})
    assert data["device"] == svc.UNKNOWN_DEVICE
    assert data["location"] == svc.UNKNOWN_LOCATION
    assert data["at"]  # la fachada aporta el instante aunque el emisor no lo traiga


# --------------------------------------- plantillas pulidas (placeholders intactos)
def test_polished_templates_keep_placeholders_and_tildes():
    assert "{code}" in BASE_TEMPLATES["otp_code_email"]["body_template"]
    assert "{ttl_minutes}" in BASE_TEMPLATES["otp_code_email"]["body_template"]
    assert "{account_masked}" in BASE_TEMPLATES["account_activated"]["body_template"]
    assert "{device}" in BASE_TEMPLATES["login_alert"]["body_template"]
    assert "{application_ref}" in BASE_TEMPLATES["credit_contract_signed"]["body_template"]
    assert "{signed_at}" in BASE_TEMPLATES["credit_contract_signed"]["body_template"]
    assert BASE_TEMPLATES["otp_code_email"]["subject"] == "Tu código de verificación"
    assert BASE_TEMPLATES["account_activated"]["subject"] == "Tu cuenta está activa"
    assert (
        BASE_TEMPLATES["credit_contract_signed"]["subject"] == "Tu contrato de crédito fue firmado"
    )
    assert "código" in BASE_TEMPLATES["otp_code"]["body_template"]
    assert "sesión" in BASE_TEMPLATES["login_alert"]["body_template"]
    assert "Bienvenido" in BASE_TEMPLATES["account_activated"]["body_template"]


def test_polished_templates_render_without_raw_braces():
    cases = {
        "otp_code": {"code": "123456", "ttl_minutes": 10},
        "otp_code_email": {"code": "123456", "ttl_minutes": 10},
        "account_activated": {"account_masked": "***-1234"},
        "login_alert": {"device": "Pixel-test", "at": "17/09/2026 19:00"},
        "credit_contract_signed": {"application_ref": "EXP-0001", "signed_at": "15/10/2026"},
    }
    for code, data in cases.items():
        body = render_template(BASE_TEMPLATES[code]["body_template"], data)
        assert "{" not in body and "}" not in body, code
        subject = render_subject(BASE_TEMPLATES[code]["subject"], data)
        if BASE_TEMPLATES[code]["subject"] is None:
            assert subject is None, code
        else:
            assert subject and "{" not in subject, code
    otp_body = render_template(
        BASE_TEMPLATES["otp_code_email"]["body_template"], cases["otp_code_email"]
    )
    assert "123456" in otp_body  # el codigo solo viaja cuando corresponde (OTP)


def test_send_renders_static_subjects_unchanged(db):
    """El contrato `send` sigue intacto: asunto estatico tal cual."""
    _, session = db
    mail = _CapturingSender()
    row = svc.send(
        session,
        channel="email",
        recipient="cliente@example.com",
        template_code="otp_code_email",
        data={"code": "123456", "ttl_minutes": 10},
        sender=mail,
        sleep_fn=lambda _s: None,
    )
    assert row.status == "SENT"
    assert mail.sent[0]["subject"] == "Tu código de verificación"
    assert "123456" in mail.sent[0]["body"]


# --------------------------------------- semillas en BD (Postgres; se omite sin BD)
def test_db_seeds_match_base_templates(db_session: Session):
    from app.modules.notifications.models import NotificationTemplate

    for code in ("otp_code", "otp_code_email", "account_activated", "login_alert"):
        row = db_session.scalar(
            sa.select(NotificationTemplate).where(NotificationTemplate.code == code)
        )
        assert row is not None, f"falta semilla {code}"
        seed = BASE_TEMPLATES[code]
        assert (row.channel, row.subject, row.body_template, row.enabled) == (
            seed["channel"],
            seed["subject"],
            seed["body_template"],
            True,
        ), f"semilla {code} desfasada de BASE_TEMPLATES"


def test_migration_0045_is_data_only_and_reversible():
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0045_notification_email_content.py"
    )
    assert path.exists(), "falta migracion 0045_notification_email_content.py"
    content = path.read_text(encoding="utf-8")
    assert 'revision = "0045_notification_email_content"' in content
    assert 'down_revision = "0044_notifications_preferences"' in content
    assert len("0045_notification_email_content") <= 32
    for token in (
        "otp_code_email",
        "account_activated",
        "login_alert",
        "UPDATE notifications.notification_templates",
    ):
        assert token in content, f"migracion sin {token}"
    for ddl in ("op.create_table", "op.add_column", "op.drop_table", "op.drop_column"):
        assert ddl not in content, f"0045 debe ser data-only, contiene {ddl}"
    for foreign in ("identity.", "ledger.", "transactions.", "accounts."):
        assert foreign not in content, f"la migracion no debe tocar {foreign}"


def test_migration_0045_up_down_roundtrip_in_db(db_session: Session):
    """`downgrade` restaura los textos previos y `upgrade` los pule (idempotente)."""
    from sqlalchemy import text

    from tests.db_utils import (
        downgrade_in_process,
        resolve_test_database_url,
        run_migrations_in_process,
    )

    url = resolve_test_database_url()
    bind = db_session.get_bind()

    def _subject(code: str) -> object:
        return bind.execute(
            text("SELECT subject FROM notifications.notification_templates WHERE code = :code"),
            {"code": code},
        ).scalar_one()

    assert _subject("otp_code_email") == "Tu código de verificación"
    try:
        downgrade_in_process(url, "0044_notifications_preferences")
        assert _subject("otp_code_email") == "Tu codigo de verificacion"
        assert _subject("account_activated") == "Tu cuenta esta activa"
    finally:
        run_migrations_in_process(url, "head")
    assert _subject("otp_code_email") == "Tu código de verificación"
    assert _subject("account_activated") == "Tu cuenta está activa"

    run_migrations_in_process(url, "head")  # re-aplicar en head es no-op
    assert _subject("otp_code_email") == "Tu código de verificación"
