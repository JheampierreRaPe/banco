"""Adaptador Gmail para OTP por email (E1-T25, HU02 P-S2-01).

- SMTP simulado con una factory inyectable: NUNCA se abre una conexion real.
- Solo canal `email`; otro canal / destinatario invalido -> no reintentable.
- Autenticacion fallida o 5xx SMTP -> no reintentable; 4xx/red/timeout ->
  reintentable (`NotificationProviderError`).
- `EMAIL_PROVIDER` selecciona gmail|mock (default mock); SMS (`SMS_PROVIDER`)
  y el mock siguen sin regresion.
- Migracion data-only `0016` inserta/elimina la plantilla de forma reversible.
"""

from __future__ import annotations

import logging
import smtplib
import subprocess
import sys

import pytest
import sqlalchemy as sa
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.adapters.notification_sender import (
    GmailNotificationSender,
    MockNotificationSender,
    NotificationProviderError,
    NotificationValidationError,
    SendResult,
)
from app.core.db import Base

NO_SLEEP = lambda _seconds: None  # backoff sin espera en tests

GMAIL_USER = "emisor-test@gmail.com"
APP_PASSWORD = "apppassword16xx"
RECIPIENT = "cliente@example.com"
BODY = "Tu codigo de verificacion es 123456. Vence en 10 minutos."
SUBJECT = "Tu codigo de verificacion"


# ---------------------------------------------------- SMTP simulado (sin red)
class _FakeSMTP:
    def __init__(self, host, port, timeout=None, *, errors=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.errors = errors or {}
        self.starttls_called = False
        self.login_args = None
        self.messages: list = []
        self.quit_called = False

    def starttls(self):
        if "starttls" in self.errors:
            raise self.errors["starttls"]
        self.starttls_called = True

    def login(self, user, password):
        if "login" in self.errors:
            raise self.errors["login"]
        self.login_args = (user, password)

    def send_message(self, message):
        if "send" in self.errors:
            raise self.errors["send"]
        self.messages.append(message)

    def quit(self):
        self.quit_called = True

    def close(self):
        pass


class _SMTPRecorder:
    """Factory inyectable que simula `smtplib.SMTP` y registra instancias."""

    def __init__(self, errors=None):
        self.errors = errors or {}
        self.instances: list[_FakeSMTP] = []

    def __call__(self, host, port, timeout=None):
        smtp = _FakeSMTP(host, port, timeout, errors=self.errors)
        self.instances.append(smtp)
        return smtp


def _sender(recorder: _SMTPRecorder) -> GmailNotificationSender:
    return GmailNotificationSender(
        gmail_user=GMAIL_USER,
        app_password=APP_PASSWORD,
        smtp_factory=recorder,
    )


# ------------------------------------------------------------- camino feliz
def test_gmail_send_email_ok_con_smtp_simulado_sin_correos_reales():
    recorder = _SMTPRecorder()
    result = _sender(recorder).send(
        channel="email", recipient=RECIPIENT, subject=SUBJECT, body=BODY
    )
    assert result.ok and result.provider_ref
    assert len(recorder.instances) == 1
    smtp = recorder.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.gmail.com", 587)
    assert smtp.starttls_called is True
    assert smtp.login_args == (GMAIL_USER, APP_PASSWORD)
    assert smtp.quit_called is True
    assert len(smtp.messages) == 1
    message = smtp.messages[0]
    assert message["To"] == RECIPIENT
    assert message["From"] == GMAIL_USER
    assert message["Subject"] == SUBJECT
    assert message["Message-ID"] == result.provider_ref
    assert "123456" in message.get_content()


def test_gmail_asunto_por_defecto_cuando_es_none():
    recorder = _SMTPRecorder()
    result = _sender(recorder).send(channel="email", recipient=RECIPIENT, subject=None, body=BODY)
    assert result.ok
    assert recorder.instances[0].messages[0]["Subject"] == "Notificacion"


# ------------------------------------------------------------ errores tipados
def test_gmail_solo_atiende_canal_email_sin_llamar_a_la_red():
    recorder = _SMTPRecorder()
    with pytest.raises(NotificationValidationError):
        _sender(recorder).send(channel="sms", recipient="+51999999999", subject=None, body=BODY)
    assert recorder.instances == []


def test_gmail_recipient_invalido_no_llama_a_la_red():
    recorder = _SMTPRecorder()
    with pytest.raises(NotificationValidationError):
        _sender(recorder).send(
            channel="email", recipient="no-es-un-email", subject=SUBJECT, body=BODY
        )
    assert recorder.instances == []


def test_gmail_body_vacio_es_invalido():
    recorder = _SMTPRecorder()
    with pytest.raises(NotificationValidationError):
        _sender(recorder).send(channel="email", recipient=RECIPIENT, subject=SUBJECT, body="  ")
    assert recorder.instances == []


def test_gmail_autenticacion_fallida_no_es_reintentable():
    recorder = _SMTPRecorder(errors={"login": smtplib.SMTPAuthenticationError(535, b"bad")})
    with pytest.raises(NotificationValidationError):
        _sender(recorder).send(channel="email", recipient=RECIPIENT, subject=SUBJECT, body=BODY)
    assert issubclass(NotificationValidationError, ValueError)
    assert not issubclass(NotificationValidationError, NotificationProviderError)


def test_gmail_smtp_5xx_no_es_reintentable():
    recorder = _SMTPRecorder(errors={"send": smtplib.SMTPDataError(550, b"no such user")})
    with pytest.raises(NotificationValidationError):
        _sender(recorder).send(channel="email", recipient=RECIPIENT, subject=SUBJECT, body=BODY)


def test_gmail_smtp_4xx_es_reintentable():
    recorder = _SMTPRecorder(errors={"send": smtplib.SMTPDataError(451, b"try later")})
    with pytest.raises(NotificationProviderError):
        _sender(recorder).send(channel="email", recipient=RECIPIENT, subject=SUBJECT, body=BODY)


def test_gmail_timeout_es_reintentable():
    recorder = _SMTPRecorder(errors={"login": TimeoutError("red lenta")})
    with pytest.raises(NotificationProviderError):
        _sender(recorder).send(channel="email", recipient=RECIPIENT, subject=SUBJECT, body=BODY)


def test_gmail_caida_de_red_al_conectar_es_reintentable():
    def _factory(_host, _port, timeout=None):
        raise ConnectionRefusedError("smtp caido")

    sender = GmailNotificationSender(
        gmail_user=GMAIL_USER, app_password=APP_PASSWORD, smtp_factory=_factory
    )
    with pytest.raises(NotificationProviderError):
        sender.send(channel="email", recipient=RECIPIENT, subject=SUBJECT, body=BODY)


def test_gmail_credenciales_obligatorias():
    with pytest.raises(ValueError):
        GmailNotificationSender(gmail_user="", app_password=APP_PASSWORD)
    with pytest.raises(ValueError):
        GmailNotificationSender(gmail_user=GMAIL_USER, app_password="")


# ------------------------------------------------------------------- logs
def test_gmail_logs_sin_destinatario_cuerpo_ni_secreto(caplog):
    recorder = _SMTPRecorder()
    with caplog.at_level(logging.INFO):
        result = _sender(recorder).send(
            channel="email", recipient=RECIPIENT, subject=SUBJECT, body=BODY
        )
    assert result.ok
    assert RECIPIENT not in caplog.text
    assert "123456" not in caplog.text
    assert APP_PASSWORD not in caplog.text
    assert result.provider_ref in caplog.text  # solo canal + provider_ref


# --------------------------------------------------- seleccion por entorno
def test_email_provider_selecciona_gmail_mock_e_invalido(monkeypatch):
    from app.modules.notifications import service as svc

    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    assert isinstance(svc.email_sender(), MockNotificationSender)
    monkeypatch.setenv("EMAIL_PROVIDER", "mock")
    assert isinstance(svc.email_sender(), MockNotificationSender)

    monkeypatch.setenv("EMAIL_PROVIDER", "gmail")
    monkeypatch.setenv("GMAIL_USER", GMAIL_USER)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", APP_PASSWORD)
    assert isinstance(svc.email_sender(), GmailNotificationSender)

    monkeypatch.setenv("EMAIL_PROVIDER", "palomas")
    with pytest.raises(ValueError):
        svc.email_sender()


def test_gmail_provider_sin_credenciales_falla_rapido(monkeypatch):
    from app.modules.notifications import service as svc

    monkeypatch.setenv("EMAIL_PROVIDER", "gmail")
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    with pytest.raises(ValueError):
        svc.email_sender()


def test_sender_for_channel_no_altera_el_routing_sms(monkeypatch):
    from app.modules.notifications import service as svc

    monkeypatch.setenv("EMAIL_PROVIDER", "gmail")
    monkeypatch.setenv("GMAIL_USER", GMAIL_USER)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", APP_PASSWORD)
    monkeypatch.delenv("SMS_PROVIDER", raising=False)
    assert isinstance(svc.sender_for_channel("email"), GmailNotificationSender)
    assert isinstance(svc.sender_for_channel("sms"), MockNotificationSender)
    assert isinstance(svc.sender_for_channel("push"), MockNotificationSender)


# ------------------------------------------- fachada: plantilla email OTP
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


class _CapturingSender:
    def __init__(self):
        self.sent: list[dict] = []

    def send(self, *, channel, recipient, subject, body):
        self.sent.append(
            {"channel": channel, "recipient": recipient, "subject": subject, "body": body}
        )
        return SendResult(ok=True, provider_ref="capture-1")


def test_fachada_renderiza_otp_code_email_con_semilla(sqlite_session: Session):
    from app.modules.notifications.service import send

    capturer = _CapturingSender()
    row = send(
        sqlite_session,
        channel="email",
        recipient=RECIPIENT,
        template_code="otp_code_email",
        data={"code": "123456", "ttl_minutes": 10},
        sender=capturer,
        sleep_fn=NO_SLEEP,
    )
    assert row.status == "SENT"
    assert capturer.sent[0]["channel"] == "email"
    assert capturer.sent[0]["subject"] == SUBJECT
    assert "123456" in capturer.sent[0]["body"]
    assert "10" in capturer.sent[0]["body"]


def test_fachada_otp_code_email_canal_cruzado_es_error(sqlite_session: Session):
    from app.modules.notifications.service import send

    with pytest.raises(ValueError):
        send(
            sqlite_session,
            channel="sms",
            recipient="+51999999999",
            template_code="otp_code_email",
            data={"code": "123456", "ttl_minutes": 10},
            sender=MockNotificationSender(),
            sleep_fn=NO_SLEEP,
        )


def test_semilla_otp_code_email_registrada_sin_romper_las_previas():
    from app.modules.notifications.domain.templates import BASE_TEMPLATES

    assert set(BASE_TEMPLATES) >= {"otp_code", "otp_code_email", "account_activated", "login_alert"}
    otp_email = BASE_TEMPLATES["otp_code_email"]
    assert otp_email["channel"] == "email"
    assert otp_email["subject"]
    assert "{code}" in otp_email["body_template"]
    assert "{ttl_minutes}" in otp_email["body_template"]
    assert BASE_TEMPLATES["otp_code"]["channel"] == "sms"


# ------------------------------------------------------------- migracion 0016
def test_migracion_0016_es_data_only_y_reversible():
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / ("0016_notification_email_template.py")
    )
    assert path.exists(), "falta migracion 0016_notification_email_template.py"
    content = path.read_text(encoding="utf-8")
    assert 'revision = "0016_notification_email_template"' in content
    assert 'down_revision = "0015_identity_login"' in content
    for token in (
        "otp_code_email",
        "ON CONFLICT (code) DO NOTHING",
        "DELETE FROM notifications.notification_templates",
    ):
        assert token in content, f"migracion sin {token}"
    for ddl in ("op.create_table", "op.add_column", "op.drop_table", "op.drop_column"):
        assert ddl not in content, f"0016 debe ser data-only, contiene {ddl}"
    for foreign in ("identity.", "ledger.", "transactions.", "accounts."):
        assert foreign not in content, f"la migracion no debe tocar {foreign}"


def test_migracion_0016_up_down_en_bd_de_prueba(db_session: Session):
    """`downgrade` elimina la fila y `upgrade` la restaura (idempotente)."""
    from tests.db_utils import BACKEND_DIR, resolve_test_database_url

    url = resolve_test_database_url()
    bind = db_session.get_bind()

    def _count() -> int:
        return bind.execute(
            text(
                "SELECT count(*) FROM notifications.notification_templates "
                "WHERE code = 'otp_code_email'"
            )
        ).scalar_one()

    def _alembic(*args: str) -> None:
        import os

        env = os.environ.copy()
        env["DATABASE_URL"] = url
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=str(BACKEND_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert (
            proc.returncode == 0
        ), f"alembic {' '.join(args)} fallo:\n{proc.stdout}\n{proc.stderr}"

    assert _count() == 1, "0016 debe dejar la plantilla al aplicar head"
    try:
        _alembic("downgrade", "0015_identity_login")
        assert _count() == 0, "downgrade debe eliminar la plantilla"
    finally:
        _alembic("upgrade", "head")
    assert _count() == 1, "upgrade debe restaurar la plantilla"

    _alembic("upgrade", "head")  # re-aplicar en head es no-op
    assert _count() == 1, "re-aplicar no debe duplicar la plantilla"


def test_plantilla_persistida_por_0016_coincide_con_la_semilla(db_session: Session):
    from app.modules.notifications.domain.templates import BASE_TEMPLATES
    from app.modules.notifications.models import NotificationTemplate

    row = db_session.scalar(
        sa.select(NotificationTemplate).where(NotificationTemplate.code == "otp_code_email")
    )
    assert row is not None
    seed = BASE_TEMPLATES["otp_code_email"]
    assert (row.channel, row.subject, row.body_template, row.enabled) == (
        seed["channel"],
        seed["subject"],
        seed["body_template"],
        True,
    )


# ------------------------------------------------ sin regresion mock/SMS
def test_mock_sigue_siendo_el_default_sin_env(monkeypatch):
    from app.modules.notifications import service as svc

    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.delenv("SMS_PROVIDER", raising=False)
    assert isinstance(svc.default_sender(), MockNotificationSender)
    assert isinstance(svc.sender_for_channel("email"), MockNotificationSender)
