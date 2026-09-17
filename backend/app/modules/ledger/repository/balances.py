"""Proyeccion atomica de saldos `ledger_balances` (E5-T12, HU18 CA-04).

Fuente de verdad: `postings` (`docs/03c-modelo-er.md#14`). Esta proyeccion
se actualiza en la misma sesion/transaccion que el asiento, con optimistic
locking (`version`: se lee, se verifica `expected_version` y se incrementa).

Convencion: `flush` sin `commit`; quien llama decide la transaccion.
Dinero entero en centimos (firmado: DEBIT suma, CREDIT resta); nunca `float`.
No publica eventos (regla de oro 8: la emision de `ledger.entry.posted`
queda para E5-T05).

Seam con `accounts` (E2-T01 pendiente): este modulo NO toca tablas de
`accounts`. Expone el puerto `AccountProjectionPort` con implementacion por
defecto que lanza `NotImplementedError("E2-T01 pendiente")`. La funcion
`post_entry_and_update_balances` lo recibe por parametro (por defecto el
stub, que se omite silenciosamente hasta que E2-T01 aporte el adaptador
real); los tests inyectan un fake.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.ledger.domain.entries import PostingInput
from app.modules.ledger.models import JournalEntry, LedgerBalance, Posting
from app.modules.ledger.repository.entries import list_postings, post_entry, reverse_entry


class VersionConflictError(ValueError):
    """Conflicto de version concurrente en `ledger_balances`."""


class AccountProjectionPort(Protocol):
    """Puerto hacia la proyeccion de saldos de `accounts` (E2-T01)."""

    def apply_projection(
        self,
        session: Session,
        ledger_account_id: uuid.UUID,
        signed_delta_minor: int,
        currency: str,
    ) -> None:
        ...


class DefaultAccountProjection:
    """Stub por defecto: E2-T01 pendiente (esa tabla aun no existe)."""

    def apply_projection(
        self,
        session: Session,
        ledger_account_id: uuid.UUID,
        signed_delta_minor: int,
        currency: str,
    ) -> None:
        raise NotImplementedError("E2-T01 pendiente")


DEFAULT_ACCOUNT_PROJECTION = DefaultAccountProjection()


def signed_delta(direction: str, amount_minor: int) -> int:
    """Delta firmado: DEBIT suma, CREDIT resta. Enteros, sin `float`."""
    if direction == "DEBIT":
        return int(amount_minor)
    if direction == "CREDIT":
        return -int(amount_minor)
    raise ValueError(f"direction debe ser DEBIT/CREDIT, recibido: {direction!r}")


def get_balance(
    session: Session, ledger_account_id: uuid.UUID | str
) -> LedgerBalance | None:
    """Lee la proyeccion de una cuenta (`None` si aun no existe)."""
    return session.get(LedgerBalance, ledger_account_id)


def apply_balance_delta(
    session: Session,
    ledger_account_id: uuid.UUID,
    signed_delta_minor: int,
    currency: str,
    *,
    expected_version: int | None = None,
) -> LedgerBalance:
    """Aplica un delta firmado con optimistic locking (flush, sin commit).

    - Fila inexistente: la crea con `balance = delta`, `version = 0`.
    - Fila existente: verifica `expected_version` (si se da), suma el delta
      e incrementa `version` en 1. Moneda debe coincidir.
    - Lanza `VersionConflictError` si la version no coincide.
    """
    if not isinstance(signed_delta_minor, int) or isinstance(signed_delta_minor, bool):
        raise ValueError("signed_delta_minor debe ser int (centimos), sin float")
    row = session.get(LedgerBalance, ledger_account_id)
    if row is None:
        row = LedgerBalance(
            ledger_account_id=ledger_account_id,
            currency=currency,
            balance_minor=signed_delta_minor,
            version=0,
        )
        session.add(row)
        session.flush()
        return row
    if row.currency != currency:
        raise ValueError(
            f"moneda de la proyeccion {row.currency!r} != {currency!r} "
            f"para {ledger_account_id}"
        )
    if expected_version is not None and row.version != expected_version:
        raise VersionConflictError(
            f"conflicto de version en {ledger_account_id}: "
            f"esperada {expected_version}, actual {row.version}"
        )
    row.balance_minor = int(row.balance_minor) + int(signed_delta_minor)
    row.version = int(row.version) + 1
    session.flush()
    return row


def post_entry_and_update_balances(
    session: Session,
    *,
    entry_type: str,
    postings: list[PostingInput | dict],
    transaction_id: uuid.UUID | str | None = None,
    description: str | None = None,
    value_date: date | None = None,
    account_projection: AccountProjectionPort | None = None,
) -> JournalEntry:
    """Crea el asiento (reutiliza `post_entry`) y actualiza `ledger_balances`.

    Todo en la misma sesion/transaccion: si la proyeccion falla, el
    llamador hace `rollback` y el asiento tambien se revierte. No publica
    eventos. El puerto `account_projection` (seam E2-T01) se invoca por
    posting; el stub por defecto lanza `NotImplementedError`, que aqui se
    omite hasta que exista el adaptador real.
    """
    entry = post_entry(
        session,
        entry_type=entry_type,
        postings=postings,
        transaction_id=transaction_id,
        description=description,
        value_date=value_date,
    )
    port = account_projection if account_projection is not None else DEFAULT_ACCOUNT_PROJECTION
    rows = list_postings(session, entry.id)
    for p in rows:
        delta = signed_delta(p.direction, p.amount_minor)
        apply_balance_delta(session, p.ledger_account_id, delta, p.currency)
        try:
            port.apply_projection(session, p.ledger_account_id, delta, p.currency)
        except NotImplementedError as exc:
            if "E2-T01 pendiente" not in str(exc):
                raise
    session.flush()
    return entry


def reverse_entry_and_update_balances(
    session: Session,
    entry_id: uuid.UUID | str,
    *,
    entry_type: str | None = None,
    description: str | None = None,
    value_date: date | None = None,
    transaction_id: uuid.UUID | str | None = None,
    account_projection: AccountProjectionPort | None = None,
) -> JournalEntry:
    """Revierte un asiento y proyecta el compensatorio (misma transaccion)."""
    original = session.get(JournalEntry, entry_id)
    if original is None:
        raise ValueError(f"journal_entry inexistente: {entry_id!r}")
    comp = reverse_entry(
        session,
        entry_id,
        entry_type=entry_type,
        description=description,
        value_date=value_date,
        transaction_id=transaction_id,
    )
    port = account_projection if account_projection is not None else DEFAULT_ACCOUNT_PROJECTION
    for p in list_postings(session, comp.id):
        delta = signed_delta(p.direction, p.amount_minor)
        apply_balance_delta(session, p.ledger_account_id, delta, p.currency)
        try:
            port.apply_projection(session, p.ledger_account_id, delta, p.currency)
        except NotImplementedError as exc:
            if "E2-T01 pendiente" not in str(exc):
                raise
    session.flush()
    return comp


def check_projection_consistency(session: Session) -> list[dict]:
    """Verifica `sum(postings por cuenta) == ledger_balances` (base E5-T13).

    Retorna la lista de diferencias (vacia = consistente). Cada item:
    `{"ledger_account_id", "currency", "postings_total_minor",
    "balance_minor", "version"}`. Solo lectura: no modifica filas.
    Gana `postings` si hay diferencia (regla `03c#14`).
    """
    totals = session.execute(
        sa.select(
            Posting.ledger_account_id,
            Posting.currency,
            sa.func.coalesce(
                sa.func.sum(
                    sa.case(
                        (Posting.direction == "DEBIT", Posting.amount_minor),
                        else_=-Posting.amount_minor,
                    )
                ),
                0,
            ).label("total"),
        ).group_by(Posting.ledger_account_id, Posting.currency)
    ).all()
    diffs: list[dict] = []
    seen: set = set()
    for account_id, currency, total in totals:
        seen.add(account_id)
        row = session.get(LedgerBalance, account_id)
        current = int(row.balance_minor) if row is not None else 0
        if row is None or current != int(total) or row.currency != currency:
            diffs.append(
                {
                    "ledger_account_id": account_id,
                    "currency": currency,
                    "postings_total_minor": int(total),
                    "balance_minor": current,
                    "version": int(row.version) if row is not None else None,
                }
            )
    for row in session.scalars(sa.select(LedgerBalance)).all():
        if row.ledger_account_id not in seen and int(row.balance_minor) != 0:
            diffs.append(
                {
                    "ledger_account_id": row.ledger_account_id,
                    "currency": row.currency,
                    "postings_total_minor": 0,
                    "balance_minor": int(row.balance_minor),
                    "version": int(row.version),
                }
            )
    return diffs


__all__ = [
    "AccountProjectionPort",
    "DEFAULT_ACCOUNT_PROJECTION",
    "DefaultAccountProjection",
    "VersionConflictError",
    "apply_balance_delta",
    "check_projection_consistency",
    "get_balance",
    "post_entry_and_update_balances",
    "reverse_entry_and_update_balances",
    "signed_delta",
]
