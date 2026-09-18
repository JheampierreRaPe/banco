"""Endpoints proxy de KYC pre-registro (E1-T02, HU01 CA-01/CA-02).

`POST /auth/kyc/challenge` -> `{token, steps, expires_in}`;
`POST /auth/kyc/submit` -> `{overall_result, ...}`. Montados bajo `/api/v1`
por `app.main` via `iter_routers` (este `router` lo recoge el ensamblado;
sin registro extra).

Sin logica en el router (05#1): valida, traduce y delega al `service/`
(`kyc_proxy`) + adaptador `KycProvider` (E1-T01). Sin auth todavia:
pre-registro (el dispositivo aun no tiene cuenta). Respuestas 05#4
(`{"data", "meta"}` + `request_id`); errores problem+json via `AppError`:

- 422 `VALIDATION_ERROR`: payload local invalido o `KYC_INVALID` del servicio.
- 503 `KYC_UNAVAILABLE`: servicio caido (`KYC_UNAVAILABLE`, sin internos).
- 504 `KYC_UNAVAILABLE`: timeout del servicio (`KYC_TIMEOUT`).
- 429 `RATE_LIMITED`: ventana en memoria excedida (prod: Redis/middleware).

NOTA: los errores de esquema (campos faltantes/tipos) devuelven el 422
estandar de FastAPI (`{"detail": [...]}`); las validaciones de negocio KYC
(documento/segmentos) y los fallos del servicio usan el envoltorio 05#4
(`{"error": {...}}`) via `AppError`.

La `X-API-Key` del microservicio vive solo en el adaptador/backend y jamas
se expone al movil. OpenAPI automatico por FastAPI (`response_model`).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request

from app.adapters.kyc_provider import (
    KycInvalidError,
    KycProvider,
    KycTimeoutError,
    KycUnavailableError,
    create_kyc_provider,
)
from app.core.errors import AppError
from app.modules.identity.schemas.kyc import (
    KycChallengeRequest,
    KycChallengeResponse,
    KycSubmitRequest,
    KycSubmitResponse,
)
from app.modules.identity.service import kyc_proxy

router = APIRouter(tags=["identity"])


def get_kyc_provider() -> KycProvider:
    """Proveedor KYC por entorno (testeable via `dependency_overrides`)."""
    return create_kyc_provider()


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


def _rate_key(request: Request, device_id: str | None) -> str:
    ip = request.client.host if request.client else "unknown"
    device = (device_id or request.headers.get("x-device-id") or "").strip() or None
    return kyc_proxy.build_rate_key(ip, device)


def _enforce_rate_limit(request: Request, device_id: str | None) -> None:
    try:
        kyc_proxy.check_rate_limit(_rate_key(request, device_id))
    except kyc_proxy.KycRateLimitedError as exc:
        raise AppError(
            code="RATE_LIMITED",
            message=str(exc),
            status_code=429,
        ) from exc


@router.post(
    "/auth/kyc/challenge",
    response_model=KycChallengeResponse,
    summary="Inicia el flujo KYC (proxy al microservicio)",
)
def kyc_challenge(
    body: KycChallengeRequest,
    request: Request,
    provider: KycProvider = Depends(get_kyc_provider),
) -> dict:
    """Emite token de desafio opaco + pasos + TTL (HU01 CA-01)."""
    _enforce_rate_limit(request, body.device_id)
    try:
        data = kyc_proxy.request_challenge(provider, device_id=body.device_id)
    except KycTimeoutError as exc:
        raise AppError(
            code="KYC_UNAVAILABLE",
            message="Servicio KYC sin respuesta, reintente luego",
            status_code=504,
        ) from exc
    except KycUnavailableError as exc:
        raise AppError(
            code="KYC_UNAVAILABLE",
            message="Servicio KYC no disponible, reintente luego",
            status_code=503,
        ) from exc
    except KycInvalidError as exc:
        raise AppError(
            code="VALIDATION_ERROR",
            message="Solicitud KYC rechazada por el servicio",
            status_code=422,
        ) from exc
    return {"data": data, "meta": {"request_id": _request_id(request)}}


@router.post(
    "/auth/kyc/submit",
    response_model=KycSubmitResponse,
    summary="Envia documento + liveness y obtiene el resultado KYC",
)
def kyc_submit(
    body: KycSubmitRequest,
    request: Request,
    provider: KycProvider = Depends(get_kyc_provider),
) -> dict:
    """Valida imagenes, reenvia al microservicio y devuelve `overall_result` (CA-02)."""
    _enforce_rate_limit(request, None)
    try:
        data = kyc_proxy.submit_kyc(
            provider,
            challenge_token=body.challenge_token,
            document_type=body.document.type,
            document_image_b64=body.document.image_b64,
            segments=[s.model_dump() for s in body.segments],
        )
    except kyc_proxy.KycProxyValidationError as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    except KycInvalidError as exc:
        raise AppError(
            code="VALIDATION_ERROR",
            message="Solicitud KYC rechazada por el servicio",
            status_code=422,
        ) from exc
    except KycTimeoutError as exc:
        raise AppError(
            code="KYC_UNAVAILABLE",
            message="Servicio KYC sin respuesta, reintente luego",
            status_code=504,
        ) from exc
    except KycUnavailableError as exc:
        raise AppError(
            code="KYC_UNAVAILABLE",
            message="Servicio KYC no disponible, reintente luego",
            status_code=503,
        ) from exc
    return {"data": data, "meta": {"request_id": _request_id(request)}}


__all__ = ["get_kyc_provider", "router"]
