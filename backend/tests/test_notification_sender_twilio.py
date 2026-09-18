"""Proveedor SMS real Twilio con fallback al mock (E1-T09 aditivo).

- Transporte simulado con `httpx.MockTransport`: NUNCA se llama a la API real.
- 201 -> OK con SID como `provider_ref`; 400 -> error tipado no reintentable
  (`NotificationValidationError`, subclase de `ValueError`: la fachada no lo
  reintenta); timeout/5xx -> `NotificationProviderError` (reintentable).
- `SMS_PROVIDER` ausente -> mock (default sin red); `twilio` -> Twilio.
- Sin PII en logs: se loguea canal + SID, nunca destinatario ni cuerpo.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl

import httpx
import pytest

from app.adapters.notification_sender import (
    MockNotificationSender,
    NotificationProviderError,
    NotificationValidationError,
    TwilioNotificationSender,
)

ACCOUNT_SID = "AC00000000000000000000000000000000"
FROM_NUMBER = "+15005550006"  # numero de prueba oficial de Twilio (placeholder)
TO_NUMBER = "+51999999999"
BODY = "Tu codigo OTP es 123456. Vigencia 10 min."

MESSAGES_PATH = f"/2010-04-01/Accounts/{ACCOUNT_SID}/Messages.json"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _sender(handler, **kwargs) -> TwilioNotificationSender:
    return TwilioNotificationSender(
        account_sid=ACCOUNT_SID,
        auth_token="token-solo-test",
        from_number=FROM_NUMBER,
        client=_client(handler),
        **kwargs,
    )


def _ok_handler(request: httpx.Request) -> httpx.Response:
    assert request.method == "POST"
    assert request.url.path == MESSAGES_PATH
    assert request.headers["authorization"].startswith("Basic ")
    form = dict(parse_qsl(request.content.decode()))
    assert form["From"] == FROM_NUMBER
    assert form["To"] == TO_NUMBER
    assert form["Body"] == BODY
    return httpx.Response(201, json={"sid": "SM123abc", "status": "queued"})


def test_twilio_send_201_ok_con_sid():
    sender = _sender(_ok_handler)
    result = sender.send(channel="sms", recipient=TO_NUMBER, subject=None, body=BODY)
    assert result.ok and result.provider_ref == "SM123abc"


def test_twilio_400_es_error_no_reintentable():
    def _bad_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 21211, "message": "Invalid 'To'"})

    sender = _sender(_bad_request)
    with pytest.raises(NotificationValidationError):
        sender.send(channel="sms", recipient=TO_NUMBER, subject=None, body=BODY)
    # Tipado como ValueError: la fachada lo deja pasar SIN reintentar.
    assert issubclass(NotificationValidationError, ValueError)
    assert not issubclass(NotificationValidationError, NotificationProviderError)


def test_twilio_timeout_es_reintentable():
    def _timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("red lenta", request=request)

    sender = _sender(_timeout)
    with pytest.raises(NotificationProviderError):
        sender.send(channel="sms", recipient=TO_NUMBER, subject=None, body=BODY)


def test_twilio_500_es_reintentable():
    def _server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "caida interna"})

    sender = _sender(_server_error)
    with pytest.raises(NotificationProviderError):
        sender.send(channel="sms", recipient=TO_NUMBER, subject=None, body=BODY)


def test_recipient_exige_formato_e164_sin_llamar_a_red():
    def _must_not_call(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no debe haber llamada HTTP sin E.164 valido")

    sender = _sender(_must_not_call)
    with pytest.raises(NotificationValidationError):
        sender.send(channel="sms", recipient="51999999999", subject=None, body=BODY)


def test_twilio_solo_atiende_canal_sms():
    def _must_not_call(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("el canal email no debe salir por Twilio SMS")

    sender = _sender(_must_not_call)
    with pytest.raises(NotificationValidationError):
        sender.send(channel="email", recipient="c@example.com", subject="x", body=BODY)


def test_twilio_logs_sin_pii(caplog):
    sender = _sender(_ok_handler)
    with caplog.at_level(logging.INFO):
        result = sender.send(channel="sms", recipient=TO_NUMBER, subject=None, body=BODY)
    assert result.provider_ref == "SM123abc"
    assert TO_NUMBER not in caplog.text
    assert "123456" not in caplog.text
    assert "SM123abc" in caplog.text  # solo canal + provider_ref se loguean


def test_mock_sigue_default_cuando_no_hay_env(monkeypatch):
    from app.modules.notifications import service as svc

    monkeypatch.delenv("SMS_PROVIDER", raising=False)
    assert isinstance(svc.default_sender(), MockNotificationSender)
    monkeypatch.setenv("SMS_PROVIDER", "mock")
    assert isinstance(svc.default_sender(), MockNotificationSender)


def test_env_twilio_selecciona_proveedor_real(monkeypatch):
    from app.modules.notifications import service as svc

    monkeypatch.setenv("SMS_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", ACCOUNT_SID)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token-solo-test")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", FROM_NUMBER)
    assert isinstance(svc.default_sender(), TwilioNotificationSender)


def test_env_invalido_falla_rapido(monkeypatch):
    from app.modules.notifications import service as svc

    monkeypatch.setenv("SMS_PROVIDER", "palomas")
    with pytest.raises(ValueError):
        svc.default_sender()


# --- Esquema separado AC (path) + SK (auth) (aditivo, sin SMS reales) ---
API_KEY_SID = "SK00000000000000000000000000000000"


def _decode_basic_user(request: httpx.Request) -> str:
    import base64

    scheme, _, credentials = request.headers["authorization"].partition(" ")
    assert scheme == "Basic"
    decoded = base64.b64decode(credentials).decode()
    user, _, _password = decoded.partition(":")
    return user


def test_twilio_api_key_en_auth_y_ac_en_path():
    def _handler(request: httpx.Request) -> httpx.Response:
        # El path SIEMPRE lleva el Account SID (AC...), nunca la API Key.
        assert request.url.path == MESSAGES_PATH
        # El usuario del Basic Auth es la API Key (SK...).
        assert _decode_basic_user(request) == API_KEY_SID
        return httpx.Response(201, json={"sid": "SM123abc", "status": "queued"})

    sender = _sender(_handler, api_key_sid=API_KEY_SID)
    result = sender.send(channel="sms", recipient=TO_NUMBER, subject=None, body=BODY)
    assert result.ok and result.provider_ref == "SM123abc"


def test_twilio_sin_api_key_usa_account_sid_en_auth_por_defecto():
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == MESSAGES_PATH
        assert _decode_basic_user(request) == ACCOUNT_SID
        return httpx.Response(201, json={"sid": "SM123abc", "status": "queued"})

    sender = _sender(_handler)  # compatibilidad: firma anterior sin api_key_sid
    result = sender.send(channel="sms", recipient=TO_NUMBER, subject=None, body=BODY)
    assert result.ok


def test_twilio_rechaza_sid_de_path_sin_prefijo_ac():
    with pytest.raises(ValueError, match="prefijo 'AC"):
        TwilioNotificationSender(
            account_sid=API_KEY_SID,  # SK... en el path -> 404/20404 en Twilio
            auth_token="token-solo-test",
            from_number=FROM_NUMBER,
            client=_client(_ok_handler),
        )


def test_env_twilio_pasa_api_key_sid_al_sender(monkeypatch):
    from app.modules.notifications import service as svc

    monkeypatch.setenv("SMS_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", ACCOUNT_SID)
    monkeypatch.setenv("TWILIO_API_KEY_SID", API_KEY_SID)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token-solo-test")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", FROM_NUMBER)
    sender = svc.default_sender()
    assert isinstance(sender, TwilioNotificationSender)
    assert sender._account_sid == ACCOUNT_SID
    assert sender._api_key_sid == API_KEY_SID
