"""Endpoints de gestion de sesiones (E1-T15, HU03 CA-04).

`POST /auth/refresh {refresh_token}` -> verifica hash + vigencia + no
revocado + inactividad y rota el refresh (emite uno nuevo, invalida el
anterior; reuso del viejo = posible robo: revoca toda la cadena).
`POST /auth/logout {refresh_token}` -> revoca la sesion (idempotente).

Montados bajo `/api/v1` por `app.main` via `iter_routers` (este `router` lo
recoge `api/__init__.py`; sin registro extra). Sin logica en el router
(05#1): valida, traduce y delega a `service/sessions`.

Respuestas 05#4 (`{"data", "meta"}` + `request_id`); errores problem+json
via `AppError`:

- 401 `INVALID_REFRESH`: refresh malformado o desconocido.
- 401 `REFRESH_EXPIRED`: `expires_at` pasado (la sesion se cierra).
- 401 `SESSION_INACTIVE`: inactividad excedida (la sesion se cierra).
- 401 `REFRESH_REUSED`: reuso de un refresh revocado (toda la cadena del
  usuario queda revocada).
- 422 estandar de FastAPI (`{"detail": [...]}`) para esquema malformado.

Transaccion: el servicio hace `flush`; el endpoint confirma (`commit`) en
exito y tambien ante error de negocio con cambios que deben persistir
(cierre por expiracion/inactividad, revocacion de cadena ante reuso,
logout). Solo `INVALID_REFRESH` (sin cambios) revierte (`rollback`).
El refresh jamas sale en logs. OpenAPI automatico por FastAPI
(`response_model`).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.modules.identity.schemas.sessions import (
    LogoutRequest,
    LogoutResponse,
    RefreshRequest,
    RefreshResponse,
)
from app.modules.identity.service import sessions as sessions_service

router = APIRouter(tags=["identity"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


@router.post(
    "/auth/refresh",
    response_model=RefreshResponse,
    summary="Rota el refresh y emite tokens nuevos",
)
def refresh_tokens(body: RefreshRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    """Valida el refresh vigente y lo rota (HU03 CA-04)."""
    try:
        result = sessions_service.refresh_session(db, refresh_token=body.refresh_token)
    except sessions_service.RefreshInvalidError as exc:
        db.rollback()
        raise AppError(code="INVALID_REFRESH", message=str(exc), status_code=401) from exc
    except sessions_service.RefreshExpiredError as exc:
        db.commit()
        raise AppError(code="REFRESH_EXPIRED", message=str(exc), status_code=401) from exc
    except sessions_service.SessionInactiveError as exc:
        db.commit()
        raise AppError(code="SESSION_INACTIVE", message=str(exc), status_code=401) from exc
    except sessions_service.RefreshReuseError as exc:
        db.commit()
        raise AppError(code="REFRESH_REUSED", message=str(exc), status_code=401) from exc
    db.commit()
    return {"data": result, "meta": {"request_id": _request_id(request)}}


@router.post(
    "/auth/logout",
    response_model=LogoutResponse,
    summary="Revoca la sesion del refresh presentado",
)
def logout(body: LogoutRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    """Cierra la sesion (HU03 CA-04). Idempotente: exito aun si el refresh
    ya estaba revocado o es desconocido."""
    try:
        result = sessions_service.logout_session(db, refresh_token=body.refresh_token)
    except sessions_service.RefreshInvalidError as exc:
        db.rollback()
        raise AppError(code="INVALID_REFRESH", message=str(exc), status_code=401) from exc
    db.commit()
    return {"data": result, "meta": {"request_id": _request_id(request)}}


__all__ = ["router"]
