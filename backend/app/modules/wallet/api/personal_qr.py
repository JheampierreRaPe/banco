"""Endpoints del QR personal estatico (E4-T21, HU14).

- `POST /wallet/qr` (generar QR personal sin monto ni caducidad;
  `Idempotency-Key` opcional, no mueve dinero; cualquier titular
  autenticado con cuenta `ACTIVE`, sin rol comercial) -> `201` con
  `payload` + firma + sobre `BO-QR1`.
- `GET /wallet/qr/{id}` (estado, solo el titular dueno; ajeno/
  inexistente/de comercio -> `404` neutro) -> `200`.

Sin logica en el router (05#1): valida, traduce y delega al
`service/`; el servicio hace `flush`, el endpoint confirma
(`commit`) o revierte. **No mueve dinero** (el pago es
`POST /payments/qr` con el monto digitado por el pagador).
Respuestas segun 05#4 (`{"data", "meta"}`) y errores via `AppError`.
Sin PII/secretos en logs ni respuestas (regla 7/9).
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
from app.modules.wallet.schemas.personal_qr import (
    PersonalQrCreate,
    PersonalQrCreateResponse,
    PersonalQrStatusResponse,
)

router = APIRouter(tags=["wallet-qr"])

logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


@router.post(
    "/wallet/qr",
    response_model=PersonalQrCreateResponse,
    status_code=201,
    summary="Genera el QR personal estatico del titular",
    responses={
        201: {"description": "QR personal firmado (`ACTIVE`, sin monto ni caducidad)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        409: {"description": "`IDEMPOTENCY_CONFLICT` (misma clave + cuerpo distinto)"},
        422: {"description": "`VALIDATION_ERROR` (moneda/concepto o sin cuenta de abono)"},
    },
)
def create_personal_qr(
    body: PersonalQrCreate,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Genera el QR personal estatico del titular (HU14, E4-T21 CA-01/CA-02).

    Sin rol comercial: basta una cuenta `ACTIVE` (la de abono se fija
    al generar por la fachada `accounts`). El QR es `STATIC`,
    `amount_minor = null` (el pagador digita el monto al escanear) y
    `expires_at = null` (reutilizable). `Idempotency-Key` opcional (no
    mueve dinero): misma clave + mismo cuerpo = replay (un solo
    cargo); cuerpo distinto = `409`. Emite `qr.charge.created` por
    `outbox`.
    """
    try:
        view, replayed = wallet_service.create_personal_qr_charge_idempotent(
            db, user_id=user_id, body=body.model_dump(), idempotency_key=idempotency_key
        )
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
    logger.debug("qr personal generado charge_id=%s replayed=%s", view["charge_id"], replayed)
    return {"data": view, "meta": {"request_id": _request_id(request), "replayed": replayed}}


@router.get(
    "/wallet/qr/{charge_id}",
    response_model=PersonalQrStatusResponse,
    summary="Consulta el estado del QR personal del titular",
    responses={
        200: {"description": "Estado del QR personal (`ACTIVE`, reutilizable)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (ajeno/inexistente/de comercio, neutro)"},
    },
)
def get_personal_qr_status(
    charge_id: str,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Consulta el estado del QR personal (E4-T21 CA-02).

    Solo el titular dueno de un cargo `PERSONAL`: ajeno/inexistente/
    de comercio/id malformado -> `404 NOT_FOUND` neutro (sin montos,
    anti-enumeracion). Sin caducidad: el estado es el de la fila.
    """
    try:
        view = wallet_service.get_personal_charge_status(db, user_id=user_id, charge_id=charge_id)
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
