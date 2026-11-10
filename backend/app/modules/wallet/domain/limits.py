"""Regla pura del pago express de billetera (E4-T02, HU13 CA-02/CA-04).

Decide, sin tocar BD ni relojes, que camino sigue un pago express
segun el **limite base** por operacion (`wallet.express_limit_minor`)
y el **limite diario** (`wallet.daily_limit_minor`):

- `EXPRESS`: `total <= limite base` y cabe en el diario -> sin reto
  adicional del servidor (el cliente puede pedir confirmacion local).
- `REQUIRES_REINFORCED`: supera el limite base pero cabe en el diario
  -> seguridad reforzada (biometria del dispositivo o PIN, patron del
  motor `E2-T17`/`E2-T31`: `PENDING_AUTHORIZATION` + nonce).
- `LIMIT_EXCEEDED`: `acumulado + total > limite diario` -> rechazo
  (`DAILY_LIMIT_EXCEEDED`) sin debito ni retencion (semantica
  `E2-T36`, re-chequeo al autorizar).

Puro y determinista: mismos argumentos -> misma decision. Dinero
entero en centimos; nunca `float` (regla de oro 3). El motor
(`transactions.service.transfer_third_party` con los lectores de
`wallet.service.express`) es el ejecutor autoritativo; esta regla
solo anticipa el camino para observabilidad/UX.
"""

from __future__ import annotations

from typing import Literal

#: Pago dentro del limite base: sin reto del servidor.
EXPRESS = "EXPRESS"
#: Pago sobre el limite base: exige biometria/PIN del motor.
REQUIRES_REINFORCED = "REQUIRES_REINFORCED"
#: Acumulado + total supera el diario: rechazo sin mover fondos.
LIMIT_EXCEEDED = "LIMIT_EXCEEDED"

#: Decision valida de `decide_express`.
ExpressDecision = Literal["EXPRESS", "REQUIRES_REINFORCED", "LIMIT_EXCEEDED"]


def _require_minor(name: str, value: object, *, allow_zero: bool) -> int:
    """Entero de centimos validado (`bool`/`float`/texto -> error)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} debe ser entero de centimos, recibido: {value!r}")
    floor = 0 if allow_zero else 1
    if value < floor:
        raise ValueError(f"{name} debe ser >= {floor}, recibido: {value}")
    return value


def decide_express(
    total_minor: int,
    accumulated_minor: int,
    *,
    express_limit_minor: int,
    daily_limit_minor: int,
) -> ExpressDecision:
    """Decide el camino del pago express (pura, sin efectos).

    `total_minor = amount_minor + fee_minor` (el motor compara el total,
    `E2-T34`); `accumulated_minor` es lo ya comprometido hoy por el
    titular (misma semantica que el acumulado del motor: pendientes
    incluidas, `E2-T36`). El limite diario se evalua primero (manda
    sobre el base, como el gate del motor).
    """
    total = _require_minor("total_minor", total_minor, allow_zero=False)
    accumulated = _require_minor("accumulated_minor", accumulated_minor, allow_zero=True)
    express_limit = _require_minor("express_limit_minor", express_limit_minor, allow_zero=True)
    daily_limit = _require_minor("daily_limit_minor", daily_limit_minor, allow_zero=True)
    if accumulated + total > daily_limit:
        return LIMIT_EXCEEDED
    if total > express_limit:
        return REQUIRES_REINFORCED
    return EXPRESS


__all__ = [
    "EXPRESS",
    "LIMIT_EXCEEDED",
    "REQUIRES_REINFORCED",
    "ExpressDecision",
    "decide_express",
]
