"""Lecturas sobre `audit.audit_log` (E1-T04).

Append-only (regla de oro 2): solo lectura y helpers de cadena para la
fachada `record`. No se expone `update`/`delete` y no deben agregarse:
cualquier correccion es un registro nuevo.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def latest_seq(session: Session) -> int:
    """Maximo `seq` registrado (`0` si la bitacora esta vacia)."""
    return session.scalar(sa.select(sa.func.max(AuditLog.seq))) or 0


def latest_hash(session: Session) -> str | None:
    """`hash` del ultimo registro (`None` si la bitacora esta vacia)."""
    return session.scalar(
        sa.select(AuditLog.hash).order_by(AuditLog.seq.desc()).limit(1)
    )


def get(session: Session, log_id: uuid.UUID | str) -> AuditLog | None:
    """Lee un registro por id (`None` si no existe)."""
    return session.get(AuditLog, _coerce_uuid(log_id, "log_id"))


def list_by_entity(
    session: Session, entity_type: str, entity_id: uuid.UUID | str
) -> list[AuditLog]:
    """Registros de una entidad en orden de cadena (`seq` ascendente)."""
    stmt = (
        sa.select(AuditLog)
        .where(
            AuditLog.entity_type == entity_type,
            AuditLog.entity_id == _coerce_uuid(entity_id, "entity_id"),
        )
        .order_by(AuditLog.seq.asc())
    )
    return list(session.scalars(stmt).all())


__all__ = [
    "get",
    "latest_hash",
    "latest_seq",
    "list_by_entity",
]
