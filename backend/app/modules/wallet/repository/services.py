"""Repositorio de servicios de `wallet` (E4-T16, HU16): solo tablas propias.

`billers` (catalogo) + `service_subscriptions` (registro para
recordatorios de `E4-T18`) + `service_payments`/`topups` (los escribe
el pago de `E4-T17` por el motor, aqui solo el registro con su
`transaction_id` REF logica). Convencion: `flush` sin `commit`;
el endpoint confirma. Sin PII en logs (regla de oro 7): aqui no se
loguea `supply_ref`/`phone` (solo `id`s y conteos).
"""

from __future__ import annotations

import logging
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.wallet.models import (
    Biller,
    ServicePayment,
    ServiceSubscription,
    Topup,
)

logger = logging.getLogger(__name__)

#: Billers demo sembrados por la migracion `0042_wallet_services`
#: (decision 3 del brief E4-T16): `(biller_id, category, name,
#: required_fields)`. Los `supply_prefixes` para la busqueda por
#: suministro viven solo en el adaptador (`service_provider`).
DEMO_BILLERS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "c1111111-1111-4111-8111-111111111111",
        "ELECTRICIDAD",
        "Luz del Hogar Demo",
        ("supply_ref",),
    ),
    (
        "c2222222-2222-4222-8222-222222222222",
        "AGUA",
        "Agua Potable Demo",
        ("supply_ref",),
    ),
    (
        "c3333333-3333-4333-8333-333333333333",
        "GAS",
        "Gas Domiciliario Demo",
        ("supply_ref",),
    ),
    (
        "c4444444-4444-4444-8444-444444444444",
        "INTERNET",
        "Internet Fibra Demo",
        ("supply_ref",),
    ),
    (
        "c5555555-5555-4555-8555-555555555555",
        "TELEFONIA",
        "Recarga Movil Sol Demo",
        ("phone",),
    ),
    (
        "c6666666-6666-4666-8666-666666666666",
        "TELEFONIA",
        "Recarga Movil Luna Demo",
        ("phone",),
    ),
)


class BillerNotFoundError(LookupError):
    """El `biller` no existe o esta deshabilitado (-> 404 neutro)."""


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def ensure_demo_billers(session: Session) -> int:
    """Inserta los `billers` demo que falten (idempotente, sin `commit`).

    Espejo de la siembra `0042_wallet_services` para tests/dev sin
    migracion (patron `repository/qr.py::ensure_demo_merchants`).
    Devuelve cuantos inserto.
    """
    inserted = 0
    for biller_id, category, name, required_fields in DEMO_BILLERS:
        bid = uuid.UUID(biller_id)
        if session.get(Biller, bid) is not None:
            continue
        session.add(
            Biller(
                id=bid,
                category=category,
                name=name,
                required_fields=list(required_fields),
                enabled=True,
            )
        )
        inserted += 1
    if inserted:
        session.flush()
    logger.debug("billers demo asegurados insertados=%d", inserted)
    return inserted


def list_billers(
    session: Session,
    *,
    category: str | None = None,
    query: str | None = None,
    enabled_only: bool = True,
) -> list[Biller]:
    """Catalogo de `billers` filtrable por categoria/nombre (solo lectura).

    `category` ya normalizada (mayusculas); `query` subcadena sobre
    `name`/`category` (insensible a mayusculas). Orden estable por
    `category`, `name`, `id`.
    """
    stmt = sa.select(Biller)
    if enabled_only:
        stmt = stmt.where(Biller.enabled.is_(True))
    if category:
        stmt = stmt.where(Biller.category == category)
    if query:
        like = f"%{query}%"
        stmt = stmt.where(
            sa.or_(
                Biller.name.ilike(like),
                Biller.category.ilike(like),
            )
        )
    stmt = stmt.order_by(Biller.category, Biller.name, Biller.id)
    return list(session.scalars(stmt).all())


def get_biller(session: Session, biller_id: uuid.UUID | str) -> Biller | None:
    """`Biller` por id o `None` (id malformado -> `None`; solo lectura)."""
    try:
        bid = _coerce_uuid(biller_id, "biller_id")
    except ValueError:
        return None
    return session.get(Biller, bid)


def find_active_subscription(
    session: Session,
    user_id: uuid.UUID,
    biller_id: uuid.UUID,
    supply_ref: str,
) -> ServiceSubscription | None:
    """Suscripcion `ACTIVE` del titular para ese `biller`+suministro, o `None`."""
    stmt = sa.select(ServiceSubscription).where(
        ServiceSubscription.user_id == user_id,
        ServiceSubscription.biller_id == biller_id,
        ServiceSubscription.supply_ref == supply_ref,
        ServiceSubscription.status == "ACTIVE",
    )
    return session.scalar(stmt)


def create_subscription(
    session: Session,
    *,
    user_id: uuid.UUID,
    biller_id: uuid.UUID | str,
    supply_ref: str,
    notify_days_before: int = 3,
) -> tuple[ServiceSubscription, bool]:
    """Registra el servicio a recordar (idempotente por naturaleza).

    Si ya existe una suscripcion `ACTIVE` del titular para ese
    `biller`+suministro la devuelve (`created=False`, sin duplicar);
    si no, la inserta (`created=True`). `biller` inexistente o
    deshabilitado -> `BillerNotFoundError` (404 neutro). Solo `flush`.
    """
    try:
        bid = _coerce_uuid(biller_id, "biller_id")
    except ValueError as exc:
        raise BillerNotFoundError("servicio inexistente") from exc
    biller = session.get(Biller, bid)
    if biller is None or not biller.enabled:
        raise BillerNotFoundError("servicio inexistente")
    existing = find_active_subscription(session, user_id, bid, supply_ref)
    if existing is not None:
        logger.debug("suscripcion existente subscription_id=%s", existing.id)
        return existing, False
    row = ServiceSubscription(
        user_id=user_id,
        biller_id=bid,
        supply_ref=supply_ref,
        notify_days_before=notify_days_before,
        status="ACTIVE",
    )
    session.add(row)
    session.flush()
    logger.debug("suscripcion creada subscription_id=%s biller_id=%s", row.id, bid)
    return row, True


def list_subscriptions(
    session: Session,
    user_id: uuid.UUID,
    *,
    status: str = "ACTIVE",
) -> list[ServiceSubscription]:
    """Suscripciones del titular (por defecto `ACTIVE`), orden estable."""
    stmt = (
        sa.select(ServiceSubscription)
        .where(
            ServiceSubscription.user_id == user_id,
            ServiceSubscription.status == status,
        )
        .order_by(ServiceSubscription.id)
    )
    return list(session.scalars(stmt).all())


def list_active_subscriptions(
    session: Session,
    *,
    limit: int = 100,
) -> list[ServiceSubscription]:
    """Suscripciones `ACTIVE` para el job de recordatorios (E4-T18).

    Solo lectura, orden estable por `id` (el job acota con `limit`;
    el siguiente ciclo retoma). Sin PII en logs (solo conteos en el
    llamante).
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError(f"limit debe ser entero > 0, recibido: {limit!r}")
    stmt = (
        sa.select(ServiceSubscription)
        .where(ServiceSubscription.status == "ACTIVE")
        .order_by(ServiceSubscription.id)
        .limit(limit)
    )
    return list(session.scalars(stmt).all())


#: Estados admitidos en `service_payments`/`topups` (los fija el pago
#: de `E4-T17` segun el estado del motor: `SETTLED` -> `PAID`,
#: `PENDING_AUTHORIZATION` -> `PENDING`, resto -> `REJECTED`).
SERVICE_PAYMENT_FINAL_STATUSES = ("PENDING", "PAID", "REJECTED")


def _check_money(amount_minor: int, currency: str) -> tuple[int, str]:
    """Valida dinero entero en centimos + moneda ISO (sin `float`)."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise TypeError("amount_minor debe ser entero de centimos")
    if amount_minor <= 0:
        raise ValueError("amount_minor debe ser > 0")
    code = str(currency or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError("currency debe ser ISO-4217 (3 letras)")
    return amount_minor, code


def create_service_payment(
    session: Session,
    *,
    user_id: uuid.UUID,
    biller_id: uuid.UUID,
    supply_ref: str,
    amount_minor: int,
    currency: str,
    transaction_id: uuid.UUID | str,
    status: str,
    receipt_data: dict | None = None,
) -> ServicePayment:
    """Registra el pago de servicio con su `transaction_id` del motor.

    El dinero vive en el motor (asiento `docs/04#7.10` via
    `pay_express` sin comision); aqui solo el registro + el
    comprobante (`receipt_data`, solo cuando `PAID`). Solo `flush`.
    """
    amount, code = _check_money(amount_minor, currency)
    if status not in SERVICE_PAYMENT_FINAL_STATUSES:
        raise ValueError(f"status con valor invalido: {status!r}")
    row = ServicePayment(
        user_id=user_id,
        biller_id=biller_id,
        supply_ref=supply_ref,
        amount_minor=amount,
        currency=code,
        transaction_id=_coerce_uuid(transaction_id, "transaction_id"),
        receipt_data=receipt_data,
        status=status,
    )
    session.add(row)
    session.flush()
    logger.debug("service payment registrado status=%s", status)
    return row


def get_service_payment(session: Session, payment_id: uuid.UUID | str) -> ServicePayment | None:
    """Pago de servicio por id o `None` (solo lectura)."""
    try:
        pid = _coerce_uuid(payment_id, "payment_id")
    except ValueError:
        return None
    return session.get(ServicePayment, pid)


def get_service_payment_by_transaction(
    session: Session, transaction_id: uuid.UUID | str
) -> ServicePayment | None:
    """Pago por `transaction_id` del motor o `None` (solo lectura)."""
    try:
        tid = _coerce_uuid(transaction_id, "transaction_id")
    except ValueError:
        return None
    stmt = sa.select(ServicePayment).where(ServicePayment.transaction_id == tid)
    return session.scalar(stmt)


def mark_service_payment_paid(
    session: Session, row: ServicePayment, *, receipt_data: dict
) -> ServicePayment:
    """Marca el pago `PAID` con su comprobante (al liquidar `SETTLED`)."""
    if not isinstance(receipt_data, dict) or not receipt_data:
        raise ValueError("receipt_data con el comprobante es obligatorio al pagar")
    row.status = "PAID"
    row.receipt_data = receipt_data
    session.flush()
    logger.debug("service payment pagado payment_id=%s", row.id)
    return row


def set_service_payment_status(
    session: Session, row: ServicePayment, status: str
) -> ServicePayment:
    """Fija el estado del pago (`PENDING`/`PAID`/`REJECTED`)."""
    if status not in SERVICE_PAYMENT_FINAL_STATUSES:
        raise ValueError(f"status con valor invalido: {status!r}")
    row.status = status
    session.flush()
    logger.debug("service payment estado=%s", status)
    return row


def create_topup(
    session: Session,
    *,
    user_id: uuid.UUID,
    operator: str,
    phone: str,
    amount_minor: int,
    currency: str,
    transaction_id: uuid.UUID | str,
    status: str,
) -> Topup:
    """Registra la recarga con su `transaction_id` del motor.

    El dinero vive en el motor (asiento `docs/04#7.10` via
    `pay_express` sin comision); aqui solo el registro. Solo `flush`.
    """
    amount, code = _check_money(amount_minor, currency)
    if status not in SERVICE_PAYMENT_FINAL_STATUSES:
        raise ValueError(f"status con valor invalido: {status!r}")
    row = Topup(
        user_id=user_id,
        operator=operator,
        phone=phone,
        amount_minor=amount,
        currency=code,
        transaction_id=_coerce_uuid(transaction_id, "transaction_id"),
        status=status,
    )
    session.add(row)
    session.flush()
    logger.debug("topup registrado status=%s", status)
    return row


def get_topup(session: Session, topup_id: uuid.UUID | str) -> Topup | None:
    """Recarga por id o `None` (solo lectura)."""
    try:
        tid = _coerce_uuid(topup_id, "topup_id")
    except ValueError:
        return None
    return session.get(Topup, tid)


def get_topup_by_transaction(session: Session, transaction_id: uuid.UUID | str) -> Topup | None:
    """Recarga por `transaction_id` del motor o `None` (solo lectura)."""
    try:
        tid = _coerce_uuid(transaction_id, "transaction_id")
    except ValueError:
        return None
    stmt = sa.select(Topup).where(Topup.transaction_id == tid)
    return session.scalar(stmt)


def set_topup_status(session: Session, row: Topup, status: str) -> Topup:
    """Fija el estado de la recarga (`PENDING`/`PAID`/`REJECTED`)."""
    if status not in SERVICE_PAYMENT_FINAL_STATUSES:
        raise ValueError(f"status con valor invalido: {status!r}")
    row.status = status
    session.flush()
    logger.debug("topup estado=%s", status)
    return row


__all__ = [
    "DEMO_BILLERS",
    "SERVICE_PAYMENT_FINAL_STATUSES",
    "BillerNotFoundError",
    "create_service_payment",
    "create_subscription",
    "create_topup",
    "ensure_demo_billers",
    "find_active_subscription",
    "get_biller",
    "get_service_payment",
    "get_service_payment_by_transaction",
    "get_topup",
    "get_topup_by_transaction",
    "list_active_subscriptions",
    "list_billers",
    "list_subscriptions",
    "mark_service_payment_paid",
    "set_service_payment_status",
    "set_topup_status",
]
