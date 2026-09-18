"""Fachada `record(...)` de auditoria (E1-T04, `docs/modules/README.md#audit`).

Unico punto de escritura de `audit_log` (regla de oro 4: los demas modulos
no tocan la tabla, llaman aqui). `flush` sin `commit`: quien llama decide
la transaccion (atomicidad con la operacion de negocio en la misma sesion).
Append-only (regla de oro 2): solo inserta; no expone `update`/`delete`.

Hash encadenado (patron como `ledger.domain.entries.compute_entry_hash`,
implementacion propia sin importar `ledger`): SHA-256 hex de los campos
canonicos unidos por `"\\n"`; `prev_hash` es el `hash` del registro
anterior (`None` en el genesis).
"""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.audit import repository as audit_repo
from app.modules.audit.models import ACTOR_TYPES, AuditLog


def _coerce_uuid(value: uuid.UUID | str | None, field: str) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def _require_text(value: str, field: str, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} es obligatorio")
    text = value.strip()
    if len(text) > max_len:
        raise ValueError(f"{field} supera {max_len} caracteres")
    return text


def _canonical_json(payload: dict | None) -> str:
    if payload is not None and not isinstance(payload, dict):
        raise TypeError(f"payload debe ser dict o None, recibido: {payload!r}")
    return json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)


def compute_audit_hash(
    *,
    actor_type: str,
    actor_id: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    after_json: dict | None,
    prev_hash: str | None,
) -> str:
    """Hash SHA-256 hex (64 chars) del registro de auditoria (puro, sin BD).

    Formato canonico: `actor_type`, `actor_id`, `action`, `entity_type`,
    `entity_id`, `after_json` canonico (claves ordenadas), `prev_hash`
    (genesis: `prev_hash None` -> campo vacio), unidos por `"\\n"`.
    """
    if actor_type not in ACTOR_TYPES:
        raise ValueError(f"actor_type debe ser uno de {ACTOR_TYPES}, recibido: {actor_type!r}")
    if prev_hash is not None and (not isinstance(prev_hash, str) or len(prev_hash) != 64):
        raise ValueError(f"prev_hash debe ser hex de 64 chars, recibido: {prev_hash!r}")
    canonical = "\n".join(
        [
            actor_type,
            str(actor_id) if actor_id is not None else "",
            action,
            entity_type,
            str(entity_id) if entity_id is not None else "",
            _canonical_json(after_json),
            prev_hash or "",
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_chain(entries: list[AuditLog]) -> bool:
    """Verifica la cadena: cada `hash` referencia al anterior (puro, sin BD)."""
    ordered = sorted(entries, key=lambda row: row.seq)
    expected_prev: str | None = None
    for row in ordered:
        if row.prev_hash != expected_prev:
            return False
        if (
            row.hash
            != compute_audit_hash(
                actor_type=row.actor_type,
                actor_id=row.actor_id,
                action=row.action,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                after_json=row.after_json,
                prev_hash=row.prev_hash,
            )
        ) or len(row.hash) != 64:
            return False
        expected_prev = row.hash
    return True


def record(
    session: Session,
    *,
    actor: uuid.UUID | str | None,
    action: str,
    entity: str,
    entity_id: uuid.UUID | str | None,
    metadata: dict | None = None,
    actor_type: str | None = None,
    before: dict | None = None,
    request_id: str | None = None,
    device_id: str | None = None,
    ip: str | None = None,
) -> AuditLog:
    """Registra un evento en `audit_log` (`flush`, sin `commit`).

    `actor`/`entity_id` aceptan UUID o texto UUID (`None` permitido segun
    `03b#14.1`); `metadata` se guarda como `after_json` (solo resumen de
    resultado, sin PII ni material biometrico). `actor_type` por defecto:
    `USER` si hay `actor`, `SYSTEM` si no.

    Concurrencia (`uq_audit_log_seq`): `seq` se asigna como `max(seq)+1` sin
    bloqueo, asi que dos transacciones concurrentes pueden colisionar en el
    UQ. Mitigacion portable (sin cambio de schema/migracion): hasta 3
    intentos; ante `IntegrityError` por UQ se revierte al savepoint
    (`Session.begin_nested()`, la transaccion externa queda intacta), se
    recomputa `max(seq)+1` (+ `prev_hash`) y se reintenta el `flush`. Si se
    agotan los intentos, se relanza. Mejora futura: secuencia de BD
    (`BIGSERIAL`/`SEQUENCE` con `nextval`) en lugar de `max+1`.
    """
    actor_id = _coerce_uuid(actor, "actor")
    resolved_actor_type = actor_type or ("USER" if actor_id is not None else "SYSTEM")
    if resolved_actor_type not in ACTOR_TYPES:
        raise ValueError(f"actor_type debe ser uno de {ACTOR_TYPES}, recibido: {actor_type!r}")
    action_text = _require_text(action, "action", 60)
    entity_text = _require_text(entity, "entity", 60)
    target_id = _coerce_uuid(entity_id, "entity_id")
    if metadata is not None and not isinstance(metadata, dict):
        raise TypeError(f"metadata debe ser dict o None, recibido: {metadata!r}")
    if before is not None and not isinstance(before, dict):
        raise TypeError(f"before debe ser dict o None, recibido: {before!r}")
    if request_id is not None and len(request_id) > 60:
        raise ValueError("request_id supera 60 caracteres")
    if device_id is not None and len(device_id) > 128:
        raise ValueError("device_id supera 128 caracteres")
    if ip is not None and len(ip) > 45:
        raise ValueError("ip supera 45 caracteres")

    last_exc: Exception | None = None
    for _ in range(3):
        prev = audit_repo.latest_hash(session)
        candidate_hash = compute_audit_hash(
            actor_type=resolved_actor_type,
            actor_id=actor_id,
            action=action_text,
            entity_type=entity_text,
            entity_id=target_id,
            after_json=metadata,
            prev_hash=prev,
        )
        try:
            with session.begin_nested():
                entry = AuditLog(
                    seq=audit_repo.latest_seq(session) + 1,
                    actor_type=resolved_actor_type,
                    actor_id=actor_id,
                    action=action_text,
                    entity_type=entity_text,
                    entity_id=target_id,
                    before_json=before,
                    after_json=metadata,
                    ip=ip,
                    device_id=device_id,
                    request_id=request_id,
                    prev_hash=prev,
                    hash=candidate_hash,
                )
                session.add(entry)
                session.flush()
            return entry
        except IntegrityError as exc:
            # Solo se reintenta la colision del UQ de secuencia; cualquier
            # otro IntegrityError se relanza de inmediato.
            message = str(getattr(exc, "orig", exc)).lower()
            driver_msg = str(exc).lower()
            if (
                "uq_audit_log_seq" not in message
                and "uq_audit_log_seq" not in driver_msg
                and "seq" not in message
                and "seq" not in driver_msg
            ):
                raise
            last_exc = exc
            continue
    assert last_exc is not None
    raise last_exc


__all__ = [
    "compute_audit_hash",
    "record",
    "verify_chain",
]
