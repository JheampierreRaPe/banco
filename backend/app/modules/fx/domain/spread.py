"""Spread dinamico por volumen (E7-T10, HU25).

Dominio puro: sin E/S, sin `float`, determinista. Los tramos son una
lista de `{min_amount_minor, bps}` ordenada por `min_amount_minor`
(borde inferior inclusive; el ultimo tramo no tiene maximo: rige
hasta cualquier monto mayor). El tramo aplicable a un monto es el de
mayor `min_amount_minor` que no lo supere.

Semantica de la tasa efectiva (coherente con el adaptador `E7-T08`):
el `spread_bps` son puntos basicos sobre la tasa de mercado
(`150 bps = 1.5 %`) y la tasa efectiva es

    effective = rate * (1 + bps / 10000)

cuantizada a 8 decimales con `ROUND_HALF_UP` (espejo de
`NUMERIC(18,8)` de `fx_quotes.rate`). En `fx_quotes` se congela la
tasa efectiva en `rate` y el spread como porcentaje (`bps / 100`,
p. ej. `150 bps -> 1.5`) en `spread`; el destino se calcula con
`fx.domain.target_for(source, effective, 0)`.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "EFFECTIVE_RATE_PLACES",
    "SEED_TIERS",
    "SpreadTierError",
    "apply_spread",
    "normalize_tiers",
    "spread_bps_for",
    "spread_percent_for",
]

#: Tramos semilla (conservadores; espejo del default de
#: `config.parameters fx.spread_tiers_json`, `03b#10.2`): montos en
#: centimos; `< 1,000 PEN` -> 150 bps, `>= 1,000 PEN` -> 100 bps,
#: `>= 10,000 PEN` -> 50 bps. El ultimo tramo no tiene maximo.
SEED_TIERS: tuple[tuple[int, int], ...] = ((0, 150), (100_000, 100), (1_000_000, 50))

#: Cuantizacion de la tasa efectiva (espejo de `NUMERIC(18,8)`).
EFFECTIVE_RATE_PLACES = Decimal("0.00000001")


class SpreadTierError(ValueError):
    """Tramos de spread invalidos o sin tramo aplicable."""


def _coerce_nonneg_int(value: object, field: str) -> int:
    """Entero `>= 0` (nunca `bool`/`float`; `ValueError` si no cumple)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpreadTierError(f"{field} debe ser entero de centimos/puntos")
    if value < 0:
        raise SpreadTierError(f"{field} debe ser >= 0")
    return value


def normalize_tiers(tiers: object) -> tuple[tuple[int, int], ...]:
    """Normaliza y valida los tramos (`SpreadTierError` si no cumplen).

    Acepta una secuencia no vacia de `{min_amount_minor, bps}` (mapeos
    con esas claves, o pares `(min, bps)` por conveniencia). Exige:
    `min_amount_minor` entero `>= 0`, `bps` entero `>= 0` (`0` es
    valido: tramo sin recargo) y `min_amount_minor` estrictamente
    creciente (ordenados y sin solapamientos/duplicados). Nunca
    reordena en silencio: unos tramos desordenados son invalidez, no
    un orden implicito.
    """
    if isinstance(tiers, (str, bytes, bytearray)) or not isinstance(tiers, Sequence):
        raise SpreadTierError("tiers debe ser una lista de {min_amount_minor, bps}")
    items = list(tiers)
    if not items:
        raise SpreadTierError("tiers no puede ser vacio")
    normalized: list[tuple[int, int]] = []
    for index, item in enumerate(items):
        if isinstance(item, Mapping):
            try:
                raw_min = item["min_amount_minor"]
                raw_bps = item["bps"]
            except KeyError as exc:
                raise SpreadTierError(f"tramo {index} sin clave {exc}") from exc
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            raw_min, raw_bps = item
        else:
            raise SpreadTierError(f"tramo {index} debe ser {{min_amount_minor, bps}}")
        minimum = _coerce_nonneg_int(raw_min, f"tramo {index} min_amount_minor")
        bps = _coerce_nonneg_int(raw_bps, f"tramo {index} bps")
        normalized.append((minimum, bps))
    for prev, current in itertools.pairwise(normalized):
        if current[0] <= prev[0]:
            raise SpreadTierError("tiers debe estar ordenado por min_amount_minor sin solapes")
    return tuple(normalized)


def spread_bps_for(amount_minor: int, tiers: Sequence[Mapping[str, int] | tuple[int, int]]) -> int:
    """`bps` del tramo aplicable al monto (`SpreadTierError` si no hay).

    `amount_minor` entero `>= 0` (el llamante exige `> 0` para
    cotizar); los tramos se validan con `normalize_tiers`
    (desordenados/solapados/mal tipados -> `SpreadTierError`). Si el
    monto es menor que el primer `min_amount_minor`, ningun tramo
    aplica -> `SpreadTierError` (el llamante cae al default). Con la
    semilla (primer tramo en `0`) esto nunca ocurre.
    """
    amount = _coerce_nonneg_int(amount_minor, "amount_minor")
    valid = normalize_tiers(tiers)
    applicable: int | None = None
    for minimum, bps in valid:
        if amount < minimum:
            break
        applicable = bps
    if applicable is None:
        raise SpreadTierError("sin tramo aplicable para el monto")
    return applicable


def apply_spread(rate: Decimal | int | str, spread_bps: int) -> Decimal:
    """Tasa efectiva `rate * (1 + bps/10000)` (`Decimal`, nunca `float`).

    `rate > 0` (`Decimal`; `int`/`str` se aceptan por conveniencia del
    repositorio, `float` se rechaza), `spread_bps` entero `>= 0`.
    Se cuantiza a 8 decimales con `ROUND_HALF_UP` (documentado,
    coherente con `fx_quotes.rate`); `bps = 0` devuelve la tasa
    cuantizada sin recargo. Ejemplo: `3.80` + `150 bps` -> `3.857`.
    """
    if isinstance(spread_bps, bool) or not isinstance(spread_bps, int):
        raise SpreadTierError("spread_bps debe ser entero de puntos basicos")
    if spread_bps < 0:
        raise SpreadTierError("spread_bps debe ser >= 0")
    if isinstance(rate, (bool, float)):
        raise SpreadTierError("rate debe ser Decimal (nunca float)")
    try:
        clean = rate if isinstance(rate, Decimal) else Decimal(str(rate))
    except Exception as exc:
        raise SpreadTierError("rate debe ser Decimal") from exc
    if clean.is_nan() or clean <= 0:
        raise SpreadTierError("rate debe ser > 0")
    effective = clean * (Decimal(10_000) + Decimal(spread_bps)) / Decimal(10_000)
    return effective.quantize(EFFECTIVE_RATE_PLACES, rounding=ROUND_HALF_UP)


def spread_percent_for(spread_bps: int) -> Decimal:
    """Spread como porcentaje (`bps / 100`, `Decimal`, nunca `float`).

    Es lo que se congela en `fx_quotes.spread`: `150 bps -> 1.5`.
    `spread_bps` entero `>= 0` (`SpreadTierError` si no cumple).
    """
    if isinstance(spread_bps, bool) or not isinstance(spread_bps, int):
        raise SpreadTierError("spread_bps debe ser entero de puntos basicos")
    if spread_bps < 0:
        raise SpreadTierError("spread_bps debe ser >= 0")
    return Decimal(spread_bps) / Decimal(100)
