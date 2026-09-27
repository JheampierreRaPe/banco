"""Endpoints de beneficiarios frecuentes (E2-T13, HU07 CA-01..CA-04).

- `GET /beneficiaries` (lista + `q` de busqueda, hasta 100, `< 1 s`),
  `POST /beneficiaries` (alta, `Idempotency-Key` opcional pero respetada),
  `PUT /beneficiaries/{id}` (edicion parcial incl. tope pre-aprobado),
  `DELETE /beneficiaries/{id}?confirm=true` (soft-delete con
  confirmacion de seguridad). Montados bajo `/api/v1` por `app.main` via
  el `router` de `accounts.api` (este sub-router se incluye alli).
- Sin logica en el router (05#1): valida, traduce y delega al `service/`;
  el servicio hace `flush`, el endpoint confirma (`commit`) o revierte.
- Auth/RBAC: `get_current_user_id` de `accounts.api` (JWT `Bearer`,
  `sub` = titular); sin token valido -> 401. El `service/` verifica
  propiedad (-> 403 si es ajeno, 404 si no existe/baja).
- Respuestas segun 05#4 (`{"data", "meta"}`) y errores problem+json via
  `AppError` (`NOT_FOUND`/`NOT_AUTHORIZED`/`VALIDATION_ERROR`/
  `DUPLICATE_BENEFICIARY`, 05#4-#5). La cuenta/CCI jamas sale en claro
  (solo `account_masked`); el documento nunca se retorna. Sin PII en
  logs (regla de oro 7).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.modules.accounts.api import get_current_user_id
from app.modules.accounts.schemas.beneficiaries import (
    BeneficiariesListResponse,
    BeneficiaryCreate,
    BeneficiaryResponse,
    BeneficiaryUpdate,
)
from app.modules.accounts.service import beneficiaries as beneficiaries_service

router = APIRouter(tags=["accounts"])

logger = logging.getLogger(__name__)


def _to_not_found(exc: Exception) -> AppError:
    return AppError(code="NOT_FOUND", message=str(exc), status_code=404)


def _to_forbidden(exc: Exception) -> AppError:
    return AppError(code="NOT_AUTHORIZED", message=str(exc), status_code=403)


@router.get(
    "/beneficiaries",
    response_model=BeneficiariesListResponse,
    summary="Lista y busca beneficiarios frecuentes",
)
def list_beneficiaries(
    q: str | None = Query(default=None, max_length=80, description="Busqueda por alias/nombre"),
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Lista ACTIVE del titular o busqueda indexada `q` (hasta 100).

    401 sin JWT valido. La cuenta/CCI sale enmascarada (`****` + ultimos
    4); el documento nunca se expone.
    """
    try:
        items = beneficiaries_service.search_beneficiaries(db, user_id, q)
    except (ValueError, TypeError) as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=400) from exc
    return {"data": items, "meta": {"total": len(items), "q": q}}


@router.post(
    "/beneficiaries",
    response_model=BeneficiaryResponse,
    status_code=201,
    summary="Da de alta un beneficiario frecuente",
)
def create_beneficiary(
    body: BeneficiaryCreate,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Alta (CA-01): valida, cifra en reposo, indexa y emite
    `beneficiary.saved`. `Idempotency-Key` opcional: misma clave + mismo
    cuerpo no duplica (replay); distinta -> 409. Duplicado de alias/CCI
    del titular -> 409 `DUPLICATE_BENEFICIARY`; formato invalido -> 400.
    """
    payload = body.model_dump()
    try:
        view, replayed = beneficiaries_service.create_beneficiary_idempotent(
            db, user_id, body=payload, idempotency_key=idempotency_key
        )
    except beneficiaries_service.DuplicateBeneficiaryError as exc:
        db.rollback()
        raise AppError(code="DUPLICATE_BENEFICIARY", message=str(exc), status_code=409) from exc
    except beneficiaries_service.IdempotencyConflictError as exc:
        db.rollback()
        raise AppError(code="IDEMPOTENCY_CONFLICT", message=str(exc), status_code=409) from exc
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=400) from exc
    db.commit()
    return {"data": view, "meta": {"replayed": replayed}}


@router.put(
    "/beneficiaries/{beneficiary_id}",
    response_model=BeneficiaryResponse,
    summary="Edita un beneficiario frecuente",
)
def update_beneficiary(
    beneficiary_id: uuid.UUID,
    body: BeneficiaryUpdate,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Edicion parcial (CA-03/CA-04, incl. tope pre-aprobado, solo el
    titular). 404 si no existe/baja; 403 si es ajeno; 409 si el cambio
    duplica alias/CCI del titular; 400 si el formato es invalido."""
    try:
        view = beneficiaries_service.update_beneficiary(
            db, user_id, beneficiary_id, **body.model_dump(exclude_unset=True)
        )
    except beneficiaries_service.BeneficiaryNotFoundError as exc:
        db.rollback()
        raise _to_not_found(exc) from exc
    except beneficiaries_service.BeneficiaryForbiddenError as exc:
        db.rollback()
        raise _to_forbidden(exc) from exc
    except beneficiaries_service.DuplicateBeneficiaryError as exc:
        db.rollback()
        raise AppError(code="DUPLICATE_BENEFICIARY", message=str(exc), status_code=409) from exc
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=400) from exc
    db.commit()
    return {"data": view, "meta": {}}


@router.delete(
    "/beneficiaries/{beneficiary_id}",
    response_model=BeneficiaryResponse,
    summary="Elimina (baja logica) un beneficiario frecuente",
)
def delete_beneficiary(
    beneficiary_id: uuid.UUID,
    confirm: bool = Query(default=False, description="Confirmacion de seguridad"),
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Soft-delete (`status=DELETED`, CA-04): exige `?confirm=true`;
    sin confirmacion -> 400. 404 si no existe/baja; 403 si es ajeno.
    No borra fisicamente (trazabilidad)."""
    try:
        view = beneficiaries_service.delete_beneficiary(
            db, user_id, beneficiary_id, confirm=confirm
        )
    except beneficiaries_service.BeneficiaryNotFoundError as exc:
        db.rollback()
        raise _to_not_found(exc) from exc
    except beneficiaries_service.BeneficiaryForbiddenError as exc:
        db.rollback()
        raise _to_forbidden(exc) from exc
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=400) from exc
    db.commit()
    return {"data": view, "meta": {}}


__all__ = ["router"]
