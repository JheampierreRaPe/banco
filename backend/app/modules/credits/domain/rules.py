"""Motor de reglas de credito puro y configurable (E3-T07, HU10).

Dominio puro: sin BD, sin I/O, sin red, sin estado global y sin logs con
PII. Decide ``APPROVED`` / ``REJECTED`` / ``IN_REVIEW`` (valores del enum
``loan_application_status``, ``docs/03b#1``) con ``reason`` legible y
estable, listo para persistir en ``loan_applications.decision_reason``
(auditoria; la persistencia y los eventos los hace E3-T08).

Entradas: montos en centimos ``int``, plazo en meses, ``BureauScore``
(contrato de E3-T06; ``None`` si el bureau cayo) y ``params`` (foto de
``config.parameters``). El dueno del esquema es ``shared/config``: las
constantes ``LOAN_*_KEY`` / ``DEFAULT_*`` de este modulo son un espejo
documentado de ``app.modules.shared.config.parameters`` (verificado por
``backend/tests/test_e3_t07_credit_rules.py::test_defaults_mirror_shared_config``).

Dinero entero en centimos; ratios como :class:`~decimal.Decimal` (jamas
``float``, regla de oro 3).

Orden de evaluacion (determinista; el primer hallazgo manda en ``reason``):

1. Validacion (``CreditRulesValidationError`` -> ``VALIDATION_ERROR``).
2. DTI (``total_debt / total_income > loan.max_dti_ratio`` -> ``REJECTED``).
3. Tope de monto (``requested > tope por perfil`` -> ``REJECTED``).
4. Tope de deuda (``total_debt > loan.max_debt_minor`` -> ``REJECTED``).
5. Score (``< min`` -> ``REJECTED``; banda ``[min, min + margin)`` o
   bureau caido -> ``IN_REVIEW``).
6. Resto -> ``APPROVED``.

Los topes financieros (2-4) se evaluan antes que el bureau: un hecho
financiero adverso rechaza aunque el bureau este caido; si pasan y el
bureau falta, el caso va a revision (el reintento lo gobierna E3-T08).

``params`` admite valores crudos de ``value_json`` (``int`` / ``float`` /
``str`` / ``Decimal``): la semilla ``0001`` guardo ``loan.max_dti_ratio``
como texto ``"0.4"`` y el motor lo acepta igual que el numero ``0.4``.
Clave ausente o valor invalido -> fallback documentado (``DEFAULT_*``);
los parametros nunca lanzan.

La ``morosidad`` del bureau no altera el dictamen en esta version: se
conserva en el expediente (E3-T08, historial interno) pero la decision
sigue solo DTI + topes + score (``docs/08#hu10`` y ``docs/01#11``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

__all__ = [
    "APPROVED",
    "DEFAULT_MAX_AMOUNT_MINOR",
    "DEFAULT_MAX_DEBT_MINOR",
    "DEFAULT_MAX_DTI_RATIO",
    "DEFAULT_MIN_SCORE",
    "DEFAULT_REVIEW_MARGIN",
    "IN_REVIEW",
    "LOAN_MAX_AMOUNT_MINOR_KEY",
    "LOAN_MAX_AMOUNT_PROFILE_TEMPLATE",
    "LOAN_MAX_DEBT_MINOR_KEY",
    "LOAN_MAX_DTI_RATIO_KEY",
    "LOAN_MIN_SCORE_KEY",
    "LOAN_REVIEW_MARGIN_KEY",
    "REJECTED",
    "CreditDecision",
    "CreditRulesValidationError",
    "compute_dti",
    "evaluate",
]

#: Dictamenes (enum `loan_application_status`, `docs/03b#1`).
APPROVED = "APPROVED"
REJECTED = "REJECTED"
IN_REVIEW = "IN_REVIEW"

#: Claves en `config.parameters` (espejo de `shared/config`, dueno).
LOAN_MAX_DTI_RATIO_KEY = "loan.max_dti_ratio"
LOAN_MAX_AMOUNT_MINOR_KEY = "loan.max_amount_minor"
LOAN_MIN_SCORE_KEY = "loan.min_score"
LOAN_REVIEW_MARGIN_KEY = "loan.review_margin"
LOAN_MAX_DEBT_MINOR_KEY = "loan.max_debt_minor"

#: Patron opcional de tope por perfil (espejo de `shared/config`).
LOAN_MAX_AMOUNT_PROFILE_TEMPLATE = "loan.max_amount_{profile}_minor"

#: Fallbacks documentados = semillas (`0001` para el DTI, `0030` resto).
DEFAULT_MAX_DTI_RATIO = Decimal("0.4")
DEFAULT_MAX_AMOUNT_MINOR = 5_000_000
DEFAULT_MIN_SCORE = 650
DEFAULT_REVIEW_MARGIN = Decimal(50)
DEFAULT_MAX_DEBT_MINOR = 10_000_000

#: Cuantizacion de salida de `dti_ratio` (`NUMERIC(12,4)` en `docs/03b#82`).
_DTI_QUANT = Decimal("0.0001")

_MISSING: object = object()


class CreditRulesValidationError(ValueError):
    """Error tipado del motor de reglas (mapea a ``VALIDATION_ERROR``)."""


@dataclass(frozen=True)
class CreditDecision:
    """Dictamen del motor: reproducible y auditable desde sus entradas.

    ``applied_amount_key`` indica que tope rigio (generico o por perfil);
    ``reason`` es estable (mismas entradas + mismos parametros -> mismo
    texto) y no lleva PII (solo montos, score y umbrales del expediente).
    """

    decision: str
    reason: str
    dti_ratio: Decimal
    score: int | None
    requested_amount_minor: int
    total_income_minor: int
    total_debt_minor: int
    max_dti_ratio: Decimal
    max_amount_minor: int
    applied_amount_key: str
    min_score: int
    review_margin: Decimal
    max_debt_minor: int
    profile: str | None


def _require_minor_int(name: str, value: object, *, minimum: int) -> int:
    """Entero en centimos validado (`bool` se rechaza; `VALIDATION_ERROR`)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise CreditRulesValidationError(f"{name} debe ser entero en centimos, recibido: {value!r}")
    if value < minimum:
        raise CreditRulesValidationError(f"{name} debe ser >= {minimum}, recibido: {value!r}")
    return value


def _try_int(raw: object, minimum: int) -> int | None:
    """Entero validado (`None` si no es entero o es `< minimum`)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        candidate = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            return None
        candidate = int(raw)
    elif isinstance(raw, str):
        try:
            candidate = int(raw.strip())
        except (ValueError, AttributeError):
            return None
    else:
        return None
    return candidate if candidate >= minimum else None


def _try_decimal(raw: object, minimum: Decimal | None = None) -> Decimal | None:
    """Decimal validado (`None` si no es numerico o es `< minimum`).

    Acepta `Decimal`, `int`, `str` numerico (`"0.4"`, forma de la semilla
    `0001`) y `float` finito (via `str()`); rechaza `bool`, `NaN`/`inf`
    y el resto de tipos.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        candidate = raw
    elif isinstance(raw, int):
        candidate = Decimal(raw)
    elif isinstance(raw, float):
        if not math.isfinite(raw):
            return None
        candidate = Decimal(str(raw))
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            candidate = Decimal(text)
        except InvalidOperation:
            return None
    else:
        return None
    if candidate.is_nan() or candidate.is_infinite():
        return None
    if minimum is not None and candidate < minimum:
        return None
    return candidate


def _int_param(params: Mapping[str, object], key: str, default: int) -> int:
    """Entero de `params` o `default` (ausente/invalido -> fallback)."""
    candidate = _try_int(params.get(key, _MISSING), 0)
    return candidate if candidate is not None else default


def _decimal_param(params: Mapping[str, object], key: str, default: Decimal) -> Decimal:
    """Decimal de `params` o `default` (ausente/invalido -> fallback)."""
    candidate = _try_decimal(params.get(key, _MISSING), Decimal(0))
    return candidate if candidate is not None else default


def _normalize_profile(profile: object) -> str | None:
    """Perfil normalizado (`STANDARD`) o `None` si no es un token valido.

    Espejo de `shared/config.daily_limit_key_for_profile`: nunca lanza;
    un perfil inesperado usa el tope generico.
    """
    if not isinstance(profile, str):
        return None
    token = profile.strip().upper()
    if not token or len(token) > 20:
        return None
    if not all(ch.isalnum() or ch == "_" for ch in token):
        return None
    return token


def _extract_score(bureau_score: object) -> int | None:
    """Score del bureau (`None` si el bureau cayo).

    Acepta el contrato `BureauScore` de E3-T06 (o cualquier objeto con
    atributo `score`) y el score crudo `int`. Un score corrupto (no
    entero o negativo) es `VALIDATION_ERROR`, no un rechazo silencioso.
    """
    if bureau_score is None:
        return None
    if isinstance(bureau_score, bool):
        raise CreditRulesValidationError(f"bureau_score invalido: {bureau_score!r}")
    if isinstance(bureau_score, int):
        if bureau_score < 0:
            raise CreditRulesValidationError(f"bureau_score invalido: {bureau_score!r}")
        return bureau_score
    raw = getattr(bureau_score, "score", _MISSING)
    if raw is _MISSING or isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise CreditRulesValidationError(f"bureau_score invalido: {bureau_score!r}")
    return raw


def compute_dti(total_debt_minor: int, total_income_minor: int) -> Decimal:
    """DTI exacto (`debt / income`); `income <= 0` -> `VALIDATION_ERROR`.

    La comparacion contra el umbral usa el valor exacto; la salida que
    se persiste (`dti_ratio NUMERIC(12,4)`, acotada por el repositorio
    si es extrema) se cuantiza a 4 decimales.
    """
    debt = _require_minor_int("total_debt_minor", total_debt_minor, minimum=0)
    income = _require_minor_int("total_income_minor", total_income_minor, minimum=1)
    return Decimal(debt) / Decimal(income)


def _quantize_dti(exact: Decimal) -> Decimal:
    return exact.quantize(_DTI_QUANT, rounding=ROUND_HALF_UP)


def evaluate(
    *,
    requested_amount_minor: int,
    term_months: int,
    total_income_minor: int,
    total_debt_minor: int,
    bureau_score: Any | None = None,
    profile: str | None = None,
    params: Mapping[str, object] | None = None,
) -> CreditDecision:
    """Pre-califica una solicitud (pura y determinista).

    - `requested_amount_minor` / `term_months`: monto y plazo (`> 0`).
    - `total_income_minor` (`> 0`) / `total_debt_minor` (`>= 0`).
    - `bureau_score`: `BureauScore` (E3-T06), score `int`, o `None`
      (bureau caido -> `IN_REVIEW` si los topes financieros pasan).
    - `profile`: perfil para el tope por perfil (opcional).
    - `params`: foto de `config.parameters` (claves `loan.*`; ausente o
      parcial -> fallbacks `DEFAULT_*`).

    El plazo se valida pero no decide (lo usan el producto y E3-T08).
    """
    requested = _require_minor_int("requested_amount_minor", requested_amount_minor, minimum=1)
    term = term_months
    if isinstance(term, bool) or not isinstance(term, int) or term <= 0:
        raise CreditRulesValidationError(
            f"term_months debe ser entero > 0, recibido: {term_months!r}"
        )
    income = _require_minor_int("total_income_minor", total_income_minor, minimum=1)
    debt = _require_minor_int("total_debt_minor", total_debt_minor, minimum=0)
    score = _extract_score(bureau_score)
    token = _normalize_profile(profile)
    mapping: Mapping[str, object] = params if params is not None else {}
    if not isinstance(mapping, Mapping):
        raise CreditRulesValidationError(f"params debe ser un mapeo, recibido: {params!r}")

    max_dti = _decimal_param(mapping, LOAN_MAX_DTI_RATIO_KEY, DEFAULT_MAX_DTI_RATIO)
    min_score = _int_param(mapping, LOAN_MIN_SCORE_KEY, DEFAULT_MIN_SCORE)
    review_margin = _decimal_param(mapping, LOAN_REVIEW_MARGIN_KEY, DEFAULT_REVIEW_MARGIN)
    max_debt = _int_param(mapping, LOAN_MAX_DEBT_MINOR_KEY, DEFAULT_MAX_DEBT_MINOR)

    applied_key = LOAN_MAX_AMOUNT_MINOR_KEY
    cap = _try_int(mapping.get(LOAN_MAX_AMOUNT_MINOR_KEY, _MISSING), 0)
    if token is not None:
        override_key = LOAN_MAX_AMOUNT_PROFILE_TEMPLATE.format(profile=token)
        override = _try_int(mapping.get(override_key, _MISSING), 0)
        if override is not None:
            cap, applied_key = override, override_key
    if cap is None:
        cap = DEFAULT_MAX_AMOUNT_MINOR

    exact_dti = Decimal(debt) / Decimal(income)
    dti = _quantize_dti(exact_dti)
    review_bound = Decimal(min_score) + review_margin

    def _decide(decision: str, reason: str) -> CreditDecision:
        return CreditDecision(
            decision=decision,
            reason=reason,
            dti_ratio=dti,
            score=score,
            requested_amount_minor=requested,
            total_income_minor=income,
            total_debt_minor=debt,
            max_dti_ratio=max_dti,
            max_amount_minor=cap,
            applied_amount_key=applied_key,
            min_score=min_score,
            review_margin=review_margin,
            max_debt_minor=max_debt,
            profile=token,
        )

    if exact_dti > max_dti:
        return _decide(REJECTED, f"rechazado por DTI: {dti} supera el maximo {max_dti}")
    if requested > cap:
        who = f"perfil {token}" if token is not None else "tope generico"
        return _decide(
            REJECTED, f"rechazado por tope: monto {requested} supera el tope {cap} ({who})"
        )
    if debt > max_debt:
        return _decide(
            REJECTED, f"rechazado por deuda: deuda total {debt} supera el maximo {max_debt}"
        )
    if score is None:
        return _decide(IN_REVIEW, "en revision: bureau no disponible; reintentar consulta")
    if score < min_score:
        return _decide(REJECTED, f"rechazado por score: {score} bajo el minimo {min_score}")
    if Decimal(score) < review_bound:
        return _decide(
            IN_REVIEW, f"en revision por score: {score} en banda [{min_score}, {review_bound})"
        )
    return _decide(
        APPROVED,
        f"aprobado: dti {dti} <= {max_dti}; monto {requested} <= {cap}; "
        f"score {score} >= {review_bound}",
    )
