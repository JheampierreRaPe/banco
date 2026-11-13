"""Dominio puro de cotizaciones `fx` (E7-T09, HU25).

Regla de redondeo (supuesto aprobado por el orquestador): el
`target_amount_minor` se calcula en `Decimal` exacto (nunca `float`,
regla de oro 3) como

    target = source_amount_minor / (rate + spread)

cuantizado al entero (centavo) con `ROUND_HALF_UP`.

Ejemplo numerico: `source_amount_minor=100`, `rate=8.00`,
`spread=0` -> `100 / 8.00 = 12.5` -> `13` (`HALF_UP` sube el `.5`;
con truncamiento o `HALF_EVEN` daria `12`). La tasa+spread se
congelan en `fx_quotes` y jamas se recalculan al leer (E7-T11 las
consume tal cual; una cotizacion vencida no se ejecuta).

Semantica de la tasa (coherente con el adaptador `E7-T08`): `rate`
son unidades de `base` por 1 unidad de `quote` (p. ej. el par
`PEN/USD=3.80` significa `3.80 PEN` por cada `USD`); por eso el
monto origen (en `base`) se DIVIDE por la tasa efectiva para
obtener el destino (en `quote`): `S/ 380.00` (`38000` minor) a
`3.80` -> `$ 100.00` (`10000` minor).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

__all__ = ["target_for"]


def target_for(
    source_amount_minor: int,
    rate: Decimal,
    spread: Decimal = Decimal(0),
) -> int:
    """Destino entero en centimos (`ROUND_HALF_UP`, nunca `float`).

    `rate > 0`, `spread >= 0` (los valida el llamante con el mismo
    criterio que `fx.repository`); la tasa efectiva es `rate + spread`
    (`E7-T10` hara el spread dinamico; con el default `0` el calculo
    es `source / rate`).
    """

    effective = rate + spread
    return int(
        (Decimal(source_amount_minor) / effective).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )
