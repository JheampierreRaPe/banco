"""Repositorio del catalogo contable (`ledger`, E5-T09).

Capa de datos sin endpoints: solo SQLAlchemy sobre el schema propio.
No accede a tablas de otros modulos (`owner_ref` es UUID logico).
No publica eventos (regla de oro 8).

Convencion: las funciones hacen `flush` y no `commit`; quien llama decide
la transaccion (permite rollback en pruebas y atomicidad futura con `outbox`).
Idempotencia por `code` unico: `ensure_*` re-lee ante `IntegrityError`
usando `SAVEPOINT` (`begin_nested`) para no ensuciar la transaccion.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.ledger.domain.chart import (
    SYSTEM_CATALOG,
    CatalogEntry,
    coerce_account_ref,
    customer_codes,
    validate_currency,
)
from app.modules.ledger.models import LedgerAccount
from app.modules.ledger.repository.balances import (
    DEFAULT_ACCOUNT_PROJECTION as DEFAULT_ACCOUNT_PROJECTION,
)
from app.modules.ledger.repository.balances import (  # E5-T12 (HU18 CA-04)
    AccountProjectionPort as AccountProjectionPort,
)
from app.modules.ledger.repository.balances import (
    DefaultAccountProjection as DefaultAccountProjection,
)
from app.modules.ledger.repository.balances import (
    VersionConflictError as VersionConflictError,
)
from app.modules.ledger.repository.balances import (
    apply_balance_delta as apply_balance_delta,
)
from app.modules.ledger.repository.balances import (
    check_projection_consistency as check_projection_consistency,
)
from app.modules.ledger.repository.balances import (
    get_balance as get_balance,
)
from app.modules.ledger.repository.balances import (
    post_entry_and_update_balances as post_entry_and_update_balances,
)
from app.modules.ledger.repository.balances import (
    reverse_entry_and_update_balances as reverse_entry_and_update_balances,
)
from app.modules.ledger.repository.balances import (
    signed_delta as signed_delta,
)
from app.modules.ledger.repository.entries import (
    get_entry as get_entry,
)
from app.modules.ledger.repository.entries import (
    list_postings as list_postings,
)
from app.modules.ledger.repository.entries import (
    post_entry as post_entry,
)
from app.modules.ledger.repository.entries import (
    reverse_entry as reverse_entry,
)


def get_by_code(session: Session, code: str) -> LedgerAccount | None:
    stmt = sa.select(LedgerAccount).where(LedgerAccount.code == code)
    return session.scalars(stmt).first()


def get_by_owner(session: Session, owner_ref: uuid.UUID | str) -> list[LedgerAccount]:
    ref = coerce_account_ref(owner_ref)
    stmt = sa.select(LedgerAccount).where(LedgerAccount.owner_ref == ref)
    return list(session.scalars(stmt).all())


def _insert_account(
    session: Session,
    *,
    code: str,
    name: str,
    type: str,
    currency: str,
    owner_type: str,
    owner_ref: uuid.UUID | None,
    parent_account_id: uuid.UUID | None,
    is_system: bool,
) -> LedgerAccount:
    account = LedgerAccount(
        code=code,
        name=name,
        type=type,
        currency=currency,
        owner_type=owner_type,
        owner_ref=owner_ref,
        parent_account_id=parent_account_id,
        is_system=is_system,
    )
    session.add(account)
    session.flush()
    return account


def _get_or_create(session: Session, **kwargs) -> LedgerAccount:
    """Inserta con `SAVEPOINT`; ante carrera concurrente re-lee por `code`."""
    code: str = kwargs["code"]
    existing = get_by_code(session, code)
    if existing is not None:
        return existing
    try:
        with session.begin_nested():
            return _insert_account(session, **kwargs)
    except IntegrityError:
        existing = get_by_code(session, code)
        if existing is None:  # pragma: no cover - defensivo
            raise
        return existing


def ensure_system_catalog(
    session: Session,
    catalog: tuple[CatalogEntry, ...] = SYSTEM_CATALOG,
) -> dict[str, LedgerAccount]:
    """Crea el catalogo semilla si falta. Idempotente por `code`."""
    result: dict[str, LedgerAccount] = {}
    for entry in catalog:
        result[entry.code] = _get_or_create(
            session,
            code=entry.code,
            name=entry.name,
            type=entry.type,
            currency=entry.currency,
            owner_type=entry.owner_type,
            owner_ref=None,
            parent_account_id=None,
            is_system=entry.is_system,
        )
    session.flush()
    return result


def ensure_customer_accounts(
    session: Session,
    account_ref: uuid.UUID | str,
    currency: str,
) -> tuple[LedgerAccount, LedgerAccount]:
    """Crea (o retorna) las subcuentas `2000-<cuenta>` y `2100-<cuenta>`.

    Idempotente: llamar dos veces con el mismo `account_ref` retorna las
    mismas filas sin duplicar (`code` unico). Durante un hold el motor mueve
    valor de `2000` a `2100`; el total del cliente no cambia (`04#2.1`).
    """
    ref = coerce_account_ref(account_ref)
    cur = validate_currency(currency)
    available_code, hold_code = customer_codes(ref)

    # Asegura padres del catalogo para jerarquia (si existen).
    catalog = ensure_system_catalog(session)
    parent_available = catalog.get("2000")
    parent_hold = catalog.get("2100")

    available = _get_or_create(
        session,
        code=available_code,
        name=f"Cuenta {ref} - disponible",
        type="liability",
        currency=cur,
        owner_type="CUSTOMER",
        owner_ref=ref,
        parent_account_id=parent_available.id if parent_available else None,
        is_system=False,
    )
    hold = _get_or_create(
        session,
        code=hold_code,
        name=f"Cuenta {ref} - retenido",
        type="liability",
        currency=cur,
        owner_type="CUSTOMER",
        owner_ref=ref,
        parent_account_id=parent_hold.id if parent_hold else None,
        is_system=False,
    )
    session.flush()
    return (available, hold)
