"""Adaptador de notificaciones push/email/SMS (E1-T09, HU02).

Contrato (`docs/02-arquitectura.md#9`): `send(canal, destinatario, plantilla,
datos)`. Este modulo expone la interfaz `NotificationSender` (Protocol) y la
implementacion `MockNotificationSender`, activable por configuracion y lista
para reemplazarse por un proveedor real sin tocar el dominio.

Reglas (docs/16):
- Timeouts/reintentos con backoff: el reintento vive en la fachada
  (`app.modules.notifications.service`); el adaptador solo intenta un envio.
- Sin PII en logs: solo se registra canal, `provider_ref` y resultado; nunca
  el destinatario ni el contenido renderizado.
- Sin secretos, sin `float`, sin dependencias de otros modulos.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

# Canales soportados (`docs/03b-diccionario-de-datos.md#13`).
CHANNELS = ("push", "email", "sms")

# Timeout unico del POST a Twilio (el reintento vive en la fachada, aqui no).
TWILIO_TIMEOUT_SECONDS = 10.0


class NotificationProviderError(RuntimeError):
    """Fallo del proveedor (caida transitoria o permanente de un canal)."""


class NotificationValidationError(ValueError):
    """Dato rechazado por el proveedor (no reintentable).

    Subclase de `ValueError` a proposito: la fachada
    (`app.modules.notifications.service`) deja pasar `ValueError` sin
    reintentar, asi un 4xx de Twilio falla rapido en lugar de reenviar un
    mensaje que el proveedor nunca va a aceptar.
    """


@dataclass(frozen=True)
class SendResult:
    """Resultado de un intento de envio contra el proveedor."""

    ok: bool
    provider_ref: str | None = None
    error: str | None = None


class NotificationSender(Protocol):
    """Interfaz del adaptador de notificaciones (contrato estable).

    La implementacion real y el mock comparten esta firma: `send` intenta un
    unico envio y nunca duerme ni reintenta (el backoff es responsabilidad de
    la fachada del modulo `notifications`).
    """

    def send(self, *, channel: str, recipient: str, subject: str | None, body: str) -> SendResult:
        """Intenta un envio. No lanza por fallo de negocio: devuelve `ok=False`."""
        ...


class MockNotificationSender:
    """Mock multicanal con caida configurable por canal (E1-T09).

    - Registra cada intento en `calls` (solo memoria, para aserciones).
    - `fail_channels`: `{canal: motivo}` simula que ese proveedor cayo; los
      demas canales siguen enviando (tolerancia a caida parcial).
    - `raise_channels`: `{canal: motivo}` simula caida dura (excepcion
      `NotificationProviderError`) en lugar de `SendResult(ok=False)`.
    - Los logs solo incluyen canal y `provider_ref` (sin PII).
    """

    def __init__(self) -> None:
        self._failures: dict[str, str] = {}
        self._raise: dict[str, str] = {}
        self.calls: list[dict] = []

    # -- configuracion del simulacro -------------------------------------
    def set_channel_failure(self, channel: str, error: str = "provider caido") -> None:
        """Hace que `channel` devuelva `SendResult(ok=False, error=...)`."""
        self._validate_channel(channel)
        self._failures[channel] = error

    def set_channel_raise(self, channel: str, error: str = "provider caido") -> None:
        """Hace que `channel` lance `NotificationProviderError`."""
        self._validate_channel(channel)
        self._raise[channel] = error

    def clear_channel_failure(self, channel: str) -> None:
        """Repara `channel`: vuelve a enviar con exito."""
        self._validate_channel(channel)
        self._failures.pop(channel, None)
        self._raise.pop(channel, None)

    # -- contrato ---------------------------------------------------------
    def send(self, *, channel: str, recipient: str, subject: str | None, body: str) -> SendResult:
        self._validate_channel(channel)
        if not recipient or not str(recipient).strip():
            raise ValueError("recipient es obligatorio")
        if not body or not str(body).strip():
            raise ValueError("body es obligatorio")
        if channel in self._raise:
            error = self._raise[channel]
            logger.warning("notifications.mock.error channel=%s error=%s", channel, error)
            raise NotificationProviderError(error)
        if channel in self._failures:
            error = self._failures[channel]
            self.calls.append(
                {"channel": channel, "ok": False, "provider_ref": None, "error": error}
            )
            logger.warning("notifications.mock.failed channel=%s error=%s", channel, error)
            return SendResult(ok=False, error=error)
        provider_ref = f"mock-{channel}-{uuid.uuid4().hex[:12]}"
        self.calls.append(
            {"channel": channel, "ok": True, "provider_ref": provider_ref, "error": None}
        )
        logger.info("notifications.mock.sent channel=%s provider_ref=%s", channel, provider_ref)
        return SendResult(ok=True, provider_ref=provider_ref)

    @staticmethod
    def _validate_channel(channel: str) -> None:
        if channel not in CHANNELS:
            raise ValueError(f"channel debe ser uno de {CHANNELS}, recibido: {channel!r}")


class TwilioNotificationSender:
    """Proveedor SMS real via API REST de Twilio (aditivo, E1-T09).

    Implementa el MISMO `NotificationSender` Protocol que el mock: un unico
    intento por `send`, sin dormir ni reintentar (el backoff es
    responsabilidad de la fachada). Solo transporta el `body` ya renderizado
    por la fachada; no persiste nada en BD.

    - `recipient`: telefono destino en formato E.164 con prefijo `+`
      (ej. `+51999999999`); sin `+` (o sin digitos validos) se rechaza con
      `NotificationValidationError` antes de cualquier llamada HTTP.
    - Solo atiende el canal `sms`; otro canal -> `NotificationValidationError`.
    - POST `https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json`
      con `From`/`To`/`Body` y Basic Auth `(api_key_sid, auth_token)`,
      timeout 10 s.
    - Esquema de credenciales separado (diagnostico: Twilio exige el Account
      SID real `AC...` en el path; con la API Key `SK...` en el path responde
      404/20404):
      `account_sid` (path, debe empezar con `AC`) + `api_key_sid` (usuario del
      Basic Auth, default = `account_sid` para compatibilidad con auth clasica
      `AC`+token) + `auth_token` (password: API Key secret o Auth Token).
    - 201 -> `SendResult(ok=True, provider_ref=SID)`.
    - 4xx -> `NotificationValidationError` (no reintentable; el mensaje de
      Twilio NO se propaga porque puede ecoar el numero: solo se informa el
      HTTP status + codigo numerico de Twilio).
    - Timeout, caida de red o 5xx -> `NotificationProviderError`
      (reintentable por la fachada).
    - Logs: solo canal + `provider_ref`; nunca destinatario, cuerpo ni secreto.
    """

    BASE_URL = "https://api.twilio.com/2010-04-01"

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        from_number: str,
        api_key_sid: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = TWILIO_TIMEOUT_SECONDS,
    ) -> None:
        sid = str(account_sid or "").strip()
        token = str(auth_token or "").strip()
        sender = str(from_number or "").strip()
        if not sid:
            raise ValueError("account_sid es obligatorio (env TWILIO_ACCOUNT_SID)")
        if not sid.startswith("AC"):
            raise ValueError(
                "account_sid debe ser el Account SID real (prefijo 'AC...') "
                "para el path /Accounts/{AC}/Messages.json; no uses la API Key "
                "(SK...) en el path (Twilio responde 404/20404). Configura "
                "TWILIO_ACCOUNT_SID con el AC... y la API Key en TWILIO_API_KEY_SID."
            )
        if not token:
            raise ValueError("auth_token es obligatorio (env TWILIO_AUTH_TOKEN)")
        if not sender:
            raise ValueError("from_number es obligatorio (env TWILIO_FROM_NUMBER)")
        # `api_key_sid` es el usuario del Basic Auth: API Key SID (SK...) cuando
        # se autentica con API Key, o el mismo Account SID (AC...) con auth
        # clasica. Default = `account_sid` (firma anterior sigue funcionando).
        key_sid = str(api_key_sid or "").strip() or sid
        if not key_sid:
            raise ValueError("api_key_sid es obligatorio (env TWILIO_API_KEY_SID)")
        self._account_sid = sid
        self._api_key_sid = key_sid
        self._auth_token = token
        self._from_number = sender
        self._timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)

    # -- contrato ---------------------------------------------------------
    def send(self, *, channel: str, recipient: str, subject: str | None, body: str) -> SendResult:
        if channel != "sms":
            raise NotificationValidationError(
                f"TwilioNotificationSender solo atiende canal 'sms', recibido: {channel!r}"
            )
        to = self._validate_recipient(recipient)
        text = str(body or "")
        if not text.strip():
            raise NotificationValidationError("body es obligatorio")
        url = f"{self.BASE_URL}/Accounts/{self._account_sid}/Messages.json"
        try:
            response = self._client.post(
                url,
                data={"From": self._from_number, "To": to, "Body": text},
                auth=(self._api_key_sid, self._auth_token),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            logger.warning("notifications.twilio.timeout channel=%s", channel)
            raise NotificationProviderError(
                f"twilio timeout: {type(exc).__name__}"
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning(
                "notifications.twilio.network channel=%s error=%s",
                channel,
                type(exc).__name__,
            )
            raise NotificationProviderError(
                f"twilio red no disponible: {type(exc).__name__}"
            ) from exc
        if response.status_code == 201:
            sid = self._extract_sid(response)
            logger.info(
                "notifications.twilio.sent channel=%s provider_ref=%s", channel, sid
            )
            return SendResult(ok=True, provider_ref=sid)
        if 400 <= response.status_code < 500:
            code = self._extract_twilio_code(response)
            logger.warning(
                "notifications.twilio.rejected channel=%s http=%s code=%s",
                channel,
                response.status_code,
                code,
            )
            raise NotificationValidationError(
                f"twilio rechazo el sms: http={response.status_code} code={code}"
            )
        logger.warning(
            "notifications.twilio.unavailable channel=%s http=%s",
            channel,
            response.status_code,
        )
        raise NotificationProviderError(
            f"twilio no disponible: http={response.status_code}"
        )

    def close(self) -> None:
        """Libera el cliente HTTP propio (no llamar si se inyecto uno externo)."""
        self._client.close()

    @staticmethod
    def _validate_recipient(recipient: str) -> str:
        to = str(recipient or "").strip()
        digits = to[1:] if to.startswith("+") else ""
        if not to.startswith("+") or not digits.isdigit() or len(digits) < 7:
            raise NotificationValidationError(
                "recipient debe ser un telefono E.164 con prefijo '+' (ej. '+51999999999')"
            )
        return to

    @staticmethod
    def _extract_sid(response: httpx.Response) -> str:
        try:
            sid = (response.json() or {}).get("sid")
        except ValueError:
            sid = None
        # 201 significa aceptado: si faltara el SID se usa ref local antes que
        # fallar (fallar provocaria un reintento que duplicaria el SMS).
        return str(sid) if sid else f"twilio-sin-sid-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _extract_twilio_code(response: httpx.Response) -> str:
        try:
            code = (response.json() or {}).get("code")
        except ValueError:
            code = None
        return str(code) if code is not None else "sin-codigo"
