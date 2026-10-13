"""Listado paginado del titular (E2-T24, HU06/HU15/HU17).

Vista de SOLO lectura sobre `transactions`: pagina + filtros por fecha
(`created_at` UTC), `type` y `status`, con orden estable
(`created_at DESC, id DESC`). No mueve dinero, no crea filas, no publica
eventos.

Frontera (reglas de oro 7/9):
- Solo devuelve transacciones del `initiator_user_id` autenticado.
- Cuentas siempre enmascaradas via fachada `accounts` (`****1234`;
  interbancaria sin cuenta destino: `CCI **** (banco <code>)`); jamas
  numero completo, documento, CCI ni titular en claro.
- Sin PII/secretos en logs: solo UUIDs de usuario, conteos y paginas.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.modules.transactions import repository as tx_repository
from app.modules.transactions.models import Transaction

logger = logging.getLogger(__name__)

#: Paginacion (`docs/05#4`): default 20, maximo 100.
DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

#: Tipos validos (`docs/03b#6.1`, `transaction_type`).
TRANSACTION_TYPES = frozenset(
    {
        "OWN_TRANSFER",
        "THIRD_PARTY_TRANSFER",
        "INTERBANK_TRANSFER",
        "QR_PAYMENT",
        "SERVICE_PAYMENT",
        "TOPUP",
        "LOAN_DISBURSEMENT",
        "LOAN_INSTALLMENT",
        "FX_EXCHANGE",
        "POCKET_TRANSFER",
        "SALARY_DISTRIBUTION",
        "REVERSAL",
    }
)

#: Estados validos (`docs/03b#6.1`, `transaction_status`).
TRANSACTION_STATUSES = frozenset(
    {
        "INITIATED",
        "VALIDATED",
        "PENDING_AUTHORIZATION",
        "AUTHORIZED",
        "FUNDS_HELD",
        "POSTED",
        "SETTLED",
        "CONCILIATED",
        "REJECTED",
        "FAILED",
        "REVERSED",
    }
)


class ListingValidationError(ValueError):
    """Filtro/paginacion invalidos (-> `422 VALIDATION_ERROR`)."""


def normalize_instant(value: datetime | None, name: str) -> datetime | None:
    """Normaliza un filtro de fecha a UTC con zona (-> `422` si no es fecha)."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ListingValidationError(f"{name} debe ser fecha ISO-8601, recibido: {value!r}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def validate_listing_filters(
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    type: str | None,
    status: str | None,
    page: int,
    page_size: int,
) -> tuple[datetime | None, datetime | None, str | None, str | None, int, int]:
    """Valida filtros + paginacion (puro, sin BD). Lanza `ListingValidationError`."""
    norm_from = normalize_instant(date_from, "date_from")
    norm_to = normalize_instant(date_to, "date_to")
    if norm_from is not None and norm_to is not None and norm_from > norm_to:
        raise ListingValidationError("date_from no puede ser posterior a date_to")
    if type is not None and type not in TRANSACTION_TYPES:
        raise ListingValidationError(f"type desconocido: {type!r}")
    if status is not None and status not in TRANSACTION_STATUSES:
        raise ListingValidationError(f"status desconocido: {status!r}")
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ListingValidationError(f"page debe ser entero >= 1, recibido: {page!r}")
    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or page_size < 1
        or page_size > MAX_PAGE_SIZE
    ):
        raise ListingValidationError(
            f"page_size debe ser entero entre 1 y {MAX_PAGE_SIZE}, recibido: {page_size!r}"
        )
    return norm_from, norm_to, type, status, page, page_size


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    stamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _masked_account(session: Session, account_id: uuid.UUID | None) -> str | None:
    """Cuenta enmascarada via fachada `accounts` (jamas en claro)."""
    if account_id is None:
        return None
    from app.modules.accounts.service import get_account as _get_account_info
    from app.modules.accounts.service import mask_account_number as _mask

    try:
        info = _get_account_info(session, account_id)
    except (ValueError, AttributeError, TypeError):
        return "****"
    if info is None:
        return "****"
    try:
        return _mask(str(info.get("account_number") or ""))
    except (ValueError, AttributeError, TypeError):
        return "****"


def _masked_target(session: Session, tx: Transaction) -> str | None:
    """Destino enmascarado (interbancaria: `CCI ****`, nunca el CCI en claro)."""
    if tx.target_account_id is not None:
        return _masked_account(session, tx.target_account_id)
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    interbank = meta.get("interbank") if isinstance(meta.get("interbank"), dict) else {}
    bank_code = str(interbank.get("bank_code") or "") or None
    return f"CCI **** (banco {bank_code})" if bank_code else "CCI ****"


def serialize_item(session: Session, tx: Transaction) -> dict[str, Any]:
    """Fila del listado (sin PII: solo mascaras + UUIDs + montos en centimos)."""
    return {
        "transaction_id": str(tx.id),
        "type": tx.type,
        "status": tx.status,
        "amount_minor": int(tx.amount_minor),
        "fee_minor": int(tx.fee_minor or 0),
        "currency": tx.currency,
        "created_at": _iso_utc(tx.created_at),
        "settled_at": _iso_utc(tx.settled_at),
        "source_account_masked": _masked_account(session, tx.source_account_id),
        "target_account_masked": _masked_target(session, tx),
        "external_ref": tx.external_ref,
    }


def list_transactions(
    session: Session,
    user_id: uuid.UUID,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    type: str | None = None,
    status: str | None = None,
    page: int = DEFAULT_PAGE,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> tuple[list[dict[str, Any]], int]:
    """Caso de uso: pagina del titular + total (solo lectura, sin N+1).

    Dos consultas acotadas (`count` + pagina con `limit <= 100`, orden
    estable); el enmascarado resuelve cuentas por fachada solo para las
    filas de la pagina. Lanza `ListingValidationError` (-> 422).
    """
    norm_from, norm_to, norm_type, norm_status, norm_page, norm_size = validate_listing_filters(
        date_from=date_from,
        date_to=date_to,
        type=type,
        status=status,
        page=page,
        page_size=page_size,
    )
    total = tx_repository.count_user_transactions(
        session,
        user_id,
        date_from=norm_from,
        date_to=norm_to,
        type=norm_type,
        status=norm_status,
    )
    offset = (norm_page - 1) * norm_size
    rows = tx_repository.list_user_transactions(
        session,
        user_id,
        date_from=norm_from,
        date_to=norm_to,
        type=norm_type,
        status=norm_status,
        limit=norm_size,
        offset=offset,
    )
    items = [serialize_item(session, tx) for tx in rows]
    logger.info(
        "transactions list user reads page=%s size=%s total=%s", norm_page, norm_size, total
    )
    return items, total


__all__ = [
    "DEFAULT_PAGE",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "TRANSACTION_STATUSES",
    "TRANSACTION_TYPES",
    "ListingValidationError",
    "list_transactions",
    "serialize_item",
    "validate_listing_filters",
]
