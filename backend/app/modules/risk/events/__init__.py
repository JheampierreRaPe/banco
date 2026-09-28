"""Consumidor `auth.failed_attempt` -> `risk` + notificacion (E2-T20, HU08).

Contrato D-04 (mismo que `accounts.events`, E2-T23): el bus
`app.core.bus` entrega **solo el `entry`** (registro de
`shared.outbox`, sin sesion). `on_auth_failed_attempt(entry)` abre
**su propia sesion** con `commit` propio, deriva
`event_id = entry.id`, mapea `entry.payload` y delega en
`risk.service.handle_auth_failed_attempt` (idempotente por
`processed_events`, consumidor `"risk-auth-failures"`).

Sin imports a modelos ajenos: solo dicts del payload por
evento/fachada (regla de oro 4). Sin PII ni secretos en logs. No mueve
dinero (regla de oro 1): solo registra severidad, audita y notifica.
Ante fallo se revierte y se re-lanza para que el worker aplique
reintento con backoff (diseno E5-T05).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def on_auth_failed_attempt(
    entry: Any,
    *,
    session_factory: Callable[[], Any] | None = None,
    sender: Any | None = None,
) -> bool:
    """Handler del bus para `auth.failed_attempt` (D-04).

    Abre su propia sesion (`session_factory` o `SessionLocal`), deriva
    `event_id = entry.id`, mapea `entry.payload` y delega en el servicio.
    `commit` propio si registra; `rollback` y re-lanza ante fallo (el
    worker reintenta). Retorna `True` en el primer consumo, `False` ante
    duplicado ignorado. `sender` se inyecta (tests sin red).
    """
    from app.modules.risk.service import handle_auth_failed_attempt

    if session_factory is None:
        from app.core.db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    payload = dict(entry.payload or {})
    session = session_factory()
    try:
        outcome = handle_auth_failed_attempt(
            session,
            event_id=entry.id,
            payload=payload,
            sender=sender,
        )
        session.commit()
        return bool(outcome.get("consumed"))
    except Exception:
        try:
            session.rollback()
        except Exception:  # noqa: BLE001, S110 - limpieza best-effort
            pass
        raise
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001, S110 - limpieza best-effort
            pass


__all__ = ["on_auth_failed_attempt"]
