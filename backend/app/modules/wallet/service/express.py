"""Pago express de billetera (E4-T02, HU13 CA-02/CA-04).

Servicio **interno** (decision del dueno, `docs/18` G66: sin endpoint
publico nuevo; lo consumen HU15/HU16): con una fuente de fondos
vinculada (`wallet_link ACTIVE` de tipo `ACCOUNT`), ejecuta el pago
por el **motor** (`transactions` -> `ledger`) sin pedir clave cuando
el total esta dentro del **limite base** (`wallet.express_limit_minor`)
y delega en el gate del motor (`PENDING_AUTHORIZATION` + nonce/PIN,
patron `E2-T17`/`E2-T31`) cuando lo supera, respetando ademas el
**limite diario** (`wallet.daily_limit_minor`).

Camino de dinero reutilizado (`docs/04#7.2`, tercero mismo banco):

- `wallet.service.express.pay_express` resuelve el vinculo y delega en
  `transactions.service.transfer_third_party` con lectores propios
  (`wallet_threshold_reader` / `wallet_daily_limit_reader`): el gate
  del motor (umbral por operacion `E2-T34` + limite diario acumulado
  `E2-T36` con re-chequeo al autorizar) se aplica tal cual, sin
  reimplementarlo. El `tx_type` es `THIRD_PARTY_TRANSFER` para que la
  autorizacion diferida (`authorize`, PIN o firma) reutilice el hold
  sin duplicar asiento, y el reverso/listado existentes apliquen.
- `wallet` **no** escribe `transactions`/`ledger`/`accounts`
  directamente (regla de oro 4); tampoco crea tablas (el registro
  contable vive en el motor). Solo lee lo propio (`wallets`,
  `wallet_links`) + fachadas (`accounts` via el motor,
  `shared/config` para limites).

Reglas:

- Céntimos enteros; nunca `float` (regla de oro 3). `total =
  amount_minor + fee_minor` (el motor compara el total, `E2-T34`).
- `Idempotency-Key` obligatoria (regla de oro 5): misma clave + mismo
  cuerpo = replay (un solo movimiento); cuerpo distinto = `CONFLICT`.
- Vinculos `CARD` no son debitables por el motor (sin cuenta origen):
  `VALIDATION_ERROR`. Vinculo ajeno/inexistente/inactivo -> `NOT_FOUND`
  neutro (sin oraculo).
- Eventos: se reutilizan los del motor (`transaction.initiated`,
  `funds.held`, `transfer.settled`); sin evento propio. Auditoria
  best-effort `wallet.express.executed` (sin PII ni cifras).
- Sin PII/secretos en logs (regla 7): solo `link_id`, decision y
  estado; la cuenta solo viaja enmascarada en `metadata`.

Convencion: `flush` sin `commit`; el llamante confirma.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.modules.shared.config import parameters as config_params
from app.modules.wallet import repository as wallet_repo
from app.modules.wallet.domain.limits import decide_express

logger = logging.getLogger(__name__)

#: Tipo de `Transaction` del pago express: reutiliza el camino de
#: tercero (`docs/04#7.2`) para que autorizacion (`E2-T19`/`E2-T31`),
#: re-chequeo diario (`E2-T36`) y reverso apliquen sin cambios.
WALLET_TX_TYPE = "THIRD_PARTY_TRANSFER"

#: Marca en `transactions.meta` del pago express (trazabilidad + match
#: de idempotencia; sin PII: solo el `link_id` y la etiqueta).
WALLET_META_KEY = "wallet_link_id"
WALLET_META_VALUE = "express"

#: Accion de auditoria best-effort (sin evento nuevo: bastan los del
#: motor + esta traza).
AUDIT_ACTION = "wallet.express.executed"


class ExpressError(ValueError):
    """Error base del pago express (mapeable a 4xx)."""

    code = "EXPRESS_ERROR"


class ExpressNotFoundError(ExpressError, LookupError):
    """Vinculo inexistente, ajeno o inactivo (neutro, `NOT_FOUND`)."""

    code = "NOT_FOUND"


class ExpressValidationError(ExpressError):
    """Monto/moneda/clave/link no debitable invalidos."""

    code = "VALIDATION_ERROR"


class ExpressConflictError(ExpressError):
    """Misma clave con cuerpo distinto (`CONFLICT`)."""

    code = "CONFLICT"


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ExpressValidationError(f"{field} debe ser UUID") from exc


def get_wallet_express_limit_minor(session: Session) -> int:
    """Limite base del pago express en centimos (best-effort con fallback).

    Lee `wallet.express_limit_minor` via la unica fachada
    `shared/config` (regla de oro 6); nunca lanza.
    """
    return config_params.get_int_parameter(
        session,
        config_params.WALLET_EXPRESS_LIMIT_MINOR_KEY,
        config_params.DEFAULT_WALLET_EXPRESS_LIMIT_MINOR,
        0,
    )


def get_wallet_daily_limit_minor(session: Session, profile: Any = None) -> int:
    """Limite diario de billetera en centimos (best-effort con fallback).

    MVP generico (sin variantes por perfil; `profile` se acepta por la
    firma `DailyLimitReader` del motor y se ignora de forma
    documentada). Nunca lanza.
    """
    _ = profile
    return config_params.get_int_parameter(
        session,
        config_params.WALLET_DAILY_LIMIT_MINOR_KEY,
        config_params.DEFAULT_WALLET_DAILY_LIMIT_MINOR,
        0,
    )


def wallet_threshold_reader(session: Session) -> int:
    """Lector de umbral para el gate del motor (base express)."""
    return get_wallet_express_limit_minor(session)


def wallet_daily_limit_reader(session: Session, profile: Any) -> int:
    """Lector de limite diario para el gate del motor (y su re-chequeo).

    HU15/HU16 deben pasar este lector tambien al autorizar
    (`authorize_transaction` / `authorize_transaction_with_pin` via
    `daily_limit_reader=...`): el re-chequeo `E2-T36` usa por defecto
    el limite generico de transferencias, que coincide con el de
    billetera salvo personalizacion del dueno.
    """
    return get_wallet_daily_limit_minor(session, profile)


def _require_key(idempotency_key: object) -> str:
    key = str(idempotency_key).strip() if isinstance(idempotency_key, str) else ""
    if not key:
        raise ExpressValidationError("cabecera Idempotency-Key obligatoria")
    if len(key) > 80:
        raise ExpressValidationError("Idempotency-Key supera 80 caracteres")
    return key


def _resolve_source(
    session: Session, initiator: uuid.UUID, link_ref: uuid.UUID
) -> tuple[uuid.UUID, Any]:
    """Fuente de fondos del vinculo (solo lectura + fachada propia).

    El vinculo debe existir, pertenecer a la billetera del titular y
    estar `ACTIVE` (otro caso -> `NOT_FOUND` neutro); la billetera debe
    estar `ACTIVE`; solo los vinculos `ACCOUNT` son debitables por el
    motor (`CARD` -> `VALIDATION_ERROR`, sin mover dinero).
    """
    wallet = wallet_repo.get_wallet_by_user(session, initiator)
    link = wallet_repo.get_link(session, link_ref)
    if (
        wallet is None
        or link is None
        or link.wallet_id != wallet.id
        or str(link.status) != "ACTIVE"
    ):
        raise ExpressNotFoundError("vinculo inexistente")
    if str(wallet.status) != "ACTIVE":
        raise ExpressValidationError(f"billetera no disponible ({wallet.status})")
    if link.account_id is None:
        raise ExpressValidationError(
            "el vinculo no es debitable por el motor (solo vinculos ACCOUNT)"
        )
    return link.account_id, link


def _check_replay_match(
    tx: Any,
    *,
    source: uuid.UUID,
    target: uuid.UUID,
    amount_minor: int,
    fee_minor: int,
    currency: str,
    link_id: uuid.UUID,
) -> None:
    """Misma clave + cuerpo distinto -> `CONFLICT` (sin oraculo de datos)."""
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    if (
        tx.type != WALLET_TX_TYPE
        or tx.source_account_id != source
        or tx.target_account_id != target
        or int(tx.amount_minor) != int(amount_minor)
        or int(tx.fee_minor or 0) != int(fee_minor)
        or str(tx.currency) != str(currency)
        or str(meta.get(WALLET_META_KEY) or "") != str(link_id)
    ):
        raise ExpressConflictError("Idempotency-Key ya usada con un cuerpo distinto")


def _audit_express(session: Session, owner: uuid.UUID, link_id: uuid.UUID, tx: Any) -> None:
    """Auditoria best-effort del pago (sin PII ni cifras)."""
    try:
        from app.modules.audit.service import record as audit_record

        audit_record(
            session,
            actor=owner,
            action=AUDIT_ACTION,
            entity="transaction",
            entity_id=tx.id,
            metadata={
                "link_id": str(link_id),
                "transaction_id": str(tx.id),
                "status": str(tx.status),
            },
        )
    except Exception as exc:  # noqa: BLE001 - la auditoria no bloquea el pago
        logger.warning("wallet express audit best-effort omitida err=%s", type(exc).__name__)


def pay_express(
    session: Session,
    *,
    initiator_user_id: uuid.UUID | str,
    link_id: uuid.UUID | str,
    target_account_id: uuid.UUID | str,
    amount_minor: int,
    currency: str,
    idempotency_key: str,
    fee_minor: int | None = None,
    device_id: str | None = None,
    risk_profile: str | None = None,
    balance_port: Any | None = None,
    description: str | None = None,
) -> Any:
    """Ejecuta el pago express por el motor (`docs/04#7.2`).

    Resuelve el `wallet_link ACTIVE` del titular como cuenta origen y
    crea la `Transaction` via `transfer_third_party` con los lectores
    de limites de billetera: bajo el limite base liquida de inmediato
    (`SETTLED`, sin reto); sobre el base queda en
    `PENDING_AUTHORIZATION` (nonce a firmar o PIN, `E2-T19`/`E2-T31`);
    sobre el diario queda `REJECTED` (`DAILY_LIMIT_EXCEEDED`) sin tocar
    saldos; sin fondos queda `REJECTED` (sin tocar saldos, `docs/04#10`).

    Idempotente por (`idempotency_key`, titular): misma clave + mismo
    cuerpo devuelve la operacion existente sin mover de nuevo; cuerpo
    distinto lanza `ExpressConflictError`. Solo `flush` (el llamante
    confirma o revierte toda la operacion de negocio).
    """
    from app.modules.transactions.domain.state_machine import (
        validate_amount_minor,
        validate_currency,
    )
    from app.modules.transactions.service import biometric as biometric_service
    from app.modules.transactions.service import (
        find_by_idempotency_key,
        resolve_third_party_fee,
        transfer_third_party,
    )

    initiator = _coerce_uuid(initiator_user_id, "initiator_user_id")
    link_ref = _coerce_uuid(link_id, "link_id")
    target = _coerce_uuid(target_account_id, "target_account_id")
    key = _require_key(idempotency_key)
    try:
        validate_amount_minor(amount_minor)
        validate_currency(currency)
    except (TypeError, ValueError) as exc:
        raise ExpressValidationError(str(exc)) from exc
    try:
        fee = resolve_third_party_fee(session, fee_minor)
    except ValueError as exc:
        raise ExpressValidationError(str(exc)) from exc

    source, link = _resolve_source(session, initiator, link_ref)

    existing = find_by_idempotency_key(session, key, initiator_user_id=initiator)
    if existing is not None:
        _check_replay_match(
            existing,
            source=source,
            target=target,
            amount_minor=amount_minor,
            fee_minor=fee,
            currency=currency,
            link_id=link.id,
        )
        logger.info("wallet express replay status=%s", existing.status)
        return existing

    express_limit = get_wallet_express_limit_minor(session)
    daily_limit = get_wallet_daily_limit_minor(session)
    total = int(amount_minor) + int(fee)
    try:
        accumulated = biometric_service.daily_accumulated_minor(session, initiator)
        decision = decide_express(
            total,
            int(accumulated),
            express_limit_minor=express_limit,
            daily_limit_minor=daily_limit,
        )
    except Exception:  # noqa: BLE001 - anticipacion best-effort; el motor decide
        decision = "UNKNOWN"

    tx = transfer_third_party(
        session,
        initiator_user_id=initiator,
        source_account_id=source,
        target_account_id=target,
        amount_minor=amount_minor,
        currency=currency,
        fee_minor=fee,
        idempotency_key=key,
        metadata={
            WALLET_META_KEY: str(link.id),
            "wallet_payment": WALLET_META_VALUE,
        },
        balance_port=balance_port,
        risk_profile=risk_profile,
        device_id=device_id,
        threshold_reader=wallet_threshold_reader,
        daily_limit_reader=wallet_daily_limit_reader,
    )
    _audit_express(session, initiator, link.id, tx)
    logger.info(
        "wallet express decision=%s status=%s link_id=%s",
        decision,
        tx.status,
        link.id,
    )
    _ = description
    return tx


__all__ = [
    "AUDIT_ACTION",
    "WALLET_META_KEY",
    "WALLET_META_VALUE",
    "WALLET_TX_TYPE",
    "ExpressConflictError",
    "ExpressError",
    "ExpressNotFoundError",
    "ExpressValidationError",
    "get_wallet_daily_limit_minor",
    "get_wallet_express_limit_minor",
    "pay_express",
    "wallet_daily_limit_reader",
    "wallet_threshold_reader",
]
