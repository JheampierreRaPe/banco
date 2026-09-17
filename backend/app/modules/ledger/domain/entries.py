"""Asientos de partida doble: validacion de cuadre y hash encadenado (E5-T10, HU18).

Dominio puro: sin BD ni SQLAlchemy. Testeable sin base de datos.

Fuente: `docs/04-motor-transaccional-y-ledger.md#2-contabilidad-de-partida-doble-obligatoria`
y `#79-reverso-compensacion`, `docs/03b-diccionario-de-datos.md#72-journal_entries`
y `#73-postings-append-only`.

Reglas:
- Por moneda: `sum(DEBIT) == sum(CREDIT)`; si no cuadra, el asiento no se crea.
- Dinero entero en centimos (`amount_minor: int > 0`); nunca `float`.
- `direction` y `status` en MAYUSCULAS segun `03b#1`
  (`posting_direction=DEBIT/CREDIT`, `journal_status=POSTED/REVERSED`).
- `hash` SHA-256 en hex (64 chars) sobre campos canonicos del asiento +
  postings ordenados + `prev_hash` (genesis: `prev_hash None`).
- El reverso invierte direcciones y referencia al original
  (`reverses_entry_id`); nunca se edita ni se borra.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date

POSTING_DIRECTIONS: frozenset[str] = frozenset({"DEBIT", "CREDIT"})
JOURNAL_STATUSES: frozenset[str] = frozenset({"POSTED", "REVERSED"})

# Entrada por defecto cuando el reverso no indica otro concepto.
REVERSAL_ENTRY_TYPE = "REVERSAL"


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def validate_direction(direction: str) -> str:
    """Valida `posting_direction` (`03b#1`): `DEBIT`/`CREDIT` en mayusculas."""
    if direction not in POSTING_DIRECTIONS:
        raise ValueError(
            f"direction debe ser una de {sorted(POSTING_DIRECTIONS)}, " f"recibido: {direction!r}"
        )
    return direction


def validate_amount(amount_minor: int) -> int:
    """Valida dinero entero en centimos: `int > 0`, nunca `float` (regla de oro 3)."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise TypeError(f"amount_minor debe ser entero en centimos, recibido: {amount_minor!r}")
    if amount_minor <= 0:
        raise ValueError(f"amount_minor debe ser > 0, recibido: {amount_minor!r}")
    return amount_minor


def validate_currency(currency: str) -> str:
    """Valida ISO-4217 basico: 3 letras mayusculas."""
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        raise ValueError(f"currency debe ser ISO-4217 de 3 letras, recibido: {currency!r}")
    if currency != currency.upper():
        raise ValueError(f"currency debe ser mayusculas, recibido: {currency!r}")
    return currency


def validate_entry_type(entry_type: str) -> str:
    """Valida concepto contable (`03b#7.2`: `VARCHAR(30)`, no nulo)."""
    if not isinstance(entry_type, str) or not entry_type.strip():
        raise ValueError(f"entry_type no puede ser vacio, recibido: {entry_type!r}")
    if len(entry_type) > 30:
        raise ValueError(f"entry_type supera VARCHAR(30), recibido: {entry_type!r}")
    return entry_type


@dataclass(frozen=True)
class PostingInput:
    """Movimiento de un asiento, en centimos enteros (sin `float`)."""

    ledger_account_id: uuid.UUID
    direction: str  # DEBIT | CREDIT
    amount_minor: int  # > 0
    currency: str  # ISO-4217 en mayusculas
    account_ref: uuid.UUID | None = None  # cuenta de cliente (logica)


def normalize_posting(raw: PostingInput | dict) -> PostingInput:
    """Normaliza un dict o `PostingInput` validando tipos y formatos."""
    if isinstance(raw, PostingInput):
        data = {
            "ledger_account_id": raw.ledger_account_id,
            "direction": raw.direction,
            "amount_minor": raw.amount_minor,
            "currency": raw.currency,
            "account_ref": raw.account_ref,
        }
    elif isinstance(raw, dict):
        data = dict(raw)
    else:
        raise TypeError(f"posting debe ser PostingInput o dict, recibido: {raw!r}")
    account_ref = data.get("account_ref")
    return PostingInput(
        ledger_account_id=_coerce_uuid(data["ledger_account_id"], "ledger_account_id"),
        direction=validate_direction(data["direction"]),
        amount_minor=validate_amount(data["amount_minor"]),
        currency=validate_currency(data["currency"]),
        account_ref=(None if account_ref is None else _coerce_uuid(account_ref, "account_ref")),
    )


def validate_balanced(items: list[PostingInput]) -> list[PostingInput]:
    """Validador de cuadre contable (E5-T11, HU18 CA-01/CA-03).

    Pieza explicita del agregado: exige `sum(DEBIT) == sum(CREDIT)` por
    moneda con igualdad exacta de enteros en centimos (sin tolerancias,
    sin redondeos, sin flags de desactivacion). Lanza `ValueError` si
    alguna moneda descuadra. No toca BD.
    """
    totals: dict[str, dict[str, int]] = {}
    for item in items:
        bucket = totals.setdefault(item.currency, {"DEBIT": 0, "CREDIT": 0})
        bucket[item.direction] += item.amount_minor
    for currency, sums in totals.items():
        if sums["DEBIT"] != sums["CREDIT"]:
            raise ValueError(
                f"asiento descuadrado en {currency}: "
                f"DEBIT={sums['DEBIT']} != CREDIT={sums['CREDIT']}"
            )
    return items


def validate_postings(postings: list[PostingInput | dict]) -> list[PostingInput]:
    """Valida que el asiento cuadre por moneda (`04#2`).

    Exige al menos 2 movimientos y delega el cuadre en `validate_balanced`
    (siempre, sin opcion de omitirlo). Retorna los movimientos
    normalizados. Lanza `ValueError` si descuadra.
    """
    if not isinstance(postings, list) or len(postings) < 2:
        raise ValueError("un asiento requiere al menos 2 postings")
    items = [normalize_posting(p) for p in postings]
    return validate_balanced(items)


def invert_direction(direction: str) -> str:
    """Invierte la direccion de un movimiento (base del reverso, `04#7.9`)."""
    validate_direction(direction)
    return "CREDIT" if direction == "DEBIT" else "DEBIT"


def build_reversal_inputs(postings: list[PostingInput]) -> list[PostingInput]:
    """Construye los movimientos compensatorios con direcciones invertidas."""
    if not postings:
        raise ValueError("el asiento original no tiene postings para revertir")
    return [
        PostingInput(
            ledger_account_id=p.ledger_account_id,
            direction=invert_direction(p.direction),
            amount_minor=p.amount_minor,
            currency=p.currency,
            account_ref=p.account_ref,
        )
        for p in postings
    ]


def _canonical_posting(p: PostingInput) -> str:
    return "|".join(
        (
            str(p.ledger_account_id),
            p.direction,
            str(p.amount_minor),
            p.currency,
            str(p.account_ref) if p.account_ref is not None else "",
        )
    )


def compute_entry_hash(
    *,
    entry_type: str,
    description: str | None,
    value_date: date,
    transaction_id: uuid.UUID | None,
    postings: list[PostingInput],
    prev_hash: str | None,
) -> str:
    """Calcula el hash encadenado SHA-256 en hex (64 chars) del asiento.

    Formato canonico: campos del asiento + postings ordenados + `prev_hash`
    (genesis: `prev_hash None` -> campo vacio), unidos por `"\\n"`.
    """
    entry_type = validate_entry_type(entry_type)
    if not isinstance(value_date, date):
        raise TypeError(f"value_date debe ser DATE, recibido: {value_date!r}")
    if transaction_id is not None:
        transaction_id = _coerce_uuid(transaction_id, "transaction_id")
    if prev_hash is not None and (not isinstance(prev_hash, str) or len(prev_hash) != 64):
        raise ValueError(f"prev_hash debe ser hex de 64 chars, recibido: {prev_hash!r}")
    ordered = sorted(_canonical_posting(p) for p in postings)
    canonical = "\n".join(
        [
            entry_type,
            description or "",
            value_date.isoformat(),
            str(transaction_id) if transaction_id is not None else "",
            *ordered,
            prev_hash or "",
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
