"""Fachada interna del `ledger` (E5-T09).

Contrato (`docs/modules/README.md#ledger`): los asientos solo entran por la
fachada; otros modulos no tocan `ledger_accounts` directamente.
Esta tarea expone `ensure_customer_accounts(account_ref, currency)`.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.modules.ledger.models import LedgerAccount
from app.modules.ledger.repository import ensure_customer_accounts as _ensure


def ensure_customer_accounts(
    session: Session,
    account_ref: uuid.UUID | str,
    currency: str,
) -> tuple[LedgerAccount, LedgerAccount]:
    """Fachada: subcuentas disponible/retenido de una cuenta de cliente."""
    return _ensure(session, account_ref, currency)
