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

logger = logging.getLogger(__name__)

# Canales soportados (`docs/03b-diccionario-de-datos.md#13`).
CHANNELS = ("push", "email", "sms")


class NotificationProviderError(RuntimeError):
    """Fallo del proveedor (caida transitoria o permanente de un canal)."""


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
