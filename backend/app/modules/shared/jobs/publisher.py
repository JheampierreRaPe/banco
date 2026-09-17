"""Worker de publicacion del outbox (E5-T05, HU17).

Toma pendientes (`fetch_pending`), reclama cada fila de forma atomica
(`claim_entry`: el reintento de otro worker no duplica), la publica via bus
en memoria (`app.core.bus`) y la marca PUBLISHED; ante fallo aplica
reintento con backoff y marca FAILED tras N intentos.

Hace `flush`, no `commit`: quien llama decide la transaccion. Nunca se
publica dentro de transacciones de negocio (regla de oro 8): este worker
corre DESPUES, fuera de ellas.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.bus import EventBus, get_default_bus
from app.modules.shared import repository as repo
from app.modules.shared.models import OutboxEntry


def publish_pending(
    session: Session,
    *,
    bus: EventBus | None = None,
    limit: int = 100,
    max_attempts: int = repo.DEFAULT_MAX_ATTEMPTS,
    backoff_base_seconds: int = repo.DEFAULT_BACKOFF_BASE_SECONDS,
    lease_seconds: int = repo.DEFAULT_CLAIM_LEASE_SECONDS,
    now: datetime | None = None,
) -> dict[str, int]:
    """Publica pendientes y reporta `{published, failed, skipped}`.

    - `skipped`: otro worker reclamo la fila primero (claim atomico).
    - Orden por agregado (lo da `list_pending`).
    - Sin handlers para el tipo de evento: se marca PUBLISHED (nada que
      entregar; el evento queda registrado en el outbox).
    """
    transport = bus if bus is not None else get_default_bus()
    result = {"published": 0, "failed": 0, "skipped": 0}
    pending = repo.list_pending(session, limit=limit, now=now)
    for candidate in pending:
        entry_id: uuid.UUID = candidate.id
        if not repo.claim_entry(
            session, entry_id, lease_seconds=lease_seconds, now=now
        ):
            result["skipped"] += 1
            continue
        entry: OutboxEntry | None = session.get(OutboxEntry, entry_id)
        if entry is None or entry.status != "PENDING":
            result["skipped"] += 1
            continue
        try:
            transport.dispatch(entry)
        except Exception as exc:  # noqa: BLE001 - el fallo se reintenta
            repo.mark_failed(
                session,
                entry_id,
                exc,
                max_attempts=max_attempts,
                backoff_base_seconds=backoff_base_seconds,
                now=now,
            )
            result["failed"] += 1
        else:
            repo.mark_published(session, entry_id, now=now)
            result["published"] += 1
    session.flush()
    return result
