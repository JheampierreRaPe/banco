"""Endpoints del juego contractual (E3-T12, HU11 CA-01).

- `POST /credits/applications/{id}/contract` (genera y persiste los
  tres documentos del expediente `APPROVED`; `Idempotency-Key`
  obligatoria; `201` en la primera generacion, `200` con el existente
  en el replay).
- `GET /credits/applications/{id}/contract` (metadatos del juego en
  JSON por defecto; con `?format=pdf&doc=contrato|pagare|resumen`
  descarga ese documento en `application/pdf`).

Sin logica en el router (`05#1`): valida, traduce y delega al
`service/` (`documents`), que copia las condiciones del expediente +
producto + motor (E3-T01) y persiste por el repositorio (`contracts`).
Solo el titular (ajeno/inexistente -> `404` neutro). **No mueve
dinero** (el desembolso es HU12).

Respuestas segun `05#4` (`{"data", "meta"}`) y errores problem+json
via `AppError` (`NOT_AUTHENTICATED` 401, `NOT_FOUND` 404, `CONFLICT` /
`IDEMPOTENCY_CONFLICT` 409, `VALIDATION_ERROR` 422,
`EXPORT_FORMAT_NOT_SUPPORTED` 501). OpenAPI automatico por FastAPI.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.modules.credits.api import get_current_user_id
from app.modules.credits.schemas.contracts import ContractSetResponse
from app.modules.credits.service import documents as docs_service

router = APIRouter(tags=["credits"])


def _to_app_error(exc: Exception) -> AppError:
    """Traduce errores del caso de uso a `AppError` (con rollback fuera)."""
    if isinstance(exc, docs_service.ContractNotFoundError):
        return AppError(code="NOT_FOUND", message=str(exc), status_code=404)
    if isinstance(exc, docs_service.ContractConflictError):
        code = "IDEMPOTENCY_CONFLICT" if "Idempotency-Key" in str(exc) else "CONFLICT"
        return AppError(code=code, message=str(exc), status_code=409)
    if isinstance(exc, docs_service.ContractPdfUnavailableError):
        return AppError(code="EXPORT_FORMAT_NOT_SUPPORTED", message=str(exc), status_code=501)
    if isinstance(exc, DataError):
        # Red de seguridad: un numerico fuera de rango nunca es 500
        # (mensaje generico, sin eco del dato: sin PII).
        return AppError(
            code="VALIDATION_ERROR",
            message="dato numerico fuera de rango",
            status_code=422,
        )
    return AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422)


@router.post(
    "/credits/applications/{application_id}/contract",
    response_model=ContractSetResponse,
    status_code=201,
    summary="Genera el juego contractual (contrato + pagare + resumen)",
    responses={
        201: {"description": "Juego generado (`GENERATED`, 3 documentos)"},
        200: {"description": "Replay idempotente (juego existente)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (expediente inexistente o ajeno)"},
        409: {"description": "`CONFLICT` (no `APPROVED`) / `IDEMPOTENCY_CONFLICT`"},
        422: {"description": "`VALIDATION_ERROR` (sin `Idempotency-Key`)"},
    },
)
def generate_credit_contract(
    application_id: uuid.UUID,
    response: Response,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Genera contrato + pagare + hoja resumen (HU11 CA-01, < 1 min).

    `Idempotency-Key` obligatoria (crea recurso): misma clave + mismo
    expediente no duplica el juego ni el evento (`meta.replayed`); el
    juego ademas es idempotente por naturaleza (UQ por expediente:
    otra clave sobre el mismo expediente devuelve el existente).
    """
    try:
        payload, replayed, status = docs_service.generate_contracts_idempotent(
            db,
            user_id,
            application_id,
            idempotency_key,
        )
    except (
        docs_service.ContractNotFoundError,
        docs_service.ContractConflictError,
        docs_service.ContractValidationError,
        DataError,
        ValueError,
        TypeError,
    ) as exc:
        db.rollback()
        raise _to_app_error(exc) from exc
    db.commit()
    response.status_code = status
    return {"data": payload, "meta": {"replayed": replayed}}


@router.get(
    "/credits/applications/{application_id}/contract",
    response_model=ContractSetResponse,
    summary="Metadatos o descarga del juego contractual (titular)",
    responses={
        200: {"description": "Metadatos (`data.documents`) o PDF (`application/pdf`)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (expediente ajeno/inexistente o sin juego)"},
        422: {"description": "`VALIDATION_ERROR` (`format`/`doc` invalidos)"},
        501: {"description": "`EXPORT_FORMAT_NOT_SUPPORTED` (sin backend PDF)"},
    },
)
def get_credit_contract(
    application_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    format: str = Query(default="json", description="json (metadatos) o pdf (descarga)"),
    doc: str = Query(default="contrato", description="contrato, pagare o resumen"),
) -> dict | Response:
    """Metadatos del juego o descarga de un documento (HU11 CA-01).

    `?format=json` (defecto) -> metadatos con `doc_ref`/`doc_hash`/
    `status`; `?format=pdf&doc=<tipo>` -> ese documento en PDF
    (`Content-Disposition: attachment`). Solo lectura: no genera, no
    emite eventos (sin `Idempotency-Key`).
    """
    if format not in ("json", "pdf"):
        raise AppError(
            code="VALIDATION_ERROR",
            message="format debe ser json o pdf",
            status_code=422,
        )
    if format == "json":
        try:
            payload = docs_service.get_contract_set(db, user_id, application_id)
        except (
            docs_service.ContractNotFoundError,
            docs_service.ContractValidationError,
            ValueError,
            TypeError,
        ) as exc:
            if not isinstance(exc, docs_service.ContractNotFoundError):
                db.rollback()
                raise _to_app_error(exc) from exc
            raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
        return {"data": payload, "meta": {}}
    try:
        pdf, filename = docs_service.download_contract_pdf(db, user_id, application_id, doc)
    except (
        docs_service.ContractNotFoundError,
        docs_service.ContractValidationError,
        docs_service.ContractPdfUnavailableError,
        ValueError,
        TypeError,
    ) as exc:
        if isinstance(exc, docs_service.ContractNotFoundError):
            raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
        db.rollback()
        raise _to_app_error(exc) from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


__all__ = ["router"]
