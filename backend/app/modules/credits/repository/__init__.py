"""Repositorio del catalogo `credits.loan_products` (E3-T02, HU09).

Capa de datos sin endpoints ni logica de negocio: solo SQLAlchemy sobre
el schema propio (`credits`), sin `JOIN` entre schemas ni imports de
modelos ajenos (regla de oro 4). Convencion: `flush` sin `commit`
(decide el llamante).

Errores de negocio con `code` estable (`docs/05`, formato de errores):
`NOT_FOUND` (producto inexistente), `VALIDATION_ERROR` (rangos/tipos
invalidos) y `DUPLICATE_PRODUCT` (conflicto de `code`, UQ).

La semilla demo (`seed_demo_products`) es idempotente por `code`
(upsert: inserta o actualiza a los valores demo; repetir no duplica).
La migracion `0029` siembra los mismos 3 productos con
`ON CONFLICT (code) DO NOTHING` (respeta tasas ya personalizadas por el
negocio); la copia SQL de la migracion esta congelada y esta lista
Python es la viva para tests/herramientas.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.credits.models import LoanProduct

logger = logging.getLogger(__name__)

__all__ = [
    "DEMO_PRODUCTS",
    "LoanProductConflictError",
    "LoanProductError",
    "LoanProductNotFoundError",
    "LoanProductValidationError",
    "create_product",
    "get_by_code",
    "get_by_id",
    "list_products",
    "seed_demo_products",
]


class LoanProductError(ValueError):
    """Error base del catalogo (mapeable a 4xx)."""

    code = "CREDITS_ERROR"


class LoanProductNotFoundError(LoanProductError):
    """Producto inexistente (`NOT_FOUND`)."""

    code = "NOT_FOUND"


class LoanProductValidationError(LoanProductError):
    """Dato de producto invalido (`VALIDATION_ERROR`)."""

    code = "VALIDATION_ERROR"


class LoanProductConflictError(LoanProductError):
    """`code` duplicado (conflicto de UQ, `DUPLICATE_PRODUCT`)."""

    code = "DUPLICATE_PRODUCT"


#: Productos demo (montos en centimos, tasas `Decimal` exactas, sin `float`).
DEMO_PRODUCTS: tuple[dict[str, Any], ...] = (
    {
        "code": "PERSONAL_LIBRE",
        "name": "Prestamo personal de libre disponibilidad",
        "tcea": Decimal("0.32500000"),
        "tea": Decimal("0.28000000"),
        "insurance_rate": Decimal("0.00350000"),
        "min_amount_minor": 100_000,
        "max_amount_minor": 5_000_000,
        "min_term_months": 6,
        "max_term_months": 36,
        "allowed_terms": [6, 12, 18, 24, 36],
        "enabled": True,
    },
    {
        "code": "VEHICULAR",
        "name": "Credito vehicular",
        "tcea": Decimal("0.18500000"),
        "tea": Decimal("0.16500000"),
        "insurance_rate": Decimal("0.00280000"),
        "min_amount_minor": 1_000_000,
        "max_amount_minor": 20_000_000,
        "min_term_months": 12,
        "max_term_months": 60,
        "allowed_terms": [12, 24, 36, 48, 60],
        "enabled": True,
    },
    {
        "code": "PYME_CAPITAL",
        "name": "Capital de trabajo PyME",
        "tcea": Decimal("0.24900000"),
        "tea": Decimal("0.22000000"),
        "insurance_rate": Decimal("0.00300000"),
        "min_amount_minor": 500_000,
        "max_amount_minor": 15_000_000,
        "min_term_months": 6,
        "max_term_months": 48,
        "allowed_terms": [6, 12, 24, 36, 48],
        "enabled": True,
    },
)

_MAX_CODE_LEN = 30
_MAX_NAME_LEN = 100
_MAX_TERM_MONTHS = 32767  # SMALLINT


def _coerce_code(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LoanProductValidationError(f"code debe ser texto no vacio, recibido: {value!r}")
    code = value.strip()
    if len(code) > _MAX_CODE_LEN:
        raise LoanProductValidationError(f"code excede {_MAX_CODE_LEN} caracteres: {code!r}")
    return code


def _coerce_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LoanProductValidationError("name debe ser texto no vacio")
    name = value.strip()
    if len(name) > _MAX_NAME_LEN:
        raise LoanProductValidationError(f"name excede {_MAX_NAME_LEN} caracteres")
    return name


def _coerce_rate(name: str, value: object) -> Decimal:
    if isinstance(value, (bool, float)):
        raise LoanProductValidationError(f"{name} debe ser Decimal/str/int (nunca float)")
    try:
        rate = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, AttributeError) as exc:
        raise LoanProductValidationError(f"{name} debe ser numerico, recibido: {value!r}") from exc
    if rate < 0:
        raise LoanProductValidationError(f"{name} debe ser >= 0, recibido: {value!r}")
    return rate


def _coerce_minor(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LoanProductValidationError(f"{name} debe ser entero de centimos, recibido: {value!r}")
    if value < 0:
        raise LoanProductValidationError(f"{name} debe ser >= 0, recibido: {value!r}")
    return value


def _coerce_term(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LoanProductValidationError(f"{name} debe ser entero de meses, recibido: {value!r}")
    if not 1 <= value <= _MAX_TERM_MONTHS:
        raise LoanProductValidationError(f"{name} debe estar en 1..{_MAX_TERM_MONTHS}")
    return value


def _coerce_terms(value: object, *, lo: int, hi: int) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not value:
        raise LoanProductValidationError("allowed_terms debe ser lista no vacia o None")
    terms: list[int] = []
    for item in value:
        term = _coerce_term("allowed_terms[]", item)
        if not lo <= term <= hi:
            raise LoanProductValidationError(
                f"allowed_terms con plazo {term} fuera del rango {lo}..{hi}"
            )
        terms.append(term)
    return terms


def _coerce_uuid(value: uuid.UUID | str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise LoanProductValidationError(f"product_id debe ser UUID, recibido: {value!r}") from exc


def _validate_ranges(
    *,
    min_amount_minor: int,
    max_amount_minor: int,
    min_term_months: int,
    max_term_months: int,
    allowed_terms: list[int] | None,
) -> None:
    if min_amount_minor > max_amount_minor:
        raise LoanProductValidationError(
            f"min_amount_minor ({min_amount_minor}) > max_amount_minor ({max_amount_minor})"
        )
    if min_term_months > max_term_months:
        raise LoanProductValidationError(
            f"min_term_months ({min_term_months}) > max_term_months ({max_term_months})"
        )
    if allowed_terms is not None:
        for term in allowed_terms:
            if not min_term_months <= term <= max_term_months:
                raise LoanProductValidationError(
                    f"allowed_terms con plazo {term} fuera del rango "
                    f"{min_term_months}..{max_term_months}"
                )


def create_product(
    session: Session,
    *,
    code: str,
    name: str,
    tcea: Decimal | str | int,
    tea: Decimal | str | int | None = None,
    insurance_rate: Decimal | str | int = Decimal(0),
    min_amount_minor: int,
    max_amount_minor: int,
    min_term_months: int,
    max_term_months: int,
    allowed_terms: list[int] | tuple[int, ...] | None = None,
    enabled: bool = True,
) -> LoanProduct:
    """Crea un producto del catalogo (valida rangos antes de escribir).

    Un `code` duplicado lanza `LoanProductConflictError` (UQ) sin dejar
    cambios. Hace `flush`, no `commit`.
    """
    clean_code = _coerce_code(code)
    clean_name = _coerce_name(name)
    clean_tcea = _coerce_rate("tcea", tcea)
    clean_tea = None if tea is None else _coerce_rate("tea", tea)
    clean_insurance = _coerce_rate("insurance_rate", insurance_rate)
    clean_min_amount = _coerce_minor("min_amount_minor", min_amount_minor)
    clean_max_amount = _coerce_minor("max_amount_minor", max_amount_minor)
    clean_min_term = _coerce_term("min_term_months", min_term_months)
    clean_max_term = _coerce_term("max_term_months", max_term_months)
    clean_terms = _coerce_terms(allowed_terms, lo=clean_min_term, hi=clean_max_term)
    if not isinstance(enabled, bool):
        raise LoanProductValidationError(f"enabled debe ser bool, recibido: {enabled!r}")
    _validate_ranges(
        min_amount_minor=clean_min_amount,
        max_amount_minor=clean_max_amount,
        min_term_months=clean_min_term,
        max_term_months=clean_max_term,
        allowed_terms=clean_terms,
    )
    row = LoanProduct(
        code=clean_code,
        name=clean_name,
        tcea=clean_tcea,
        tea=clean_tea,
        insurance_rate=clean_insurance,
        min_amount_minor=clean_min_amount,
        max_amount_minor=clean_max_amount,
        min_term_months=clean_min_term,
        max_term_months=clean_max_term,
        allowed_terms=clean_terms,
        enabled=enabled,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        logger.debug("loan_products code duplicado (%s): conflicto UQ", clean_code)
        raise LoanProductConflictError(f"code duplicado: {clean_code!r}") from exc
    return row


def get_by_code(session: Session, code: str) -> LoanProduct | None:
    """Producto por `code` (`None` si no existe)."""
    clean = _coerce_code(code)
    return session.query(LoanProduct).filter(LoanProduct.code == clean).one_or_none()


def get_by_id(session: Session, product_id: uuid.UUID | str) -> LoanProduct | None:
    """Producto por `id` (`None` si no existe; UUID malformado -> error)."""
    return session.get(LoanProduct, _coerce_uuid(product_id))


def list_products(session: Session, *, only_enabled: bool = True) -> list[LoanProduct]:
    """Catalogo ordenado por `code` (por defecto solo habilitados)."""
    query = session.query(LoanProduct).order_by(LoanProduct.code)
    if only_enabled:
        query = query.filter(LoanProduct.enabled.is_(True))
    return list(query.all())


def seed_demo_products(session: Session) -> list[LoanProduct]:
    """Siembra los productos demo (idempotente por `code`).

    Inserta o actualiza a los valores demo; repetir no duplica filas.
    Hace `flush`, no `commit`.
    """
    rows: list[LoanProduct] = []
    for spec in DEMO_PRODUCTS:
        existing = get_by_code(session, spec["code"])
        if existing is None:
            rows.append(create_product(session, **spec))
            continue
        validated = dict(spec)
        validated.pop("code")
        clean_terms = _coerce_terms(
            validated.pop("allowed_terms"),
            lo=int(validated["min_term_months"]),
            hi=int(validated["max_term_months"]),
        )
        existing.name = _coerce_name(validated["name"])
        existing.tcea = _coerce_rate("tcea", validated["tcea"])
        existing.tea = _coerce_rate("tea", validated["tea"])
        existing.insurance_rate = _coerce_rate("insurance_rate", validated["insurance_rate"])
        existing.min_amount_minor = _coerce_minor("min_amount_minor", validated["min_amount_minor"])
        existing.max_amount_minor = _coerce_minor("max_amount_minor", validated["max_amount_minor"])
        existing.min_term_months = _coerce_term("min_term_months", validated["min_term_months"])
        existing.max_term_months = _coerce_term("max_term_months", validated["max_term_months"])
        existing.allowed_terms = clean_terms
        existing.enabled = bool(validated["enabled"])
        _validate_ranges(
            min_amount_minor=existing.min_amount_minor,
            max_amount_minor=existing.max_amount_minor,
            min_term_months=existing.min_term_months,
            max_term_months=existing.max_term_months,
            allowed_terms=existing.allowed_terms,
        )
        session.flush()
        rows.append(existing)
    logger.info("loan_products semilla demo aplicada (%s productos)", len(rows))
    return rows
