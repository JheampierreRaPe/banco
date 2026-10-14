"""Consumidor `fraud.alert.raised` -> bloqueo de canales (E6-T14, HU22).

Contrato D-04 (mismo que `risk.events` E2-T20): el bus entrega **solo
el `entry`** (registro de `shared.outbox`, sin sesion).
`on_fraud_alert_raised(entry)` abre **su propia sesion** con `commit`
propio, deriva `event_id = entry.id`, reclama idempotencia en
`processed_events` (consumidor `"identity-channel-lock"`: duplicado ->
`False` sin efectos) y delega en la fachada
`identity.service.sessions.lock_user_channels` (revoca TODAS las
sesiones del titular via `revoke_user_sessions`; un `REVOKED` no se
reactiva; nada se borra).

Sin imports a modelos ajenos: solo dicts del payload por evento/fachada
(regla de oro 4). Sin PII ni secretos en logs. No mueve dinero (regla
de oro 1): solo fija `revoked_at` y audita. Ante fallo se revierte y se
re-lanza para que el worker aplique reintento con backoff (E5-T05).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Consumidor idempotente en `shared.processed_events` (<= 60 caracteres).
CONSUMER = "identity-channel-lock"


def _parse_alert(payload: Any) -> uuid.UUID | None:
    """`user_id` del titular a bloquear (`None` si ausente/invalido)."""
    data = dict(payload) if isinstance(payload, dict) else {}
    try:
        return uuid.UUID(str(data.get("user_id"))) if data.get("user_id") else None
    except (ValueError, AttributeError, TypeError):
        return None


def on_fraud_alert_raised(
    entry: Any,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Handler del bus para `fraud.alert.raised` (D-04, E6-T14).

    Abre su propia sesion (`session_factory` o `SessionLocal`), reclama
    idempotencia (`processed_events`), revoca las sesiones del titular
    y confirma (`commit` propio). Sin `user_id` valido se marca
    procesado sin efectos (sin reintento infinito). `rollback` y
    re-lanza ante fallo (el worker reintenta). Retorna `True` en el
    primer consumo, `False` ante duplicado ignorado.
    """
    from app.modules.identity.service.sessions import lock_user_channels
    from app.modules.shared.repository import outbox as outbox_repo

    if session_factory is None:
        from app.core.db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    payload = dict(entry.payload or {})
    event_id = entry.id
    session = session_factory()
    try:
        if outbox_repo.is_processed(session, event_id=event_id, consumer=CONSUMER):
            logger.info("identity.channel_lock.duplicado event=fraud.alert.raised")
            return False
        user_id = _parse_alert(payload)
        outbox_repo.try_mark_processed(session, event_id=event_id, consumer=CONSUMER)
        if user_id is None:
            logger.warning("identity.channel_lock sin user_id: sin efectos")
            session.commit()
            return True
        lock_user_channels(session, user_id)
        session.commit()
        logger.info("identity.channel_lock.consumido event=fraud.alert.raised")
        return True
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


__all__ = ["CONSUMER", "on_fraud_alert_raised"]
