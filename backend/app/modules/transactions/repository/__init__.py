"""Repositorio de persistencia del motor (`transactions`, E5-T02).

Capa de datos sin endpoints: solo SQLAlchemy sobre el schema propio.
No accede a tablas de `ledger` ni `accounts` (referencias logicas por UUID).
No publica eventos (regla 8: `outbox` lo hace la capa de servicio).

Convencion: las funciones hacen `flush` y no `commit`; quien llama decide
la transaccion (permite rollback en pruebas y atomicidad con `outbox`).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.transactions.domain.state_machine import (
    ActorType,
    ConcurrencyError,
    TransactionStatus,
    transition,
    validate_amount_minor,
    validate_currency,
)
from app.modules.transactions.models import (
    Hold,
    HoldStatus,
    Transaction,
    TransactionStatusHistory,
)

# Hold: solo sale de ACTIVE; estados destino son terminales.
_HOLD_TRANSITIONS: dict[HoldStatus, frozenset[HoldStatus]] = {
    HoldStatus.ACTIVE: frozenset({HoldStatus.RELEASED, HoldStatus.CAPTURED, HoldStatus.EXPIRED}),
    HoldStatus.RELEASED: frozenset(),
    HoldStatus.CAPTURED: frozenset(),
    HoldStatus.EXPIRED: frozenset(),
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def create_transaction(
    session: Session,
    *,
    type: str,
    amount_minor: int,
    currency: str,
    status: str = TransactionStatus.INITIATED.value,
    idempotency_key: str | None = None,
    initiator_user_id: uuid.UUID | None = None,
    source_account_id: uuid.UUID | None = None,
    target_account_id: uuid.UUID | None = None,
    external_ref: str | None = None,
    fee_minor: int = 0,
    risk_level: str = "LOW",
    risk_score: int | None = None,
    metadata: dict[str, Any] | None = None,
    actor: str = ActorType.SYSTEM.value,
) -> Transaction:
    """Crea la transaccion y su historial inicial (`None -> status`)."""
    validate_amount_minor(amount_minor)
    validate_currency(currency)
    if not isinstance(fee_minor, int) or isinstance(fee_minor, bool) or fee_minor < 0:
        raise ValueError(f"fee_minor debe ser entero >= 0, recibido: {fee_minor!r}")
    initial = TransactionStatus(status)  # ValueError si el estado no existe
    tx = Transaction(
        type=type,
        status=initial.value,
        idempotency_key=idempotency_key,
        initiator_user_id=initiator_user_id,
        source_account_id=source_account_id,
        target_account_id=target_account_id,
        external_ref=external_ref,
        amount_minor=amount_minor,
        currency=currency,
        fee_minor=fee_minor,
        risk_level=risk_level,
        risk_score=risk_score,
        meta=metadata,
        version=0,
    )
    session.add(tx)
    session.flush()  # genera id para el historial
    session.add(
        TransactionStatusHistory(
            transaction_id=tx.id,
            from_status=None,
            to_status=initial.value,
            reason="created",
            actor_type=ActorType(actor).value,
            actor_id=initiator_user_id,
        )
    )
    session.flush()
    return tx


def get_transaction(session: Session, tx_id: uuid.UUID) -> Transaction | None:
    return session.get(Transaction, tx_id)


def get_by_idempotency_key(session: Session, key: str) -> Transaction | None:
    stmt = sa.select(Transaction).where(Transaction.idempotency_key == key)
    return session.scalars(stmt).first()


def transition_transaction(
    session: Session,
    tx_id: uuid.UUID,
    target: str,
    *,
    actor: str = ActorType.SYSTEM.value,
    reason: str | None = None,
    actor_id: uuid.UUID | None = None,
    expected_version: int | None = None,
) -> Transaction:
    """Avanza de estado validando con la maquina de E5-T01 + historial.

    Lanza `InvalidTransitionError`/`UnauthorizedActorError` del dominio y
    `ConcurrencyError` si `expected_version` no coincide (doble procesamiento).
    """
    tx = session.get(Transaction, tx_id)
    if tx is None:
        raise KeyError(f"transaccion no encontrada: {tx_id}")
    if expected_version is not None and tx.version != expected_version:
        raise ConcurrencyError(
            f"version obsoleta: esperada {expected_version}, actual {tx.version} "
            "(posible doble procesamiento)"
        )
    from_status = tx.status
    new_status = transition(from_status, target, actor)
    tx.status = new_status.value
    tx.version = tx.version + 1
    tx.updated_at = _utcnow()
    if new_status is TransactionStatus.SETTLED:
        tx.settled_at = _utcnow()
    session.add(
        TransactionStatusHistory(
            transaction_id=tx.id,
            from_status=from_status,
            to_status=new_status.value,
            reason=reason,
            actor_type=ActorType(actor).value,
            actor_id=actor_id,
        )
    )
    session.flush()
    return tx


def list_history(session: Session, tx_id: uuid.UUID) -> Sequence[TransactionStatusHistory]:
    stmt = (
        sa.select(TransactionStatusHistory)
        .where(TransactionStatusHistory.transaction_id == tx_id)
        .order_by(TransactionStatusHistory.created_at.asc())
    )
    return list(session.scalars(stmt).all())


def create_hold(
    session: Session,
    *,
    transaction_id: uuid.UUID,
    account_id: uuid.UUID,
    amount_minor: int,
    currency: str,
    expires_at: datetime | None = None,
) -> Hold:
    """Crea un hold en `ACTIVE` ligado a la transaccion."""
    validate_amount_minor(amount_minor)
    validate_currency(currency)
    hold = Hold(
        transaction_id=transaction_id,
        account_id=account_id,
        amount_minor=amount_minor,
        currency=currency,
        status=HoldStatus.ACTIVE.value,
        expires_at=expires_at,
    )
    session.add(hold)
    session.flush()
    return hold


def get_hold(session: Session, hold_id: uuid.UUID) -> Hold | None:
    return session.get(Hold, hold_id)


def list_holds_by_transaction(session: Session, tx_id: uuid.UUID) -> Sequence[Hold]:
    stmt = sa.select(Hold).where(Hold.transaction_id == tx_id)
    return list(session.scalars(stmt).all())


def list_active_holds(session: Session, *, account_id: uuid.UUID | None = None) -> Sequence[Hold]:
    stmt = sa.select(Hold).where(Hold.status == HoldStatus.ACTIVE.value)
    if account_id is not None:
        stmt = stmt.where(Hold.account_id == account_id)
    return list(session.scalars(stmt).all())


def update_hold_status(session: Session, hold_id: uuid.UUID, target: str) -> Hold:
    """Cambia el estado del hold; solo se sale de `ACTIVE` (terminal despues)."""
    hold = session.get(Hold, hold_id)
    if hold is None:
        raise KeyError(f"hold no encontrado: {hold_id}")
    try:
        to_status = HoldStatus(target)
    except ValueError as exc:
        raise ValueError(f"estado de hold desconocido: {target!r}") from exc
    allowed = _HOLD_TRANSITIONS[HoldStatus(hold.status)]
    if to_status not in allowed:
        raise ValueError(f"transicion de hold invalida: {hold.status} -> {to_status.value}")
    hold.status = to_status.value
    hold.released_at = _utcnow()
    session.flush()
    return hold


def expire_due_holds(session: Session, *, now: datetime | None = None) -> Sequence[Hold]:
    """Marca `EXPIRED` los holds `ACTIVE` con `expires_at <= now`."""
    moment = now or _utcnow()
    stmt = sa.select(Hold).where(
        Hold.status == HoldStatus.ACTIVE.value,
        Hold.expires_at.is_not(None),
        Hold.expires_at <= moment,
    )
    due = list(session.scalars(stmt).all())
    for hold in due:
        hold.status = HoldStatus.EXPIRED.value
        hold.released_at = moment
    if due:
        session.flush()
    return due


__all__ = [
    "create_hold",
    "create_transaction",
    "expire_due_holds",
    "get_by_idempotency_key",
    "get_hold",
    "get_transaction",
    "list_active_holds",
    "list_history",
    "list_holds_by_transaction",
    "transition_transaction",
    "update_hold_status",
]
