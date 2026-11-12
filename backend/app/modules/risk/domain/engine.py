"""Motor de evaluacion antifraude sincrono (E6-T02 corte 1, HU20).

Dominio puro y determinista: sin E/S, sin BD, sin red, sin reloj y sin
PII en la decision. La misma entrada produce siempre la misma salida,
sin importar el orden de `rules` (se ordena por `priority, code`).

Firma:

    evaluate(rules, profile=None, context=None) -> RiskDecision

- `rules`: iterable de reglas con forma `RiskRule` (`code`, `enabled`,
  `priority`, `action`, `params_json`); tambien acepta dicts con esas
  claves (los tests no necesitan BD). `enabled=false` no participa.
- `profile`: vista con `avg_amount_minor`, `usual_countries` y
  `last_device_id` (objeto o dict; `None` = sin historial conocido).
- `context`: dict con los hechos de la operacion (solo IDs, montos,
  pais/hora/dispositivo; nunca nombre/documento/cuenta completa).

Reglas evaluadas (todas tolerantes a datos ausentes: lo malformado o
ausente simplemente no hace match, nunca lanza):

- `FACIAL_SPOOFING`: contexto `auth_failed_attempts` (+alias
  `failed_attempts`/`attempts`), `auth_blocked` (+alias `blocked`) y
  `auth_fail_reason` (+alias `reason`/`fail_reason`). Hace match si
  `blocked is True` (y `block_on_lock`, default `True`), si los intentos
  `>= failed_attempts_threshold` (default `3`) o si el motivo esta en el
  conjunto de spoofing (`spoofing`, `liveness_failed`, `face_mismatch`,
  `presentation_attack`, `blocked`). Motivos genericos (`invalid`,
  `expired`, `unknown`) por si solos NO hacen match.
- `ATYPICAL_GEO`: contexto `country` (+alias `country_code`) fuera de
  `profile.usual_countries` (comparacion insensible a mayusculas). Sin
  pais en el contexto o sin habito conocido -> no match.
- `UNUSUAL_AMOUNT`: `amount_minor > factor * avg_amount_minor`, con
  `factor` desde `params_json["factor"]` (si es `> 0`) o desde el
  contexto `unusual_amount_factor` o el default `3.0`; ademas respeta el
  piso `params_json["min_amount_minor"]` (default `0`). Con
  `avg_amount_minor <= 0`/ausente -> no match (no hay base de
  comparacion).
- `VELOCITY`: conteo de operaciones en la ventana
  (`operations_in_window`, alias `recent_operations_count`,
  `tx_count_in_window`, `operation_count`; o el largo de
  `recent_operation_ids`/`recent_timestamps`/`recent_operations`)
  `>= params_json["max_operations"]` (default `10`).
- `NEW_DEVICE`: `device_id` (alias `device`) distinto de
  `profile.last_device_id` (ambos no vacios). Sin dispositivo conocido
  previo -> no match (primer uso no es sospechoso por si solo).

Puntaje y decision:

- Cada regla que hace match aporta `params_json["score_bump"]`
  (alias `score`/`weight`, numerico `0..100`) o su aporte por defecto
  (`FACIAL_SPOOFING=25`, `ATYPICAL_GEO=10`, `UNUSUAL_AMOUNT=15`,
  `VELOCITY=20`, `NEW_DEVICE=5`, desconocida=`10`). `score` es la suma
  recortada a `0..100`.
- `action` es la de mayor severidad entre las reglas que hicieron
  match (`BLOCK > HOLD > ALERT > ALLOW`); sin matches -> `ALLOW`.
  Una `action` de regla desconocida se degrada a `ALERT`.
- `reason` solo nombra `code`s (`"matched 2 rule(s): ..."` o
  `"no rules matched"`): jamas montos, paises ni dispositivos.

La fachada con BD (`evaluate_pre_transaction`), el hook en
`transactions` y los consumidores son el corte 2: aqui no existen.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Decision sin efecto.
ALLOW = "ALLOW"
#: La operacion continua pero se audita (el corte 2 crea la alerta).
ALERT = "ALERT"
#: La operacion queda retenida (el corte 2 la mapea a `PENDING_AUTHORIZATION`).
HOLD = "HOLD"
#: La operacion se rechaza (el corte 2 la mapea a `REJECTED`/`RISK_BLOCKED`).
BLOCK = "BLOCK"

#: Severidad creciente: la decision es el maximo entre los matches.
_SEVERITY = {ALLOW: 0, ALERT: 1, HOLD: 2, BLOCK: 3}

FACIAL_SPOOFING = "FACIAL_SPOOFING"
ATYPICAL_GEO = "ATYPICAL_GEO"
UNUSUAL_AMOUNT = "UNUSUAL_AMOUNT"
VELOCITY = "VELOCITY"
NEW_DEVICE = "NEW_DEVICE"

#: Reglas iniciales sembradas por `0048_risk_rules` (E6-T01).
RULE_CODES = (
    FACIAL_SPOOFING,
    ATYPICAL_GEO,
    UNUSUAL_AMOUNT,
    VELOCITY,
    NEW_DEVICE,
)

#: Aporte al `score` cuando la regla no trae `score_bump` propio.
_DEFAULT_BUMPS = {
    FACIAL_SPOOFING: 25,
    ATYPICAL_GEO: 10,
    UNUSUAL_AMOUNT: 15,
    VELOCITY: 20,
    NEW_DEVICE: 5,
}
_DEFAULT_UNKNOWN_BUMP = 10

#: Umbrales por defecto (los efectivos viven en `params_json` o en
#: `config.parameters`; aqui solo el fallback documentado).
DEFAULT_FAILED_ATTEMPTS_THRESHOLD = 3
DEFAULT_UNUSUAL_AMOUNT_FACTOR = 3.0
DEFAULT_VELOCITY_MAX_OPERATIONS = 10
DEFAULT_PRIORITY = 100

#: Motivos de `auth.failed_attempt` que senalan suplantacion/spoofing
#: (normalizados a minusculas). Los genericos (`invalid`, `expired`,
#: `unknown`) NO hacen match por si solos.
_SPOOFING_REASONS = frozenset(
    {
        "spoofing",
        "liveness_failed",
        "face_mismatch",
        "presentation_attack",
        "blocked",
    }
)


@dataclass(frozen=True)
class RiskDecision:
    """Decision del motor (valor inmutable, sin PII)."""

    action: str = ALLOW
    score: int = 0
    matched_rules: tuple[str, ...] = ()
    reason: str = "no rules matched"

    def to_dict(self) -> dict[str, Any]:
        """Vista serializable (`matched_rules` como lista)."""
        return {
            "action": self.action,
            "score": self.score,
            "matched_rules": list(self.matched_rules),
            "reason": self.reason,
        }


def evaluate(
    rules: Any,
    profile: Any | None = None,
    context: Mapping[str, Any] | None = None,
) -> RiskDecision:
    """Evalua las reglas habilitadas contra el perfil y el contexto.

    Pura y determinista: ordena por (`priority`, `code`), ignora
    `enabled=false` y codigos desconocidos, y nunca lanza por forma de
    los datos (lo malformado no hace match). Sin E/S ni PII.
    """
    ctx = dict(context) if isinstance(context, Mapping) else {}
    ordered = _enabled_rules_sorted(rules)
    matched: list[str] = []
    action = ALLOW
    total = 0
    for code, rule_action, params in ordered:
        if _matches(code, params, profile, ctx):
            matched.append(code)
            total += _rule_bump(code, params)
            if _SEVERITY[rule_action] > _SEVERITY[action]:
                action = rule_action
    score = max(0, min(100, int(total)))
    if not matched:
        return RiskDecision()
    return RiskDecision(
        action=action,
        score=score,
        matched_rules=tuple(matched),
        reason=f"matched {len(matched)} rule(s): {', '.join(matched)}",
    )


# ---------------------------------------------------------------------------
# Normalizacion de entradas (tolerante: nunca lanza)
# ---------------------------------------------------------------------------


def _read(source: Any, name: str, default: Any = None) -> Any:
    """Lee `name` de un Mapping o de atributos (default si falta)."""
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _ctx_first(context: Mapping[str, Any], *names: str) -> Any:
    """Primer valor presente y no-`None` entre los alias dados."""
    for name in names:
        if name in context and context[name] is not None:
            return context[name]
    return None


def _to_int(value: Any) -> int | None:
    """Entero o `None` (sin lanzar; `bool` no es entero valido)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (ValueError, AttributeError):
            return None
    return None


def _to_float(value: Any) -> float | None:
    """Flotante finito o `None` (sin lanzar; `bool` no es valido)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        candidate = float(value)
    elif isinstance(value, str):
        try:
            candidate = float(value.strip())
        except (ValueError, AttributeError):
            return None
    else:
        return None
    if math.isnan(candidate) or math.isinf(candidate):
        return None
    return candidate


def _positive_int(raw: Any, default: int) -> int:
    candidate = _to_int(raw)
    if candidate is None or candidate <= 0:
        return default
    return candidate


def _nonneg_int(raw: Any, default: int) -> int:
    candidate = _to_int(raw)
    if candidate is None or candidate < 0:
        return default
    return candidate


def _positive_float(raw: Any, default: float) -> float:
    candidate = _to_float(raw)
    if candidate is None or candidate <= 0:
        return default
    return candidate


def _enabled_rules_sorted(rules: Any) -> list[tuple[str, str, dict[str, Any]]]:
    """Reglas habilitadas como `(code, action, params)` ordenadas.

    Orden determinista por (`priority`, `code`); `priority` invalida ->
    `100`. Sin `code` valido o con `enabled` falsy -> se excluye (una
    regla sin `enabled` informado se evalua: fallo cerrado). `action`
    desconocida se degrada a `ALERT`.
    """
    if rules is None:
        return []
    try:
        items = list(rules)
    except TypeError:
        return []
    normalized: list[tuple[int, str, str, dict[str, Any]]] = []
    for item in items:
        code = _read(item, "code")
        if not isinstance(code, str) or not code.strip():
            continue
        code = code.strip().upper()
        enabled = _read(item, "enabled", True)
        if enabled is None:
            enabled = True
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in ("", "0", "false", "no", "off")
        if not bool(enabled):
            continue
        priority = _to_int(_read(item, "priority"))
        if priority is None:
            priority = DEFAULT_PRIORITY
        action = _read(item, "action")
        action = action.strip().upper() if isinstance(action, str) else ALERT
        if action not in _SEVERITY or action == ALLOW:
            action = ALERT if action != ALLOW else ALLOW
        params = _read(item, "params_json", None)
        if params is None:
            params = _read(item, "params", None)
        params = dict(params) if isinstance(params, Mapping) else {}
        normalized.append((priority, code, action, params))
    normalized.sort(key=lambda row: (row[0], row[1]))
    return [(code, action, params) for _, code, action, params in normalized]


def _rule_bump(code: str, params: Mapping[str, Any]) -> int:
    """Aporte al `score` de una regla con match (recortado a `0..100`)."""
    for key in ("score_bump", "score", "weight"):
        if key in params:
            candidate = _to_float(params[key])
            if candidate is not None:
                return max(0, min(100, int(candidate)))
    return _DEFAULT_BUMPS.get(code, _DEFAULT_UNKNOWN_BUMP)


# ---------------------------------------------------------------------------
# Evaluadores por regla (puros: `bool`, sin efectos)
# ---------------------------------------------------------------------------


def _match_facial_spoofing(
    params: Mapping[str, Any], profile: Any, context: Mapping[str, Any]
) -> bool:
    nested = context.get("auth_failed_attempt")
    merged = dict(nested) if isinstance(nested, Mapping) else {}
    for key in (
        "auth_failed_attempts",
        "failed_attempts",
        "attempts",
        "auth_blocked",
        "blocked",
        "auth_fail_reason",
        "reason",
        "fail_reason",
    ):
        if key in context and context[key] is not None:
            merged[key] = context[key]
    blocked = merged.get("auth_blocked", merged.get("blocked"))
    attempts = _to_int(
        merged.get("auth_failed_attempts", merged.get("failed_attempts", merged.get("attempts")))
    )
    reason = merged.get("auth_fail_reason", merged.get("reason", merged.get("fail_reason")))
    threshold = _positive_int(
        params.get("failed_attempts_threshold"),
        DEFAULT_FAILED_ATTEMPTS_THRESHOLD,
    )
    block_on_lock = params.get("block_on_lock", True)
    if isinstance(block_on_lock, str):
        block_on_lock = block_on_lock.strip().lower() in ("1", "true", "yes", "y", "t")
    else:
        block_on_lock = bool(block_on_lock)
    if block_on_lock and blocked is True:
        return True
    if attempts is not None and attempts >= threshold:
        return True
    return isinstance(reason, str) and reason.strip().lower() in _SPOOFING_REASONS


def _match_atypical_geo(
    params: Mapping[str, Any], profile: Any, context: Mapping[str, Any]
) -> bool:
    _ = params
    country = _ctx_first(context, "country", "country_code")
    if not isinstance(country, str) or not country.strip():
        return False
    usual = _read(profile, "usual_countries")
    if not isinstance(usual, (list, tuple)):
        return False
    known = {item.strip().upper() for item in usual if isinstance(item, str) and item.strip()}
    if not known:
        return False
    return country.strip().upper() not in known


def _match_unusual_amount(
    params: Mapping[str, Any], profile: Any, context: Mapping[str, Any]
) -> bool:
    amount = _to_int(_ctx_first(context, "amount_minor", "amount"))
    avg = _to_int(_read(profile, "avg_amount_minor"))
    if amount is None or avg is None or avg <= 0 or amount < 0:
        return False
    factor = _positive_float(
        params.get("factor", _ctx_first(context, "unusual_amount_factor")),
        DEFAULT_UNUSUAL_AMOUNT_FACTOR,
    )
    floor = _nonneg_int(params.get("min_amount_minor"), 0)
    if amount < floor:
        return False
    return amount > factor * avg


def _match_velocity(params: Mapping[str, Any], profile: Any, context: Mapping[str, Any]) -> bool:
    _ = profile
    max_operations = _positive_int(
        params.get("max_operations"),
        DEFAULT_VELOCITY_MAX_OPERATIONS,
    )
    count = _to_int(
        _ctx_first(
            context,
            "operations_in_window",
            "recent_operations_count",
            "tx_count_in_window",
            "operation_count",
        )
    )
    if count is None:
        for key in ("recent_operation_ids", "recent_timestamps", "recent_operations"):
            value = context.get(key)
            if isinstance(value, (list, tuple)):
                count = len(value)
                break
    if count is None:
        return False
    return count >= max_operations


def _match_new_device(params: Mapping[str, Any], profile: Any, context: Mapping[str, Any]) -> bool:
    _ = params
    device = _ctx_first(context, "device_id", "device")
    last = _read(profile, "last_device_id")
    if not isinstance(device, str) or not device.strip():
        return False
    if not isinstance(last, str) or not last.strip():
        return False
    return device.strip() != last.strip()


_MATCHERS = {
    FACIAL_SPOOFING: _match_facial_spoofing,
    ATYPICAL_GEO: _match_atypical_geo,
    UNUSUAL_AMOUNT: _match_unusual_amount,
    VELOCITY: _match_velocity,
    NEW_DEVICE: _match_new_device,
}


def _matches(
    code: str,
    params: Mapping[str, Any],
    profile: Any,
    context: Mapping[str, Any],
) -> bool:
    """Despacha al evaluador de `code` (desconocido -> sin match)."""
    matcher = _MATCHERS.get(code)
    if matcher is None:
        return False
    return bool(matcher(params, profile, context))


__all__ = [
    "ALERT",
    "ALLOW",
    "ATYPICAL_GEO",
    "BLOCK",
    "DEFAULT_FAILED_ATTEMPTS_THRESHOLD",
    "DEFAULT_UNUSUAL_AMOUNT_FACTOR",
    "DEFAULT_VELOCITY_MAX_OPERATIONS",
    "FACIAL_SPOOFING",
    "HOLD",
    "NEW_DEVICE",
    "RULE_CODES",
    "UNUSUAL_AMOUNT",
    "VELOCITY",
    "RiskDecision",
    "evaluate",
]
