"""Endpoint de pago QR del pagador (E4-T12, HU15 CA-01..CA-04).

- `POST /payments/qr` (Bearer, `Idempotency-Key` obligatoria, solo el
  titular pagador): recibe el `qr_content` escaneado + el vinculo de
  pago y, en una sola llamada, **decodifica/valida** el QR y **paga por
  el motor** delegando en el servicio de `E4-T11`
  (`wallet.service.pay_qr_charge`; sin duplicar logica), con
  **autorizacion biometrica condicional**: dentro del limite base el
  pago se ejecuta **en un toque** (`SETTLED`); por encima queda
  `PENDING_AUTHORIZATION` con el reto del motor (patron
  `_challenge_fields`, `E2-T19`) para confirmar con biometria/PIN en
  `POST /transactions/{id}/authorize`; sobre el diario/fondos queda
  `REJECTED` con `failure_code` (patron `E2-T07`), sin tocar saldos.
- **Finalizacion del pago diferido** (`docs/18` G72 lado endpoint):
  si el pago estaba `PENDING_AUTHORIZATION` y su operacion del motor
  ya esta liquidada (`SETTLED`, autorizada por
  `POST /transactions/{id}/authorize`), el endpoint invoca el helper
  de `E4-T11` (`finalize_qr_payment`) que marca `qr_payments`/
  `qr_charges` `PAID` y dispara el aviso de abono `qr_charge_paid`
  (una sola vez). Asi un reintento con la misma `Idempotency-Key`
  tras autorizar devuelve el cobro cerrado E2E. Si la operacion quedo
  `REJECTED` (bloqueo preventivo o limite diario al autorizar), el
  pago pendiente se espeja a `REJECTED` con su `failure_code` (sin
  evento ni aviso, igual que el camino `REJECTED` del servicio).
- El re-chequeo del limite diario al autorizar usa
  `wallet_daily_limit_reader` (G75): el servicio lo pasa al crear
  (`pay_express`) y la finalizacion/autorizacion diferida lo recibe
  en el flujo de billetera (`finalize_qr_payment` + tests con
  `daily_limit_reader=wallet_daily_limit_reader`); el dispatch por
  `metadata` en `POST /transactions/{id}/authorize` queda como
  decision abierta del dueno (ver `docs/18` G75).

Sin logica en el router (05#1): valida, traduce y delega al
`service/`; el servicio hace `flush`, el endpoint confirma
(`commit`) o revierte. Respuestas segun 05#4 (`{"data", "meta"}`) y
errores via `AppError`. Sin PII/secretos en logs ni respuestas
(regla 7/9): nunca el RUC en claro ni el secreto HMAC (solo `id`s,
`replayed` y el nombre publico del comercio).
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
from app.modules.wallet.repository import qr as qr_repo
from app.modules.wallet.repository import qr_payment as qr_payment_repo
from app.modules.wallet.schemas.payment import (
    QrPayRequest,
    QrPayResponse,
    QrPreviewRequest,
    QrPreviewResponse,
)

router = APIRouter(tags=["payments-qr"])

logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


def _rejected_failure_code(tx) -> str | None:
    """Codigo de rechazo de negocio (patron `E2-T07`).

    `DAILY_LIMIT_EXCEEDED` (CA-03) y `BIOMETRIC_PREVENTIVE_BLOCK`
    (CA-02) viajan en `failure_reason`/`meta.failure_code`; cualquier
    otro `REJECTED` (fondos) es `INSUFFICIENT_FUNDS`. Fuera de
    `REJECTED` es `None`. Sin PII: solo el codigo.
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


def _pay_data(session: Session, payment, tx) -> dict:
    """Serializa el cobro al `data` de `QrPayResponse` (solo `id`s)."""
    merchant_name = ""
    try:
        merchant = qr_repo.get_merchant(session, payment.merchant_id)
    except Exception:  # noqa: BLE001 - best-effort: el nombre es presentacion
        merchant = None
    if merchant is not None and isinstance(getattr(merchant, "business_name", None), str):
        merchant_name = merchant.business_name
    status = str(payment.status)
    if status == "PAID":
        # Cobro liquidado: la operacion del motor esta `SETTLED` (el
        # `PAID` es el estado del `qr_payment`, no del contrato).
        status = "SETTLED"
    data = {
        "transaction_id": str(payment.transaction_id),
        "qr_payment_id": str(payment.id),
        "charge_id": str(payment.charge_id),
        "merchant": {
            "merchant_id": str(payment.merchant_id),
            "business_name": merchant_name,
        },
        "amount_minor": int(payment.amount_minor),
        "currency": str(payment.currency),
        "status": status,
        "failure_code": None,
        "authorization": None,
    }
    if status == "PENDING_AUTHORIZATION":
        data["authorization"] = _authorization_challenge(tx)
    elif status == "REJECTED":
        data["failure_code"] = _rejected_failure_code(tx)
    return data


@router.post(
    "/payments/qr/preview",
    response_model=QrPreviewResponse,
    status_code=200,
    summary="Previsualiza un QR antes de confirmar (HU15 CA-01)",
    responses={
        200: {"description": "Comercio y monto del cargo (sin mover dinero)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        409: {"description": "`CONFLICT` (QR ya pagado)"},
        422: {"description": "`VALIDATION_ERROR` (QR corrupto/manipulado/vencido/ajeno)"},
    },
)
def preview_qr(
    body: QrPreviewRequest,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Muestra comercio y monto antes de confirmar (consentimiento, HU15 CA-01).

    Delega en `wallet.service.preview_qr_charge` (decodifica `BO-QR1`,
    verifica firma/hash/caducidad en **solo lectura**: sin retencion,
    sin asiento, sin pagos ni eventos; sin `Idempotency-Key` porque no
    mueve dinero). Vencido/corrupto/manipulado -> `422` sin efectos;
    ya cobrado -> `409`. La respuesta nunca incluye el `qr_content`
    crudo, la firma, el secreto ni el RUC (solo el nombre publico del
    comercio, como `POST /payments/qr`).
    """
    _ = user_id  # titular autenticado (el preview no filtra por titular: es lectura neutra)
    try:
        data = wallet_service.preview_qr_charge(db, qr_content=body.qr_content)
    except wallet_service.QrAlreadyPaidError as exc:
        db.rollback()
        raise AppError(code="CONFLICT", message=str(exc), status_code=409) from exc
    except (
        wallet_service.InvalidQrError,
        wallet_service.QrExpiredError,
        wallet_service.QrMerchantUnknownError,
        wallet_service.ExpressValidationError,
    ) as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    logger.debug("qr preview mostrado")
    return {"data": data, "meta": {"request_id": _request_id(request)}}


@router.post(
    "/payments/qr",
    response_model=QrPayResponse,
    status_code=201,
    summary="Paga un QR escaneado por el motor (HU15)",
    responses={
        201: {"description": "Resultado del pago (`SETTLED`/`PENDING_AUTHORIZATION`/`REJECTED`)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (vinculo ajeno/inexistente, neutro)"},
        409: {"description": "`CONFLICT` (QR ya pagado / `Idempotency-Key` con cuerpo distinto)"},
        422: {
            "description": "`VALIDATION_ERROR` (sin clave, QR corrupto/manipulado/vencido/ajeno)"
        },
    },
)
def pay_qr(
    body: QrPayRequest,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Paga el QR en un toque o deja el reto biometrico (HU15 CA-01..CA-04).

    Delega en `wallet.service.pay_qr_charge` (decodifica `BO-QR1`,
    verifica firma/hash/caducidad **antes de operar** y mueve el
    dinero **solo** por `pay_express`/motor, `docs/04#7.4`):
    `<=` limite base -> `201` `SETTLED` sin biometria adicional;
    `>` base -> `201` `PENDING_AUTHORIZATION` + reto (el cliente
    confirma con `POST /transactions/{id}/authorize`); vencido/
    corrupto/manipulado -> `422` sin descontar fondos; ya pagado ->
    `409`. `Idempotency-Key` obligatoria (mueve dinero): misma clave
    + mismo cuerpo = replay (`replayed: true`, un solo pago); cuerpo
    distinto = `409`. Si el pago estaba pendiente y su operacion ya
    liquido, se finaliza aqui (`finalize_qr_payment`: cargo `PAID` +
    aviso `qr_charge_paid`, una sola vez; `docs/18` G72).
    """
    key = str(idempotency_key or "").strip()
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
    try:
        pre = find_key(db, key=key, user_id=user_id)
        payment, tx = wallet_service.pay_qr_charge(
            db,
            payer_user_id=user_id,
            qr_content=body.qr_content,
            link_id=body.link_id,
            idempotency_key=key,
        )
        if str(payment.status) == "PENDING_AUTHORIZATION" and str(tx.status) == "SETTLED":
            payment = wallet_service.finalize_qr_payment(
                db, qr_payment_id=payment.id, transaction=tx
            )
        elif str(payment.status) == "PENDING_AUTHORIZATION" and str(tx.status) == "REJECTED":
            payment = qr_payment_repo.set_status(db, payment, "REJECTED")
        data = _pay_data(db, payment, tx)
        replayed = (
            pre is not None and not is_expired(pre) and isinstance(pre.response_snapshot, dict)
        )
    except wallet_service.QrAlreadyPaidError as exc:
        db.rollback()
        raise AppError(code="CONFLICT", message=str(exc), status_code=409) from exc
    except (
        wallet_service.QrPaymentConflictError,
        wallet_service.ExpressConflictError,
    ) as exc:
        db.rollback()
        raise AppError(code="CONFLICT", message=str(exc), status_code=409) from exc
    except wallet_service.ExpressNotFoundError as exc:
        db.rollback()
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except (
        wallet_service.InvalidQrError,
        wallet_service.QrExpiredError,
        wallet_service.QrMerchantUnknownError,
        wallet_service.ExpressValidationError,
    ) as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    db.commit()
    logger.info("qr pagado status=%s replayed=%s", data["status"], replayed)
    return {"data": data, "meta": {"request_id": _request_id(request), "replayed": replayed}}


__all__ = ["router"]
