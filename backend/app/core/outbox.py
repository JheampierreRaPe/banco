"""API estable del outbox (E5-T05, HU17).

CONTRATO con E5-T03: otro worker importa `record` de forma perezosa desde
aqui (`from app.core.outbox import record`). NO cambiar la firma::

    record(session, *, aggregate_type, aggregate_id, event_type, payload)

- `aggregate_id` acepta UUID o str (UUID en texto).
- `payload` es dict JSON-serializable.
- Solo inserta en la sesion del llamante (`flush`, sin `commit`, sin
  publicar): el worker publica DESPUES (regla de oro 8).
- Estados segun `03b`: PENDING / PUBLISHED / FAILED.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.modules.shared import repository as _repo
from app.modules.shared.models import OutboxEntry


def record(
    session: Session,
    *,
    aggregate_type: str,
    aggregate_id: uuid.UUID | str,
    event_type: str,
    payload: dict[str, Any],
) -> OutboxEntry:
    """Registra un evento PENDING en la transaccion del llamante."""
    return _repo.insert_event(
        session,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=payload,
    )


def fetch_pending(session: Session, limit: int = 100) -> list[OutboxEntry]:
    """Pendientes listos para publicar, en orden por agregado."""
    return _repo.list_pending(session, limit=limit)


def mark_published(
    session: Session, entry_id: uuid.UUID, *, now: datetime | None = None
) -> OutboxEntry | None:
    """Marca un evento como PUBLISHED (`flush`, sin `commit`)."""
    return _repo.mark_published(session, entry_id, now=now)


def mark_failed(
    session: Session,
    entry_id: uuid.UUID,
    error: str | BaseException | None,
    *,
    max_attempts: int = _repo.DEFAULT_MAX_ATTEMPTS,
    backoff_base_seconds: int = _repo.DEFAULT_BACKOFF_BASE_SECONDS,
    now: datetime | None = None,
) -> OutboxEntry | None:
    """Registra un intento fallido con backoff; FAILED tras N intentos."""
    return _repo.mark_failed(
        session,
        entry_id,
        error,
        max_attempts=max_attempts,
        backoff_base_seconds=backoff_base_seconds,
        now=now,
    )
