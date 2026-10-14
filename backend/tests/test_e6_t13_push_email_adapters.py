"""Adaptadores push/correo desacoplados por canal (E6-T13, HU22).

- `sender_for_channel("push")` resuelve por `PUSH_PROVIDER` (mock por
  defecto); `email` por `EMAIL_PROVIDER`; `sms` por `SMS_PROVIDER`.
- El mock push es determinista y sin red (guiones `ok`/`down`/`timeout`).
- El fallo de un canal queda aislado (error tipado, sin cruzar al otro).
- Sin PII ni secretos en logs; sin red real en ningun test.
"""

from __future__ import annotations

import logging
import socket

import pytest

from app.adapters.notification_sender import (
    GmailNotificationSender,
    MockNotificationSender,
    NotificationProviderError,
    NotificationValidationError,
    PushNotificationSender,
    TwilioNotificationSender,
)

RECIPIENT = "titular-hu22@example.com"
BODY_SECRETO = "codigo-super-secreto-123456"


@pytest.fixture()
def sin_red(monkeypatch):
    """Bloquea la red: cualquier intento de abrir un socket falla el test."""

    def _prohibido(*args, **kwargs):
        raise AssertionError("el mock no debe abrir red")

    monkeypatch.setattr(socket, "socket", _prohibido)
    monkeypatch.setattr(socket, "create_connection", _prohibido)


# ------------------------------------------------- resolucion por entorno
def test_push_resuelve_mock_por_defecto(monkeypatch):
    from app.modules.notifications import service as svc

    monkeypatch.delenv("PUSH_PROVIDER", raising=False)
    sender = svc.sender_for_channel("push")
    assert isinstance(sender, PushNotificationSender)
    # Compatibilidad: sigue siendo el mock multicanal (E1-T09/E1-T25 intactos).
    assert isinstance(sender, MockNotificationSender)
    assert isinstance(svc.push_sender(), PushNotificationSender)


def test_push_provider_desconocido_falla_rapido(monkeypatch):
    from app.modules.notifications import service as svc

    monkeypatch.setenv("PUSH_PROVIDER", "palomas")
    with pytest.raises(ValueError):
        svc.push_sender()
    with pytest.raises(ValueError):
        svc.sender_for_channel("push")


def test_email_y_sms_conservan_su_resolucion(monkeypatch):
    from app.modules.notifications import service as svc

    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.delenv("SMS_PROVIDER", raising=False)
    assert isinstance(svc.sender_for_channel("email"), MockNotificationSender)
    assert isinstance(svc.sender_for_channel("sms"), MockNotificationSender)

    monkeypatch.setenv("EMAIL_PROVIDER", "gmail")
    monkeypatch.setenv("GMAIL_USER", "demo@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "xxxxxxxxxxxxxxxx")
    monkeypatch.setenv("SMS_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token-demo")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15005550006")
    assert isinstance(svc.sender_for_channel("email"), GmailNotificationSender)
    assert isinstance(svc.sender_for_channel("sms"), TwilioNotificationSender)


def test_canales_son_independientes(monkeypatch):
    """Cambiar/romper un canal no altera la resolucion de los demas."""
    from app.modules.notifications import service as svc

    monkeypatch.setenv("PUSH_PROVIDER", "palomas")
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.delenv("SMS_PROVIDER", raising=False)
    with pytest.raises(ValueError):
        svc.sender_for_channel("push")
    # Los demas canales siguen resolviendo con normalidad.
    assert isinstance(svc.sender_for_channel("email"), MockNotificationSender)
    assert isinstance(svc.sender_for_channel("sms"), MockNotificationSender)


def test_cada_llamada_devuelve_instancia_independiente(monkeypatch):
    """Sin estado compartido entre canales ni entre llamadas."""
    from app.modules.notifications import service as svc

    monkeypatch.delenv("PUSH_PROVIDER", raising=False)
    primero = svc.sender_for_channel("push")
    segundo = svc.sender_for_channel("push")
    assert primero is not segundo
    primero.set_mode("down")
    assert segundo.mode == "ok"


# ------------------------------------------------------- contrato push
def test_push_ok_envia_sin_red(sin_red):
    sender = PushNotificationSender()
    result = sender.send(channel="push", recipient="device-token-abc", subject=None, body="hola")
    assert result.ok is True
    assert result.provider_ref.startswith("mock-push-")
    assert result.error is None
    assert sender.calls and sender.calls[-1]["ok"] is True


def test_push_down_devuelve_error_sin_lanzar(sin_red):
    sender = PushNotificationSender(mode="down")
    result = sender.send(channel="push", recipient="device-token-abc", subject=None, body="hola")
    assert result.ok is False
    assert result.error
    assert result.provider_ref is None


def test_push_timeout_lanza_error_tipado(sin_red):
    sender = PushNotificationSender(mode="timeout")
    with pytest.raises(NotificationProviderError):
        sender.send(channel="push", recipient="device-token-abc", subject=None, body="hola")


def test_push_rechaza_otro_canal():
    sender = PushNotificationSender()
    with pytest.raises(NotificationValidationError):
        sender.send(channel="email", recipient=RECIPIENT, subject="s", body="b")
    with pytest.raises(NotificationValidationError):
        sender.send(channel="sms", recipient="+51999999999", subject=None, body="b")


def test_push_valida_destinatario_y_cuerpo():
    sender = PushNotificationSender()
    with pytest.raises(ValueError):
        sender.send(channel="push", recipient="  ", subject=None, body="hola")
    with pytest.raises(ValueError):
        sender.send(channel="push", recipient="device-token-abc", subject=None, body="  ")


def test_push_mode_invalido_falla_rapido():
    with pytest.raises(ValueError):
        PushNotificationSender(mode="avion")
    sender = PushNotificationSender()
    with pytest.raises(ValueError):
        sender.set_mode("avion")


# ------------------------------------------------------- aislamiento
def test_push_caido_no_altera_el_correo_y_viceversa(sin_red, monkeypatch):
    from app.modules.notifications import service as svc

    monkeypatch.delenv("PUSH_PROVIDER", raising=False)
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)

    push = svc.sender_for_channel("push")
    assert isinstance(push, PushNotificationSender)
    push.set_mode("down")
    correo = svc.sender_for_channel("email")

    push_result = push.send(
        channel="push", recipient="device-token-abc", subject=None, body="alerta"
    )
    correo_result = correo.send(channel="email", recipient=RECIPIENT, subject="Aviso", body="hola")
    assert push_result.ok is False
    assert correo_result.ok is True

    # Y al reves: el correo caido no tumba al push.
    correo.set_channel_failure("email", "gmail caido")
    push.set_mode("ok")
    assert (
        push.send(channel="push", recipient="device-token-abc", subject=None, body="alerta").ok
        is True
    )
    assert (
        correo.send(channel="email", recipient=RECIPIENT, subject="Aviso", body="hola").ok is False
    )


def test_mocks_no_abren_red_en_ningun_canal(sin_red):
    multicanal = MockNotificationSender()
    for canal, destinatario in (
        ("push", "device-token-abc"),
        ("email", RECIPIENT),
        ("sms", "+51999999999"),
    ):
        result = multicanal.send(
            channel=canal, recipient=destinatario, subject="Aviso", body="hola"
        )
        assert result.ok is True


# ------------------------------------------------------- sin PII en logs
def test_push_sin_pii_ni_secretos_en_logs(caplog):
    sender = PushNotificationSender()
    with caplog.at_level(logging.INFO, logger="app.adapters.notification_sender"):
        ok_result = sender.send(
            channel="push", recipient=RECIPIENT, subject="Asunto", body=BODY_SECRETO
        )
        sender.set_mode("down")
        sender.send(channel="push", recipient=RECIPIENT, subject="Asunto", body=BODY_SECRETO)
        sender.set_mode("timeout")
        with pytest.raises(NotificationProviderError):
            sender.send(channel="push", recipient=RECIPIENT, subject="Asunto", body=BODY_SECRETO)
    assert RECIPIENT not in caplog.text
    assert BODY_SECRETO not in caplog.text
    assert "Asunto" not in caplog.text
    assert "push" in caplog.text
    assert ok_result.provider_ref in caplog.text  # solo canal + provider_ref
