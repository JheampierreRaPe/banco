"""Repositorio de `wallet` (E4-T01): solo tablas propias.

Convencion: `flush` sin `commit`; el endpoint confirma. Sin PII en logs
(regla de oro 7): aqui no se loguea `token_ref` completo ni `last4`
(solo `id`s y conteos).
"""

from __future__ import annotations

import logging
import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.wallet.models import Wallet, WalletLink
from app.modules.wallet.repository import qr as qr_repository
from app.modules.wallet.repository import qr_payment as qr_payment_repository
from app.modules.wallet.repository import services as services_repository

logger = logging.getLogger(__name__)


class DuplicateLinkError(ValueError):
    """El `token_ref` (o la cuenta) ya esta vinculado (-> 409)."""


def _coerce_user(user_id: uuid.UUID | str) -> uuid.UUID:
    if isinstance(user_id, uuid.UUID):
        return user_id
    try:
        return uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"user_id debe ser UUID, recibido: {user_id!r}") from exc


def get_wallet_by_user(session: Session, user_id: uuid.UUID) -> Wallet | None:
    """Billetera del titular o `None` (solo lectura)."""
    stmt = sa.select(Wallet).where(Wallet.user_id == user_id)
    return session.scalar(stmt)


def create_wallet(session: Session, user_id: uuid.UUID) -> Wallet:
    """Crea la billetera del titular (una fila por `user_id`, UQ)."""
    row = Wallet(user_id=user_id, status="ACTIVE")
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        raise DuplicateLinkError("billetera ya existe para el titular") from exc
    logger.debug("billetera creada user_id=%s wallet_id=%s", user_id, row.id)
    return row


def list_links(session: Session, wallet_id: uuid.UUID) -> list[WalletLink]:
    """Vinculos de la billetera (todos los estados), orden estable."""
    stmt = (
        sa.select(WalletLink)
        .where(WalletLink.wallet_id == wallet_id)
        .order_by(WalletLink.linked_at, WalletLink.id)
    )
    return list(session.scalars(stmt).all())


def get_link(session: Session, link_id: uuid.UUID | str) -> WalletLink | None:
    """Vinculo por id o `None` (solo lectura)."""
    try:
        lid = link_id if isinstance(link_id, uuid.UUID) else uuid.UUID(str(link_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return session.get(WalletLink, lid)


def find_by_token(session: Session, token_ref: str) -> WalletLink | None:
    """Vinculo por `token_ref` (UQ global) o `None` (solo lectura)."""
    stmt = sa.select(WalletLink).where(WalletLink.token_ref == token_ref)
    return session.scalar(stmt)


def find_active_by_account(
    session: Session, wallet_id: uuid.UUID, account_id: uuid.UUID
) -> WalletLink | None:
    """Vinculo `ACTIVE` de la billetera para esa cuenta, o `None`."""
    stmt = sa.select(WalletLink).where(
        WalletLink.wallet_id == wallet_id,
        WalletLink.account_id == account_id,
        WalletLink.status == "ACTIVE",
    )
    return session.scalar(stmt)


def create_link(
    session: Session,
    *,
    wallet_id: uuid.UUID,
    token_ref: str,
    account_id: uuid.UUID | None = None,
    last4: str | None = None,
    brand: str | None = None,
) -> WalletLink:
    """Inserta el vinculo `ACTIVE` (el crudo nunca llega aqui)."""
    row = WalletLink(
        wallet_id=wallet_id,
        account_id=account_id,
        token_ref=token_ref,
        last4=last4,
        brand=brand,
        status="ACTIVE",
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        raise DuplicateLinkError("fuente ya vinculada") from exc
    logger.debug("vinculo creado wallet_id=%s link_id=%s", wallet_id, row.id)
    return row


def reactivate_link(session: Session, row: WalletLink) -> WalletLink:
    """Reactiva un vinculo `INACTIVE` (re-vinculacion; sin borrar filas)."""
    row.status = "ACTIVE"
    session.flush()
    logger.debug("vinculo reactivado link_id=%s", row.id)
    return row


def deactivate_link(session: Session, row: WalletLink) -> WalletLink:
    """Baja logica a `INACTIVE` (sin borrar filas; solo `flush`)."""
    row.status = "INACTIVE"
    session.flush()
    logger.debug("vinculo desactivado link_id=%s", row.id)
    return row


__all__ = [
    "DuplicateLinkError",
    "create_link",
    "create_wallet",
    "deactivate_link",
    "find_active_by_account",
    "find_by_token",
    "get_link",
    "get_wallet_by_user",
    "list_links",
    "qr_payment_repository",
    "qr_repository",
    "reactivate_link",
    "services_repository",
]
