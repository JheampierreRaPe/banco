"""Centro de notificaciones del titular (E6-T12, HU22 CA-05).

- `GET /notifications` (solo el titular): lista paginada de SUS
  notificaciones (metadatos, sin `payload_json` crudo ni PII), filtros
  `channel`/`unread`.
- `POST /notifications/{id}/read` (solo el titular): fija `read_at`
  (idempotente; `404` neutro si ajena/inexistente).
- `GET /notifications/preferences` y `PUT /notifications/preferences`
  (solo el titular): devuelve/actualiza `user_channel_preferences`
  (idempotente por naturaleza). Nota: desactivar `email` NO desactiva
  el respaldo de alertas de seguridad (decision del dueno).

Montados bajo `/api/v1` por `app.main` via `iter_routers` (sin registro
extra). Sin logica en el router (05#1): valida, traduce y delega al
`service/`; el servicio hace `flush`, el endpoint confirma (`commit`)
o revierte. Auth: JWT `Bearer` propio del modulo (`sub` = titular,
05#2); en tests se inyecta contexto falso con
`app.dependency_overrides[get_current_user_id]`.

Respuestas segun 05#4 (`{"data", "meta"}`) y errores problem+json via
`AppError` (`NOT_AUTHENTICATED`/`NOT_FOUND` neutro/`VALIDATION_ERROR`).
Sin PII en logs (regla de oro 7: solo ids, canal y codigo de plantilla).
"""

from __future__ import annotations

import logging
import uuid

import jwt
from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import decode_token
from app.modules.notifications import service as notifications_service
from app.modules.notifications.schemas import ChannelPreferencesUpdate, ReportUnrecognizedBody

logger = logging.getLogger(__name__)

router = APIRouter(tags=["notifications"])


def get_current_user_id(authorization: str | None = Header(default=None)) -> uuid.UUID:
    """Extrae el `user_id` (`sub`) del JWT `Bearer` (05#2).

    401 si falta la cabecera, no es `Bearer`, el token es invalido o el
    `sub` no es UUID (mismo patron que `wallet.api`, sin importar su
    router).
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
        logger.debug("rechazo JWT notifications: %s", type(exc).__name__)
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
    "/notifications/preferences",
    summary="Preferencias de canal del titular",
    responses={
        200: {"description": "Preferencias `{push, email, sms}`"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
    },
)
def get_preferences(
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Devuelve `user_channel_preferences` (defaults si aun no hay fila)."""
    data = notifications_service.get_channel_preferences(db, user_id)
    return {"data": data, "meta": {"request_id": _request_id(request)}}


@router.put(
    "/notifications/preferences",
    summary="Actualiza las preferencias de canal del titular",
    responses={
        200: {"description": "Preferencias guardadas (idempotente)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        422: {"description": "`VALIDATION_ERROR` (flags no booleanos)"},
    },
)
def put_preferences(
    body: ChannelPreferencesUpdate,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Reemplaza `{push, email, sms}` (idempotente por `user_id` PK)."""
    try:
        data = notifications_service.set_channel_preferences(
            db,
            user_id=user_id,
            push=body.push,
            email=body.email,
            sms=body.sms,
        )
    except (ValueError, TypeError) as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    db.commit()
    return {"data": data, "meta": {"request_id": _request_id(request)}}


@router.get(
    "/notifications",
    summary="Centro de notificaciones del titular",
    responses={
        200: {"description": "Pagina de notificaciones propias (metadatos)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        422: {"description": "`VALIDATION_ERROR` (filtros/paginado invalidos)"},
    },
)
def list_notifications(
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    channel: str | None = Query(default=None),
    unread: bool = Query(default=False),
    page: int = Query(default=1),
    page_size: int = Query(default=20),
) -> dict:
    """Lista SOLO lo propio (aislado por `sub` del JWT), mas recientes primero.

    El paginado/filtros los valida el servicio (`VALIDATION_ERROR` 422).
    """
    try:
        data = notifications_service.list_notifications_for_user(
            db,
            user_id=user_id,
            channel=channel,
            unread_only=unread,
            page=page,
            page_size=page_size,
        )
    except (ValueError, TypeError) as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    meta = {
        "page": data["page"],
        "page_size": data["page_size"],
        "total": data["total"],
        "request_id": _request_id(request),
    }
    return {"data": data["items"], "meta": meta}


@router.get(
    "/notifications/{notification_id}",
    summary="Detalle de una notificacion del titular",
    responses={
        200: {"description": "Notificacion propia (metadatos acotados)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (ajena/inexistente, neutro)"},
        422: {"description": "`VALIDATION_ERROR` (id malformado)"},
    },
)
def get_notification_detail(
    notification_id: str,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Detalle read-only de UNA notificacion propia (A2, HU22).

    Misma vista acotada del listado (`device`/`location`/`reference`
    sin PII ni `payload_json` crudo). Solo el titular; ajena o
    inexistente -> `404` neutro (indistinguible, sin fuga).
    """
    try:
        view = notifications_service.get_notification_detail(
            db, notification_id=notification_id, user_id=user_id
        )
    except (ValueError, TypeError) as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    if view is None:
        raise AppError(
            code="NOT_FOUND",
            message="Notificacion no encontrada",
            status_code=404,
        )
    return {"data": view, "meta": {"request_id": _request_id(request)}}


@router.post(
    "/notifications/{notification_id}/read",
    summary="Marca una notificacion como leida",
    responses={
        200: {"description": "Notificacion marcada (idempotente)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (ajena/inexistente, neutro)"},
        422: {"description": "`VALIDATION_ERROR` (id malformado)"},
    },
)
def mark_notification_read(
    notification_id: str,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Fija `read_at` si la fila es del titular (idempotente)."""
    try:
        view = notifications_service.mark_notification_as_read(
            db, notification_id=notification_id, user_id=user_id
        )
    except (ValueError, TypeError) as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    if view is None:
        raise AppError(
            code="NOT_FOUND",
            message="Notificacion no encontrada",
            status_code=404,
        )
    db.commit()
    return {"data": view, "meta": {"request_id": _request_id(request)}}


@router.post(
    "/notifications/{notification_id}/report",
    summary="Reporta 'no reconozco esta operacion'",
    responses={
        200: {"description": "Reporte registrado (idempotente)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (ajena/inexistente, neutro)"},
        422: {"description": "`VALIDATION_ERROR` (id/body malformados)"},
    },
)
def report_unrecognized_operation(
    notification_id: str,
    request: Request,
    body: ReportUnrecognizedBody | None = None,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Registra el reporte del titular y escala a `risk` por `outbox` (E6-T14).

    Idempotente: repetir el reporte devuelve `reported=True` con
    `replayed=True` sin nuevos efectos. El bloqueo de canales
    (`identity.sessions.revoked_at`) y la alerta (`fraud.alert.raised`)
    los ejecutan los consumidores D-04 (`risk`/`identity`), fuera de
    esta transaccion (regla de oro 8).
    """
    try:
        outcome = notifications_service.report_unrecognized_operation(
            db,
            notification_id=notification_id,
            user_id=user_id,
            reason=(body.reason if body is not None else None),
        )
    except (ValueError, TypeError) as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    if outcome is None:
        raise AppError(
            code="NOT_FOUND",
            message="Notificacion no encontrada",
            status_code=404,
        )
    db.commit()
    return {"data": outcome, "meta": {"request_id": _request_id(request)}}


__all__ = ["get_current_user_id", "router"]
