"""Endpoints del comercio para el QR de cobro (E4-T08, HU14).

- `POST /merchants/qr` (generar QR firmado con monto/concepto;
  `Idempotency-Key` opcional, patron `E2-T13`; solo comercio
  autenticado con cuenta `ACTIVE`) -> `201` con `payload` + firma +
  TTL.
- `GET /merchants/qr/{id}` (consulta de estado, solo el comercio
  dueno; `404` neutro si ajeno/inexistente) -> `200` con el estado
  efectivo (`EXPIRED` on-read si vencio, `E4-T07`).

Sin logica en el router (05#1): valida, traduce y delega al
`service/`; el servicio hace `flush`, el endpoint confirma
(`commit`) o revierte. **No mueve dinero** (el pago es HU15).
Respuestas segun 05#4 (`{"data", "meta"}`) y errores via `AppError`.
Sin PII/secretos en logs ni respuestas (regla 7/9): nunca el RUC en
claro ni el secreto HMAC (solo `id`s, `mode` y `replayed`).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.modules.wallet import service as wallet_service
from app.modules.wallet.api import get_current_user_id
from app.modules.wallet.schemas.qr import QrCreate, QrCreateResponse, QrStatusResponse

router = APIRouter(tags=["merchants-qr"])

logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


@router.post(
    "/merchants/qr",
    response_model=QrCreateResponse,
    status_code=201,
    summary="Genera un QR de cobro firmado del comercio",
    responses={
        201: {"description": "QR firmado persistido (`ACTIVE` + `expires_at`)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        403: {"description": "`NOT_AUTHORIZED` (sin cuenta de comercio `ACTIVE`)"},
        409: {"description": "`IDEMPOTENCY_CONFLICT` (misma clave + cuerpo distinto)"},
        422: {"description": "`VALIDATION_ERROR` (monto/moneda/modo/concepto)"},
    },
)
def create_merchant_qr(
    body: QrCreate,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Genera el QR de cobro del comercio (HU14 CA-01).

    Delega en `wallet.service.create_qr_charge_idempotent` (firma
    HMAC de `E4-T06` + `expires_at` desde `wallet.qr_ttl_seconds`).
    `Idempotency-Key` opcional (decision 1 del brief: no mueve
    dinero): misma clave + mismo cuerpo = replay (un solo cargo);
    cuerpo distinto = `409`. Emite `qr.charge.created` por `outbox`.
    """
    try:
        view, replayed = wallet_service.create_qr_charge_idempotent(
            db, user_id=user_id, body=body.model_dump(), idempotency_key=idempotency_key
        )
    except wallet_service.QrNotAuthorizedError as exc:
        db.rollback()
        raise AppError(code="NOT_AUTHORIZED", message=str(exc), status_code=403) from exc
    except wallet_service.QrIdempotencyConflictError as exc:
        db.rollback()
        raise AppError(code="IDEMPOTENCY_CONFLICT", message=str(exc), status_code=409) from exc
    except wallet_service.QrValidationError as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    db.commit()
    logger.debug("qr generado charge_id=%s replayed=%s", view["charge_id"], replayed)
    return {"data": view, "meta": {"request_id": _request_id(request), "replayed": replayed}}


@router.get(
    "/merchants/qr/{charge_id}",
    response_model=QrStatusResponse,
    summary="Consulta el estado del QR del comercio",
    responses={
        200: {"description": "Estado del cargo (`ACTIVE`/`EXPIRED` on-read/`PAID`/…)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        403: {"description": "`NOT_AUTHORIZED` (sin cuenta de comercio `ACTIVE`)"},
        404: {"description": "`NOT_FOUND` (ajeno/inexistente, neutro)"},
    },
)
def get_merchant_qr_status(
    charge_id: str,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Consulta el estado del cargo (HU14 CA-02 parcial).

    Solo el comercio dueno: ajeno/inexistente/id malformado ->
    `404 NOT_FOUND` neutro (sin montos, anti-enumeracion). El
    `status` efectivo recalcula la caducidad on-read (`E4-T07`):
    `ACTIVE` vencido se reporta `EXPIRED` sin esperar al job.
    """
    try:
        view = wallet_service.get_charge_status(db, user_id=user_id, charge_id=charge_id)
    except wallet_service.QrNotAuthorizedError as exc:
        db.rollback()
        raise AppError(code="NOT_AUTHORIZED", message=str(exc), status_code=403) from exc
    except wallet_service.QrNotFoundError as exc:
        db.rollback()
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except wallet_service.QrValidationError as exc:
        db.rollback()
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    return {"data": view, "meta": {"request_id": _request_id(request)}}


__all__ = ["router"]
