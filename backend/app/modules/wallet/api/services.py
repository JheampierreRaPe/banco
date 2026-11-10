"""Endpoints del catalogo y el pago de servicios (E4-T16/E4-T17, HU16).

- `GET /services/catalog` (catalogo filtrable por `category`/`q`;
  solo lectura) -> `200` con los `billers` habilitados y sus
  `required_fields` (extensible sin cambios estructurales).
- `POST /services/subscriptions` (registrar un servicio a recordar;
  lo consume el job de `E4-T18`) -> `201` (replay por naturaleza:
  re-registrar devuelve la fila existente con `replayed: true`).
- `POST /services/payments` (`E4-T17`, HU16 CA-02/CA-04): paga un
  servicio por el motor (`Idempotency-Key` obligatoria, solo el
  titular) -> `201` con el pago + comprobante (`receipt` con
  `receipt_no` oficial `OP-…`).
- `POST /topups` (`E4-T17`, HU16 CA-02/CA-04): recarga celular por el
  motor (`Idempotency-Key` obligatoria, solo el titular) -> `201`
  con la recarga + comprobante.

Sin logica en el router (05#1): valida, traduce y delega al
`service/`; el servicio hace `flush`, el endpoint confirma
(`commit`) o revierte. Respuestas segun 05#4 (`{"data", "meta"}`) y
errores via `AppError`. Sin PII/secretos en logs ni respuestas fuera
de lo necesario (regla 7/9): el suministro/telefono solo viaja hacia
el titular dueno (enmascarado en el comprobante).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.modules.shared.repository.idempotency import find_key, is_expired
from app.modules.transactions.service import biometric as biometric_service
from app.modules.wallet import service as wallet_service
from app.modules.wallet.api import get_current_user_id
from app.modules.wallet.schemas.services import (
    CatalogResponse,
    ServicePaymentCreate,
    ServicePaymentResponse,
    SubscriptionCreate,
    SubscriptionResponse,
    TopupCreate,
    TopupResponse,
)

router = APIRouter(tags=["services"])

logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


def _require_idempotency_key(request_key: str | None, db: Session) -> str:
    """`Idempotency-Key` obligatoria (mueve dinero, regla 5)."""
    key = str(request_key or "").strip()
    if not key:
        db.rollback()
        raise AppError(
            code="VALIDATION_ERROR",
            message="cabecera Idempotency-Key obligatoria",
            status_code=422,
        )
    if len(key) > 80:
        db.rollback()
        raise AppError(
            code="VALIDATION_ERROR",
            message="Idempotency-Key supera 80 caracteres",
            status_code=422,
        )
    return key


def _rejected_failure_code(tx) -> str | None:
    """Codigo de rechazo de negocio (patron `E2-T07`).

    `DAILY_LIMIT_EXCEEDED` y `BIOMETRIC_PREVENTIVE_BLOCK` viajan en
    `failure_reason`/`meta.failure_code`; cualquier otro `REJECTED`
    (fondos) es `INSUFFICIENT_FUNDS`. Fuera de `REJECTED` es `None`.
    Sin PII: solo el codigo.
    """
    if getattr(tx, "status", None) != "REJECTED":
        return None
    known = {"DAILY_LIMIT_EXCEEDED", "BIOMETRIC_PREVENTIVE_BLOCK"}
    reason = getattr(tx, "failure_reason", None)
    if isinstance(reason, str) and reason in known:
        return reason
    meta = getattr(tx, "meta", None)
    if isinstance(meta, dict):
        code = meta.get("failure_code")
        if isinstance(code, str) and code in known:
            return code
    return "INSUFFICIENT_FUNDS"


def _authorization_challenge(tx) -> dict | None:
    """Reto del motor para `PENDING_AUTHORIZATION` (patron `E2-T19`).

    Solo con reto vigente: `{"method": "BIOMETRIC", "nonce",
    "expires_at"}` (el `authorize` admite firma o PIN, `E2-T31`). En
    otro estado (o sin reto) es `None`. El `nonce` nunca se loguea.
    """
    if getattr(tx, "status", None) != "PENDING_AUTHORIZATION":
        return None
    try:
        block = biometric_service.get_block(tx)
    except Exception:  # noqa: BLE001 - best-effort: sin reto visible
        return None
    if not isinstance(block, dict) or not block.get("nonce"):
        return None
    expires_at = block.get("expires_at")
    return {
        "method": "BIOMETRIC",
        "nonce": str(block.get("nonce")),
        "expires_at": str(expires_at) if expires_at is not None else None,
    }


def _row_status(tx) -> str:
    """Estado del motor para la respuesta (`SETTLED`/`PENDING_AUTHORIZATION`/`REJECTED`)."""
    status = str(getattr(tx, "status", "") or "")
    if status in ("SETTLED", "PENDING_AUTHORIZATION", "REJECTED"):
        return status
    return "REJECTED"


def _payment_errors(exc: Exception, db: Session) -> AppError:
    """Mapea errores del servicio a `AppError` (sin PII en mensajes)."""
    db.rollback()
    if isinstance(
        exc,
        (
            wallet_service.ServiceBillerNotFoundError,
            wallet_service.TopupOperatorNotFoundError,
        ),
    ):
        return AppError(code="NOT_FOUND", message=str(exc), status_code=404)
    if isinstance(
        exc,
        (
            wallet_service.ServicePaymentConflictError,
            wallet_service.ExpressConflictError,
        ),
    ):
        return AppError(code="CONFLICT", message=str(exc), status_code=409)
    if isinstance(exc, wallet_service.ExpressNotFoundError):
        return AppError(code="NOT_FOUND", message=str(exc), status_code=404)
    if isinstance(exc, wallet_service.ServiceProviderDownError):
        return AppError(code="SERVICE_PROVIDER_UNAVAILABLE", message=str(exc), status_code=503)
    if isinstance(exc, wallet_service.ServiceProviderGatewayTimeoutError):
        return AppError(code="SERVICE_PROVIDER_TIMEOUT", message=str(exc), status_code=502)
    if isinstance(exc, wallet_service.ServiceSettlementUnavailableError):
        # Configuracion de liquidacion del proveedor ausente/no viable: no es
        # un error de formulario (422). Mensaje neutro, sin detalles internos
        # (regla de oro 7): el frontend lo traduce a "servicio de pagos no
        # disponible por ahora".
        return AppError(
            code="SETTLEMENT_UNAVAILABLE",
            message="servicio de pagos no disponible",
            status_code=503,
        )
    return AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422)


@router.get(
    "/services/catalog",
    response_model=CatalogResponse,
    summary="Catalogo de servicios y operadores",
    responses={
        200: {"description": "Billers habilitados (filtrable por categoria/nombre)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        422: {"description": "`VALIDATION_ERROR` (filtro invalido)"},
    },
)
def get_services_catalog(
    request: Request,
    category: str | None = None,
    q: str | None = None,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Lista los `billers` habilitados (HU16 CA-01).

    Filtros opcionales: `category` (p. ej. `ELECTRICIDAD`,
    `TELEFONIA`) y `q` (subcadena de nombre/categoria). Solo
    lectura y < 2 s; el catalogo se amplia con filas (sin
    migracion). `user_id` solo autentica (el catalogo es publico
    para titulares).
    """
    _ = user_id
    try:
        data = wallet_service.list_catalog(db, category=category, query=q)
    except (ValueError, TypeError) as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    return {"data": data, "meta": {"request_id": _request_id(request)}}


@router.post(
    "/services/subscriptions",
    response_model=SubscriptionResponse,
    status_code=201,
    summary="Registra un servicio a recordar",
    responses={
        201: {"description": "Suscripcion registrada (base de recordatorios `E4-T18`)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (biller inexistente/deshabilitado, neutro)"},
        422: {"description": "`VALIDATION_ERROR` (suministro/campos invalidos)"},
        502: {"description": "`SERVICE_PROVIDER_TIMEOUT` (timeout del proveedor)"},
        503: {"description": "`SERVICE_PROVIDER_UNAVAILABLE` (proveedor caido)"},
    },
)
def create_service_subscription(
    body: SubscriptionCreate,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Registra el servicio del titular para recordatorios (HU16 CA-03 base).

    Valida el suministro contra el proveedor vigente (`mock`
    determinista por defecto) antes de persistir; **no mueve dinero**.
    Idempotente por naturaleza: re-registrar el mismo `biller`+
    suministro devuelve la fila existente (`replayed: true`).
    """
    try:
        view, created = wallet_service.register_subscription(
            db,
            user_id,
            biller_id=body.biller_id,
            supply_ref=body.supply_ref,
            notify_days_before=body.notify_days_before,
        )
    except wallet_service.ServiceBillerNotFoundError as exc:
        db.rollback()
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except wallet_service.ServiceProviderDownError as exc:
        db.rollback()
        raise AppError(
            code="SERVICE_PROVIDER_UNAVAILABLE", message=str(exc), status_code=503
        ) from exc
    except wallet_service.ServiceProviderGatewayTimeoutError as exc:
        db.rollback()
        raise AppError(code="SERVICE_PROVIDER_TIMEOUT", message=str(exc), status_code=502) from exc
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    db.commit()
    return {
        "data": view,
        "meta": {"request_id": _request_id(request), "replayed": not created},
    }


@router.post(
    "/services/payments",
    response_model=ServicePaymentResponse,
    status_code=201,
    summary="Paga un servicio por el motor (HU16)",
    responses={
        201: {"description": "Resultado del pago (`SETTLED`/pendiente/`REJECTED` + comprobante)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (biller/vinculo ajeno o inexistente, neutro)"},
        409: {"description": "`CONFLICT` (`Idempotency-Key` con cuerpo distinto)"},
        422: {"description": "`VALIDATION_ERROR` (sin clave, suministro/monto invalidos)"},
        502: {"description": "`SERVICE_PROVIDER_TIMEOUT` (timeout del proveedor)"},
        503: {"description": "`SERVICE_PROVIDER_UNAVAILABLE` (proveedor caido)"},
    },
)
def pay_service_endpoint(
    body: ServicePaymentCreate,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Paga un servicio con una fuente vinculada (HU16 CA-02/CA-04).

    Valida el suministro contra el proveedor vigente **antes de
    cobrar** y mueve el dinero **solo** por el motor (`pay_express`,
    `docs/04#7.10` sin comision): `<=` limite base -> `201` `SETTLED`
    con comprobante (`receipt {receipt_no `OP-…`, concept,
    supply_masked, amount_minor, currency, paid_at}`); `>` base ->
    `201` `PENDING_AUTHORIZATION` + reto (se confirma con
    `POST /transactions/{id}/authorize`, firma o PIN; el reintento
    con la misma clave finaliza el pago); diario/fondos -> `201`
    `REJECTED` + `failure_code` sin tocar saldos. `Idempotency-Key`
    obligatoria: misma clave + mismo cuerpo = replay
    (`replayed: true`, un solo pago); cuerpo distinto = `409`.
    """
    key = _require_idempotency_key(idempotency_key, db)
    try:
        pre = find_key(db, key=key, user_id=user_id)
        payment, tx = wallet_service.pay_service(
            db,
            user_id,
            biller_id=body.biller_id,
            supply_ref=body.supply_ref,
            amount_minor=body.amount_minor,
            currency=body.currency,
            link_id=body.link_id,
            idempotency_key=key,
        )
        status = _row_status(tx)
        data = {
            "payment_id": str(payment.id),
            "transaction_id": str(payment.transaction_id),
            "status": status,
            "amount_minor": int(payment.amount_minor),
            "currency": str(payment.currency),
            "receipt": dict(payment.receipt_data) if payment.receipt_data else None,
            "failure_code": _rejected_failure_code(tx),
            "authorization": _authorization_challenge(tx),
        }
        replayed = (
            pre is not None and not is_expired(pre) and isinstance(pre.response_snapshot, dict)
        )
    except (
        wallet_service.ServiceBillerNotFoundError,
        wallet_service.TopupOperatorNotFoundError,
        wallet_service.ServicePaymentConflictError,
        wallet_service.ServicePaymentError,
        wallet_service.ServiceCatalogError,
        wallet_service.ExpressConflictError,
        wallet_service.ExpressNotFoundError,
        wallet_service.ExpressValidationError,
        wallet_service.ExpressError,
        ValueError,
        TypeError,
    ) as exc:
        raise _payment_errors(exc, db) from exc
    db.commit()
    logger.info("service pagado status=%s replayed=%s", data["status"], replayed)
    return {"data": data, "meta": {"request_id": _request_id(request), "replayed": replayed}}


@router.post(
    "/topups",
    response_model=TopupResponse,
    status_code=201,
    summary="Recarga celular por el motor (HU16)",
    responses={
        201: {
            "description": "Resultado de la recarga (`SETTLED`/pendiente/`REJECTED` + comprobante)"
        },
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (operador/vinculo ajeno o inexistente, neutro)"},
        409: {"description": "`CONFLICT` (`Idempotency-Key` con cuerpo distinto)"},
        422: {"description": "`VALIDATION_ERROR` (sin clave, telefono/monto invalidos)"},
        502: {"description": "`SERVICE_PROVIDER_TIMEOUT` (timeout del proveedor)"},
        503: {"description": "`SERVICE_PROVIDER_UNAVAILABLE` (proveedor caido)"},
    },
)
def pay_topup_endpoint(
    body: TopupCreate,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Recarga celular con una fuente vinculada (HU16 CA-02/CA-04).

    Resuelve el operador contra los `billers` `TELEFONIA` habilitados,
    valida el telefono con el adaptador `E4-T16` antes de cobrar y
    mueve el dinero **solo** por el motor (`pay_express`,
    `docs/04#7.10` sin comision). Respuesta y errores con la misma
    semantica que `POST /services/payments` (comprobante con
    `receipt_no` oficial `OP-…` cuando liquida; `failure_code` o reto
    en otro caso). `Idempotency-Key` obligatoria.
    """
    key = _require_idempotency_key(idempotency_key, db)
    try:
        pre = find_key(db, key=key, user_id=user_id)
        topup, tx = wallet_service.pay_topup(
            db,
            user_id,
            operator=body.operator,
            phone=body.phone,
            amount_minor=body.amount_minor,
            currency=body.currency,
            link_id=body.link_id,
            idempotency_key=key,
        )
        status = _row_status(tx)
        data = {
            "topup_id": str(topup.id),
            "transaction_id": str(topup.transaction_id),
            "status": status,
            "amount_minor": int(topup.amount_minor),
            "currency": str(topup.currency),
            "receipt": wallet_service.to_topup_receipt(topup),
            "failure_code": _rejected_failure_code(tx),
            "authorization": _authorization_challenge(tx),
        }
        replayed = (
            pre is not None and not is_expired(pre) and isinstance(pre.response_snapshot, dict)
        )
    except (
        wallet_service.ServiceBillerNotFoundError,
        wallet_service.TopupOperatorNotFoundError,
        wallet_service.ServicePaymentConflictError,
        wallet_service.ServicePaymentError,
        wallet_service.ServiceCatalogError,
        wallet_service.ExpressConflictError,
        wallet_service.ExpressNotFoundError,
        wallet_service.ExpressValidationError,
        wallet_service.ExpressError,
        ValueError,
        TypeError,
    ) as exc:
        raise _payment_errors(exc, db) from exc
    db.commit()
    logger.info("topup pagado status=%s replayed=%s", data["status"], replayed)
    return {"data": data, "meta": {"request_id": _request_id(request), "replayed": replayed}}


__all__ = ["router"]
