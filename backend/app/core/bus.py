"""Dispatcher en memoria de eventos de dominio (E5-T05, HU17).

Registra handlers por tipo de evento y despacha. Es el transporte que usa
el worker del outbox (`jobs/publisher.py`) DESPUES de la transaccion de
negocio (regla de oro 8). Sin I/O, sin hilos, sin logica de negocio.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class EventBus:
    """Bus en memoria: handlers por `event_type`."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[Any], None]]] = {}

    def subscribe(self, event_type: str, handler: Callable[[Any], None]) -> None:
        """Registra `handler(entry)` para `event_type`."""
        if not event_type or not str(event_type).strip():
            raise ValueError("event_type es obligatorio")
        if not callable(handler):
            raise ValueError("handler debe ser callable")
        self._handlers.setdefault(str(event_type), []).append(handler)

    def unsubscribe(self, event_type: str, handler: Callable[[Any], None]) -> None:
        """Retira un handler previamente registrado (best-effort)."""
        handlers = self._handlers.get(str(event_type), [])
        if handler in handlers:
            handlers.remove(handler)

    def handler_count(self, event_type: str) -> int:
        """Handlers registrados para `event_type`."""
        return len(self._handlers.get(str(event_type), []))

    def clear(self) -> None:
        """Retira todos los handlers (util en tests)."""
        self._handlers.clear()

    def dispatch(self, entry: Any) -> int:
        """Despacha `entry` a sus handlers en orden de registro.

        Sin handlers registrados se considera entregado (0 llamadas, sin
        error): el outbox ya garantiza el registro del evento. Si un
        handler falla, la excepcion se propaga para que el worker aplique
        reintento con backoff.
        """
        handlers = list(self._handlers.get(str(entry.event_type), []))
        for handler in handlers:
            handler(entry)
        return len(handlers)


_default_bus = EventBus()


def get_default_bus() -> EventBus:
    """Bus global por defecto (el worker lo usa si no recibe otro)."""
    return _default_bus


def subscribe(event_type: str, handler: Callable[[Any], None]) -> None:
    """Atajo sobre el bus global."""
    _default_bus.subscribe(event_type, handler)


def dispatch(entry: Any) -> int:
    """Atajo de despacho sobre el bus global."""
    return _default_bus.dispatch(entry)


def clear() -> None:
    """Limpia los handlers del bus global (util en tests)."""
    _default_bus.clear()
