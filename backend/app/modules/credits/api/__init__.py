"""Endpoints del simulador de creditos (E3-T03, HU09 CA-01..CA-04).

- `GET /credits/products` (catalogo habilitado) y `POST /credits/simulate`
  (cuota + cronograma provisionales) mas `POST /credits/simulate/export`
  (PDF del cronograma), montados bajo `/api/v1` por `app.main` via
  `iter_routers` (sin registro extra: este `router` ya lo recoge el
  ensamblado del monolito modular).
- Sin logica en el router (05#1): valida, traduce y delega al `service/`
  (`simulation`), que orquesta el catalogo `E3-T02` sobre el motor puro
  `E3-T01`. Simulacion **efimera**: no persiste, no emite eventos.
- `POST` sin `Idempotency-Key`: no mueve dinero ni crea recurso (calculo
  puro; repetir el cuerpo devuelve el mismo resultado sin filas nuevas).
- Auth/RBAC: reutiliza `app.core.security.decode_token` (JWT `Bearer`,
  `sub` = user_id, 05#2); sin cabecera/token valido -> 401. En tests se
  inyecta contexto falso con `app.dependency_overrides[get_current_user_id]`.
- Respuestas segun 05#4 (`{"data", "meta"}`) y errores problem+json via
  `AppError` (`NOT_FOUND` 404, `VALIDATION_ERROR` 422,
  `EXPORT_FORMAT_NOT_SUPPORTED` 501). OpenAPI automatico por FastAPI
  (`response_model`; el export declara `application/pdf`).
"""

from __future__ import annotations

import logging
import uuid

import jwt
from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import decode_token
from app.modules.credits.domain.financial import FinancialValidationError
from app.modules.credits.repository import (
    LoanProductNotFoundError,
    LoanProductValidationError,
)
from app.modules.credits.schemas.simulation import (
    ProductsListResponse,
    SimulateRequest,
    SimulateResponse,
)
from app.modules.credits.service import simulation as sim_service
from app.modules.credits.service import simulation_pdf as sim_pdf

router = APIRouter(tags=["credits"])

logger = logging.getLogger(__name__)


def get_current_user_id(authorization: str | None = Header(default=None)) -> uuid.UUID:
    """Extrae el `user_id` (`sub`) del JWT `Bearer` (05#2).

    401 si falta la cabecera, no es `Bearer`, el token es invalido/expirado
    o `sub` no es UUID. No inventa mecanismo: delega en `core.security`.
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
        logger.debug("rechazo JWT credits: %s", type(exc).__name__)
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


def _product_to_out(product: dict) -> dict:
    return {
        "id": product["id"],
        "code": product["code"],
        "name": product["name"],
        "tcea": str(product["tcea"]),
        "tea": None if product["tea"] is None else str(product["tea"]),
        "insurance_rate": str(product["insurance_rate"]),
        "min_amount_minor": product["min_amount_minor"],
        "max_amount_minor": product["max_amount_minor"],
        "min_term_months": product["min_term_months"],
        "max_term_months": product["max_term_months"],
        "allowed_terms": product["allowed_terms"],
        "enabled": product["enabled"],
    }


def _simulation_to_data(result: dict) -> dict:
    return {
        **result,
        "tea": str(result["tea"]),
        "tcea": str(result["tcea"]),
    }


def _run_simulation_or_422(db: Session, body: SimulateRequest) -> dict:
    """Ejecuta la simulacion traduciendo errores de negocio a 404/422."""
    try:
        return sim_service.run_simulation(
            db,
            product_id=body.product_id,
            amount_minor=body.amount_minor,
            term_months=body.term_months,
            tcea=body.tcea,
            insurance=body.insurance,
        )
    except LoanProductNotFoundError as exc:
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except (LoanProductValidationError, FinancialValidationError) as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc


@router.get(
    "/credits/products",
    response_model=ProductsListResponse,
    summary="Productos de credito habilitados con tasas y topes",
    responses={
        200: {"description": "Catalogo habilitado (`data` + `meta.total`)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
    },
)
def list_credit_products(
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Catalogo para el simulador (HU09 CA-01/CA-02, E3-T03).

    Solo productos `enabled`, ordenados por `code`, con tasas exactas
    (strings decimales) y topes en centimos. Solo lectura.
    """
    _ = user_id  # El titular autenticado solo gates el acceso (401 sin token).
    items = sim_service.list_enabled_products(db)
    return {"data": [_product_to_out(p) for p in items], "meta": {"total": len(items)}}


@router.post(
    "/credits/simulate",
    response_model=SimulateResponse,
    status_code=200,
    summary="Simula cuota y cronograma (efimero, sin registro real)",
    responses={
        200: {"description": "Cuota, TCEA y cronograma provisional"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (producto inexistente)"},
        422: {"description": "`VALIDATION_ERROR` (rango/plazo/tasa invalidos)"},
    },
)
def simulate_credit(
    body: SimulateRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Cuota francesa + cronograma con o sin desgravamen (HU09 CA-01..CA-03).

    Con `product_id` la tasa y los topes mandan desde el catalogo; sin el,
    `tcea` explicita dentro de `0..5`. `200` (no crea recurso: calculo
    puro sin `Idempotency-Key`); nada se persiste ni se emite.
    """
    _ = user_id  # El titular autenticado solo gates el acceso (401 sin token).
    result = _run_simulation_or_422(db, body)
    return {"data": _simulation_to_data(result), "meta": {}}


@router.post(
    "/credits/simulate/export",
    status_code=200,
    summary="Exporta el cronograma simulado en PDF",
    response_class=Response,
    responses={
        200: {"description": "PDF del cronograma (`application/pdf`)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (producto inexistente)"},
        422: {"description": "`VALIDATION_ERROR` (rango/plazo/tasa invalidos)"},
        501: {"description": "`EXPORT_FORMAT_NOT_SUPPORTED` (sin backend PDF)"},
    },
)
def export_simulation_pdf(
    body: SimulateRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> Response:
    """PDF con el mismo cronograma de `simulate` (HU09 CA-04).

    Mismo cuerpo y validaciones que `simulate` (404/422 identicos); `501
    EXPORT_FORMAT_NOT_SUPPORTED` si no hay backend PDF instalado (sin
    romper `simulate`). Sin PII en el documento ni en el nombre.
    """
    _ = user_id  # El titular autenticado solo gates el acceso (401 sin token).
    result = _run_simulation_or_422(db, body)
    payload = _simulation_to_data(result)
    try:
        pdf = sim_pdf.simulation_to_pdf(payload)
    except sim_pdf.SimulationPdfUnavailableError as exc:
        raise AppError(
            code="EXPORT_FORMAT_NOT_SUPPORTED", message=str(exc), status_code=501
        ) from exc
    code = result["product"]["code"].lower() if result["product"] else "libre"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="simulacion-credito-{code}.pdf"'},
    )


__all__ = ["get_current_user_id", "router"]

# Expediente (E3-T08, HU10): se incluye al final para que
# `applications` reutilice `get_current_user_id` sin ciclo.
from app.modules.credits.api import applications as _applications
from app.modules.credits.api import contracts as _contracts

router.include_router(_applications.router)
router.include_router(_contracts.router)
