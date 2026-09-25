"""Endpoints proxy de KYC pre-registro (E1-T02, HU01 CA-01/CA-02).

`POST /auth/kyc/challenge` -> `{token, steps, expires_in}`;
`POST /auth/kyc/evaluate` -> `{step, passed, reason, frames_analyzed, details}`;
`POST /auth/kyc/document/validate` -> `{is_valid, issues, checks}` (E1-T30);
`POST /auth/kyc/document/lookup` -> `{document_type, first_name, last_name,
business_name}` (E1-T35, proxy a apiinti con `APIINTI_API_KEY` server-side);
`POST /auth/kyc/submit` -> `{overall_result, ...}`. Montados bajo `/api/v1`
por `app.main` via `iter_routers` (este `router` lo recoge el ensamblado;
sin registro extra).

E1-T30: `document/validate` valida la legibilidad del documento (base64, sin
persistir) y lo reenvia al microservicio como `file` multipart con timeout
dedicado (`KYC_DOCUMENT_TIMEOUT_SECONDS`, default 15 s). `is_valid=false` es
un 200 con `issues` (no se colapsa en error).

E1-T29: cada segmento admite una rafaga `frames_b64` (o `image_b64` por
compatibilidad); `/evaluate` evalua un paso en vivo. Los 4xx del
microservicio se propagan con `step`/`reason` en `details` (sin colapsarlos
en un mensaje unico). `verify-full` usa un timeout dedicado
(`KYC_VERIFY_TIMEOUT_SECONDS`, default 60 s) sin tocar el timeout corto del
challenge/evaluate.

Sin logica en el router (05#1): valida, traduce y delega al `service/`
(`kyc_proxy`) + adaptador `KycProvider` (E1-T01). Sin auth todavia:
pre-registro (el dispositivo aun no tiene cuenta). Respuestas 05#4
(`{"data", "meta"}` + `request_id`); errores problem+json via `AppError`:

- 422 `VALIDATION_ERROR`: payload local invalido o `KYC_INVALID` del servicio.
- 409 `DUPLICATE_DOCUMENT` / `DUPLICATE_EMAIL`: documento/email ya registrados.
- 503 `KYC_UNAVAILABLE`: servicio caido (`KYC_UNAVAILABLE`, sin internos).
- 504 `KYC_UNAVAILABLE`: timeout del servicio (`KYC_TIMEOUT`).
- 429 `RATE_LIMITED`: ventana en memoria excedida (prod: Redis/middleware).

E1-T24: con KYC exitoso el submit integra el alta (`onboard_customer`) y la
persistencia (`save_verification`) en la MISMA transaccion; el endpoint
confirma (`commit`) en exito y revierte (`rollback`) ante error. La
verificacion de un KYC fallido se guarda igual (sin usuario). El numero de
documento se hashea server-side (nunca en claro) y los frames se descartan
tras reenviarlos al adaptador.

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
from sqlalchemy.orm import Session

from app.adapters.document_lookup_provider import (
    DocumentLookupProvider,
    DocumentLookupTimeoutError,
    DocumentLookupUnavailableError,
    DocumentNotFoundError,
    create_document_lookup_provider,
)
from app.adapters.kyc_provider import (
    KycInvalidError,
    KycProvider,
    KycTimeoutError,
    KycUnavailableError,
    create_kyc_provider,
)
from app.core.db import get_db
from app.core.errors import AppError
from app.modules.identity.schemas.kyc import (
    DocumentLookupRequest,
    DocumentLookupResponse,
    KycChallengeRequest,
    KycChallengeResponse,
    KycDocumentValidateRequest,
    KycDocumentValidateResponse,
    KycEvaluateRequest,
    KycEvaluateResponse,
    KycSubmitRequest,
    KycSubmitResponse,
)
from app.modules.identity.service import document_lookup, kyc_onboarding, kyc_proxy

router = APIRouter(tags=["identity"])


def get_kyc_provider() -> KycProvider:
    """Proveedor KYC por entorno (testeable via `dependency_overrides`)."""
    return create_kyc_provider()


def get_document_lookup_provider() -> DocumentLookupProvider:
    """Proveedor de consulta del titular por entorno (testeable via overrides)."""
    return create_document_lookup_provider()


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


def _service_error(exc: Exception) -> AppError:
    """Mapea los errores del `service/` y del adaptador al formato 05#4.

    Un 4xx del microservicio se propaga con `step`/`reason` en `details`
    (E1-T29), en lugar de colapsarse en un mensaje unico.
    """
    if isinstance(exc, kyc_proxy.KycProxyValidationError):
        return AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422)
    if isinstance(exc, KycInvalidError):
        message = exc.reason or "Solicitud KYC rechazada por el servicio"
        details: dict = {}
        if exc.step:
            details["step"] = exc.step
        if exc.reason:
            details["reason"] = exc.reason
        return AppError(
            code="VALIDATION_ERROR",
            message=message,
            status_code=422,
            details=details,
        )
    if isinstance(exc, KycTimeoutError):
        return AppError(
            code="KYC_UNAVAILABLE",
            message="Servicio KYC sin respuesta, reintente luego",
            status_code=504,
        )
    if isinstance(exc, KycUnavailableError):
        return AppError(
            code="KYC_UNAVAILABLE",
            message="Servicio KYC no disponible, reintente luego",
            status_code=503,
        )
    raise exc


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
    db: Session = Depends(get_db),
) -> dict:
    """Valida imagenes, reenvia al microservicio, da de alta y persiste el intento.

    E1-T36: valida al titular por tipo (`validate_applicant`) ANTES del
    proveedor (RUC exige `business_name` o nombres; el resto exige nombres)
    y propaga `business_name` al alta.
    """
    _enforce_rate_limit(request, None)
    try:
        kyc_proxy.validate_applicant(
            body.document.type,
            body.applicant.first_name,
            body.applicant.last_name,
            body.applicant.business_name,
        )
    except kyc_proxy.KycProxyValidationError as exc:
        raise _service_error(exc) from exc
    try:
        data = kyc_proxy.submit_kyc(
            provider,
            challenge_token=body.challenge_token,
            document_type=body.document.type,
            document_image_b64=body.document.image_b64,
            segments=[s.model_dump() for s in body.segments],
        )
    except (
        kyc_proxy.KycProxyValidationError,
        KycInvalidError,
        KycTimeoutError,
        KycUnavailableError,
    ) as exc:
        raise _service_error(exc) from exc
    try:
        result = kyc_onboarding.persist_kyc_submission(
            db,
            kyc_result=data,
            doc_type=body.document.type,
            document_number=body.document.number,
            first_name=body.applicant.first_name,
            last_name=body.applicant.last_name,
            business_name=body.applicant.business_name,
            email=body.applicant.email,
            phone=body.applicant.phone,
            challenge_token=body.challenge_token,
        )
    except kyc_onboarding.DuplicateDocumentError as exc:
        db.rollback()
        raise AppError(
            code="DUPLICATE_DOCUMENT",
            message="El documento ya esta registrado",
            status_code=409,
        ) from exc
    except kyc_onboarding.DuplicateEmailError as exc:
        db.rollback()
        raise AppError(
            code="DUPLICATE_EMAIL",
            message="El email ya esta registrado",
            status_code=409,
        ) from exc
    except Exception:
        db.rollback()
        raise
    db.commit()
    # El detalle de liveness (E1-T29) viene del microservicio; el alta solo
    # conserva resultado/distancia, por lo que se anexa aqui a la respuesta.
    result.update(
        {
            "steps_verified": data.get("steps_verified", []),
            "steps_total": data.get("steps_total", []),
            "failed_step": data.get("failed_step"),
            "step_results": data.get("step_results", {}),
            "overall_reason": data.get("overall_reason", ""),
        }
    )
    return {"data": result, "meta": {"request_id": _request_id(request)}}


@router.post(
    "/auth/kyc/evaluate",
    response_model=KycEvaluateResponse,
    summary="Evalua un paso de liveness en vivo (proxy al microservicio)",
)
def kyc_evaluate(
    body: KycEvaluateRequest,
    request: Request,
    provider: KycProvider = Depends(get_kyc_provider),
) -> dict:
    """Evalua una tarea con su rafaga de frames y devuelve `passed`/`reason`."""
    _enforce_rate_limit(request, None)
    try:
        data = kyc_proxy.evaluate_step(
            provider,
            challenge_token=body.challenge_token,
            step=body.step,
            frames_b64=body.frames_b64,
        )
    except (
        kyc_proxy.KycProxyValidationError,
        KycInvalidError,
        KycTimeoutError,
        KycUnavailableError,
    ) as exc:
        raise _service_error(exc) from exc
    return {"data": data, "meta": {"request_id": _request_id(request)}}


@router.post(
    "/auth/kyc/document/validate",
    response_model=KycDocumentValidateResponse,
    summary="Valida la legibilidad del documento (proxy al microservicio)",
)
def kyc_document_validate(
    body: KycDocumentValidateRequest,
    request: Request,
    provider: KycProvider = Depends(get_kyc_provider),
) -> dict:
    """Valida base64 + magic bytes y reenvia el documento (E1-T30, HU01).

    `is_valid=false` se devuelve como 200 con `issues` (no es error HTTP);
    los fallos del microservicio se mapean a 422/503/504/429.
    """
    _enforce_rate_limit(request, None)
    try:
        data = kyc_proxy.validate_document(provider, image_b64=body.image_b64)
    except (
        kyc_proxy.KycProxyValidationError,
        KycInvalidError,
        KycTimeoutError,
        KycUnavailableError,
    ) as exc:
        raise _service_error(exc) from exc
    return {"data": data, "meta": {"request_id": _request_id(request)}}


@router.post(
    "/auth/kyc/document/lookup",
    response_model=DocumentLookupResponse,
    summary="Consulta el titular por documento (proxy a apiinti)",
)
def kyc_document_lookup(
    body: DocumentLookupRequest,
    request: Request,
    provider: DocumentLookupProvider = Depends(get_document_lookup_provider),
    db: Session = Depends(get_db),
) -> dict:
    """Consulta `GET /dni/{numero}` o `GET /ruc/{numero}` desde el servidor (E1-T35, HU01).

    Cliente delgado: el frontend nunca ve la `APIINTI_API_KEY` (solo el
    backend la envia como `Authorization: Bearer`); los datos se muestran en
    campos no editables. Sin persistencia (pre-registro, read-only).
    `type`/`number` se validan antes de la red (422); E1-T37: precheck de
    duplicado en BD (DNI y RUC) ANTES del proveedor -> 409
    `DUPLICATE_DOCUMENT` neutro sin consulta externa; sin datos -> 404
    neutro; proveedor caido/timeout -> 503/504; ventana excedida -> 429.
    """
    # Rate limit en memoria compartido con el proxy KYC (prod: Redis/middleware).
    _enforce_rate_limit(request, None)
    try:
        data = document_lookup.lookup_holder(
            provider,
            doc_type=body.type,
            number=body.number,
            is_registered=lambda number: document_lookup.document_is_registered(db, number),
        )
    except (
        document_lookup.DocumentLookupValidationError,
        document_lookup.DocumentAlreadyRegisteredError,
        DocumentNotFoundError,
        DocumentLookupTimeoutError,
        DocumentLookupUnavailableError,
    ) as exc:
        raise _lookup_service_error(exc) from exc
    return {"data": data, "meta": {"request_id": _request_id(request)}}


def _lookup_service_error(exc: Exception) -> AppError:
    """Mapea los errores de la consulta del titular al formato 05#4.

    Cuerpos neutros: nunca se refleja el numero consultado ni el cuerpo del
    proveedor (anti-enumeracion, igual que el resto del proxy KYC).
    """
    if isinstance(exc, document_lookup.DocumentLookupValidationError):
        return AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422)
    if isinstance(exc, document_lookup.DocumentAlreadyRegisteredError):
        return AppError(
            code="DUPLICATE_DOCUMENT",
            message="El documento ya se encuentra registrado",
            status_code=409,
        )
    if isinstance(exc, DocumentNotFoundError):
        return AppError(
            code="DOCUMENT_NOT_FOUND",
            message="No se encontraron datos para el documento indicado",
            status_code=404,
        )
    if isinstance(exc, DocumentLookupTimeoutError):
        return AppError(
            code="DOC_LOOKUP_UNAVAILABLE",
            message="Servicio de consulta sin respuesta, reintente luego",
            status_code=504,
        )
    if isinstance(exc, DocumentLookupUnavailableError):
        return AppError(
            code="DOC_LOOKUP_UNAVAILABLE",
            message="Servicio de consulta no disponible, reintente luego",
            status_code=503,
        )
    raise exc


__all__ = ["get_document_lookup_provider", "get_kyc_provider", "router"]
