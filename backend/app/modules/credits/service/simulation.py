"""Servicio de simulacion de creditos (E3-T03, HU09).

Orquesta el catalogo (`E3-T02`) con el motor puro (`E3-T01`,
`credits/domain/financial.py`): valida el monto/plazo contra los topes
del producto, toma la tasa del producto (no del cliente) y devuelve el
agregado listo para el router. **Efimero**: solo lectura de
`credits.loan_products`; no crea `loan_applications`, no toca
`transactions`/`ledger`, no emite eventos, no hace `commit`.

Limites de la simulacion libre (sin producto), documentados en
`docs/05#64` (reglas configurables fuera del codigo: viven aqui como
constantes del modulo porque `shared/config` es de otro dueno y esta
fuera de la frontera de este brief; cambiarlos no toca datos ni
despliega tasas de productos):
- `FREE_MAX_TCEA = 5` (500% anual).
- `FREE_MAX_TERM_MONTHS = 360` (30 anios).
- `FREE_MAX_AMOUNT_MINOR = 10**12` (diez millones en moneda base).

Errores: reutiliza los del repositorio (`NOT_FOUND`, `VALIDATION_ERROR`)
y los del dominio (`FinancialValidationError` -> `VALIDATION_ERROR`).
Sin PII ni montos en logs (reglas 7 y 9 de `docs/16#2`).
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.modules.credits.domain import financial
from app.modules.credits.repository import LoanProductValidationError
from app.modules.credits.service import get_product as _catalog_get_product
from app.modules.credits.service import list_products as _catalog_list_products

logger = logging.getLogger(__name__)

__all__ = [
    "FREE_MAX_AMOUNT_MINOR",
    "FREE_MAX_TCEA",
    "FREE_MAX_TERM_MONTHS",
    "list_enabled_products",
    "run_simulation",
]

#: Tope de TCEA explicita en simulacion libre (500% anual).
FREE_MAX_TCEA = Decimal(5)
#: Plazo maximo en simulacion libre (30 anios).
FREE_MAX_TERM_MONTHS = 360
#: Monto maximo en simulacion libre (centimos enteros).
FREE_MAX_AMOUNT_MINOR = 10**12


def list_enabled_products(session: Session) -> list[dict[str, Any]]:
    """Catalogo habilitado con tasas y topes (lectura, ordenado por `code`)."""
    return _catalog_list_products(session, only_enabled=True)


def _validate_against_product(
    product: dict[str, Any], *, amount_minor: int, term_months: int
) -> None:
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
        raise LoanProductValidationError("amount_minor debe ser entero de centimos")
    if not isinstance(term_months, int) or isinstance(term_months, bool):
        raise LoanProductValidationError("term_months debe ser entero de meses")
    if not product["min_amount_minor"] <= amount_minor <= product["max_amount_minor"]:
        raise LoanProductValidationError(
            f"amount_minor fuera del rango del producto "
            f"{product['min_amount_minor']}..{product['max_amount_minor']}"
        )
    if not product["min_term_months"] <= term_months <= product["max_term_months"]:
        raise LoanProductValidationError(
            f"term_months fuera del rango del producto "
            f"{product['min_term_months']}..{product['max_term_months']}"
        )
    allowed = product["allowed_terms"]
    if allowed is not None and term_months not in allowed:
        raise LoanProductValidationError(
            f"term_months {term_months} no permitido por el producto "
            f"(permitidos: {sorted(allowed)})"
        )


def _validate_free(*, amount_minor: int, term_months: int, tcea: Decimal | None) -> Decimal:
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
        raise LoanProductValidationError("amount_minor debe ser entero de centimos")
    if not isinstance(term_months, int) or isinstance(term_months, bool):
        raise LoanProductValidationError("term_months debe ser entero de meses")
    if not 1 <= amount_minor <= FREE_MAX_AMOUNT_MINOR:
        raise LoanProductValidationError(f"amount_minor fuera del rango 1..{FREE_MAX_AMOUNT_MINOR}")
    if not 1 <= term_months <= FREE_MAX_TERM_MONTHS:
        raise LoanProductValidationError(f"term_months fuera del rango 1..{FREE_MAX_TERM_MONTHS}")
    if tcea is None or not isinstance(tcea, Decimal):
        raise LoanProductValidationError("tcea explicita requerida (string decimal)")
    if isinstance(tcea, bool) or tcea < 0 or tcea > FREE_MAX_TCEA:
        raise LoanProductValidationError(f"tcea fuera del rango 0..{FREE_MAX_TCEA}")
    return tcea


def run_simulation(
    session: Session,
    *,
    product_id: uuid.UUID | str | None,
    amount_minor: int,
    term_months: int,
    tcea: Decimal | None,
    insurance: bool = False,
) -> dict[str, Any]:
    """Simula cuota + cronograma (efimero, resuelve muy por debajo de 1 s).

    Con `product_id`: tasa = `tea` del producto (o su `tcea` si `tea` es
    `NULL`); desgravamen = `insurance_rate` si `insurance`, si no `0`.
    Sin producto: `tcea` explicita dentro de los topes libres y
    desgravamen `0` (no hay producto del cual tomarlo).
    """
    product: dict[str, Any] | None = None
    if product_id is not None:
        product = _catalog_get_product(session, product_id=product_id)
        _validate_against_product(product, amount_minor=amount_minor, term_months=term_months)
        annual_rate: Decimal = product["tea"] if product["tea"] is not None else product["tcea"]
        insurance_rate = product["insurance_rate"] if insurance else Decimal(0)
        logger.debug("simulate con producto code=%s", product["code"])
    else:
        annual_rate = _validate_free(amount_minor=amount_minor, term_months=term_months, tcea=tcea)
        insurance_rate = Decimal(0)

    sim = financial.simulate(
        amount_minor,
        annual_rate,
        term_months,
        insurance_rate,
    )
    return {
        "product": (
            {"id": product["id"], "code": product["code"], "name": product["name"]}
            if product is not None
            else None
        ),
        "amount_minor": amount_minor,
        "term_months": term_months,
        "insurance": bool(insurance),
        "tea": annual_rate,
        "tcea": sim.tcea,
        "payment_minor": sim.payment_minor,
        "total_principal_minor": sim.total_principal_minor,
        "total_interest_minor": sim.total_interest_minor,
        "total_insurance_minor": sim.total_insurance_minor,
        "total_paid_minor": sim.total_paid_minor,
        "schedule": [
            {
                "installment_no": row.installment_no,
                "due_date": row.due_date,
                "principal_minor": row.principal_minor,
                "interest_minor": row.interest_minor,
                "insurance_minor": row.insurance_minor,
                "total_minor": row.total_minor,
                "balance_minor": row.balance_minor,
            }
            for row in sim.schedule
        ],
    }
