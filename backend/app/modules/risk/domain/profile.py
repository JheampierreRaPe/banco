"""Perfil de comportamiento: actualizacion incremental pura (E6-T03, HU20).

Calcula como evoluciona `risk.risk_profiles` ante una observacion del
historial (operacion, login o fallo de autorizacion). Dominio puro y
determinista: sin E/S, sin BD, sin red, sin reloj y sin PII en la
decision. La misma entrada produce siempre la misma salida.

Firma:

    next_profile(current, observation, *, operations=0) -> ProfileUpdate

- `current`: vista con `avg_amount_minor`, `usual_countries`,
  `usual_hours`, `last_device_id` (objeto o dict; `None` = sin
  historial conocido). Solo se lee, nunca se muta.
- `observation`: `BehaviorObservation` (o dict con las mismas claves):
  `amount_minor` (entero `>= 0`), `currency` (informativa, no se
  persiste: el promedio del MVP es agnostico a la moneda, igual que la
  comparacion de `UNUSUAL_AMOUNT` en `engine.py`), `country`
  (normalizado a mayusculas), `hour` (`0..23`, UTC), `device_id`.
- `operations`: operaciones ya reflejadas en el promedio actual
  (`>= 0`). El servicio lo deriva del conteo en la ventana
  `risk.profile_window_days` (menos la operacion en curso); aqui solo
  es aritmetica.

Reglas de actualizacion:

- Promedio: media incremental entera con redondeo `ROUND_HALF_UP`
  (mitades hacia arriba), **sin `float` ni `Decimal`**: con
  `n = operations + 1`,
  `nuevo = (avg * operations + amount) / n`.
  Ejemplo: `avg=2` (1 op) + `amount=3` -> `5/2 = 2.5` -> `3`
  (con `ROUND_HALF_EVEN` daria `2`). Sin monto observado se conserva.
  Nota E6-T03 (fixes PR #26, A3): el servicio ya NO usa esta media
  incremental para persistir (recalcula el promedio real de la ventana
  con la fachada `transactions.get_window_amount_stats`, de modo que
  el arranque en frio usa el historial real); la formula queda como
  ayuda pura y documentada.
- El monto `0` NO aporta (B3, fixes PR #26): una observacion con
  `amount_minor=0` conserva el promedio (no lo diluye).
- `usual_hours` / `usual_countries`: union que preserva el orden de
  aparicion (lo existente primero, lo nuevo al final), acotada a los
  ultimos 5 valores (`MAX_USUAL_HOURS` / `MAX_USUAL_COUNTRIES`,
  supuestos aprobados). Sin habito previo, el primer valor lo funda.
- `last_device_id`: el ultimo dispositivo observado valido gana; sin
  dispositivo valido se conserva.
- `score`: nunca se toca aqui (`None` en el resultado): la severidad
  la poseen `E2-T20` (`auth.failed_attempt`) y `E6-T14` (reportes);
  este modulo solo complementa habitos.

Todo lo malformado se ignora de forma tolerante (nunca lanza): un
`amount` no entero/negativo, una `hour` fuera de `0..23`, un `country`
vacio o un `device_id` vacio simplemente no aportan. Sin PII: solo
IDs, montos, horas, paises y `device_id`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Tope de horas/paises habituales (supuesto aprobado; evita listas sin cota).
MAX_USUAL_HOURS = 5
MAX_USUAL_COUNTRIES = 5

#: Largo maximo de un codigo de pais (paridad con `risk_profiles`, `03b#11.2`).
_MAX_COUNTRY_LEN = 8
#: Largo maximo de `last_device_id` (paridad con `risk_profiles`).
_MAX_DEVICE_LEN = 128


@dataclass(frozen=True)
class BehaviorObservation:
    """Una observacion del historial (un evento ya normalizado)."""

    amount_minor: int | None = None
    currency: str | None = None
    country: str | None = None
    hour: int | None = None
    device_id: str | None = None


@dataclass(frozen=True)
class ProfileUpdate:
    """Delta a persistir (`None` = conservar el valor actual)."""

    avg_amount_minor: int | None = None
    usual_countries: list[str] | None = None
    usual_hours: list[int] | None = None
    last_device_id: str | None = None
    operations: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Vista serializable (listas copiadas)."""
        return {
            "avg_amount_minor": self.avg_amount_minor,
            "usual_countries": (
                list(self.usual_countries) if self.usual_countries is not None else None
            ),
            "usual_hours": list(self.usual_hours) if self.usual_hours is not None else None,
            "last_device_id": self.last_device_id,
            "operations": self.operations,
        }


def _read(source: Any, name: str, default: Any = None) -> Any:
    """Lee `name` de un Mapping o de atributos (default si falta)."""
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _coerce_operations(value: Any) -> int:
    """Operaciones ya reflejadas en el promedio (`>= 0`; tolerante)."""
    if isinstance(value, bool):
        return 0
    try:
        candidate = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, candidate)


def _coerce_amount(value: Any) -> int | None:
    """Monto valido (`int > 0`) o `None` (sin lanzar).

    El `0` no aporta (B3): conservar el promedio en vez de diluirlo.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _coerce_hour(value: Any) -> int | None:
    """Hora valida (`0..23`) o `None` (sin lanzar)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 23 else None


def _coerce_country(value: Any) -> str | None:
    """Pais normalizado (`MAYUSCULAS`, `1..8` chars) o `None`."""
    if not isinstance(value, str):
        return None
    clean = value.strip().upper()
    if not clean or len(clean) > _MAX_COUNTRY_LEN:
        return None
    return clean


def _coerce_device(value: Any) -> str | None:
    """Dispositivo valido (no vacio, `<= 128`) o `None`."""
    if not isinstance(value, str):
        return None
    clean = value.strip()
    if not clean or len(clean) > _MAX_DEVICE_LEN:
        return None
    return clean


def _round_half_up(numerator: int, denominator: int) -> int:
    """Cociente entero con `ROUND_HALF_UP` (mitades hacia arriba).

    Solo aritmetica entera, sin `float` (regla de oro 3): para
    `numerator, denominator >= 0`,
    `floor((2 * numerator + denominator) / (2 * denominator))`
    equivale a redondear mitades hacia arriba.
    Ejemplo: `(2*5 + 2) // (2*2) = 12 // 4 = 3` para `5/2 = 2.5`.
    """
    return (2 * numerator + denominator) // (2 * denominator)


def _merge_capped(existing: list, new_value: Any, cap: int) -> list | None:
    """Union que preserva orden de aparicion, acotada a los ultimos `cap`.

    `None` si no hay nada nuevo que aportar (se conserva lo actual).
    """
    if new_value is None:
        return None
    merged = [item for item in existing if item != new_value]
    merged.append(new_value)
    return merged[-cap:]


def _as_list(value: Any) -> list:
    """Lista actual (copia) o `[]` si no hay habito conocido."""
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def next_profile(
    current: Any | None,
    observation: BehaviorObservation | Mapping[str, Any] | None,
    *,
    operations: int = 0,
) -> ProfileUpdate:
    """Calcula el delta del perfil ante una observacion (puro).

    Nunca lanza por forma de los datos: lo malformado no aporta (ese
    campo se conserva). `operations` = operaciones ya reflejadas en
    `current.avg_amount_minor` (el servicio lo deriva de la ventana
    `risk.profile_window_days`); el promedio resultante es la media
    incremental entera con `ROUND_HALF_UP`.
    """
    obs = observation if isinstance(observation, BehaviorObservation) else None
    if obs is None:
        data = dict(observation) if isinstance(observation, Mapping) else {}
        obs = BehaviorObservation(
            amount_minor=data.get("amount_minor"),
            currency=data.get("currency"),
            country=data.get("country"),
            hour=data.get("hour"),
            device_id=data.get("device_id"),
        )

    prior_ops = _coerce_operations(operations)
    if current is None:
        prior_ops = 0
    current_avg = _read(current, "avg_amount_minor", 0)
    if isinstance(current_avg, bool) or not isinstance(current_avg, int) or current_avg < 0:
        current_avg = 0

    amount = _coerce_amount(obs.amount_minor)
    if amount is None:
        new_avg: int | None = None
        new_ops: int | None = None
    else:
        total = prior_ops + 1
        new_avg = _round_half_up(current_avg * prior_ops + amount, total)
        new_ops = total

    countries = _as_list(_read(current, "usual_countries", None))
    new_countries = _merge_capped(countries, _coerce_country(obs.country), MAX_USUAL_COUNTRIES)

    hours = [h for h in _as_list(_read(current, "usual_hours", None)) if _coerce_hour(h) == h]
    new_hours = _merge_capped(hours, _coerce_hour(obs.hour), MAX_USUAL_HOURS)

    device = _coerce_device(obs.device_id)
    new_device = device if device is not None else None

    return ProfileUpdate(
        avg_amount_minor=new_avg,
        usual_countries=new_countries,
        usual_hours=new_hours,
        last_device_id=new_device,
        operations=new_ops,
    )


__all__ = [
    "MAX_USUAL_COUNTRIES",
    "MAX_USUAL_HOURS",
    "BehaviorObservation",
    "ProfileUpdate",
    "next_profile",
]
