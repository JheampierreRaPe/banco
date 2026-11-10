"""Repositorio outbox/inbox del schema `shared` (E5-T05, HU17).

Capa de datos sin endpoints ni logica de negocio: solo SQLAlchemy sobre el
schema propio. Sin publicar eventos aqui (regla de oro 8: `record` solo
inserta en la sesion del llamante; el worker de `jobs/publisher.py` publica
DESPUES, fuera de la transaccion de negocio).

Convencion: las funciones hacen `flush` y no `commit`; quien llama decide
la transaccion. Asi el evento se persiste junto al cambio en la misma
transaccion (un rollback del negocio revierte tambien el outbox).

`03b` no define columna de error en `outbox`: `mark_failed` acepta `error`
para traza/logging del llamante pero solo persiste `attempts`,
`available_at` y el estado terminal `FAILED`.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.shared.models import OutboxEntry, ProcessedEvent

# Reintentos: tras N intentos el evento pasa a FAILED (terminal).
DEFAULT_MAX_ATTEMPTS = 5
# Backoff exponencial: base * 2**(attempts-1) segundos.
DEFAULT_BACKOFF_BASE_SECONDS = 60
# Lease del claim atomico: un worker que reclama una fila la reserva este
# tiempo para que un segundo worker no la duplique.
DEFAULT_CLAIM_LEASE_SECONDS = 60


def _utcnow() -> datetime:
    return datetime.now(UTC)


def compute_backoff_seconds(attempts: int, base_seconds: int) -> int:
    """Backoff exponencial tras `attempts` fallos (>= 1). Dominio puro."""
    if attempts < 1:
        raise ValueError("attempts debe ser >= 1")
    if base_seconds < 0:
        raise ValueError("base_seconds debe ser >= 0")
    return base_seconds * (2 ** (attempts - 1))


def _coerce_aggregate_id(value: uuid.UUID | str) -> uuid.UUID:
    """`aggregate_id` acepta UUID o str (UUID en texto)."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"aggregate_id debe ser UUID, recibido: {value!r}") from exc


def _validate_entry_fields(aggregate_type: str, event_type: str, payload: dict) -> None:
    if not aggregate_type or not str(aggregate_type).strip():
        raise ValueError("aggregate_type es obligatorio")
    if len(str(aggregate_type)) > 60:
        raise ValueError("aggregate_type supera 60 caracteres")
    if not event_type or not str(event_type).strip():
        raise ValueError("event_type es obligatorio")
    if len(str(event_type)) > 80:
        raise ValueError("event_type supera 80 caracteres")
    if not isinstance(payload, dict):
        raise TypeError("payload debe ser dict JSON-serializable")
    try:
        json.dumps(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"payload no es JSON-serializable: {exc}") from exc


def insert_event(
    session: Session,
    *,
    aggregate_type: str,
    aggregate_id: uuid.UUID | str,
    event_type: str,
    payload: dict,
    now: datetime | None = None,
) -> OutboxEntry:
    """Inserta un evento PENDING con `flush` sin `commit`.

    La validacion ocurre antes de agregar filas: un evento invalido se
    rechaza con `ValueError` sin dejar filas residuales.
    """
    _validate_entry_fields(aggregate_type, event_type, payload)
    entry = OutboxEntry(
        event_type=str(event_type),
        aggregate_type=str(aggregate_type),
        aggregate_id=_coerce_aggregate_id(aggregate_id),
        payload=dict(payload),
        status="PENDING",
        attempts=0,
        available_at=now or _utcnow(),
    )
    session.add(entry)
    session.flush()
    return entry


def list_pending(
    session: Session, *, limit: int = 100, now: datetime | None = None
) -> list[OutboxEntry]:
    """Pendientes listos para publicar, en orden por agregado.

    Filtro: `status = PENDING` y `available_at <= now` (respeta backoff).
    Orden: (`aggregate_type`, `aggregate_id`, `created_at`, `id`).
    """
    if limit < 1:
        raise ValueError("limit debe ser >= 1")
    moment = now or _utcnow()
    stmt = (
        sa.select(OutboxEntry)
        .where(OutboxEntry.status == "PENDING", OutboxEntry.available_at <= moment)
        .order_by(
            OutboxEntry.aggregate_type,
            OutboxEntry.aggregate_id,
            OutboxEntry.created_at,
            OutboxEntry.id,
        )
        .limit(limit)
    )
    return list(session.scalars(stmt).all())


def claim_entry(
    session: Session,
    entry_id: uuid.UUID,
    *,
    lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
    now: datetime | None = None,
) -> bool:
    """Reclamo atomico: reserva la fila para que otro worker no la duplique.

    `UPDATE ... WHERE status = PENDING AND available_at <= now`: solo un
    worker obtiene `rowcount == 1`. Hace `flush`, no `commit`.
    """
    moment = now or _utcnow()
    result = session.execute(
        sa.update(OutboxEntry)
        .where(
            OutboxEntry.id == entry_id,
            OutboxEntry.status == "PENDING",
            OutboxEntry.available_at <= moment,
        )
        .values(available_at=moment + timedelta(seconds=lease_seconds)),
        # UPDATE atomico de una sola sentencia: sin sincronizacion ORM en
        # Python (ademas evita comparar naive/aware: SQLite devuelve
        # datetimes naive y Postgres aware). La fila cacheada se expira
        # para que la siguiente lectura vea el estado real.
        execution_options={"synchronize_session": False},
    )
    cached = session.get(OutboxEntry, entry_id)
    if cached is not None:
        session.expire(cached)
    session.flush()
    return (result.rowcount or 0) == 1


def mark_published(
    session: Session, entry_id: uuid.UUID, *, now: datetime | None = None
) -> OutboxEntry | None:
    """Marca PUBLISHED con `flush` sin `commit`. `None` si no existe."""
    entry = session.get(OutboxEntry, entry_id)
    if entry is None:
        return None
    entry.status = "PUBLISHED"
    entry.published_at = now or _utcnow()
    session.flush()
    return entry


def mark_failed(
    session: Session,
    entry_id: uuid.UUID,
    error: str | BaseException | None,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_seconds: int = DEFAULT_BACKOFF_BASE_SECONDS,
    now: datetime | None = None,
) -> OutboxEntry | None:
    """Registra un intento fallido con backoff; `FAILED` tras N intentos.

    `error` es solo informativo (`03b` no define columna de error): se
    acepta para traza del llamante y no se persiste. Hace `flush`, no
    `commit`. `None` si el evento no existe.
    """
    _ = error  # informativo, no persistido (ver docstring del modulo)
    entry = session.get(OutboxEntry, entry_id)
    if entry is None:
        return None
    moment = now or _utcnow()
    entry.attempts = int(entry.attempts) + 1
    if entry.attempts >= max_attempts:
        entry.status = "FAILED"
        entry.available_at = moment
    else:
        entry.status = "PENDING"
        entry.available_at = moment + timedelta(
            seconds=compute_backoff_seconds(entry.attempts, backoff_base_seconds)
        )
    session.flush()
    return entry


def _coerce_event_id(value: uuid.UUID | str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"event_id debe ser UUID, recibido: {value!r}") from exc


def try_mark_processed(session: Session, *, event_id: uuid.UUID | str, consumer: str) -> bool:
    """Registro idempotente de consumo (`processed_events`).

    `True` si es el primer consumo; `False` si ya estaba registrado (el
    segundo consumo se ignora). Usa savepoint para no revertir el trabajo
    pendiente de la sesion ante el conflicto de PK. Hace `flush`, no
    `commit`.
    """
    if not consumer or not str(consumer).strip():
        raise ValueError("consumer es obligatorio")
    if len(str(consumer)) > 60:
        raise ValueError("consumer supera 60 caracteres")
    row = ProcessedEvent(event_id=_coerce_event_id(event_id), consumer=str(consumer))
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return False
    return True


def is_processed(session: Session, *, event_id: uuid.UUID | str, consumer: str) -> bool:
    """Indica si `consumer` ya proceso `event_id`."""
    stmt = sa.select(sa.literal(1)).where(
        ProcessedEvent.event_id == _coerce_event_id(event_id),
        ProcessedEvent.consumer == str(consumer),
    )
    return session.scalar(stmt) is not None
