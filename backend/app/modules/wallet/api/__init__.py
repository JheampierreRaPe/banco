"""Endpoints de billetera (E4-T01, HU13 CA-01).

- `GET /wallet` (solo lectura): billetera + vinculos enmascarados del
  titular. Sin billetera aun -> `200` con `wallet_id` nulo y `links`
  vacios (la fila se crea on-demand en la primera vinculacion).
- `POST /wallet/links` (alta, `Idempotency-Key` opcional pero
  respetada): vincula una cuenta (validada por la fachada de
  `accounts`) o una tarjeta (tokenizada server-side, crudo
  descartado). Montados bajo `/api/v1` por `app.main` via
  `iter_routers` (sin registro extra).
- `DELETE /wallet/links/{id}` (baja logica a `INACTIVE`, solo titular;
  ajeno/inexistente/ya dado de baja -> `404` neutro).
- QR de comercio (`E4-T08`, HU14 CA-01/CA-02/CA-04 parcial):
  `POST /merchants/qr` (generar QR firmado con monto/concepto,
  `Idempotency-Key` opcional, solo comercio `ACTIVE` -> `201`) y
  `GET /merchants/qr/{id}` (estado efectivo con caducidad on-read,
  solo el dueno, `404` neutro si ajeno/inexistente). Rutas definidas
  en `api/qr.py` e incluidas aqui (mismo `router` bajo `/api/v1`).
  **No mueve dinero** (el pago es HU15); el aviso de abono al
  comercio (`qr_charge_paid` via `notifications.send`) lo dispara el
  pago (`HU15`), aqui queda la plantilla + la ruta (`docs/18` G72).
- Pago QR del pagador (`E4-T12`, HU15 CA-01..CA-04):
  `POST /payments/qr` (`{qr_content, link_id}`, `Idempotency-Key`
  obligatoria, solo el titular pagador -> `201` con `SETTLED` en un
  toque, `PENDING_AUTHORIZATION` + reto sobre el limite base o
  `REJECTED` + `failure_code`; `422` QR vencido/corrupto/manipulado
  sin descontar, `409` ya pagado). Ruta definida en
  `api/payments.py` e incluida aqui (mismo `router` bajo `/api/v1`;
  ownership `wallet`, `docs/18` G74). Delega en el servicio de cobro
  (`E4-T11`) y finaliza el diferido (`finalize_qr_payment` + aviso
  `qr_charge_paid`, `docs/18` G72 lado endpoint).

- Preview del QR (MEDIO-3, HU15 CA-01): `POST /payments/qr/preview`
  (`{qr_content}`, Bearer, sin `Idempotency-Key`: no mueve dinero ->
  `200` con destino/monto `{charge_id, merchant?, payee?,
  amount_minor, currency, expires_at, status}` (`amount_minor: null`
  + `payee` en QR personal, E4-T21); `422` vencido/corrupto/
  manipulado sin efectos, `409` ya cobrado). Solo lectura via
  `preview_qr_charge` (misma ruta `api/payments.py`).
- QR personal estatico (`E4-T21`, HU14): `POST /wallet/qr`
  (`{currency?, concept?}`, `Idempotency-Key` opcional, cualquier
  titular con cuenta `ACTIVE`, sin rol comercial -> `201` con el QR
  reutilizable sin monto ni caducidad) y `GET /wallet/qr/{id}`
  (estado, solo el titular dueno, `404` neutro si ajeno/de
  comercio). Rutas definidas en `api/personal_qr.py` e incluidas aqui
  (mismo `router` bajo `/api/v1`). **No mueve dinero** (el pago con
  el monto del pagador es `POST /payments/qr`); el aviso de abono al
  titular (`qr_charge_paid` reutilizada, `docs/18` G85) lo dispara el
  pago (`HU15`).
- Catalogo de servicios (`E4-T16`, HU16 CA-01/CA-03 base):
  `GET /services/catalog` (filtrable por `category`/`q`, solo
  lectura -> `200` con `billers` habilitados +
  `required_fields`) y `POST /services/subscriptions`
  (`{biller_id, supply_ref, notify_days_before?}` -> `201`,
  idempotente por naturaleza con `replayed`; valida el suministro
  contra el proveedor vigente, `503`/`502` si cae; base de los
  recordatorios de `E4-T18`). Rutas definidas en `api/services.py`
  e incluidas aqui (mismo `router` bajo `/api/v1`). **No mueve
  dinero** (el pago es `E4-T17`).
- Pago de servicio y recarga (`E4-T17`, HU16 CA-02/CA-04):
  `POST /services/payments` (`{biller_id, supply_ref, amount_minor,
  currency?, link_id}`, `Idempotency-Key` obligatoria, solo el
  titular -> `201` con `{payment_id, transaction_id, status,
  amount_minor, currency, receipt}`) y `POST /topups` (`{operator,
  phone, amount_minor, currency?, link_id}` -> `201` con `{topup_id,
  ...}`), definidos en `api/services.py` e incluidos aqui. Mueven
  el dinero **solo** por el motor (`pay_express`, `docs/04#7.10`
  sin comision); `<=` base -> `SETTLED` con comprobante (`receipt`
  con `receipt_no` oficial `OP-…`); `>` base ->
  `PENDING_AUTHORIZATION` + reto (el replay tras autorizar
  finaliza); diario/fondos -> `REJECTED` + `failure_code` sin tocar
  saldos. Eventos `service.paid`/`topup.completed` por `outbox`.

Sin logica en el router (05#1): valida, traduce y delega al
`service/`; el servicio hace `flush`, el endpoint confirma
(`commit`) o revierte. Auth: JWT `Bearer` propio del modulo (`sub`
= titular, 05#2); en tests se inyecta contexto falso con
`app.dependency_overrides[get_current_user_id]`.

Respuestas segun 05#4 (`{"data", "meta"}`) y errores problem+json via
`AppError` (`NOT_AUTHENTICATED`/`NOT_FOUND` neutro/`VALIDATION_ERROR`/
`DUPLICATE_LINK`/`IDEMPOTENCY_CONFLICT`). El PAN, el numero completo
y el `token_ref` jamas salen en respuestas; sin PII en logs
(regla de oro 7).
"""

from __future__ import annotations

import logging
import uuid

import jwt
from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import decode_token
from app.modules.wallet import repository as wallet_repo
from app.modules.wallet import service as wallet_service
from app.modules.wallet.schemas import LinkCreate, WalletLinkResponse, WalletResponse

router = APIRouter(tags=["wallet"])

logger = logging.getLogger(__name__)


def get_current_user_id(authorization: str | None = Header(default=None)) -> uuid.UUID:
    """Extrae el `user_id` (`sub`) del JWT `Bearer` (05#2).

    401 si falta la cabecera, no es `Bearer`, el token es invalido/
    expirado o `sub` no es UUID. Delega en `core.security` (patron de
    `accounts`/`credits`, sin importar sus routers).
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
        logger.debug("rechazo JWT wallet: %s", type(exc).__name__)
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


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


@router.get(
    "/wallet",
    response_model=WalletResponse,
    summary="Billetera y vinculos del titular",
    responses={
        200: {"description": "Billetera con vinculos enmascarados"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
    },
)
def get_wallet(
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Consulta la billetera del titular (`sub` del JWT, HU13 CA-01).

    Solo lectura y aislada por titular: jamas expone vinculos ajenos.
    """
    try:
        data = wallet_service.get_wallet_view(db, user_id)
    except (ValueError, TypeError) as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    return {"data": data, "meta": {"request_id": _request_id(request)}}


@router.post(
    "/wallet/links",
    response_model=WalletLinkResponse,
    status_code=201,
    summary="Vincula una cuenta o tarjeta como fuente de fondos",
    responses={
        201: {"description": "Vinculo creado (enmascarado)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (cuenta ajena/inexistente/inactiva, neutro)"},
        409: {"description": "`DUPLICATE_LINK` / `IDEMPOTENCY_CONFLICT`"},
        422: {"description": "`VALIDATION_ERROR` (`type` invalido, falta dato, PAN malformado)"},
    },
)
def create_wallet_link(
    body: LinkCreate,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Vincula la fuente de fondos (HU13 CA-01, tokenizacion G67).

    `type=ACCOUNT` valida la cuenta por la fachada de `accounts`
    (titular + `ACTIVE`); `type=CARD` tokeniza el PAN server-side con
    el proveedor vigente y lo descarta. `Idempotency-Key` opcional:
    misma clave + mismo cuerpo = replay (un solo vinculo); cuerpo
    distinto = `409`. Duplicado `ACTIVE` = `409 DUPLICATE_LINK`.
    """
    payload = body.model_dump()
    try:
        view, replayed = wallet_service.link_source_idempotent(
            db, user_id, body=payload, idempotency_key=idempotency_key
        )
    except wallet_service.WalletAccountNotFoundError as exc:
        db.rollback()
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except wallet_repo.DuplicateLinkError as exc:
        db.rollback()
        raise AppError(code="DUPLICATE_LINK", message=str(exc), status_code=409) from exc
    except wallet_service.IdempotencyConflictError as exc:
        db.rollback()
        raise AppError(code="IDEMPOTENCY_CONFLICT", message=str(exc), status_code=409) from exc
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    db.commit()
    return {"data": view, "meta": {"request_id": _request_id(request), "replayed": replayed}}


@router.delete(
    "/wallet/links/{link_id}",
    response_model=WalletLinkResponse,
    summary="Desvincula una fuente de fondos del titular",
    responses={
        200: {"description": "Vinculo dado de baja (`INACTIVE`, enmascarado)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (ajeno/inexistente/ya dado de baja, neutro)"},
    },
)
def delete_wallet_link(
    link_id: str,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Baja logica del vinculo (HU13, `E4-T01`).

    Solo el titular: marca el vinculo `ACTIVE` propio como `INACTIVE`
    (sin borrar filas). Ajeno/inexistente/id malformado o ya `INACTIVE`
    -> `404 NOT_FOUND` neutro (sin oraculo).
    """
    try:
        view = wallet_service.unlink_source(db, user_id, link_id)
    except wallet_service.WalletLinkNotFoundError as exc:
        db.rollback()
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    db.commit()
    return {"data": view, "meta": {"request_id": _request_id(request)}}


__all__ = ["get_current_user_id", "router"]

from app.modules.wallet.api.payments import router as _payments_router
from app.modules.wallet.api.personal_qr import router as _personal_qr_router
from app.modules.wallet.api.qr import router as _qr_router
from app.modules.wallet.api.services import router as _services_router

router.include_router(_payments_router)
router.include_router(_personal_qr_router)
router.include_router(_qr_router)
router.include_router(_services_router)
