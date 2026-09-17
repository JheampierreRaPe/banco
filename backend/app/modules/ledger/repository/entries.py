"""Asientos de partida doble (`ledger`, E5-T10 + E5-T11).

Capa de datos sin endpoints: solo SQLAlchemy sobre el schema propio.
`postings` es append-only: solo se crean filas; no se exponen funciones
de modificacion ni de borrado de postings. El reverso marca el asiento
original `POSTED` -> `REVERSED` y crea un asiento compensatorio nuevo.
No publica eventos (regla de oro 8: la emision de `ledger.entry.posted`
queda para E5-T05).

Convencion (E5-T02/E5-T09): las funciones hacen `flush` y no `commit`;
quien llama decide la transaccion. La validacion de dominio ocurre antes
de agregar filas, asi que un asiento descuadrado se rechaza con
`ValueError` sin dejar filas residuales.

Cuadre (E5-T11): la unica ruta de creacion es `post_entry` (a la que
`reverse_entry` delega) y llama siempre a `validate_balanced` via
`validate_postings`, sin flags ni bypass.
"""

from __future__ import annotations

import uuid
from datetime import date

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.ledger.domain.entries import (
    REVERSAL_ENTRY_TYPE,
    PostingInput,
    build_reversal_inputs,
    compute_entry_hash,
    validate_entry_type,
    validate_postings,
)
from app.modules.ledger.models import JournalEntry, LedgerAccount, Posting


def _coerce_uuid(value: uuid.UUID | str | None, field: str) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def _latest_hash(session: Session) -> str | None:
    """Hash del asiento mas reciente (`None` si es el genesis).

    Punta determinista de la cadena: el asiento cuyo `hash` ningun otro
    referencia como `prev_hash`. No se ordena solo por `created_at`
    porque SQLite trunca a milisegundos y dos inserts rapidos empatan
    (el desempate por `id` aleatorio rompia la cadena de forma
    intermitente). En una cadena lineal hay exactamente una punta.
    """
    referenced = sa.select(JournalEntry.prev_hash).where(
        JournalEntry.prev_hash.is_not(None)
    )
    stmt = (
        sa.select(JournalEntry.hash)
        .where(JournalEntry.hash.not_in(referenced))
        .order_by(JournalEntry.created_at.desc(), JournalEntry.id.desc())
        .limit(1)
    )
    return session.scalar(stmt)


def _require_accounts(session: Session, items: list[PostingInput]) -> None:
    wanted = {p.ledger_account_id for p in items}
    found = set(
        session.scalars(
            sa.select(LedgerAccount.id).where(LedgerAccount.id.in_(wanted))
        ).all()
    )
    missing = wanted - found
    if missing:
        raise ValueError(f"ledger_account_id inexistentes: {sorted(map(str, missing))}")


def post_entry(
    session: Session,
    *,
    entry_type: str,
    postings: list[PostingInput | dict],
    transaction_id: uuid.UUID | str | None = None,
    description: str | None = None,
    value_date: date | None = None,
    reverses_entry_id: uuid.UUID | str | None = None,
) -> JournalEntry:
    """Crea un asiento cuadrado con sus postings y lo encadena al anterior.

    Lanza `ValueError` si el asiento descuadra, si una cuenta no existe o si
    los tipos son invalidos; en ese caso no agrega ninguna fila.
    """
    entry_type = validate_entry_type(entry_type)
    items = validate_postings(postings)
    _require_accounts(session, items)
    tx_id = _coerce_uuid(transaction_id, "transaction_id")
    reverses_id = _coerce_uuid(reverses_entry_id, "reverses_entry_id")
    day = value_date if value_date is not None else date.today()
    if not isinstance(day, date):
        raise ValueError(f"value_date debe ser DATE, recibido: {value_date!r}")

    prev_hash = _latest_hash(session)
    entry_hash = compute_entry_hash(
        entry_type=entry_type,
        description=description,
        value_date=day,
        transaction_id=tx_id,
        postings=items,
        prev_hash=prev_hash,
    )
    entry = JournalEntry(
        id=uuid.uuid4(),
        transaction_id=tx_id,
        entry_type=entry_type,
        description=description,
        value_date=day,
        status="POSTED",
        reverses_entry_id=reverses_id,
        prev_hash=prev_hash,
        hash=entry_hash,
    )
    session.add(entry)
    session.add_all(
        Posting(
            journal_entry_id=entry.id,
            ledger_account_id=p.ledger_account_id,
            direction=p.direction,
            amount_minor=p.amount_minor,
            currency=p.currency,
            account_ref=p.account_ref,
        )
        for p in items
    )
    session.flush()
    return entry


def reverse_entry(
    session: Session,
    entry_id: uuid.UUID | str,
    *,
    entry_type: str | None = None,
    description: str | None = None,
    value_date: date | None = None,
    transaction_id: uuid.UUID | str | None = None,
) -> JournalEntry:
    """Revierte un asiento `POSTED` con un asiento compensatorio nuevo.

    Marca el original `POSTED` -> `REVERSED` y crea el compensatorio con
    direcciones invertidas y `reverses_entry_id` al original. Los postings
    originales nunca se tocan. Lanza `ValueError` si el asiento no existe,
    ya esta revertido o no tiene postings.
    """
    original = session.get(JournalEntry, _coerce_uuid(entry_id, "entry_id"))
    if original is None:
        raise ValueError(f"journal_entry inexistente: {entry_id!r}")
    if original.status != "POSTED":
        raise ValueError(
            f"solo se revierte un asiento POSTED, estado actual: {original.status!r}"
        )
    original_items = [
        PostingInput(
            ledger_account_id=p.ledger_account_id,
            direction=p.direction,
            amount_minor=p.amount_minor,
            currency=p.currency,
            account_ref=p.account_ref,
        )
        for p in list_postings(session, original.id)
    ]
    if not original_items:
        raise ValueError(f"el asiento {original.id} no tiene postings para revertir")
    original.status = "REVERSED"
    return post_entry(
        session,
        entry_type=entry_type or original.entry_type,
        postings=build_reversal_inputs(original_items),
        transaction_id=transaction_id,
        description=description,
        value_date=value_date if value_date is not None else original.value_date,
        reverses_entry_id=original.id,
    )


def get_entry(session: Session, entry_id: uuid.UUID | str) -> JournalEntry | None:
    """Retorna un asiento por id (`None` si no existe)."""
    return session.get(JournalEntry, _coerce_uuid(entry_id, "entry_id"))


def find_hold_release(
    session: Session,
    *,
    transaction_id: uuid.UUID | str | None,
    hold_id: uuid.UUID | str,
) -> JournalEntry | None:
    """Busca el compensatorio `HOLD_RELEASE` de un hold (solo lectura).

    Guarda de idempotencia del job de vencimiento (`transactions`): asiento
    `entry_type="HOLD_RELEASE"` con ese `transaction_id` y el `hold_id` en
    la descripcion. Retorna el asiento o `None` si no existe.
    """
    stmt = sa.select(JournalEntry).where(
        JournalEntry.entry_type == "HOLD_RELEASE",
        JournalEntry.transaction_id == _coerce_uuid(transaction_id, "transaction_id"),
        JournalEntry.description.contains(str(hold_id)),
    )
    return session.scalars(stmt).first()


def list_postings(session: Session, entry_id: uuid.UUID | str) -> list[Posting]:
    """Retorna los postings de un asiento en orden de creacion."""
    stmt = (
        sa.select(Posting)
        .where(Posting.journal_entry_id == _coerce_uuid(entry_id, "entry_id"))
        .order_by(Posting.created_at, Posting.id)
    )
    return list(session.scalars(stmt).all())


__all__ = [
    "REVERSAL_ENTRY_TYPE",
    "find_hold_release",
    "get_entry",
    "list_postings",
    "post_entry",
    "reverse_entry",
]
