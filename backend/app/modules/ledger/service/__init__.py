"""Fachada interna del `ledger` (E5-T09 + E5-T10).

Contrato (`docs/modules/README.md#ledger`): los asientos solo entran por la
fachada; otros modulos no tocan `ledger_accounts`, `journal_entries` ni
`postings` directamente. No publica eventos (regla de oro 8).
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.orm import Session

from app.modules.ledger.domain.entries import PostingInput
from app.modules.ledger.models import JournalEntry, LedgerAccount, Posting
from app.modules.ledger.repository import ensure_customer_accounts as _ensure
from app.modules.ledger.repository import get_entry as _get_entry
from app.modules.ledger.repository import list_postings as _list_postings
from app.modules.ledger.repository import post_entry as _post_entry
from app.modules.ledger.repository import reverse_entry as _reverse_entry
from app.modules.ledger.repository.entries import find_hold_release as _find_hold_release


def ensure_customer_accounts(
    session: Session,
    account_ref: uuid.UUID | str,
    currency: str,
) -> tuple[LedgerAccount, LedgerAccount]:
    """Fachada: subcuentas disponible/retenido de una cuenta de cliente."""
    return _ensure(session, account_ref, currency)


def post_entry(
    session: Session,
    *,
    entry_type: str,
    postings: list[PostingInput | dict],
    transaction_id: uuid.UUID | str | None = None,
    description: str | None = None,
    value_date: date | None = None,
) -> JournalEntry:
    """Fachada: crea un asiento cuadrado con hash encadenado (E5-T10)."""
    return _post_entry(
        session,
        entry_type=entry_type,
        postings=postings,
        transaction_id=transaction_id,
        description=description,
        value_date=value_date,
    )


def reverse_entry(
    session: Session,
    entry_id: uuid.UUID | str,
    *,
    entry_type: str | None = None,
    description: str | None = None,
    value_date: date | None = None,
    transaction_id: uuid.UUID | str | None = None,
) -> JournalEntry:
    """Fachada: revierte un asiento con un compensatorio nuevo (E5-T10)."""
    return _reverse_entry(
        session,
        entry_id,
        entry_type=entry_type,
        description=description,
        value_date=value_date,
        transaction_id=transaction_id,
    )


def get_entry(session: Session, entry_id: uuid.UUID | str) -> JournalEntry | None:
    """Fachada: lee un asiento por id."""
    return _get_entry(session, entry_id)


def find_hold_release(
    session: Session,
    *,
    transaction_id: uuid.UUID | str | None,
    hold_id: uuid.UUID | str,
) -> JournalEntry | None:
    """Fachada: busca el compensatorio `HOLD_RELEASE` de un hold (solo lectura)."""
    return _find_hold_release(session, transaction_id=transaction_id, hold_id=hold_id)


def list_postings(session: Session, entry_id: uuid.UUID | str) -> list[Posting]:
    """Fachada: lee los postings de un asiento."""
    return _list_postings(session, entry_id)
