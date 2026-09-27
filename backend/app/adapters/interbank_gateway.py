"""Pasarela interbancaria simulada (E2-T09, modulo transactions).

Simulador de la red interbancaria (`docs/05#7`, contrato propio hasta que
exista el del equipo par): enviar (`POST /interbank/transfers`), consultar
(`GET /interbank/transfers/{reference}`) y confirmacion/reverso. Esta tarea
solo implementa el `mock` determinista; la saga (`transactions.service`)
nunca bloquea el hilo: timeouts cortos y polling con reintentos.

Reglas (docs/02#8/#10, docs/16 reglas 7/9/10):
- Determinista por `reference`/monto para pruebas: la misma referencia con
  el mismo guion produce el mismo resultado; sin guion rige el `default`.
- Sin PII en logs: el CCI nunca llega aqui (solo su hash `cci_hash`); los
  mensajes de error son neutros y sin eco de datos.
- Timeouts cortos: el mock no hace red; `timeout`/`down` se simulan por
  guion para probar la rama de reintento/compensacion de la saga.
- Sin persistencia: el estado programado vive en memoria (por instancia).

Configuracion por entorno (fuente unica `backend/.env`):
- `INTERBANK_GATEWAY_MODE=mock` (default `mock`; cualquier otro -> mock).
- `INTERBANK_GATEWAY_DEFAULT_SEND=accept|confirm|reject` (default `accept`).
- `INTERBANK_GATEWAY_DEFAULT_STATUS=pending|confirmed|failed|reversed`
  (default `confirmed`).
"""

from __future__ import annotations

import hashlib
import logging
import os
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- Codigos de negocio
GATEWAY_TIMEOUT = "INTERBANK_TIMEOUT"
GATEWAY_UNAVAILABLE = "INTERBANK_UNAVAILABLE"
GATEWAY_REJECTED = "INTERBANK_REJECTED"

#: Estados de envio (`send_transfer`).
SEND_ACCEPTED = "ACCEPTED"
SEND_CONFIRMED = "CONFIRMED"
SEND_REJECTED = "REJECTED"

#: Estados de consulta (`get_status`).
STATUS_PENDING = "PENDING"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_FAILED = "FAILED"
STATUS_REJECTED = "REJECTED"
STATUS_REVERSED = "REVERSED"


class InterbankGatewayError(Exception):
    """Error base de la pasarela; expone `code` para propagacion tipada."""

    code: str = GATEWAY_UNAVAILABLE

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GatewayTimeoutError(InterbankGatewayError):
    """La pasarela no respondio en plazo (reintentable; la saga mantiene el hold)."""

    def __init__(self, message: str = "timeout contra la pasarela interbancaria") -> None:
        super().__init__(GATEWAY_TIMEOUT, message)


class GatewayUnavailableError(InterbankGatewayError):
    """La pasarela no esta disponible (reintentable)."""

    def __init__(self, message: str = "pasarela interbancaria no disponible") -> None:
        super().__init__(GATEWAY_UNAVAILABLE, message)


class GatewayRejectedError(InterbankGatewayError):
    """La contraparte rechazo el envio (no reintentable; la saga compensa)."""

    def __init__(self, message: str = "la contraparte rechazo el envio") -> None:
        super().__init__(GATEWAY_REJECTED, message)


def hash_reference(reference: str) -> str:
    """Hash corto de la referencia para correlacion en logs (sin PII)."""
    return hashlib.sha256(str(reference or "").encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- Resultado
@dataclass(frozen=True)
class SendResult:
    """Resultado del envio: `ACCEPTED` (pendiente, seguir por polling),
    `CONFIRMED` (via rapida) o `REJECTED` (compensar sin reintentar)."""

    status: str
    gateway_ref: str = ""


# ---------------------------------------------------------------- Interfaz
class InterbankGateway(ABC):
    """Interfaz comun de la pasarela interbancaria (E2-T09)."""

    @abstractmethod
    def send_transfer(
        self,
        *,
        reference: str,
        amount_minor: int,
        currency: str,
        bank_code: str = "",
        cci_hash: str = "",
    ) -> SendResult:
        """Envia la transferencia (`reference` unica: reenvio idempotente)."""

    @abstractmethod
    def get_status(self, *, reference: str) -> str:
        """Consulta el estado: `PENDING`/`CONFIRMED`/`FAILED`/`REJECTED`/`REVERSED`."""


# ---------------------------------------------------------------- Mock determinista
@dataclass
class MockInterbankGateway(InterbankGateway):
    """Mock determinista por `reference` (guion) con default configurable.

    - `script`: `{"REF-1": ["timeout", "accept"], ...}`; cada llamada consume
      el primer resultado pendiente de esa referencia (el reenvio con la
      misma referencia avanza el guion: modela timeout -> reintento).
    - Sin guion rigen `default_send` (`accept|confirm|reject`) y
      `default_status` (`pending|confirmed|failed|rejected|reversed`).
    - `"timeout"` lanza `GatewayTimeoutError`; `"down"` lanza
      `GatewayUnavailableError`; cualquier token desconocido es `ValueError`
      (falla rapido en pruebas, nunca en produccion: el default es valido).
    - `mode` identifica la instancia en logs (huella como E2-T08).
    """

    mode: str = "mock"
    default_send: str = "accept"
    default_status: str = "confirmed"
    script: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.default_send not in ("accept", "confirm", "reject"):
            raise ValueError(f"default_send invalido: {self.default_send!r}")
        if self.default_status not in ("pending", "confirmed", "failed", "rejected", "reversed"):
            raise ValueError(f"default_status invalido: {self.default_status!r}")
        self._pending: dict[str, deque[str]] = {
            ref: deque(outcomes) for ref, outcomes in dict(self.script or {}).items()
        }

    def program(self, reference: str, outcomes: list[str]) -> None:
        """Programa el guion de una referencia (solo pruebas)."""
        self._pending[str(reference)] = deque(list(outcomes))

    def _next(self, reference: str, default: str) -> str:
        queue = self._pending.get(str(reference))
        if queue:
            return queue.popleft()
        return default

    def send_transfer(
        self,
        *,
        reference: str,
        amount_minor: int,
        currency: str,
        bank_code: str = "",
        cci_hash: str = "",
    ) -> SendResult:
        ref_hash = hash_reference(reference)
        outcome = self._next(reference, self.default_send)
        logger.info(
            "interbank mock_send ref_hash=%s outcome=%s has_bank=%s",
            ref_hash,
            outcome,
            bool(bank_code),
        )
        if outcome == "timeout":
            raise GatewayTimeoutError("timeout simulado de la pasarela")
        if outcome == "down":
            raise GatewayUnavailableError("caida simulada de la pasarela")
        if outcome == "confirm":
            return SendResult(status=SEND_CONFIRMED, gateway_ref=f"GW-{ref_hash}")
        if outcome == "accept":
            return SendResult(status=SEND_ACCEPTED, gateway_ref=f"GW-{ref_hash}")
        if outcome == "reject":
            return SendResult(status=SEND_REJECTED, gateway_ref=f"GW-{ref_hash}")
        raise ValueError(f"resultado de guion desconocido: {outcome!r}")

    def get_status(self, *, reference: str) -> str:
        ref_hash = hash_reference(reference)
        outcome = self._next(reference, self.default_status)
        logger.info("interbank mock_status ref_hash=%s outcome=%s", ref_hash, outcome)
        if outcome == "timeout":
            raise GatewayTimeoutError("timeout simulado de la consulta")
        if outcome == "down":
            raise GatewayUnavailableError("caida simulada de la consulta")
        mapping = {
            "pending": STATUS_PENDING,
            "confirmed": STATUS_CONFIRMED,
            "failed": STATUS_FAILED,
            "rejected": STATUS_REJECTED,
            "reversed": STATUS_REVERSED,
        }
        try:
            return mapping[outcome]
        except KeyError as exc:
            raise ValueError(f"estado de guion desconocido: {outcome!r}") from exc


# ---------------------------------------------------------------- Fabrica por entorno
def create_interbank_gateway(kind: str | None = None, **overrides) -> InterbankGateway:
    """Fabrica: `INTERBANK_GATEWAY_MODE=mock` (default `mock`).

    Fuente: entorno (`backend/.env`); `default_send`/`default_status` se
    leen de `INTERBANK_GATEWAY_DEFAULT_SEND`/`INTERBANK_GATEWAY_DEFAULT_STATUS`.
    Cualquier modo no reconocido devuelve el mock (simulador `05#7`).
    """
    selected = kind if kind is not None else os.getenv("INTERBANK_GATEWAY_MODE", "mock")
    selected = str(selected or "mock").strip().lower()
    default_send = str(os.getenv("INTERBANK_GATEWAY_DEFAULT_SEND", "accept")).strip().lower()
    if default_send not in ("accept", "confirm", "reject"):
        default_send = "accept"
    default_status = str(os.getenv("INTERBANK_GATEWAY_DEFAULT_STATUS", "confirmed")).strip().lower()
    if default_status not in ("pending", "confirmed", "failed", "rejected", "reversed"):
        default_status = "confirmed"
    return MockInterbankGateway(
        mode=selected or "mock",
        default_send=default_send,
        default_status=default_status,
        **overrides,
    )


__all__ = [
    "GATEWAY_REJECTED",
    "GATEWAY_TIMEOUT",
    "GATEWAY_UNAVAILABLE",
    "SEND_ACCEPTED",
    "SEND_CONFIRMED",
    "SEND_REJECTED",
    "STATUS_CONFIRMED",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "STATUS_REVERSED",
    "GatewayRejectedError",
    "GatewayTimeoutError",
    "GatewayUnavailableError",
    "InterbankGateway",
    "InterbankGatewayError",
    "MockInterbankGateway",
    "SendResult",
    "create_interbank_gateway",
    "hash_reference",
]
