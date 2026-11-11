"""Repositorio base del schema `fx` (E7-T01, HU24/HU25).

Capa de datos sin endpoints ni logica de negocio: solo SQLAlchemy sobre
las tablas propias (`fx_rates`, `fx_quotes`, `fx_operations`,
`savings_pockets`, `pocket_movements`). Convencion del repo:
`flush` sin `commit` (decide el llamante). No mueve dinero ni crea
asientos (eso es `E7-T02`/`T09`/`T11` via el motor). Sin PII en logs
(regla 7): aqui solo `id`s, pares de moneda y estados (nunca nombres
ni montos en claro fuera del registro).

Validaciones (el brief exige `ValueError` por datos invalidos):
moneda ISO-4217 (3 letras ASCII, se normaliza a mayusculas);
`base != quote`; `rate`/`spread`/`yield_rate` `Decimal` (nunca `float`,
regla 3) con `rate > 0`, `spread/yield >= 0`; montos enteros en
centimos (`source/target/amount > 0`, `balance/target >= 0`, sin
`bool`); estados dentro de sus enums. Sin FK entre schemas: `user_id`,
`account_id`, `transaction_id` y `journal_entry_id` son UUID logicos.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.fx.models import (
    FX_OPERATION_STATUSES,
    FX_QUOTE_STATUSES,
    POCKET_MOVEMENT_DIRECTIONS,
    POCKET_MOVEMENT_TYPES,
    POCKET_STATUSES,
    FxOperation,
    FxQuote,
    FxRate,
    PocketMovement,
    SavingsPocket,
)

logger = logging.getLogger(__name__)


class FxError(ValueError):
    """Error base de `fx` (mapeable a 4xx)."""


class FxValidationError(FxError):
    """Dato de `fx` invalido (`VALIDATION_ERROR`)."""


class FxNotFoundError(FxError):
    """Fila de `fx` inexistente (`NOT_FOUND`)."""


class FxConflictError(FxError):
    """Conflicto de negocio de `fx` (cotizacion ya usada, `CONFLICT`)."""


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise FxValidationError(f"{field} debe ser UUID") from exc


def _coerce_currency(value: object, field: str = "currency") -> str:
    """Codigo ISO-4217: 3 letras ASCII (se normaliza a mayusculas)."""
    if not isinstance(value, str):
        raise FxValidationError(f"{field} debe ser ISO-4217 (3 letras)")
    code = value.strip().upper()
    if len(code) != 3 or not code.isalpha() or not code.isascii():
        raise FxValidationError(f"{field} debe ser ISO-4217 (3 letras)")
    return code


def _coerce_pair(base: object, quote: object) -> tuple[str, str]:
    base_code = _coerce_currency(base, "base")
    quote_code = _coerce_currency(quote, "quote")
    if base_code == quote_code:
        raise FxValidationError("base y quote deben diferir")
    return base_code, quote_code


def _coerce_decimal(
    value: object, field: str, *, minimum: Decimal, strict_minimum: bool = False
) -> Decimal:
    """`Decimal` (nunca `float`, regla 3) sobre `minimum` (`strict`: `>`)."""
    if isinstance(value, (bool, float)):
        raise FxValidationError(f"{field} debe ser Decimal (nunca float)")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, ArithmeticError) as exc:
        raise FxValidationError(f"{field} debe ser Decimal") from exc
    if amount.is_nan():
        raise FxValidationError(f"{field} no admite NaN")
    if strict_minimum:
        ok = amount > minimum
        bound = f"> {minimum}"
    else:
        ok = amount >= minimum
        bound = f">= {minimum}"
    if not ok:
        raise FxValidationError(f"{field} debe ser {bound}")
    return amount


def _coerce_rate(value: object) -> Decimal:
    return _coerce_decimal(value, "rate", minimum=Decimal(0), strict_minimum=True)


def _coerce_nonneg_decimal(value: object, field: str) -> Decimal:
    return _coerce_decimal(value, field, minimum=Decimal(0))


def _coerce_pos_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FxValidationError(f"{field} debe ser entero de centimos")
    if value <= 0:
        raise FxValidationError(f"{field} debe ser > 0")
    return value


def _coerce_nonneg_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FxValidationError(f"{field} debe ser entero de centimos")
    if value < 0:
        raise FxValidationError(f"{field} debe ser >= 0")
    return value


def _coerce_status(value: object, field: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value.strip().upper() not in allowed:
        raise FxValidationError(f"{field} debe ser uno de {sorted(allowed)}")
    return value.strip().upper()


def _coerce_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
        raise FxValidationError("name invalido")
    return value.strip()


# ------------------------------------------------------------- tasas
def create_rate(
    session: Session,
    *,
    base: str,
    quote: str,
    rate: Decimal | int | str,
    source: str,
    fetched_at: datetime | None = None,
    ttl_seconds: int = 60,
) -> FxRate:
    """Persiste una tasa (`03b#10.1`). Hace `flush`, no `commit`."""
    base_code, quote_code = _coerce_pair(base, quote)
    if not isinstance(source, str) or not source.strip() or len(source.strip()) > 40:
        raise FxValidationError("source invalido")
    ttl = _coerce_pos_int(ttl_seconds, "ttl_seconds")
    row = FxRate(
        base=base_code,
        quote=quote_code,
        rate=_coerce_rate(rate),
        source=source.strip(),
        ttl_seconds=ttl,
    )
    if fetched_at is not None:
        if not isinstance(fetched_at, datetime):
            raise FxValidationError("fetched_at debe ser datetime")
        row.fetched_at = fetched_at
    session.add(row)
    session.flush()
    logger.debug("fx rate creada %s/%s", base_code, quote_code)
    return row


def get_latest_rate(session: Session, base: str, quote: str) -> FxRate | None:
    """Tasa vigente del par (la de `fetched_at` mas reciente) o `None`."""
    base_code, quote_code = _coerce_pair(base, quote)
    return session.scalars(
        select(FxRate)
        .where(FxRate.base == base_code, FxRate.quote == quote_code)
        .order_by(FxRate.fetched_at.desc(), FxRate.id.desc())
        .limit(1)
    ).first()


# -------------------------------------------------------- cotizaciones
def create_quote(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    base: str,
    quote: str,
    rate: Decimal | int | str,
    source_amount_minor: int,
    target_amount_minor: int,
    expires_at: datetime,
    spread: Decimal | int | str = Decimal(0),
) -> FxQuote:
    """Congela una cotizacion `ACTIVE` (`03b#10.2`). Hace `flush`, no `commit`."""
    base_code, quote_code = _coerce_pair(base, quote)
    if not isinstance(expires_at, datetime):
        raise FxValidationError("expires_at debe ser datetime")
    anchor = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=UTC)
    if anchor <= datetime.now(UTC):
        raise FxValidationError("expires_at debe ser futuro")
    row = FxQuote(
        user_id=_coerce_uuid(user_id, "user_id"),
        base=base_code,
        quote=quote_code,
        rate=_coerce_rate(rate),
        spread=_coerce_nonneg_decimal(spread, "spread"),
        source_amount_minor=_coerce_pos_int(source_amount_minor, "source_amount_minor"),
        target_amount_minor=_coerce_pos_int(target_amount_minor, "target_amount_minor"),
        expires_at=expires_at,
        status="ACTIVE",
    )
    session.add(row)
    session.flush()
    logger.debug("fx quote creada status=ACTIVE")
    return row


def get_quote(session: Session, quote_id: uuid.UUID | str) -> FxQuote | None:
    """Cotizacion por id o `None` (solo lectura; `None` si el id no es UUID)."""
    try:
        qid = _coerce_uuid(quote_id, "quote_id")
    except FxValidationError:
        return None
    return session.get(FxQuote, qid)


def require_quote(session: Session, quote_id: uuid.UUID | str) -> FxQuote:
    """Cotizacion por id o `FxNotFoundError` (`NOT_FOUND`)."""
    row = get_quote(session, quote_id)
    if row is None:
        raise FxNotFoundError(f"cotizacion inexistente: {quote_id!r}")
    return row


def expire_quote(session: Session, quote_id: uuid.UUID | str) -> FxQuote:
    """Marca `ACTIVE -> EXPIRED` (idempotente si ya expiro).

    Una cotizacion `CONSUMED`/`CANCELLED` no vuelve a `EXPIRED`
    (`FxConflictError`); inexistente -> `FxNotFoundError`.
    Hace `flush`, no `commit`.
    """
    row = require_quote(session, quote_id)
    if row.status == "EXPIRED":
        return row
    if row.status != "ACTIVE":
        raise FxConflictError(f"cotizacion en estado {row.status}, no expirable")
    row.status = "EXPIRED"
    session.flush()
    return row


# --------------------------------------------------------- operaciones
def create_operation(
    session: Session,
    *,
    quote_id: uuid.UUID | str,
    transaction_id: uuid.UUID | str,
    status: str = "EXECUTED",
) -> FxOperation:
    """Registra la ejecucion de una cotizacion (`03b#10.3`).

    `quote_id` es UQ: una cotizacion se ejecuta una sola vez (el
    segundo intento viola la UQ y se reporta como `FxConflictError`).
    Hace `flush`, no `commit`.
    """
    row = FxOperation(
        quote_id=_coerce_uuid(quote_id, "quote_id"),
        transaction_id=_coerce_uuid(transaction_id, "transaction_id"),
        status=_coerce_status(status, "status", FX_OPERATION_STATUSES),
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        existing = session.scalars(
            select(FxOperation).where(FxOperation.quote_id == row.quote_id)
        ).first()
        if existing is not None:
            raise FxConflictError(f"cotizacion ya ejecutada: {quote_id!r}") from exc
        raise FxValidationError("operacion invalida (quote inexistente?)") from exc
    logger.debug("fx operation creada status=%s", row.status)
    return row


# ------------------------------------------------------------ bolsillos
def create_pocket(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    name: str,
    currency: str,
    account_id: uuid.UUID | str | None = None,
    target_minor: int | None = None,
    balance_minor: int = 0,
    roundup_enabled: bool = False,
    yield_rate: Decimal | int | str = Decimal(0),
) -> SavingsPocket:
    """Crea un bolsillo `ACTIVE` (`03b#10.3`, HU24). Hace `flush`, no `commit`."""
    if not isinstance(roundup_enabled, bool):
        raise FxValidationError("roundup_enabled debe ser booleano")
    row = SavingsPocket(
        user_id=_coerce_uuid(user_id, "user_id"),
        account_id=(_coerce_uuid(account_id, "account_id") if account_id is not None else None),
        name=_coerce_name(name),
        currency=_coerce_currency(currency),
        target_minor=(
            _coerce_nonneg_int(target_minor, "target_minor") if target_minor is not None else None
        ),
        balance_minor=_coerce_nonneg_int(balance_minor, "balance_minor"),
        roundup_enabled=roundup_enabled,
        yield_rate=_coerce_nonneg_decimal(yield_rate, "yield_rate"),
        status="ACTIVE",
    )
    session.add(row)
    session.flush()
    logger.debug("pocket creado")
    return row


def get_pocket(session: Session, pocket_id: uuid.UUID | str) -> SavingsPocket | None:
    """Bolsillo por id o `None` (solo lectura; `None` si el id no es UUID)."""
    try:
        pid = _coerce_uuid(pocket_id, "pocket_id")
    except FxValidationError:
        return None
    return session.get(SavingsPocket, pid)


def require_pocket(session: Session, pocket_id: uuid.UUID | str) -> SavingsPocket:
    """Bolsillo por id o `FxNotFoundError` (`NOT_FOUND`)."""
    row = get_pocket(session, pocket_id)
    if row is None:
        raise FxNotFoundError(f"bolsillo inexistente: {pocket_id!r}")
    return row


def list_pockets(
    session: Session,
    user_id: uuid.UUID | str,
    *,
    status: str | None = None,
) -> list[SavingsPocket]:
    """Bolsillos del titular (`status` opcional; orden estable `created_at, id`)."""
    uid = _coerce_uuid(user_id, "user_id")
    stmt = select(SavingsPocket).where(SavingsPocket.user_id == uid)
    if status is not None:
        stmt = stmt.where(SavingsPocket.status == _coerce_status(status, "status", POCKET_STATUSES))
    return list(session.scalars(stmt.order_by(SavingsPocket.created_at, SavingsPocket.id)).all())


# ----------------------------------------------------------- movimientos
def add_pocket_movement(
    session: Session,
    *,
    pocket_id: uuid.UUID | str,
    movement_type: str,
    direction: str,
    amount_minor: int,
    currency: str,
    journal_entry_id: uuid.UUID | str | None = None,
) -> PocketMovement:
    """Agrega un movimiento inmutable (`03b#10.3`; solo insercion).

    El bolsillo debe existir (`FxNotFoundError` si no) y estar `ACTIVE`
    (`FxConflictError` si esta `CLOSED`); `CREDIT` suma al `balance` y
    `DEBIT` resta (fondos insuficientes -> `FxConflictError`, sin dejar
    cambios). Hace `flush`, no `commit`.
    """
    pocket = require_pocket(session, pocket_id)
    if pocket.status != "ACTIVE":
        raise FxConflictError(f"bolsillo en estado {pocket.status}, no admite movimientos")
    mtype = _coerce_status(movement_type, "movement_type", POCKET_MOVEMENT_TYPES)
    direction_code = _coerce_status(direction, "direction", POCKET_MOVEMENT_DIRECTIONS)
    amount = _coerce_pos_int(amount_minor, "amount_minor")
    # H-2: toda la validacion ANTES de tocar `balance_minor`; si algo falla
    # aqui, el bolsillo queda intacto (sin saldo dirty para el llamante).
    journal_id = (
        _coerce_uuid(journal_entry_id, "journal_entry_id") if journal_entry_id is not None else None
    )
    currency_code = _coerce_currency(currency)
    if direction_code == "DEBIT" and amount > pocket.balance_minor:
        raise FxConflictError("fondos insuficientes en el bolsillo")
    pocket.balance_minor = (
        pocket.balance_minor + amount
        if direction_code == "CREDIT"
        else pocket.balance_minor - amount
    )
    row = PocketMovement(
        pocket_id=pocket.id,
        journal_entry_id=journal_id,
        movement_type=mtype,
        direction=direction_code,
        amount_minor=amount,
        currency=currency_code,
    )
    session.add(row)
    session.flush()
    logger.debug("pocket movement %s %s", mtype, direction_code)
    return row


__all__ = [
    "FX_OPERATION_STATUSES",
    "FX_QUOTE_STATUSES",
    "POCKET_MOVEMENT_DIRECTIONS",
    "POCKET_MOVEMENT_TYPES",
    "POCKET_STATUSES",
    "FxConflictError",
    "FxError",
    "FxNotFoundError",
    "FxValidationError",
    "add_pocket_movement",
    "create_operation",
    "create_pocket",
    "create_quote",
    "create_rate",
    "expire_quote",
    "get_latest_rate",
    "get_pocket",
    "get_quote",
    "list_pockets",
    "require_pocket",
    "require_quote",
]
