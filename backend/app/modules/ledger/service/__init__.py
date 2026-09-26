"""Fachada interna del `ledger` (E5-T09 + E5-T10 + E5-T14).

Contrato (`docs/modules/README.md#ledger`): los asientos solo entran por la
fachada; otros modulos no tocan `ledger_accounts`, `journal_entries` ni
`postings` directamente. No publica eventos (regla de oro 8).

Verificacion (E5-T14, HU18 CA-02): `verify_chain` recorre la cadena de
hashes de `journal_entries` y detecta alteracion/reorden/borrado. Solo
lectura: jamas modifica registros (regla de oro 2). Sin PII ni secretos
en los detalles (solo ids tecnicos y codigos de motivo).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.ledger.domain.entries import PostingInput, compute_entry_hash
from app.modules.ledger.domain.entries import normalize_posting as _normalize_posting
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


def _to_posting_inputs(
    raw: Sequence[Posting | PostingInput | dict] | None,
) -> list[PostingInput] | None:
    """Convierte postings ORM/dict a `PostingInput` puros para el hash."""
    if raw is None:
        return None
    items: list[PostingInput] = []
    for p in raw:
        if isinstance(p, PostingInput):
            items.append(p)
        elif isinstance(p, Posting):
            items.append(
                PostingInput(
                    ledger_account_id=p.ledger_account_id,
                    direction=p.direction,
                    amount_minor=p.amount_minor,
                    currency=p.currency,
                    account_ref=p.account_ref,
                )
            )
        elif isinstance(p, dict):
            items.append(_normalize_posting(p))
        else:  # pragma: no cover - defensivo
            raise TypeError(f"posting no soportado: {type(p).__name__}")
    return items


def _resolve_inputs(
    entry: JournalEntry,
    postings_by_entry: Mapping[Any, Sequence[Posting | PostingInput | dict]] | None,
) -> list[PostingInput] | None:
    """Postings del asiento desde el mapa o de la relacion cargada."""
    if postings_by_entry is not None and entry.id in postings_by_entry:
        return _to_posting_inputs(postings_by_entry[entry.id])
    related = getattr(entry, "postings", None)
    if related is not None:
        try:
            return _to_posting_inputs(list(related))
        except (TypeError, AttributeError):  # relacion no cargada/sesion cerrada
            return None
    return None


def verify_chain_detailed(
    entries: Sequence[JournalEntry],
    postings_by_entry: Mapping[Any, Sequence[Posting | PostingInput | dict]] | None = None,
) -> dict[str, Any]:
    """Verifica la cadena de hashes (E5-T14, HU18 CA-02; pura, sin BD).

    Recorre por enlaces `prev_hash` (no por `created_at`, que puede empatar
    en milisegundos): exige un unico genesis (`prev_hash None`), recomputa
    cada `hash` con `compute_entry_hash` (contenido + postings + `prev_hash`)
    y exige visitar TODAS las entradas (detecta borrado/huerfanos) sin
    bifurcaciones (detecta reorden/fork).

    Retorna `{"valid", "reason", "failed_entry_id", "checked"}` sin PII
    (solo ids tecnicos). `reason` es codigo para alerta critica.
    """
    rows = list(entries or [])
    if not rows:
        return {"valid": True, "reason": None, "failed_entry_id": None, "checked": 0}

    by_hash: dict[str | None, list[JournalEntry]] = {}
    for row in rows:
        by_hash.setdefault(row.hash, []).append(row)
    for dupes in by_hash.values():
        if len(dupes) > 1:
            return {
                "valid": False,
                "reason": "duplicate_hash",
                "failed_entry_id": str(dupes[1].id),
                "checked": 0,
            }

    genesis = [row for row in rows if row.prev_hash is None]
    if len(genesis) == 0:
        return {
            "valid": False,
            "reason": "missing_genesis",
            "failed_entry_id": None,
            "checked": 0,
        }
    if len(genesis) > 1:
        return {
            "valid": False,
            "reason": "multiple_genesis",
            "failed_entry_id": str(genesis[1].id),
            "checked": 0,
        }

    children: dict[str, list[JournalEntry]] = {}
    for row in rows:
        if row.prev_hash is not None:
            children.setdefault(row.prev_hash, []).append(row)

    current = genesis[0]
    expected_prev: str | None = None
    visited: set[Any] = set()
    checked = 0
    while current is not None:
        if current.id in visited:
            return {
                "valid": False,
                "reason": "cycle",
                "failed_entry_id": str(current.id),
                "checked": checked,
            }
        if current.prev_hash != expected_prev:
            return {
                "valid": False,
                "reason": "link_break",
                "failed_entry_id": str(current.id),
                "checked": checked,
            }
        inputs = _resolve_inputs(current, postings_by_entry)
        if inputs is None:
            return {
                "valid": False,
                "reason": "missing_postings",
                "failed_entry_id": str(current.id),
                "checked": checked,
            }
        try:
            expected_hash = compute_entry_hash(
                entry_type=current.entry_type,
                description=current.description,
                value_date=current.value_date,
                transaction_id=current.transaction_id,
                postings=inputs,
                prev_hash=current.prev_hash,
            )
        except (ValueError, TypeError):
            return {
                "valid": False,
                "reason": "hash_mismatch",
                "failed_entry_id": str(current.id),
                "checked": checked,
            }
        if current.hash != expected_hash or len(current.hash) != 64:
            return {
                "valid": False,
                "reason": "hash_mismatch",
                "failed_entry_id": str(current.id),
                "checked": checked,
            }
        visited.add(current.id)
        checked += 1
        expected_prev = current.hash
        nxt = children.get(current.hash, [])
        if len(nxt) > 1:
            return {
                "valid": False,
                "reason": "fork",
                "failed_entry_id": str(nxt[1].id),
                "checked": checked,
            }
        current = nxt[0] if nxt else None

    if len(visited) != len(rows):
        orphan = next((row for row in rows if row.id not in visited), None)
        return {
            "valid": False,
            "reason": "orphan_entries",
            "failed_entry_id": str(orphan.id) if orphan is not None else None,
            "checked": checked,
        }
    return {"valid": True, "reason": None, "failed_entry_id": None, "checked": checked}


def verify_chain(
    entries: Sequence[JournalEntry],
    postings_by_entry: Mapping[Any, Sequence[Posting | PostingInput | dict]] | None = None,
) -> bool:
    """Verifica la cadena (`True` si integra; pura, sin BD; espejo de `audit`)."""
    return bool(verify_chain_detailed(entries, postings_by_entry)["valid"])


def verify_ledger_chain_detailed(session: Session) -> dict[str, Any]:
    """Verifica TODA la cadena persistida (solo lectura; alerta critica si invalida)."""
    rows = list(session.scalars(sa.select(JournalEntry)).all())
    postings_map: dict[Any, list[Posting]] = {}
    for row in rows:
        postings_map[row.id] = _list_postings(session, row.id)
    return verify_chain_detailed(rows, postings_map)


def verify_ledger_chain(session: Session) -> bool:
    """Verifica TODA la cadena persistida (`True` si integra; solo lectura)."""
    return bool(verify_ledger_chain_detailed(session)["valid"])


__all__ = [
    "ensure_customer_accounts",
    "find_hold_release",
    "get_entry",
    "list_postings",
    "post_entry",
    "reverse_entry",
    "verify_chain",
    "verify_chain_detailed",
    "verify_ledger_chain",
    "verify_ledger_chain_detailed",
]
