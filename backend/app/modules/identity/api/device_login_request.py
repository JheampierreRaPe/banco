"""Paso 1 del login en dispositivo nuevo (E1-T45, HU03/HU04).

`POST /auth/login/device/request {email, doc_type, document_number}` ->
SIEMPRE 200 con cuerpo identico exista o no la cuenta (sin enumeracion) +
OTP `LOGIN` por email si `email` y documento pertenecen a la MISMA cuenta
`ACTIVE` (cooldown: reutiliza el `PENDING` vigente). El OTP emitido lo
consume el paso 2 (`POST /auth/login/device/complete`, E1-T46).

Montado bajo `/api/v1` por `app.main` via `iter_routers` (este `router` lo
recoge `api/__init__.py`; sin registro extra). Sin logica en el router
(05#1): valida, traduce y delega a `service/device_login_request`. Sin auth
todavia: es un endpoint pre-sesion.

Respuestas 05#4 (`{"data", "meta"}` + `request_id`); errores problem+json
via `AppError`:

- `request`: 200 `{"accepted", "ttl_seconds", "resend_wait_seconds"}`
  (constantes globales, identico exista o no); 429 `RATE_LIMITED`
  (ventana por email+IP reutilizada de verify, verificada antes de la
  existencia); 422 estandar de FastAPI para email/documento malformados.

Transaccion: el servicio hace `flush`; el endpoint confirma (`commit`) en
exito (incluida la rama ciega: la auditoria hace `flush`) y revierte
(`rollback`) ante rate-limit o error inesperado. El documento y el codigo
OTP jamas salen en respuestas ni logs. OpenAPI automatico por FastAPI
(`response_model`).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.modules.identity.schemas.device_login_request import (
    DeviceLoginRequestRequest,
    DeviceLoginRequestResponse,
)
from app.modules.identity.service import device_login_request as device_login_request_service
from app.modules.identity.service import recovery as recovery_service

router = APIRouter(tags=["identity"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post(
    "/auth/login/device/request",
    response_model=DeviceLoginRequestResponse,
    summary="Solicita el OTP de login en dispositivo nuevo por email",
)
def request_device_login(
    body: DeviceLoginRequestRequest, request: Request, db: Session = Depends(get_db)
) -> dict:
    """Emite (o reutiliza en cooldown) el OTP `LOGIN` (paso 1 del login sin `userRef`)."""
    try:
        result = device_login_request_service.request_device_login(
            db,
            email=body.email,
            doc_type=body.doc_type,
            document_number=body.document_number,
            ip=_client_ip(request),
        )
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
