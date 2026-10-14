"""Repositorio de `qr_payments` de `wallet` (E4-T11, HU15): solo tablas propias.

`wallet.qr_payments` (`docs/03b#94`, migracion `0040_wallet_qr_payments`).
Convencion: `flush` sin `commit`; el servicio confirma. Sin PII en logs
(regla 7): aqui solo `id`s, estados y conteos (nunca montos en claro
fuera del registro ni referencias externas).
"""

from __future__ import annotations

import logging
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.wallet.models import QrCharge, QrPayment

logger = logging.getLogger(__name__)

#: Estados con cobro real/pendiente: la guarda de doble cobro (ALTO-2)
#: solo los considera; `REJECTED` (sin mover dinero) queda fuera para
#: permitir el reintento legitimo con nueva clave (MEDIO-2).
ACTIVE_PAYMENT_STATUSES = ("PENDING", "PENDING_AUTHORIZATION", "PAID")


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def create_qr_payment(
    session: Session,
    *,
    transaction_id: uuid.UUID | str,
    charge_id: uuid.UUID | str,
    payer_user_id: uuid.UUID | str,
    merchant_id: uuid.UUID | str,
    amount_minor: int,
    currency: str,
    status: str = "PENDING",
) -> QrPayment:
    """Persiste el registro del cobro (el dinero vive en el motor)."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise TypeError("amount_minor debe ser entero de centimos")
    if amount_minor <= 0:
        raise ValueError("amount_minor debe ser > 0")
    if not isinstance(currency, str) or len(currency.strip()) != 3:
        raise ValueError("currency debe ser ISO-4217 (3 letras)")
    row = QrPayment(
        transaction_id=_coerce_uuid(transaction_id, "transaction_id"),
        charge_id=_coerce_uuid(charge_id, "charge_id"),
        payer_user_id=_coerce_uuid(payer_user_id, "payer_user_id"),
        merchant_id=_coerce_uuid(merchant_id, "merchant_id"),
        amount_minor=amount_minor,
        currency=currency.strip().upper(),
        status=status,
    )
    session.add(row)
    session.flush()
    logger.debug("qr payment creado status=%s", status)
    return row


def get_qr_payment(session: Session, payment_id: uuid.UUID | str) -> QrPayment | None:
    """Pago por id o `None` (solo lectura)."""
    try:
        pid = _coerce_uuid(payment_id, "payment_id")
    except ValueError:
        return None
    return session.get(QrPayment, pid)


def get_by_transaction(session: Session, transaction_id: uuid.UUID | str) -> QrPayment | None:
    """Pago por `transaction_id` (UQ) o `None` (solo lectura)."""
    try:
        tid = _coerce_uuid(transaction_id, "transaction_id")
    except ValueError:
        return None
    stmt = sa.select(QrPayment).where(QrPayment.transaction_id == tid)
    return session.scalar(stmt)


def get_by_charge(session: Session, charge_id: uuid.UUID | str) -> QrPayment | None:
    """Pago por `charge_id` (cualquier estado) o `None` (solo lectura).

    Historico (lo usaba la guarda anterior): para decidir el doble
    cobro usar `get_active_by_charge` (ignora `REJECTED`, MEDIO-2).
    """
    try:
        cid = _coerce_uuid(charge_id, "charge_id")
    except ValueError:
        return None
    stmt = (
        sa.select(QrPayment)
        .where(QrPayment.charge_id == cid)
        .order_by(QrPayment.created_at, QrPayment.id)
    )
    return session.scalars(stmt).first()


def get_active_by_charge(session: Session, charge_id: uuid.UUID | str) -> QrPayment | None:
    """Pago con cobro real/pendiente del cargo o `None` (solo lectura).

    Guarda de doble cobro (ALTO-2/MEDIO-2): solo estados
    `PENDING`/`PENDING_AUTHORIZATION`/`PAID`. Un `REJECTED` (sin mover
    dinero) NO bloquea: el cargo admite reintento con nueva clave.
    El backstop atomico es el indice unico parcial
    `uq_qr_payments_charge_active` (migracion `0041`).
    """
    try:
        cid = _coerce_uuid(charge_id, "charge_id")
    except ValueError:
        return None
    stmt = (
        sa.select(QrPayment)
        .where(QrPayment.charge_id == cid)
        .where(QrPayment.status.in_(ACTIVE_PAYMENT_STATUSES))
        .order_by(QrPayment.created_at, QrPayment.id)
    )
    return session.scalars(stmt).first()


def list_pending_authorization(session: Session, *, limit: int = 100) -> list[QrPayment]:
    """Pagos a la espera de autorizacion (`PENDING_AUTHORIZATION`), orden estable.

    Lo consume el job de reconciliacion `wallet/jobs/qr_reconcile.py`
    (R-1): solo los pendientes son candidatos a finalizar cuando su
    operacion del motor ya liquido (`SETTLED`). Solo lectura (sin
    `flush`); sin PII (solo `id`s).
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError(f"limit debe ser entero > 0, recibido: {limit!r}")
    stmt = (
        sa.select(QrPayment)
        .where(QrPayment.status == "PENDING_AUTHORIZATION")
        .order_by(QrPayment.created_at, QrPayment.id)
        .limit(limit)
    )
    return list(session.scalars(stmt).all())


def lock_charge(session: Session, charge_id: uuid.UUID | str) -> QrCharge | None:
    """Lee el cargo con bloqueo de fila (`FOR UPDATE` en Postgres).

    Serializa la ventana verificar-estado -> operar entre pagos
    concurrentes del mismo cargo (ALTO-2, defensa (b)). En SQLite
    (tests) el bloqueo no existe: lectura simple (el backstop atomico
    es el indice unico parcial). Solo lectura (sin `flush`); sin PII
    (solo el `id`).
    """
    try:
        cid = _coerce_uuid(charge_id, "charge_id")
    except ValueError:
        return None
    stmt = sa.select(QrCharge).where(QrCharge.id == cid)
    try:
        dialect = session.get_bind().dialect.name if session.get_bind() is not None else ""
    except Exception:  # noqa: BLE001 - best-effort: sin bloqueo
        dialect = ""
    if dialect == "postgresql":
        stmt = stmt.with_for_update()
    return session.scalar(stmt)


def mark_paid(session: Session, row: QrPayment) -> QrPayment:
    """Marca el pago `PAID` (al liquidar `SETTLED`; sin mover dinero aqui)."""
    row.status = "PAID"
    session.flush()
    logger.debug("qr payment pagado payment_id=%s", row.id)
    return row


def set_status(session: Session, row: QrPayment, status: str) -> QrPayment:
    """Fija el estado del pago (`PENDING_AUTHORIZATION`/`REJECTED`/`PAID`)."""
    if status not in ("PENDING", "PENDING_AUTHORIZATION", "PAID", "REJECTED"):
        raise ValueError(f"status con valor invalido: {status!r}")
    row.status = status
    session.flush()
    logger.debug("qr payment estado=%s", status)
    return row


__all__ = [
    "ACTIVE_PAYMENT_STATUSES",
    "create_qr_payment",
    "get_active_by_charge",
    "get_by_charge",
    "get_by_transaction",
    "get_qr_payment",
    "list_pending_authorization",
    "lock_charge",
    "mark_paid",
    "set_status",
]
