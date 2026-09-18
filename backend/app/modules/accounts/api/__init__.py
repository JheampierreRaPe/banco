"""Endpoints de lectura de cuentas (E2-T02, HU05 CA-01/CA-02).

- `GET /accounts` (consolidado) y `GET /accounts/{id}` (detalle), montados
  bajo `/api/v1` por `app.main` via `iter_routers` (sin registro extra:
  este `router` ya lo recoge el ensamblado del monolito modular).
- Sin logica en el router (05#1): valida, traduce y delega al `service/`.
- Auth/RBAC: reutiliza `app.core.security.decode_token` (JWT `Bearer`,
  `sub` = user_id, 05#2); sin cabecera/token valido -> 401. El `service/`
  filtra por `user_id` (lista) y verifica propiedad (detalle -> 403 si es
  ajena, 404 si no existe). En tests se inyecta contexto falso con
  `app.dependency_overrides[get_current_user_id]`.
- Respuestas segun 05#4 (`{"data", "meta"}`) y errores problem+json via
  `AppError` (`NOT_FOUND`/`NOT_AUTHORIZED`, 05#4-#5). OpenAPI automatico
  por FastAPI (`response_model`).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

import jwt
from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import decode_token
from app.modules.accounts import service as accounts_service
from app.modules.accounts.schemas import (
    AccountDetailResponse,
    AccountsListResponse,
    MovementsListResponse,
)

router = APIRouter(tags=["accounts"])


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
        raise AppError(
            code="NOT_AUTHENTICATED",
            message="Token invalido o expirado",
            status_code=401,
            details={"reason": str(exc)},
        ) from exc
    try:
        return uuid.UUID(str(payload.get("sub")))
    except (ValueError, AttributeError, TypeError) as exc:
        raise AppError(
            code="NOT_AUTHENTICATED",
            message="Token sin sujeto valido",
            status_code=401,
        ) from exc


@router.get("/accounts", response_model=AccountsListResponse, summary="Consolidado de cuentas")
def list_my_accounts(
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Consolidado del usuario autenticado (HU05 CA-01)."""
    items = accounts_service.list_accounts(db, user_id)
    return {
        "data": [item.model_dump(mode="json") for item in items],
        "meta": {"total": len(items)},
    }


@router.get(
    "/accounts/{account_id}",
    response_model=AccountDetailResponse,
    summary="Detalle de cuenta con saldos",
)
def get_my_account(
    account_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Detalle con disponible/retenido/contable (HU05 CA-02).

    404 si no existe; 403 si no pertenece al usuario (el numero completo
    nunca se expone: solo `account_number_masked`).
    """
    try:
        detail = accounts_service.get_account_detail(db, user_id, account_id)
    except accounts_service.AccountNotFoundError as exc:
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except accounts_service.AccountForbiddenError as exc:
        raise AppError(code="NOT_AUTHORIZED", message=str(exc), status_code=403) from exc
    return {"data": detail.model_dump(mode="json"), "meta": {}}


@router.get(
    "/accounts/{account_id}/movements",
    response_model=MovementsListResponse,
    summary="Movimientos paginados de la cuenta",
)
def list_my_movements(
    account_id: uuid.UUID,
    page: int = Query(accounts_service.MOVEMENTS_DEFAULT_PAGE, ge=1),
    page_size: int = Query(
        accounts_service.MOVEMENTS_DEFAULT_PAGE_SIZE,
        ge=1,
        le=accounts_service.MOVEMENTS_MAX_PAGE_SIZE,
    ),
    date_from: date | None = Query(default=None, description="Fecha valor desde (ISO)"),
    date_to: date | None = Query(default=None, description="Fecha valor hasta (ISO)"),
    direction: Literal["DEBIT", "CREDIT"] | None = Query(default=None),
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Movimientos de `movements_view` paginados (HU05 CA-03, E2-T03).

    Auth/RBAC E2-T02: 401 sin JWT valido; 404 si la cuenta no existe;
    403 si es ajena. Filtros: fecha valor `desde/hasta` y `direction`.
    Envoltorio 05#4 con `meta.page/page_size/total`.
    """
    try:
        items, total = accounts_service.list_account_movements(
            db,
            user_id,
            account_id,
            page=page,
            page_size=page_size,
            date_from=date_from,
            date_to=date_to,
            direction=direction,
        )
    except accounts_service.AccountNotFoundError as exc:
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except accounts_service.AccountForbiddenError as exc:
        raise AppError(code="NOT_AUTHORIZED", message=str(exc), status_code=403) from exc
    except ValueError as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=400) from exc
    return {
        "data": [m.model_dump(mode="json") for m in items],
        "meta": {"page": page, "page_size": page_size, "total": total},
    }


@router.get(
    "/accounts/{account_id}/movements/export",
    summary="Export de movimientos (CSV; XLSX/PDF si libs disponibles)",
    response_class=Response,
)
def export_my_movements(
    account_id: uuid.UUID,
    format: Literal["csv", "xlsx", "pdf"] = Query(..., description="Formato MVP: csv"),
    date_from: date = Query(..., description="Obligatorio: fecha valor desde"),
    date_to: date = Query(..., description="Obligatorio: fecha valor hasta"),
    direction: Literal["DEBIT", "CREDIT"] | None = Query(default=None),
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> Response:
    """Export con rango de fechas obligatorio (HU05 CA-04, E2-T03 + fase 4/H1).

    `format=csv` siempre disponible (UTF-8 con BOM, cabeceras `03b#5.4`).
    `format=xlsx` solo si `openpyxl` esta instalado; `format=pdf` solo si
    hay backend PDF (`reportlab`/`fpdf2`/`weasyprint`); si no, 501
    `EXPORT_FORMAT_NOT_SUPPORTED` (decision documentada en el service).
    Misma auth/RBAC que el listado (401/403/404); 400 si el rango es
    invertido; 422 si falta el rango o el formato es invalido.
    """
    try:
        rows = accounts_service.get_export_movements(
            db,
            user_id,
            account_id,
            date_from=date_from,
            date_to=date_to,
            direction=direction,
        )
    except accounts_service.AccountNotFoundError as exc:
        raise AppError(code="NOT_FOUND", message=str(exc), status_code=404) from exc
    except accounts_service.AccountForbiddenError as exc:
        raise AppError(code="NOT_AUTHORIZED", message=str(exc), status_code=403) from exc
    except ValueError as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=400) from exc
    stem = f"movimientos_{account_id}_{date_from}_{date_to}"
    if format == "csv":
        return Response(
            content=accounts_service.movements_to_csv(rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{stem}.csv"'},
        )
    if format == "pdf":
        try:
            payload = accounts_service.movements_to_pdf(
                rows,
                account_id=account_id,
                date_from=date_from,
                date_to=date_to,
            )
        except accounts_service.ExportFormatUnavailableError as exc:
            raise AppError(
                code="EXPORT_FORMAT_NOT_SUPPORTED", message=str(exc), status_code=501
            ) from exc
        return Response(
            content=payload,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{stem}.pdf"'},
        )
    try:
        payload = accounts_service.movements_to_xlsx(rows)
    except accounts_service.ExportFormatUnavailableError as exc:
        raise AppError(
            code="EXPORT_FORMAT_NOT_SUPPORTED", message=str(exc), status_code=501
        ) from exc
    return Response(
        content=payload,
        media_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        headers={"Content-Disposition": f'attachment; filename="{stem}.xlsx"'},
    )


__all__ = ["get_current_user_id", "router"]
