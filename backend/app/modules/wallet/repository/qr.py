"""Repositorio QR de `wallet` (E4-T06, HU14): solo tablas propias.

`merchant_accounts` + `qr_charges` (`docs/03b#92`/`#93`). Convencion:
`flush` sin `commit`; el servicio confirma. Sin PII en logs (regla 7):
aqui no se loguea RUC, `tax_id_hash` ni firmas (solo `id`s y conteos).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.wallet.models import MerchantAccount, QrCharge

logger = logging.getLogger(__name__)

#: Comercios demo sembrados por la migracion `0039_wallet_qr` (G69,
#: decision del dueno opcion (a)): `(merchant_id, user_id,
#: business_name)`. Los `tax_id_hash` viven solo en la migracion (RUC
#: ficticios hasheados, nunca en claro).
DEMO_MERCHANTS: tuple[tuple[str, str, str], ...] = (
    (
        "a1111111-1111-4111-8111-111111111111",
        "b1111111-1111-4111-8111-111111111111",
        "Bodega Demo San Juan",
    ),
    (
        "a2222222-2222-4222-8222-222222222222",
        "b2222222-2222-4222-8222-222222222222",
        "Cafe Demo Miraflores",
    ),
)


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


class DuplicateMerchantError(ValueError):
    """El `user_id` ya tiene cuenta de comercio (-> 409 si se expone)."""


# ------------------------------------------------------- comercios
def get_merchant(session: Session, merchant_id: uuid.UUID | str) -> MerchantAccount | None:
    """Cuenta de comercio por id o `None` (solo lectura)."""
    try:
        mid = _coerce_uuid(merchant_id, "merchant_id")
    except ValueError:
        return None
    return session.get(MerchantAccount, mid)


def get_merchant_by_user(session: Session, user_id: uuid.UUID | str) -> MerchantAccount | None:
    """Cuenta de comercio del titular o `None` (solo lectura)."""
    try:
        owner = _coerce_uuid(user_id, "user_id")
    except ValueError:
        return None
    stmt = sa.select(MerchantAccount).where(MerchantAccount.user_id == owner)
    return session.scalar(stmt)


def create_merchant(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    business_name: str,
    tax_id_hash: str | None = None,
) -> MerchantAccount:
    """Crea la cuenta de comercio `ACTIVE` (el RUC llega ya hasheado)."""
    owner = _coerce_uuid(user_id, "user_id")
    name = business_name.strip() if isinstance(business_name, str) else ""
    if not name or len(name) > 150:
        raise ValueError("business_name con formato invalido (1..150 caracteres)")
    if tax_id_hash is not None and (not isinstance(tax_id_hash, str) or len(tax_id_hash) > 128):
        raise ValueError("tax_id_hash con formato invalido")
    row = MerchantAccount(
        user_id=owner, business_name=name, tax_id_hash=tax_id_hash, status="ACTIVE"
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        raise DuplicateMerchantError("el titular ya tiene cuenta de comercio") from exc
    logger.debug("comercio creado merchant_id=%s", row.id)
    return row


def ensure_demo_merchants(session: Session) -> list[MerchantAccount]:
    """Asegura los comercios demo (idempotente; para tests/dev sin migracion).

    La migracion `0039` ya los siembra en Postgres; este helper replica
    el seed en SQLite/dev insertando solo los ausentes (por `id`).
    """

    rows: list[MerchantAccount] = []
    for merchant_id, user_id, business_name in DEMO_MERCHANTS:
        existing = session.get(MerchantAccount, uuid.UUID(merchant_id))
        if existing is not None:
            rows.append(existing)
            continue
        row = MerchantAccount(
            id=uuid.UUID(merchant_id),
            user_id=uuid.UUID(user_id),
            business_name=business_name,
            tax_id_hash=None,
            status="ACTIVE",
        )
        session.add(row)
        session.flush()
        rows.append(row)
    return rows


# ------------------------------------------------------- cargos QR
def create_qr_charge(
    session: Session,
    *,
    id: uuid.UUID,
    merchant_id: uuid.UUID,
    amount_minor: int,
    currency: str,
    concept: str | None,
    mode: str,
    payload_hash: str,
    signature: str,
    expires_at: datetime | None,
) -> QrCharge:
    """Persiste el cargo QR `ACTIVE` (la firma ya viene calculada)."""
    row = QrCharge(
        id=id,
        merchant_id=merchant_id,
        amount_minor=amount_minor,
        currency=currency,
        concept=concept,
        mode=mode,
        payload_hash=payload_hash,
        signature=signature,
        status="ACTIVE",
        expires_at=expires_at,
    )
    session.add(row)
    session.flush()
    logger.debug("qr charge creado charge_id=%s mode=%s", row.id, mode)
    return row


def get_qr_charge(session: Session, charge_id: uuid.UUID | str) -> QrCharge | None:
    """Cargo QR por id o `None` (solo lectura)."""
    try:
        cid = _coerce_uuid(charge_id, "charge_id")
    except ValueError:
        return None
    return session.get(QrCharge, cid)


def list_charges_by_merchant(
    session: Session, merchant_id: uuid.UUID | str, *, status: str | None = None
) -> list[QrCharge]:
    """Cargos del comercio (opcional por `status`), orden estable."""
    mid = _coerce_uuid(merchant_id, "merchant_id")
    stmt = sa.select(QrCharge).where(QrCharge.merchant_id == mid)
    if status is not None:
        stmt = stmt.where(QrCharge.status == status)
    stmt = stmt.order_by(QrCharge.created_at, QrCharge.id)
    return list(session.scalars(stmt).all())


def list_expired_charges(session: Session, now: datetime, *, limit: int = 100) -> list[QrCharge]:
    """Cargos `ACTIVE` vencidos (`expires_at <= now`), orden estable.

    Lo consume el job de `E4-T07`. El filtro de vencimiento se evalua
    en Python via `domain.qr.is_expired` (robusto naive/aware: SQLite
    devuelve naive) sobre los `ACTIVE` con `expires_at` no nulo; sin
    PII en logs (solo conteos). `STATIC` (`expires_at NULL`) nunca
    vence aqui.
    """
    from app.modules.wallet.domain import qr as qr_domain

    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError(f"limit debe ser entero > 0, recibido: {limit!r}")
    stmt = (
        sa.select(QrCharge)
        .where(QrCharge.status == "ACTIVE")
        .where(QrCharge.expires_at.is_not(None))
        .order_by(QrCharge.expires_at, QrCharge.id)
        .limit(limit)
    )
    return [row for row in session.scalars(stmt).all() if qr_domain.is_expired(row, now)]


def _transition(session: Session, row: QrCharge, to_status: str) -> QrCharge:
    row.status = to_status
    session.flush()
    logger.debug("qr charge transicion charge_id=%s status=%s", row.id, to_status)
    return row


def mark_paid(session: Session, row: QrCharge, *, paid_at: datetime) -> QrCharge:
    """Marca el cargo `PAID` (lo usa HU15 al confirmar; sin mover dinero aqui)."""
    row.paid_at = paid_at
    return _transition(session, row, "PAID")


def mark_expired(session: Session, row: QrCharge) -> QrCharge:
    """Marca el cargo `EXPIRED` (lo usa el job de `E4-T07`)."""
    return _transition(session, row, "EXPIRED")


def mark_cancelled(session: Session, row: QrCharge) -> QrCharge:
    """Marca el cargo `CANCELLED` (baja del comercio)."""
    return _transition(session, row, "CANCELLED")


__all__ = [
    "DEMO_MERCHANTS",
    "DuplicateMerchantError",
    "create_merchant",
    "create_qr_charge",
    "ensure_demo_merchants",
    "get_merchant",
    "get_merchant_by_user",
    "get_qr_charge",
    "list_charges_by_merchant",
    "list_expired_charges",
    "mark_cancelled",
    "mark_expired",
    "mark_paid",
]
