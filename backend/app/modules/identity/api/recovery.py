"""Endpoints de recuperacion de acceso por email (E1-T33, HU04; E1-T41 retira `verify`).

`POST /auth/recovery/request {email}` -> SIEMPRE 200 con cuerpo identico
exista o no el email (sin enumeracion) + OTP `RECOVERY` por email si el
usuario es elegible (cooldown: reutiliza el `PENDING` vigente). El OTP
emitido lo consume `POST /auth/pin-reset` (E1-T34), que registra su propia
fila `access_recovery`.

E1-T41: `POST /auth/recovery/verify` fue RETIRADO (decision del dueno: la
UI "recupera mi acceso" no tenia sentido porque redirigia a login sin dar
acceso). No hay reemplazo; este router expone solo `request`.

Montados bajo `/api/v1` por `app.main` via `iter_routers` (este `router` lo
recoge `api/__init__.py`; sin registro extra). Sin logica en el router
(05#1): valida, traduce y delega a `service/recovery`. Sin auth todavia:
son endpoints pre-sesion.

Respuestas 05#4 (`{"data", "meta"}` + `request_id`); errores problem+json
via `AppError`:

- `request`: 200 `{"accepted", "ttl_seconds", "resend_wait_seconds"}`
  (constantes globales, identico exista o no); 429 `RATE_LIMITED`
  (ventana por email+IP, verificada antes de la existencia); 422 estandar
  de FastAPI para email malformado/vacio.

Transaccion: el servicio hace `flush`; el endpoint confirma (`commit`) en
exito y revierte (`rollback`) ante rate-limit o error inesperado. El
codigo OTP jamas sale en respuestas ni logs. OpenAPI automatico por
FastAPI (`response_model`).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.modules.identity.schemas.recovery import (
    RecoveryRequest,
    RecoveryRequestResponse,
)
from app.modules.identity.service import recovery as recovery_service

router = APIRouter(tags=["identity"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post(
    "/auth/recovery/request",
    response_model=RecoveryRequestResponse,
    summary="Solicita el OTP de recuperacion por email",
)
def request_recovery(
    body: RecoveryRequest, request: Request, db: Session = Depends(get_db)
) -> dict:
    """Emite (o reutiliza en cooldown) el OTP `RECOVERY` (HU04: inicio sin clave)."""
    try:
        result = recovery_service.request_recovery(db, email=body.email, ip=_client_ip(request))
    except recovery_service.RecoveryRateLimitedError as exc:
        db.rollback()
        raise AppError(code="RATE_LIMITED", message=str(exc), status_code=429) from exc
    except Exception:
        # Fallo inesperado posterior al `flush`: revierte todo (sin OTP a medias).
        db.rollback()
        raise
    db.commit()
    return {"data": result, "meta": {"request_id": _request_id(request)}}


__all__ = ["router"]
