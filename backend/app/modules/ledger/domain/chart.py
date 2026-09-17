"""Catalogo contable y reglas de subcuentas (E5-T09, HU18).

Dominio puro: sin BD ni SQLAlchemy. Testeable sin base de datos.

Fuente: `docs/04-motor-transaccional-y-ledger.md#21-subcuentas-de-un-cliente`
y `docs/03b-diccionario-de-datos.md#71-ledger_accounts`.

Reglas:
- Catalogo minimo de 11 cuentas del sistema (`is_system=True`).
- Cada cuenta de cliente tiene dos subcuentas:
  `2000-<cuenta>` (disponible) y `2100-<cuenta>` (retenido).
- `code` unico en `ledger_accounts`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

# Cuentas raiz del catalogo minimo exigido por el brief.
CATALOG_CODES: tuple[str, ...] = (
    "1000",
    "2000",
    "2100",
    "2200",
    "3000",
    "4000",
    "4100",
    "4200",
    "5000",
    "6000",
    "9100",
)

# Tipos del CK `ledger_account_type` (ver `03-modelo-de-datos.md`).
ACCOUNT_TYPES: frozenset[str] = frozenset({"asset", "liability", "equity", "income", "expense"})

OWNER_TYPES: frozenset[str] = frozenset({"CUSTOMER", "SYSTEM", "MERCHANT", "POOL"})


@dataclass(frozen=True)
class CatalogEntry:
    """Fila del catalogo semilla del sistema."""

    code: str
    name: str
    type: str  # asset/liability/equity/income/expense
    currency: str = "PEN"
    owner_type: str = "SYSTEM"
    is_system: bool = True


# Semilla del catalogo. Supuesto documentado: tipos asignados por naturaleza
# contable (pasivo para 2000/2100/2200, patrimonio 3000, ingresos 4xxx,
# gastos 5xxx/6xxx; 9100 orden como asset para cumplir el CK de 5 tipos).
SYSTEM_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry("1000", "Caja y bancos", "asset"),
    CatalogEntry("2000", "Depositos de clientes - disponible", "liability"),
    CatalogEntry("2100", "Depositos de clientes - retenido", "liability"),
    CatalogEntry("2200", "Otras obligaciones", "liability"),
    CatalogEntry("3000", "Capital", "equity"),
    CatalogEntry("4000", "Ingresos por intereses", "income"),
    CatalogEntry("4100", "Ingresos por comisiones", "income"),
    CatalogEntry("4200", "Otros ingresos", "income"),
    CatalogEntry("5000", "Costos financieros", "expense"),
    CatalogEntry("6000", "Gastos operativos", "expense"),
    CatalogEntry("9100", "Cuentas de orden", "asset"),
)

AVAILABLE_PREFIX = "2000"
HOLD_PREFIX = "2100"


def validate_currency(currency: str) -> str:
    """Valida ISO-4217 basico: 3 letras mayusculas."""
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        raise ValueError(f"currency debe ser ISO-4217 de 3 letras, recibido: {currency!r}")
    upper = currency.upper()
    if upper != currency:
        raise ValueError(f"currency debe ser mayusculas, recibido: {currency!r}")
    return upper


def coerce_account_ref(account_ref: uuid.UUID | str) -> uuid.UUID:
    """Normaliza el `account_ref` a UUID (referencia logica, sin FK fisica)."""
    if isinstance(account_ref, uuid.UUID):
        return account_ref
    try:
        return uuid.UUID(str(account_ref))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"account_ref debe ser UUID, recibido: {account_ref!r}") from exc


def customer_codes(account_ref: uuid.UUID | str) -> tuple[str, str]:
    """Retorna `(disponible, retenido)` para una cuenta de cliente.

    Formato exigido por `04#2.1`: `2000-<cuenta>` y `2100-<cuenta>`,
    donde `<cuenta>` es el UUID completo de la cuenta logica.
    """
    ref = coerce_account_ref(account_ref)
    return (f"{AVAILABLE_PREFIX}-{ref}", f"{HOLD_PREFIX}-{ref}")


def validate_catalog(catalog: tuple[CatalogEntry, ...] = SYSTEM_CATALOG) -> list[str]:
    """Verifica el catalogo semilla. Retorna lista de errores (vacia = OK)."""
    errors: list[str] = []
    seen: set[str] = set()
    for entry in catalog:
        if entry.code in seen:
            errors.append(f"code duplicado: {entry.code}")
        seen.add(entry.code)
        if entry.type not in ACCOUNT_TYPES:
            errors.append(f"{entry.code}: type invalido {entry.type!r}")
        if entry.owner_type not in OWNER_TYPES:
            errors.append(f"{entry.code}: owner_type invalido {entry.owner_type!r}")
        try:
            validate_currency(entry.currency)
        except ValueError as exc:
            errors.append(f"{entry.code}: {exc}")
    missing = [c for c in CATALOG_CODES if c not in seen]
    if missing:
        errors.append(f"faltan cuentas del catalogo minimo: {missing}")
    return errors
