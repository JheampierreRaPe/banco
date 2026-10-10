"""Fachada interna del catalogo `credits.loan_products` (E3-T02, HU09).

Punto de entrada estable para los consumidores (E3-T01 motor de calculo,
E3-T03 endpoint de simulacion): expone los productos habilitados con sus
tasas y topes como diccionarios planos (tasas `Decimal` exactas, dinero
en centimos enteros; sin `float`), sin exponer la sesion ni los modelos
ORM. Solo lectura del schema propio; sin `JOIN` entre schemas ni
imports de modelos ajenos (regla de oro 4).

Un cambio de tasa/tope en datos se refleja de inmediato (lectura
directa, sin cache en proceso): no requiere despliegue (regla de oro 6).

Errores: `NOT_FOUND` (producto inexistente), `VALIDATION_ERROR`
(selector ausente/duplicado o `product_id` malformado). Re-exporta los
errores del repositorio para los consumidores.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.modules.credits import repository as credits_repo
from app.modules.credits.models import LoanProduct
from app.modules.credits.repository import (
    LoanProductConflictError,
    LoanProductError,
    LoanProductNotFoundError,
    LoanProductValidationError,
)

__all__ = [
    "LoanProductConflictError",
    "LoanProductError",
    "LoanProductNotFoundError",
    "LoanProductValidationError",
    "get_product",
    "list_products",
    "to_dict",
]


def to_dict(product: LoanProduct) -> dict[str, Any]:
    """Vista plana del producto (tasas `Decimal`, montos en centimos)."""
    return {
        "id": product.id,
        "code": product.code,
        "name": product.name,
        "tcea": product.tcea,
        "tea": product.tea,
        "insurance_rate": product.insurance_rate,
        "min_amount_minor": product.min_amount_minor,
        "max_amount_minor": product.max_amount_minor,
        "min_term_months": product.min_term_months,
        "max_term_months": product.max_term_months,
        "allowed_terms": (
            list(product.allowed_terms) if product.allowed_terms is not None else None
        ),
        "enabled": product.enabled,
    }


def list_products(session: Session, *, only_enabled: bool = True) -> list[dict[str, Any]]:
    """Catalogo ordenado por `code` (por defecto solo habilitados).

    Los deshabilitados no se listan a clientes; el admin puede pedirlos
    con `only_enabled=False`.
    """
    return [to_dict(row) for row in credits_repo.list_products(session, only_enabled=only_enabled)]


def get_product(
    session: Session,
    *,
    code: str | None = None,
    product_id: uuid.UUID | str | None = None,
) -> dict[str, Any]:
    """Un producto por `code` o por `product_id` (exactamente uno).

    Lanza `LoanProductValidationError` si faltan o sobran selectores (o
    si `product_id` no es UUID) y `LoanProductNotFoundError` si no existe.
    """
    if (code is None) == (product_id is None):
        raise LoanProductValidationError("get_product exige exactamente uno de code / product_id")
    row: LoanProduct | None
    if code is not None:
        if not isinstance(code, str) or not code.strip():
            raise LoanProductValidationError(f"code debe ser texto no vacio, recibido: {code!r}")
        row = credits_repo.get_by_code(session, code)
        label = f"code={code.strip()!r}"
    else:
        row = credits_repo.get_by_id(session, product_id)
        label = f"product_id={product_id!r}"
    if row is None:
        raise LoanProductNotFoundError(f"producto inexistente ({label})")
    if not isinstance(row.tcea, Decimal):
        raise LoanProductValidationError(f"tcea corrupta en {label}")  # pragma: no cover
    return to_dict(row)
