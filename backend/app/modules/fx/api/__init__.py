"""Endpoints de bolsillos de ahorro (E7-T02, HU24 CA-01..CA-04) y de
tasas/cotizaciones (E7-T09, HU25).

- `GET /pockets` (lista del titular), `POST /pockets` (apertura,
  `Idempotency-Key` opcional pero respetada), `GET /pockets/{id}`
  (detalle). Montados bajo `/api/v1` por `app.main` via `iter_routers`
  (este `router` ya lo recoge el ensamblado; sin registro extra).
- `GET /fx/rates?base=&quote=` (tasa vigente + snapshot persistido),
  `POST /fx/quotes` (congela 30 s, `Idempotency-Key` opcional pero
  respetada), `GET /fx/quotes/{id}` (vigencia on-read: vencida ->
  `EXPIRED`, ajena -> `404` neutro).
- Sin logica en el router (05#1): valida, traduce y delega al `service/`;
  el servicio hace `flush`, el endpoint confirma (`commit`) o revierte.
- Auth/RBAC: JWT `Bearer` (`sub` = titular, 05#2); sin token valido ->
  401. El `service/` verifica propiedad (detalle -> 404 neutro si es
  ajeno o no existe).
- Respuestas segun 05#4 (`{"data", "meta"}`) y errores problem+json via
  `AppError` (`VALIDATION_ERROR` 422 / `NOT_FOUND` 404 / `CONFLICT` 409 /
  `FX_RATE_UNAVAILABLE` 503 / `FX_RATE_TIMEOUT` 502 /
  `NOT_AUTHENTICATED` 401). Sin PII/secretos en logs (reglas 7/9).
"""

from __future__ import annotations

import logging
import uuid

import jwt
from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.adapters.fx_rate import (
    FX_RATE_TIMEOUT,
    FX_RATE_UNAVAILABLE,
    FX_RATE_VALIDATION_ERROR,
    HTTP_STATUS_FOR_CODE,
    FxRateError,
)
from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import decode_token
from app.modules.fx import service as pockets_service
from app.modules.fx.schemas import (
    PocketCreate,
    PocketResponse,
    PocketsListResponse,
    QuoteCreate,
    QuoteResponse,
    RateResponse,
)

router = APIRouter(tags=["fx"])

logger = logging.getLogger(__name__)


def get_current_user_id(authorization: str | None = Header(default=None)) -> uuid.UUID:
    """Extrae el `user_id` (`sub`) del JWT `Bearer` (05#2).

    401 si falta la cabecera, no es `Bearer`, el token es invalido/expirado
    o `sub` no es UUID. Delega en `core.security` (sin mecanismo propio).
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise AppError(
            code="NOT_AUTHENTICATED",
            message="Se requiere Authorization: Bearer <jwt>",
            status_code=401,
        )
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = decode_token(token)
    except jwt.PyJWTError as exc:
        logger.debug("rechazo JWT fx: %s", type(exc).__name__)
        raise AppError(
            code="NOT_AUTHENTICATED",
            message="Token invalido o expirado",
            status_code=401,
        ) from exc
    try:
        return uuid.UUID(str(payload.get("sub")))
    except (ValueError, AttributeError, TypeError) as exc:
        raise AppError(
            code="NOT_AUTHENTICATED",
            message="Token sin sujeto valido",
            status_code=401,
        ) from exc


@router.get("/pockets", response_model=PocketsListResponse, summary="Lista de bolsillos")
def list_my_pockets(
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Bolsillos del titular con saldo en centimos, moneda y meta (HU24 CA-01)."""
    try:
        items = pockets_service.list_pockets(db, user_id)
    except ValueError as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    return {"data": items, "meta": {"total": len(items)}}


@router.post(
    "/pockets",
    response_model=PocketResponse,
    status_code=201,
    summary="Abre un bolsillo de ahorro",
)
def open_my_pocket(
    body: PocketCreate,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Apertura (CA-01/CA-04): crea la cuenta `POCKET` (misma moneda) por la
    fachada de `accounts` + la fila `fx.savings_pockets` con saldo 0.

    `Idempotency-Key` opcional: misma clave + mismo cuerpo no duplica
    (`meta.replayed=true`); misma clave + distinto cuerpo -> 409.
    Duplicado exacto (mismo nombre + moneda) sin clave -> 409 `CONFLICT`.
    Moneda/meta/nombre invalidos -> 422 `VALIDATION_ERROR`. Emite
    `pocket.created` por `outbox` (sin PII). No mueve dinero.
    """
    payload = body.model_dump()
    # `Decimal` no es JSON-serializable para el hash: el servicio canoniza.
    try:
        view, replayed = pockets_service.open_pocket_idempotent(
            db, user_id, body=payload, idempotency_key=idempotency_key
        )
    except pockets_service.DuplicatePocketError as exc:
        db.rollback()
        raise AppError(code="CONFLICT", message=str(exc), status_code=409) from exc
    except pockets_service.IdempotencyConflictError as exc:
        db.rollback()
        raise AppError(code="CONFLICT", message=str(exc), status_code=409) from exc
    except ValueError as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    db.commit()
    return {"data": view, "meta": {"replayed": replayed}}


@router.get(
    "/pockets/{pocket_id}",
    response_model=PocketResponse,
    summary="Detalle de un bolsillo",
)
def get_my_pocket(
    pocket_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Detalle del bolsillo si es del titular; ajeno/inexistente -> 404 neutro."""
    try:
        view = pockets_service.get_pocket(db, user_id, pocket_id)
    except pockets_service.PocketNotFoundError as exc:
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except ValueError as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    return {"data": view, "meta": {}}


def _fx_error_status(exc: FxRateError) -> int:
    """HTTP para un error tipado del adaptador (`docs/02#9`; default `503`)."""
    return int(HTTP_STATUS_FOR_CODE.get(exc.code, 503))


@router.get("/fx/rates", response_model=RateResponse, summary="Tasa de cambio vigente")
def get_fx_rate(
    base: str,
    quote: str,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Tasa vigente del par + snapshot persistido en `fx_rates` (HU25 CA-02).

    Par no soportado / `base == quote` / moneda invalida -> `422
    VALIDATION_ERROR`; proveedor caido -> `503 FX_RATE_UNAVAILABLE`;
    timeout -> `502 FX_RATE_TIMEOUT`. Sin PII en logs (solo el par).
    """
    _ = user_id  # la tasa es publica para cualquier titular autenticado
    try:
        view = pockets_service.get_rate(db, base, quote)
    except FxRateError as exc:
        db.rollback()
        if exc.code == FX_RATE_VALIDATION_ERROR:
            raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
        if exc.code == FX_RATE_TIMEOUT:
            raise AppError(code=FX_RATE_TIMEOUT, message=str(exc), status_code=502) from exc
        raise AppError(
            code=FX_RATE_UNAVAILABLE, message=str(exc), status_code=_fx_error_status(exc)
        ) from exc
    except ValueError as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    db.commit()
    return {"data": view, "meta": {}}


@router.post(
    "/fx/quotes",
    response_model=QuoteResponse,
    status_code=201,
    summary="Congela una cotizacion por 30 s",
)
def create_fx_quote(
    body: QuoteCreate,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Congela tasa+spread+montos con `expires_at` a 30 s (HU25 CA-01).

    `Idempotency-Key` opcional: misma clave + mismo cuerpo -> misma
    cotizacion (`meta.replayed=true`, sin duplicar); misma clave +
    distinto cuerpo -> `409 CONFLICT`. Par/monto invalidos -> `422
    VALIDATION_ERROR`. Emite auditoria best-effort `fx.quote.created`
    (sin PII). No mueve dinero (la ejecucion es `E7-T11`).
    """
    payload = body.model_dump()
    try:
        view, replayed = pockets_service.create_quote_idempotent(
            db, user_id, body=payload, idempotency_key=idempotency_key
        )
    except pockets_service.QuoteConflictError as exc:
        db.rollback()
        raise AppError(code="CONFLICT", message=str(exc), status_code=409) from exc
    except pockets_service.QuoteNotFoundError as exc:  # pragma: no cover - defensa
        db.rollback()
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except FxRateError as exc:
        db.rollback()
        if exc.code == FX_RATE_VALIDATION_ERROR:
            raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
        if exc.code == FX_RATE_TIMEOUT:
            raise AppError(code=FX_RATE_TIMEOUT, message=str(exc), status_code=502) from exc
        raise AppError(
            code=FX_RATE_UNAVAILABLE, message=str(exc), status_code=_fx_error_status(exc)
        ) from exc
    except ValueError as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    db.commit()
    return {"data": view, "meta": {"replayed": replayed}}


@router.get(
    "/fx/quotes/{quote_id}",
    response_model=QuoteResponse,
    summary="Estado de una cotizacion (vigente/vencida)",
)
def get_fx_quote(
    quote_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Cotizacion del titular con vigencia on-read (HU25 CA-03).

    Vencida (`now >= expires_at`) se reporta `EXPIRED`; ajena o
    inexistente -> `404 NOT_FOUND` neutro.
    """
    try:
        view = pockets_service.get_quote(db, user_id, quote_id)
    except pockets_service.QuoteNotFoundError as exc:
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except ValueError as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    db.commit()
    return {"data": view, "meta": {}}


__all__ = ["get_current_user_id", "router"]
