"""Casos de uso del catalogo de servicios (E4-T16, HU16 CA-01/CA-03 base)
+ pago de servicio y recarga por el motor (E4-T17, HU16 CA-02/CA-04).

- Catalogo filtrable (`category`/`q`) sobre `repository.services`
  (solo lectura; < 2 s; extensible sin cambios estructurales).
- Registro de suscripciones (`POST /services/subscriptions`): valida
  el suministro contra el proveedor vigente (`mock` determinista por
  defecto, `E4-T03`-analogo) antes de persistir; **no mueve dinero**
  (el pago es `E4-T17`).
- Pago (`pay_service`) y recarga (`pay_topup`): validan el
  suministro/telefono contra el proveedor vigente **antes de cobrar**
  y mueven el dinero **solo** por el servicio interno
  `wallet.service.express.pay_express` (`E4-T02`, asiento
  `docs/04#7.10` sin comision: `Debe 2000-Pagador / Haber
  2100-Pagador` + `Debe 2100-Pagador / Haber 2000-Liquidacion`),
  persisten `service_payments`/`topups` con su `transaction_id`,
  devuelven el **comprobante estructurado** (`receipt_data` con el
  numero de transaccion oficial `OP-…`, convencion `E2-T10`) y emiten
  `service.paid`/`topup.completed` por `outbox`.

Sin logica en el router (05#1): el endpoint valida el esquema,
aqui la regla; el servicio hace `flush`, el endpoint confirma
(`commit`) o revierte. Sin PII en logs (regla 7): `supply_ref`/
`phone` solo correlacionados por hash (nunca en claro); el
comprobante y los eventos llevan referencias enmascaradas.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.service_provider import (
    ServiceProviderGateway,
    ServiceProviderTimeoutError,
    ServiceProviderUnavailableError,
    ServiceProviderValidationError,
    create_service_provider,
    hash_supply_ref,
    normalize_phone,
)
from app.core.events import SERVICE_PAID, TOPUP_COMPLETED
from app.modules.wallet.models import BILLER_CATEGORIES
from app.modules.wallet.repository import services as services_repo
from app.modules.wallet.service.express import pay_express

logger = logging.getLogger(__name__)

#: Filtros admitidos en `GET /services/catalog` (el catalogo crece
#: con filas; las categorias nuevas llegan sin migracion).
CATEGORY_RE = re.compile(r"^[A-Z_]{2,30}$")
MAX_QUERY_LEN = 80


class ServiceCatalogError(ValueError):
    """Error base del catalogo (-> 422 salvo mapeo especifico)."""

    code = "VALIDATION_ERROR"


class ServiceBillerNotFoundError(ServiceCatalogError, LookupError):
    """El `biller` no existe o esta deshabilitado (-> 404 neutro)."""

    code = "NOT_FOUND"


class ServiceProviderDownError(ServiceCatalogError):
    """Proveedor caido tras agotar reintentos (-> 503)."""

    code = "SERVICE_PROVIDER_UNAVAILABLE"


class ServiceProviderGatewayTimeoutError(ServiceCatalogError):
    """Timeout contra el proveedor tras agotar reintentos (-> 502)."""

    code = "SERVICE_PROVIDER_TIMEOUT"


class ServicePaymentError(ServiceCatalogError):
    """Error base del pago/recarga (-> 422 salvo mapeo especifico)."""


class ServiceSettlementUnavailableError(ServicePaymentError):
    """Cuenta de liquidacion del proveedor ausente/no viable (fail-closed).

    El pago/recarga no es responsabilidad del formulario del titular: si
    la cuenta de liquidacion no esta configurada o no esta disponible
    (`ACTIVE`/moneda), el servicio no puede operar. Se mapea a `503`
    `SETTLEMENT_UNAVAILABLE` (no a `422` del formulario) para que el
    cliente muestre un mensaje honesto ("servicio de pagos no disponible
    por ahora"); sin exponer detalles internos (regla de oro 7).
    """

    code = "SETTLEMENT_UNAVAILABLE"


class ServicePaymentConflictError(ServicePaymentError):
    """Misma clave con cuerpo distinto u operacion en curso (-> 409)."""

    code = "CONFLICT"


class TopupOperatorNotFoundError(ServicePaymentError, LookupError):
    """El operador no existe o no es de recarga (-> 404 neutro)."""

    code = "NOT_FOUND"


#: Endpoints logicos para la idempotencia del pago/recarga (clave
#: `shared`, UQ por (`key`, titular); patron `E2-T13`/`E4-T11`).
SERVICE_PAYMENT_IDEMPOTENCY_ENDPOINT = "/services/payments"
TOPUP_IDEMPOTENCY_ENDPOINT = "/topups"

#: Variable con la cuenta `accounts` interna de liquidacion del
#: proveedor (una por moneda; `docs/04#7.10`, `docs/18` G73): el
#: proveedor es externo (sin usuario ni cuenta en el core), asi que el
#: motor liquida contra esta cuenta de liquidacion por la fachada
#: `accounts` (sin columna nueva en `billers`; conciliacion posterior
#: por `docs/04#8`). Regla configurable fuera del codigo (regla 6).
SETTLEMENT_ACCOUNT_ENV_VAR = "WALLET_SERVICES_SETTLEMENT_ACCOUNT_ID"

#: Acciones de auditoria best-effort (sin evento nuevo: basta el outbox).
SERVICE_PAYMENT_AUDIT_ACTION = "service.payment.executed"
TOPUP_AUDIT_ACTION = "topup.executed"


def normalize_catalog_filters(
    category: str | None, query: str | None
) -> tuple[str | None, str | None]:
    """Normaliza y valida los filtros del catalogo (puros, sin BD).

    `category` malformada o `q` demasiado larga -> `ValueError`
    (422). Categoria bien formada pero desconocida -> se acepta y el
    repositorio devuelve lista vacia (sin oraculo de categorias).
    """
    normalized_category: str | None = None
    if category is not None:
        text = str(category).strip().upper()
        if not text or not CATEGORY_RE.match(text):
            raise ServiceCatalogError("filtro `category` invalido")
        normalized_category = text
    normalized_query: str | None = None
    if query is not None:
        text = str(query).strip()
        if len(text) > MAX_QUERY_LEN:
            raise ServiceCatalogError("filtro `q` demasiado largo")
        normalized_query = text or None
    return normalized_category, normalized_query


def to_biller_view(row) -> dict:
    """Vista publica del `biller` (catalogo; sin secretos)."""
    required = row.required_fields
    if not isinstance(required, list):
        required = []
    return {
        "biller_id": str(row.id),
        "category": row.category,
        "name": row.name,
        "required_fields": [str(field) for field in required],
    }


def list_catalog(
    session: Session,
    *,
    category: str | None = None,
    query: str | None = None,
) -> list[dict]:
    """Catalogo de `billers` habilitados filtrable (solo lectura)."""
    normalized_category, normalized_query = normalize_catalog_filters(category, query)
    rows = services_repo.list_billers(session, category=normalized_category, query=normalized_query)
    logger.debug(
        "catalogo listado category=%s matched=%d",
        normalized_category or "-",
        len(rows),
    )
    return [to_biller_view(row) for row in rows]


def to_subscription_view(row) -> dict:
    """Vista de la suscripcion (solo el titular dueno la ve)."""
    return {
        "subscription_id": str(row.id),
        "biller_id": str(row.biller_id),
        "supply_ref": row.supply_ref,
        "notify_days_before": row.notify_days_before,
        "status": row.status,
    }


def register_subscription(
    session: Session,
    user_id: uuid.UUID,
    *,
    biller_id: uuid.UUID,
    supply_ref: str,
    notify_days_before: int = 3,
    gateway: ServiceProviderGateway | None = None,
) -> tuple[dict, bool]:
    """Registra el servicio a recordar (base de CA-03; no mueve dinero).

    Valida el suministro contra el proveedor vigente antes de
    persistir (suministro invalido -> 422 neutro sin eco; proveedor
    caido -> 503; timeout -> 502). Idempotente por naturaleza: si ya
    existe la suscripcion `ACTIVE` la devuelve (`created=False`, sin
    duplicar). `biller` inexistente/deshabilitado -> 404 neutro. Solo
    `flush` (el endpoint confirma).
    """
    supply = str(supply_ref or "").strip()
    if not supply or len(supply) > 80:
        raise ServiceCatalogError("supply_ref invalido")
    if (
        isinstance(notify_days_before, bool)
        or not isinstance(notify_days_before, int)
        or not 1 <= notify_days_before <= 30
    ):
        raise ServiceCatalogError("notify_days_before debe estar entre 1 y 30")
    provider = gateway or create_service_provider()
    supply_hash = hash_supply_ref(supply)
    existing_biller = services_repo.get_biller(session, biller_id)
    if existing_biller is None or not existing_biller.enabled:
        raise ServiceBillerNotFoundError("servicio inexistente")
    try:
        validation = provider.validate_supply(biller_id=str(biller_id), supply_ref=supply)
    except ServiceProviderValidationError as exc:
        logger.info("suscripcion suministro invalido supply_hash=%s", supply_hash)
        raise ServiceCatalogError("suministro invalido para el servicio") from exc
    except ServiceProviderTimeoutError as exc:
        logger.warning("suscripcion timeout proveedor supply_hash=%s", supply_hash)
        raise ServiceProviderGatewayTimeoutError(
            "timeout contra el proveedor de servicios"
        ) from exc
    except ServiceProviderUnavailableError as exc:
        logger.warning("suscripcion proveedor caido supply_hash=%s", supply_hash)
        raise ServiceProviderDownError("proveedor de servicios no disponible") from exc
    if not validation.valid:
        logger.info("suscripcion suministro rechazado supply_hash=%s", supply_hash)
        raise ServiceCatalogError("suministro invalido para el servicio")
    try:
        row, created = services_repo.create_subscription(
            session,
            user_id=user_id,
            biller_id=biller_id,
            supply_ref=supply,
            notify_days_before=notify_days_before,
        )
    except services_repo.BillerNotFoundError as exc:
        raise ServiceBillerNotFoundError("servicio inexistente") from exc
    logger.debug("suscripcion registrada subscription_id=%s created=%s", row.id, created)
    return to_subscription_view(row), created


# ------------------------------------------------------- pago/recarga (E4-T17)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _require_key(idempotency_key: object) -> str:
    key = str(idempotency_key).strip() if isinstance(idempotency_key, str) else ""
    if not key:
        raise ServicePaymentError("cabecera Idempotency-Key obligatoria")
    if len(key) > 80:
        raise ServicePaymentError("Idempotency-Key supera 80 caracteres")
    return key


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ServicePaymentError(f"{field} debe ser UUID") from exc


def _check_money(amount_minor: object, currency: object) -> tuple[int, str]:
    """Dinero entero en centimos + moneda ISO (sin `float`, regla 3)."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise ServicePaymentError("amount_minor debe ser entero de centimos")
    if amount_minor <= 0:
        raise ServicePaymentError("amount_minor debe ser > 0")
    code = str(currency or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ServicePaymentError("currency debe ser ISO-4217 (3 letras)")
    return amount_minor, code


def _mask_tail(value: str, visible: int = 4) -> str:
    """Enmascara una referencia dejando solo la cola visible (sin PII)."""
    tail = re.sub(r"\s+", "", str(value or ""))[-visible:]
    return f"******{tail}" if tail else "******"


def build_receipt_no(transaction_id: uuid.UUID | str) -> str:
    """Numero de transaccion oficial del comprobante (decision 2, CA-04).

    Reutiliza la convencion del comprobante del motor (`E2-T10`,
    `docs/05#6.3`): `OP-` + 12 hex de `sha256(transaction_id)` en
    mayusculas. Derivado determinista del `transaction_id`: unico por
    operacion, estable entre reintentos y no secuencial/adivinable; no
    se persiste por separado (se deriva al emitir el comprobante).
    """
    digest = hashlib.sha256(str(transaction_id).encode("utf-8")).hexdigest()[:12].upper()
    return f"OP-{digest}"


def _build_service_receipt(
    *,
    transaction_id: uuid.UUID,
    biller_name: str,
    supply_ref: str,
    amount_minor: int,
    currency: str,
    now: datetime,
) -> dict:
    """Comprobante estructurado del pago (CA-04; sin PII en claro)."""
    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return {
        "receipt_no": build_receipt_no(transaction_id),
        "transaction_id": str(transaction_id),
        "concept": str(biller_name),
        "supply_masked": _mask_tail(supply_ref),
        "amount_minor": int(amount_minor),
        "currency": str(currency),
        "paid_at": moment.isoformat(),
        "status": "PAID",
    }


def _build_topup_receipt(
    *,
    transaction_id: uuid.UUID,
    operator: str,
    phone: str,
    amount_minor: int,
    currency: str,
    now: datetime,
) -> dict:
    """Comprobante estructurado de la recarga (CA-04; sin PII en claro)."""
    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return {
        "receipt_no": build_receipt_no(transaction_id),
        "transaction_id": str(transaction_id),
        "concept": f"Recarga {operator}",
        "phone_masked": _mask_tail(phone),
        "amount_minor": int(amount_minor),
        "currency": str(currency),
        "paid_at": moment.isoformat(),
        "status": "PAID",
    }


def _validate_supply_or_raise(
    provider: ServiceProviderGateway, *, biller_id: uuid.UUID, supply_ref: str
) -> None:
    """Valida el suministro contra el proveedor vigente **antes de cobrar**.

    Suministro invalido -> `ServicePaymentError` (422 neutro, sin eco);
    proveedor caido -> 503; timeout -> 502. Solo hash en logs (regla 7).
    """
    supply_hash = hash_supply_ref(supply_ref)
    try:
        validation = provider.validate_supply(biller_id=str(biller_id), supply_ref=supply_ref)
    except ServiceProviderValidationError as exc:
        logger.info("pago suministro invalido supply_hash=%s", supply_hash)
        raise ServicePaymentError("suministro invalido para el servicio") from exc
    except ServiceProviderTimeoutError as exc:
        logger.warning("pago timeout proveedor supply_hash=%s", supply_hash)
        raise ServiceProviderGatewayTimeoutError(
            "timeout contra el proveedor de servicios"
        ) from exc
    except ServiceProviderUnavailableError as exc:
        logger.warning("pago proveedor caido supply_hash=%s", supply_hash)
        raise ServiceProviderDownError("proveedor de servicios no disponible") from exc
    if not validation.valid:
        logger.info("pago suministro rechazado supply_hash=%s", supply_hash)
        raise ServicePaymentError("suministro invalido para el servicio")


def resolve_settlement_account(
    session: Session, currency: str, explicit: uuid.UUID | str | None = None
) -> uuid.UUID:
    """Cuenta de liquidacion del proveedor por la fachada `accounts` (G73).

    El proveedor es externo (sin usuario ni cuenta en el core): el
    motor liquida `Debe 2100-Pagador / Haber 2000-Liquidacion`
    (`docs/04#7.10`) contra esta cuenta interna (una por moneda). Se
    resuelve por parametro inyectado o por la variable
    `WALLET_SERVICES_SETTLEMENT_ACCOUNT_ID` (regla 6, sin columna
    nueva en `billers`): debe existir, estar `ACTIVE` y coincidir en
    moneda; otro caso -> `ServicePaymentError` neutro (fail-closed,
    sin mover dinero). Solo lectura.
    """
    from app.modules.accounts.service import get_account as accounts_get_account

    raw = str(explicit) if explicit is not None else os.environ.get(SETTLEMENT_ACCOUNT_ENV_VAR, "")
    try:
        wanted = uuid.UUID(str(raw).strip())
    except (ValueError, AttributeError, TypeError) as exc:
        logger.error("liquidacion de servicios sin cuenta configurada")
        raise ServiceSettlementUnavailableError("cuenta de liquidacion no configurada") from exc
    try:
        view = accounts_get_account(session, wanted)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ServiceSettlementUnavailableError("cuenta de liquidacion no disponible") from exc
    if (
        view is None
        or str(view.get("status")) != "ACTIVE"
        or str(view.get("currency") or "").upper() != str(currency).upper()
    ):
        raise ServiceSettlementUnavailableError("cuenta de liquidacion no disponible")
    return wanted


def resolve_topup_operator(session: Session, operator: object) -> Any:
    """Operador de recarga: `biller` `TELEFONIA` habilitado (404 neutro).

    Acepta el UUID del `biller` o su nombre exacto (insensible a
    mayusculas/espacios). Otra categoria, deshabilitado o inexistente
    -> `TopupOperatorNotFoundError` (sin oraculo). Solo lectura.
    """
    text = str(operator or "").strip()
    if not text:
        raise ServicePaymentError("operator invalido")
    row = None
    try:
        candidate = uuid.UUID(text)
    except (ValueError, AttributeError, TypeError):
        candidate = None
    if candidate is not None:
        row = services_repo.get_biller(session, candidate)
    else:
        folded = text.casefold()
        for item in services_repo.list_billers(session, category="TELEFONIA"):
            if str(item.name or "").strip().casefold() == folded:
                row = item
                break
    if row is None or not row.enabled or str(row.category) != "TELEFONIA":
        raise TopupOperatorNotFoundError("operador inexistente")
    return row


def _record_service_event(session: Session, payment: Any, event_type: str) -> None:
    """Registra `service.paid` en el outbox (payload sin PII).

    A1/HU22: incluye `user_id` (titular) para las alertas multicanal.
    """
    from app.modules.shared.repository.outbox import insert_event

    insert_event(
        session,
        aggregate_type="service_payment",
        aggregate_id=payment.id,
        event_type=event_type,
        payload={
            "service_payment_id": str(payment.id),
            "biller_id": str(payment.biller_id),
            "transaction_id": str(payment.transaction_id),
            "amount_minor": int(payment.amount_minor),
            "currency": str(payment.currency),
            "status": str(payment.status),
            "user_id": str(payment.user_id),
        },
    )


def _record_topup_event(session: Session, topup: Any) -> None:
    """Registra `topup.completed` en el outbox (payload sin PII).

    A1/HU22: incluye `user_id` (titular) para las alertas multicanal.
    """
    from app.modules.shared.repository.outbox import insert_event

    insert_event(
        session,
        aggregate_type="topup",
        aggregate_id=topup.id,
        event_type=TOPUP_COMPLETED,
        payload={
            "topup_id": str(topup.id),
            "operator": str(topup.operator),
            "transaction_id": str(topup.transaction_id),
            "amount_minor": int(topup.amount_minor),
            "currency": str(topup.currency),
            "status": str(topup.status),
            "user_id": str(topup.user_id),
        },
    )


def _audit_best_effort(
    session: Session, actor: uuid.UUID, action: str, entity: str, entity_id: uuid.UUID
) -> None:
    """Auditoria best-effort del pago/recarga (sin PII ni cifras)."""
    try:
        from app.modules.audit.service import record as audit_record

        audit_record(
            session,
            actor=actor,
            action=action,
            entity=entity,
            entity_id=entity_id,
            metadata={"status": "PAID"},
        )
    except Exception as exc:  # noqa: BLE001 - la auditoria no bloquea el pago
        logger.warning("service payment audit best-effort omitida err=%s", type(exc).__name__)


def _converge_pending_payment(session: Session, payment: Any, tx: Any, *, now: datetime) -> Any:
    """Converge un pago `PENDING` con el estado actual del motor.

    Si la operacion ya liquido (`SETTLED`, autorizada por
    `POST /transactions/{id}/authorize`) la finaliza: `PAID` +
    comprobante + `service.paid` (una sola vez). Si el motor la
    rechazo (bloqueo preventivo o diario al autorizar, G75) la
    espeja a `REJECTED` (sin evento ni comprobante, igual que el
    camino `REJECTED` directo). Otro estado -> intacta.
    """
    if str(payment.status) != "PENDING":
        return payment
    tx_status = str(getattr(tx, "status", "") or "")
    if tx_status == "SETTLED":
        if str(payment.transaction_id) != str(getattr(tx, "id", "")):
            raise ServicePaymentError("operacion ajena al pago")
        receipt = _build_service_receipt(
            transaction_id=tx.id,
            biller_name=str(payment.biller.name) if payment.biller is not None else "",
            supply_ref=str(payment.supply_ref),
            amount_minor=int(payment.amount_minor),
            currency=str(payment.currency),
            now=now,
        )
        services_repo.mark_service_payment_paid(session, payment, receipt_data=receipt)
        _record_service_event(session, payment, SERVICE_PAID)
        logger.info("service payment finalizado status=PAID")
        return payment
    if tx_status == "REJECTED":
        services_repo.set_service_payment_status(session, payment, "REJECTED")
        logger.info("service payment espejado status=REJECTED")
    return payment


def _converge_pending_topup(session: Session, topup: Any, tx: Any) -> Any:
    """Converge una recarga `PENDING` con el estado actual del motor.

    `SETTLED` -> `PAID` + `topup.completed` (una sola vez; el
    comprobante viaja en la respuesta, `topups` no persiste
    `receipt_data` por `03b#95`); `REJECTED` -> espejo a `REJECTED`.
    """
    if str(topup.status) != "PENDING":
        return topup
    tx_status = str(getattr(tx, "status", "") or "")
    if tx_status == "SETTLED":
        if str(topup.transaction_id) != str(getattr(tx, "id", "")):
            raise ServicePaymentError("operacion ajena a la recarga")
        services_repo.set_topup_status(session, topup, "PAID")
        _record_topup_event(session, topup)
        logger.info("topup finalizado status=PAID")
        return topup
    if tx_status == "REJECTED":
        services_repo.set_topup_status(session, topup, "REJECTED")
        logger.info("topup espejado status=REJECTED")
    return topup


def _replay_payment(
    session: Session, stored: Any, *, user_id: uuid.UUID, now: datetime
) -> tuple[Any, Any]:
    """Replay de idempotencia: devuelve el pago + la operacion vigentes."""
    from app.modules.transactions.service import find_by_idempotency_key

    snapshot = stored.response_snapshot
    if not isinstance(snapshot, dict):
        raise ServicePaymentConflictError("operacion en curso para esta Idempotency-Key")
    payment = services_repo.get_service_payment(session, snapshot.get("payment_id"))
    tx = find_by_idempotency_key(session, stored.key, initiator_user_id=user_id)
    if payment is None or tx is None:
        raise ServicePaymentConflictError("pago asociado a la clave ya no esta disponible")
    if str(tx.id) != str(snapshot.get("transaction_id")):
        raise ServicePaymentConflictError("pago asociado a la clave ya no esta disponible")
    payment = _converge_pending_payment(session, payment, tx, now=now)
    logger.info("service payment replay status=%s", payment.status)
    return payment, tx


def _replay_topup(session: Session, stored: Any, *, user_id: uuid.UUID) -> tuple[Any, Any]:
    """Replay de idempotencia: devuelve la recarga + la operacion vigentes."""
    from app.modules.transactions.service import find_by_idempotency_key

    snapshot = stored.response_snapshot
    if not isinstance(snapshot, dict):
        raise ServicePaymentConflictError("operacion en curso para esta Idempotency-Key")
    topup = services_repo.get_topup(session, snapshot.get("topup_id"))
    tx = find_by_idempotency_key(session, stored.key, initiator_user_id=user_id)
    if topup is None or tx is None:
        raise ServicePaymentConflictError("recarga asociada a la clave ya no esta disponible")
    if str(tx.id) != str(snapshot.get("transaction_id")):
        raise ServicePaymentConflictError("recarga asociada a la clave ya no esta disponible")
    topup = _converge_pending_topup(session, topup, tx)
    logger.info("topup replay status=%s", topup.status)
    return topup, tx


def pay_service(
    session: Session,
    user_id: uuid.UUID | str,
    *,
    biller_id: uuid.UUID | str,
    supply_ref: str,
    amount_minor: int,
    currency: str,
    link_id: uuid.UUID | str,
    idempotency_key: str,
    gateway: ServiceProviderGateway | None = None,
    settlement_account_id: uuid.UUID | str | None = None,
    now: datetime | None = None,
) -> tuple[Any, Any]:
    """Paga un servicio por el motor (`docs/04#7.10`, HU16 CA-02/CA-04).

    1. Valida monto/moneda en centimos (422) y el `biller`
       (inexistente/deshabilitado -> 404 neutro).
    2. Valida el suministro contra el proveedor vigente **antes de
       cobrar** (invalido -> 422 neutro; caido -> 503; timeout -> 502).
    3. Resuelve la cuenta de liquidacion del proveedor por la fachada
       `accounts` (sin ella -> 422 fail-closed, sin mover dinero).
    4. Mueve el dinero **solo** por `pay_express` (`E4-T02`, misma
       `Idempotency-Key`, sin comision: el gate del motor aplica
       base/diario; `CARD`/ajeno y conflictos los informa el express).
    5. Persiste `service_payments`: `SETTLED` -> `PAID` (+ comprobante
       `receipt_data` con `OP-…`, evento `service.paid` por `outbox`);
       `PENDING_AUTHORIZATION` -> `PENDING` (sin comprobante aun; el
       replay tras autorizar finaliza con `_converge_pending_payment`
       pasando `daily_limit_reader=wallet_daily_limit_reader`, G75);
       `REJECTED` -> `REJECTED` sin comprobante ni evento.
    6. Idempotencia por (`Idempotency-Key`, titular): misma clave +
       mismo cuerpo = replay (un solo pago); cuerpo distinto =
       `ServicePaymentConflictError` (409).

    Retorna `(ServicePayment, Transaction)`. Solo `flush` (el llamante
    confirma o revierte toda la operacion: todo o nada).
    """
    from app.modules.shared.repository.idempotency import (
        compute_request_hash,
        find_key,
        is_expired,
        renew_expired,
        resolve_ttl_seconds,
        store_response,
        try_claim,
    )
    from app.modules.transactions.service import find_by_idempotency_key

    owner = _coerce_uuid(user_id, "user_id")
    bid = _coerce_uuid(biller_id, "biller_id")
    link_ref = _coerce_uuid(link_id, "link_id")
    key = _require_key(idempotency_key)
    amount, code = _check_money(amount_minor, currency)
    supply = str(supply_ref or "").strip()
    if not supply or len(supply) > 80:
        raise ServicePaymentError("supply_ref invalido")
    moment = now if isinstance(now, datetime) else _utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    # Replay idempotente antes de tocar al proveedor o la cuenta de
    # liquidacion (docs/16#2.5, docs/05#6.5): misma clave + mismo
    # cuerpo = resultado ya liquidado, aunque el proveedor este caido
    # o la cuenta deje de estar configurada. Solo lectura; clave
    # nueva, cuerpo distinto o clave vencida siguen el camino normal
    # (validan fail-closed antes de cobrar).
    digest = compute_request_hash(
        {
            "user_id": str(owner),
            "biller_id": str(bid),
            "supply_ref": supply,
            "amount_minor": int(amount),
            "currency": code,
            "link_id": str(link_ref),
        }
    )
    _stored = find_key(session, key=key, user_id=owner)
    if _stored is not None and not is_expired(_stored) and _stored.request_hash == digest:
        return _replay_payment(session, _stored, user_id=owner, now=moment)

    biller = services_repo.get_biller(session, bid)
    if biller is None or not biller.enabled:
        raise ServiceBillerNotFoundError("servicio inexistente")
    provider = gateway or create_service_provider()
    _validate_supply_or_raise(provider, biller_id=bid, supply_ref=supply)
    target_account_id = resolve_settlement_account(session, code, explicit=settlement_account_id)

    row, created = try_claim(
        session,
        key=key,
        user_id=owner,
        endpoint=SERVICE_PAYMENT_IDEMPOTENCY_ENDPOINT,
        method="POST",
        request_hash=digest,
        ttl_seconds=resolve_ttl_seconds(),
    )
    if not created:
        stored = find_key(session, key=key, user_id=owner)
        if stored is None:  # pragma: no cover - carrera extrema
            raise ServicePaymentConflictError("clave de idempotencia en conflicto")
        if is_expired(stored):
            renew_expired(
                session,
                stored,
                request_hash=digest,
                endpoint=SERVICE_PAYMENT_IDEMPOTENCY_ENDPOINT,
                method="POST",
            )
            row = stored
            created = True
        elif stored.request_hash != digest:
            raise ServicePaymentConflictError("Idempotency-Key ya usada con un cuerpo distinto")
        else:
            return _replay_payment(session, stored, user_id=owner, now=moment)

    tx = pay_express(
        session,
        initiator_user_id=owner,
        link_id=link_ref,
        target_account_id=target_account_id,
        amount_minor=int(amount),
        currency=code,
        idempotency_key=key,
        fee_minor=0,
        description=f"service:{bid}",
    )
    # Defensa: el motor resuelve su propio replay por (clave, titular);
    # si colisiono con otra operacion, el cuerpo no coincide.
    existing_check = find_by_idempotency_key(session, key, initiator_user_id=owner)
    if existing_check is not None and existing_check.id != tx.id:  # pragma: no cover
        raise ServicePaymentConflictError("Idempotency-Key ya usada con un cuerpo distinto")

    status = str(tx.status)
    if status == "SETTLED":
        receipt = _build_service_receipt(
            transaction_id=tx.id,
            biller_name=str(biller.name),
            supply_ref=supply,
            amount_minor=int(amount),
            currency=code,
            now=moment,
        )
        payment = services_repo.create_service_payment(
            session,
            user_id=owner,
            biller_id=bid,
            supply_ref=supply,
            amount_minor=int(amount),
            currency=code,
            transaction_id=tx.id,
            status="PAID",
            receipt_data=receipt,
        )
        _record_service_event(session, payment, SERVICE_PAID)
        _audit_best_effort(
            session, owner, SERVICE_PAYMENT_AUDIT_ACTION, "service_payment", payment.id
        )
        logger.info("service payment liquidado status=PAID")
    elif status == "PENDING_AUTHORIZATION":
        payment = services_repo.create_service_payment(
            session,
            user_id=owner,
            biller_id=bid,
            supply_ref=supply,
            amount_minor=int(amount),
            currency=code,
            transaction_id=tx.id,
            status="PENDING",
        )
        _audit_best_effort(
            session, owner, SERVICE_PAYMENT_AUDIT_ACTION, "service_payment", payment.id
        )
        logger.info("service payment pendiente de autorizacion")
    else:
        payment = services_repo.create_service_payment(
            session,
            user_id=owner,
            biller_id=bid,
            supply_ref=supply,
            amount_minor=int(amount),
            currency=code,
            transaction_id=tx.id,
            status="REJECTED",
        )
        _audit_best_effort(
            session, owner, SERVICE_PAYMENT_AUDIT_ACTION, "service_payment", payment.id
        )
        logger.info("service payment rechazado status=%s", status)
    store_response(
        session,
        row,
        response_snapshot={
            "payment_id": str(payment.id),
            "transaction_id": str(tx.id),
            "status": str(payment.status),
        },
        transaction_id=tx.id,
    )
    return payment, tx


def pay_topup(
    session: Session,
    user_id: uuid.UUID | str,
    *,
    operator: str,
    phone: str,
    amount_minor: int,
    currency: str,
    link_id: uuid.UUID | str,
    idempotency_key: str,
    gateway: ServiceProviderGateway | None = None,
    settlement_account_id: uuid.UUID | str | None = None,
    now: datetime | None = None,
) -> tuple[Any, Any]:
    """Recarga celular por el motor (`docs/04#7.10`, HU16 CA-02/CA-04).

    Resuelve el operador contra los `billers` `TELEFONIA` habilitados
    (inexistente -> 404 neutro), valida el telefono con el adaptador
    `E4-T16` (`validate_supply`, mismo mapeo 422/502/503 que el pago)
    y mueve el dinero **solo** por `pay_express` (misma clave, sin
    comision). Persiste `topups` (`SETTLED` -> `PAID` + evento
    `topup.completed`; `PENDING_AUTHORIZATION` -> `PENDING`, el replay
    tras autorizar finaliza; `REJECTED` sin evento). Idempotente por
    (`Idempotency-Key`, titular). Retorna `(Topup, Transaction)`;
    solo `flush`.
    """
    from app.modules.shared.repository.idempotency import (
        compute_request_hash,
        find_key,
        is_expired,
        renew_expired,
        resolve_ttl_seconds,
        store_response,
        try_claim,
    )
    from app.modules.transactions.service import find_by_idempotency_key

    owner = _coerce_uuid(user_id, "user_id")
    link_ref = _coerce_uuid(link_id, "link_id")
    key = _require_key(idempotency_key)
    amount, code = _check_money(amount_minor, currency)
    moment = now if isinstance(now, datetime) else _utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    biller = resolve_topup_operator(session, operator)
    try:
        digits = normalize_phone(phone)
    except ServiceProviderValidationError as exc:
        logger.info("topup telefono invalido phone_hash=%s", hash_supply_ref(str(phone or "")))
        raise ServicePaymentError("telefono de recarga invalido") from exc
    # Replay idempotente antes de tocar al proveedor o la cuenta de
    # liquidacion (docs/16#2.5, docs/05#6.5): misma clave + mismo
    # cuerpo = resultado ya liquidado, aunque el proveedor este caido
    # o la cuenta deje de estar configurada. Solo lectura (resolucion
    # local del operador + normalizacion del telefono, sin red);
    # clave nueva, cuerpo distinto o clave vencida siguen el camino
    # normal (validan fail-closed antes de cobrar).
    digest = compute_request_hash(
        {
            "user_id": str(owner),
            "operator_biller_id": str(biller.id),
            "phone": digits,
            "amount_minor": int(amount),
            "currency": code,
            "link_id": str(link_ref),
        }
    )
    _stored = find_key(session, key=key, user_id=owner)
    if _stored is not None and not is_expired(_stored) and _stored.request_hash == digest:
        return _replay_topup(session, _stored, user_id=owner)
    provider = gateway or create_service_provider()
    _validate_supply_or_raise(provider, biller_id=biller.id, supply_ref=digits)
    target_account_id = resolve_settlement_account(session, code, explicit=settlement_account_id)

    row, created = try_claim(
        session,
        key=key,
        user_id=owner,
        endpoint=TOPUP_IDEMPOTENCY_ENDPOINT,
        method="POST",
        request_hash=digest,
        ttl_seconds=resolve_ttl_seconds(),
    )
    if not created:
        stored = find_key(session, key=key, user_id=owner)
        if stored is None:  # pragma: no cover - carrera extrema
            raise ServicePaymentConflictError("clave de idempotencia en conflicto")
        if is_expired(stored):
            renew_expired(
                session,
                stored,
                request_hash=digest,
                endpoint=TOPUP_IDEMPOTENCY_ENDPOINT,
                method="POST",
            )
            row = stored
            created = True
        elif stored.request_hash != digest:
            raise ServicePaymentConflictError("Idempotency-Key ya usada con un cuerpo distinto")
        else:
            return _replay_topup(session, stored, user_id=owner)

    tx = pay_express(
        session,
        initiator_user_id=owner,
        link_id=link_ref,
        target_account_id=target_account_id,
        amount_minor=int(amount),
        currency=code,
        idempotency_key=key,
        fee_minor=0,
        description=f"topup:{biller.id}",
    )
    existing_check = find_by_idempotency_key(session, key, initiator_user_id=owner)
    if existing_check is not None and existing_check.id != tx.id:  # pragma: no cover
        raise ServicePaymentConflictError("Idempotency-Key ya usada con un cuerpo distinto")

    status = str(tx.status)
    operator_name = str(biller.name)
    if status == "SETTLED":
        topup = services_repo.create_topup(
            session,
            user_id=owner,
            operator=operator_name,
            phone=digits,
            amount_minor=int(amount),
            currency=code,
            transaction_id=tx.id,
            status="PAID",
        )
        _record_topup_event(session, topup)
        _audit_best_effort(session, owner, TOPUP_AUDIT_ACTION, "topup", topup.id)
        logger.info("topup liquidado status=PAID")
    elif status == "PENDING_AUTHORIZATION":
        topup = services_repo.create_topup(
            session,
            user_id=owner,
            operator=operator_name,
            phone=digits,
            amount_minor=int(amount),
            currency=code,
            transaction_id=tx.id,
            status="PENDING",
        )
        _audit_best_effort(session, owner, TOPUP_AUDIT_ACTION, "topup", topup.id)
        logger.info("topup pendiente de autorizacion")
    else:
        topup = services_repo.create_topup(
            session,
            user_id=owner,
            operator=operator_name,
            phone=digits,
            amount_minor=int(amount),
            currency=code,
            transaction_id=tx.id,
            status="REJECTED",
        )
        _audit_best_effort(session, owner, TOPUP_AUDIT_ACTION, "topup", topup.id)
        logger.info("topup rechazado status=%s", status)
    store_response(
        session,
        row,
        response_snapshot={
            "topup_id": str(topup.id),
            "transaction_id": str(tx.id),
            "status": str(topup.status),
        },
        transaction_id=tx.id,
    )
    return topup, tx


def finalize_service_payment(
    session: Session,
    *,
    payment_id: uuid.UUID | str,
    transaction: Any,
    now: datetime | None = None,
) -> Any:
    """Finaliza un pago diferido tras la autorizacion (lo invoca el replay).

    La invoca el flujo de pago despues de autorizar la operacion del
    motor con `daily_limit_reader=wallet_daily_limit_reader` (G75):
    exige la `Transaction` ya en `SETTLED`, marca `service_payments`
    `PAID` (+ comprobante), emite `service.paid` por `outbox`, una
    sola vez. Idempotente: un pago ya `PAID` se retorna intacto. Solo
    `flush` (el llamante confirma).
    """
    try:
        pid = payment_id if isinstance(payment_id, uuid.UUID) else uuid.UUID(str(payment_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ServiceBillerNotFoundError("pago inexistente") from exc
    payment = services_repo.get_service_payment(session, pid)
    if payment is None:
        raise ServiceBillerNotFoundError("pago inexistente")
    if str(payment.status) == "PAID":
        logger.info("service payment finalize replay status=PAID")
        return payment
    if str(payment.status) != "PENDING":
        raise ServicePaymentError("el pago no espera autorizacion")
    moment = now if isinstance(now, datetime) else _utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return _converge_pending_payment(session, payment, transaction, now=moment)


def finalize_topup(
    session: Session,
    *,
    topup_id: uuid.UUID | str,
    transaction: Any,
) -> Any:
    """Finaliza una recarga diferida tras la autorizacion (la invoca el replay).

    Exige la `Transaction` ya en `SETTLED`, marca `topups` `PAID` y
    emite `topup.completed` por `outbox`, una sola vez. Idempotente:
    una recarga ya `PAID` se retorna intacta. Solo `flush`.
    """
    try:
        tid = topup_id if isinstance(topup_id, uuid.UUID) else uuid.UUID(str(topup_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise TopupOperatorNotFoundError("recarga inexistente") from exc
    topup = services_repo.get_topup(session, tid)
    if topup is None:
        raise TopupOperatorNotFoundError("recarga inexistente")
    if str(topup.status) == "PAID":
        logger.info("topup finalize replay status=PAID")
        return topup
    if str(topup.status) != "PENDING":
        raise ServicePaymentError("la recarga no espera autorizacion")
    return _converge_pending_topup(session, topup, transaction)


def to_topup_receipt(topup: Any, *, now: datetime | None = None) -> dict | None:
    """Comprobante estructurado de la recarga para la respuesta (CA-04).

    `topups` no persiste `receipt_data` (`03b#95`); el comprobante se
    deriva al responder (mismo `receipt_no` oficial `OP-…` y mismo
    enmascarado que el pago). Solo cuando la recarga esta `PAID`;
    otro estado -> `None`. Sin PII en claro.
    """
    if str(getattr(topup, "status", "")) != "PAID":
        return None
    moment = now if isinstance(now, datetime) else _utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return _build_topup_receipt(
        transaction_id=topup.transaction_id,
        operator=str(topup.operator),
        phone=str(topup.phone),
        amount_minor=int(topup.amount_minor),
        currency=str(topup.currency),
        now=moment,
    )


def notify_service_reminder(
    session: Session,
    *,
    owner: uuid.UUID | str,
    subscription_id: uuid.UUID | str,
    due_on: Any,
    sender: Any | None = None,
) -> Any:
    """Avisa el vencimiento proximo de un servicio por `notifications.send`.

    Ruta del aviso de recordatorio (HU16 CA-03, `docs/18` G77): plantilla
    `service_reminder` (push, sin PII ni cifras: solo la referencia del
    servicio —prefijo del `subscription_id`— y la fecha de vencimiento;
    el detalle vive en los servicios registrados, que solo consulta el
    titular). El disparador es el job de `E4-T18`
    (`wallet/jobs/service_reminders.py`, best-effort). Solo `flush` via
    la fachada (el llamante confirma).
    """
    from app.modules.notifications import service as notifications_service
    from app.modules.notifications.service import sender_for_channel

    sender_obj = sender if sender is not None else sender_for_channel("push")
    return notifications_service.send(
        session,
        channel="push",
        recipient=str(owner),
        template_code="service_reminder",
        data={"service_ref": str(subscription_id)[:8], "due_on": str(due_on)},
        user_id=owner,
        sender=sender_obj,
    )


__all__ = [
    "BILLER_CATEGORIES",
    "CATEGORY_RE",
    "MAX_QUERY_LEN",
    "SERVICE_PAYMENT_AUDIT_ACTION",
    "SERVICE_PAYMENT_IDEMPOTENCY_ENDPOINT",
    "SETTLEMENT_ACCOUNT_ENV_VAR",
    "TOPUP_AUDIT_ACTION",
    "TOPUP_IDEMPOTENCY_ENDPOINT",
    "ServiceBillerNotFoundError",
    "ServiceCatalogError",
    "ServicePaymentConflictError",
    "ServicePaymentError",
    "ServiceProviderDownError",
    "ServiceProviderGatewayTimeoutError",
    "ServiceSettlementUnavailableError",
    "TopupOperatorNotFoundError",
    "build_receipt_no",
    "finalize_service_payment",
    "finalize_topup",
    "list_catalog",
    "normalize_catalog_filters",
    "notify_service_reminder",
    "pay_service",
    "pay_topup",
    "register_subscription",
    "resolve_settlement_account",
    "resolve_topup_operator",
    "to_biller_view",
    "to_subscription_view",
    "to_topup_receipt",
]
