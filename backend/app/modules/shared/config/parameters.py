"""Lectura/escritura duena de `config.parameters` (E2-T18, HU08).

Ver `app/modules/shared/config/__init__.py` (contrato y claves). Detalles:

- Sin modelo ORM: `config.parameters` la gestiona la migracion `0001`
  (tabla semilla sin modelo) y `migrations/env.py` la excluye de
  autogenerate/check; la fachada usa SQL acotado a esa tabla transversal
  (no es tabla de otro modulo de negocio) dentro de un `SAVEPOINT`, como
  `identity/service/otp_service.py`: un fallo jamas contamina la
  transaccion del llamante.
- Dinero entero en centimos; sin `float`; sin secretos ni PII en logs
  (reglas de oro 3, 7 y 10: los logs solo llevan la clave).
"""

from __future__ import annotations

import json
import logging
import math
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Umbral de biometria en centimos (`0001`, modulo `transactions`).
BIOMETRIC_THRESHOLD_KEY = "transfer.biometric_threshold_minor"

#: Limite diario generico en centimos (`0001`, modulo `transactions`).
DAILY_LIMIT_KEY = "transfer.daily_limit_minor"

#: Patron de limite diario por perfil (`{profile}` en MAYUSCULAS).
DAILY_LIMIT_PROFILE_TEMPLATE = "transfer.daily_limit_{profile}_minor"

#: Tope de intentos fallidos antes del bloqueo (`0001` + `0019`).
AUTH_MAX_FAILED_ATTEMPTS_KEY = "auth.max_failed_attempts"

#: Defaults = semillas (`0001`/`0019`/`0023` + `0025` OBS-2: el umbral
#: biometrico (S/ 1,000) queda por debajo del limite diario (S/ 5,000)
#: para que el reto (`PENDING_AUTHORIZATION`) sea alcanzable).
DEFAULT_BIOMETRIC_THRESHOLD_MINOR = 100_000
DEFAULT_DAILY_LIMIT_MINOR = 500_000
DEFAULT_MAX_FAILED_ATTEMPTS = 5

#: Claves del motor de reglas de credito (E3-T07, HU10, modulo `credits`).
LOAN_MAX_DTI_RATIO_KEY = "loan.max_dti_ratio"
LOAN_MAX_AMOUNT_MINOR_KEY = "loan.max_amount_minor"
LOAN_MIN_SCORE_KEY = "loan.min_score"
LOAN_REVIEW_MARGIN_KEY = "loan.review_margin"
LOAN_MAX_DEBT_MINOR_KEY = "loan.max_debt_minor"

#: Ventana del recordatorio de cuota en dias (E3-T19, HU12 CA-04,
#: modulo `credits`): el job avisa las cuotas con
#: `due_date == hoy + loan.reminder_days_before`.
LOAN_REMINDER_DAYS_BEFORE_KEY = "loan.reminder_days_before"

#: Limite base del pago express en centimos (E4-T02, HU13, modulo
#: `wallet`): bajo este total no se exige reto del servidor.
WALLET_EXPRESS_LIMIT_MINOR_KEY = "wallet.express_limit_minor"

#: Limite diario de billetera en centimos (E4-T02, HU13, modulo
#: `wallet`): `acumulado + total > limite` -> `REJECTED`.
WALLET_DAILY_LIMIT_MINOR_KEY = "wallet.daily_limit_minor"

#: Vigencia del QR de cobro en segundos (E4-T06, HU14, modulo
#: `wallet`): `expires_at = now + ttl` en modo `DYNAMIC` (el modo
#: `STATIC` no tiene caducidad estricta en el MVP).
WALLET_QR_TTL_SECONDS_KEY = "wallet.qr_ttl_seconds"

#: Ventana del recordatorio de servicios en dias (E4-T18, HU16 CA-03,
#: modulo `wallet`): el job avisa las suscripciones cuyo vencimiento
#: derivado cae en `hoy + wallet.service_reminder_days_before`.
WALLET_SERVICE_REMINDER_DAYS_BEFORE_KEY = "wallet.service_reminder_days_before"

#: Patron de tope de monto por perfil (`{profile}` en MAYUSCULAS; espejo
#: del patron de limite diario `transfer.daily_limit_{profile}_minor`).
LOAN_MAX_AMOUNT_PROFILE_TEMPLATE = "loan.max_amount_{profile}_minor"

#: Defaults = semillas (`0001` para `loan.max_dti_ratio`; `0030` para el
#: resto): mismos valores que las migraciones siembran, asi migrar no
#: cambia la conducta del motor (el motor los usa como fallback cuando la
#: clave falta o el valor almacenado es invalido).
DEFAULT_LOAN_MAX_DTI_RATIO = Decimal("0.4")
DEFAULT_LOAN_MAX_AMOUNT_MINOR = 5_000_000
DEFAULT_LOAN_MIN_SCORE = 650
DEFAULT_LOAN_REVIEW_MARGIN = Decimal(50)
DEFAULT_LOAN_MAX_DEBT_MINOR = 10_000_000

#: Ventana del recordatorio de cuota en dias (E3-T19, HU12 CA-04):
#: mismo valor que la semilla `0036` (el job lo usa como fallback
#: cuando la clave falta o el valor almacenado es invalido).
DEFAULT_LOAN_REMINDER_DAYS_BEFORE = 3

#: Limites de billetera (E4-T02, HU13, modulo `wallet`; semillas
#: `0038`): mismos valores que las migraciones siembran, asi migrar no
#: cambia la conducta del pago express (el caso de uso los usa como
#: fallback cuando la clave falta o el valor almacenado es invalido).
#: El base (S/ 200) replica la magnitud del `qr.express_limit_minor`
#: sembrado en `0001`; el diario (S/ 5,000) replica el diario generico
#: de transferencias. Invariante: base < diario (como OBS-2 `0025`).
DEFAULT_WALLET_EXPRESS_LIMIT_MINOR = 20_000
DEFAULT_WALLET_DAILY_LIMIT_MINOR = 500_000

#: Vigencia del QR de cobro en segundos (E4-T06, HU14, modulo
#: `wallet`; semilla `0039`): mismo valor que la migracion siembra,
#: asi migrar no cambia la conducta de la generacion (el caso de uso
#: lo usa como fallback cuando la clave falta o el valor almacenado
#: es invalido).
DEFAULT_WALLET_QR_TTL_SECONDS = 300

#: Ventana del recordatorio de servicios en dias (E4-T18, HU16 CA-03,
#: modulo `wallet`; semilla `0043`): mismo valor que la migracion
#: siembra, asi migrar no cambia la conducta del job (el job lo usa
#: como fallback cuando la clave falta o el valor es invalido).
DEFAULT_WALLET_SERVICE_REMINDER_DAYS_BEFORE = 3

#: Perfiles con clave propia sembrada por migracion (`0023`).
SEEDED_PROFILES: tuple[str, ...] = ("STANDARD",)

#: Token valido de perfil en una clave por perfil (misma forma que
#: `users.risk_profile`: mayusculas, sin espacios ni SQL).
_PROFILE_TOKEN = re.compile(r"[A-Z0-9_]{1,20}")

#: (default, minimo) por clave escalar conocida.
_SPECS: dict[str, tuple[int, int]] = {
    BIOMETRIC_THRESHOLD_KEY: (DEFAULT_BIOMETRIC_THRESHOLD_MINOR, 0),
    DAILY_LIMIT_KEY: (DEFAULT_DAILY_LIMIT_MINOR, 0),
    AUTH_MAX_FAILED_ATTEMPTS_KEY: (DEFAULT_MAX_FAILED_ATTEMPTS, 1),
    LOAN_MAX_AMOUNT_MINOR_KEY: (DEFAULT_LOAN_MAX_AMOUNT_MINOR, 0),
    LOAN_MIN_SCORE_KEY: (DEFAULT_LOAN_MIN_SCORE, 0),
    LOAN_MAX_DEBT_MINOR_KEY: (DEFAULT_LOAN_MAX_DEBT_MINOR, 0),
    # Ventana del recordatorio (E3-T19): minimo 1 dia (el `0` seria
    # "avisar el mismo dia del vencimiento", fuera de HU12 2-3 dias).
    LOAN_REMINDER_DAYS_BEFORE_KEY: (DEFAULT_LOAN_REMINDER_DAYS_BEFORE, 1),
    # Pago express (E4-T02, HU13): base y diario de billetera.
    WALLET_EXPRESS_LIMIT_MINOR_KEY: (DEFAULT_WALLET_EXPRESS_LIMIT_MINOR, 0),
    WALLET_DAILY_LIMIT_MINOR_KEY: (DEFAULT_WALLET_DAILY_LIMIT_MINOR, 0),
    # QR firmado (E4-T06, HU14): vigencia en segundos (minimo 1 s: un
    # TTL `0` naceria expirado y no aporta al MVP).
    WALLET_QR_TTL_SECONDS_KEY: (DEFAULT_WALLET_QR_TTL_SECONDS, 1),
    # Recordatorio de servicios (E4-T18, HU16 CA-03): minimo 1 dia (el
    # `0` seria "avisar el mismo dia del vencimiento", fuera de HU16).
    WALLET_SERVICE_REMINDER_DAYS_BEFORE_KEY: (
        DEFAULT_WALLET_SERVICE_REMINDER_DAYS_BEFORE,
        1,
    ),
}

#: (default, minimo, maximo) por clave decimal conocida (`None` = sin
#: cota). Los ratios van como `Decimal`, nunca `float` (regla de oro 3).
_DECIMAL_SPECS: dict[str, tuple[Decimal, Decimal | None, Decimal | None]] = {
    LOAN_MAX_DTI_RATIO_KEY: (DEFAULT_LOAN_MAX_DTI_RATIO, Decimal(0), None),
    LOAN_REVIEW_MARGIN_KEY: (DEFAULT_LOAN_REVIEW_MARGIN, Decimal(0), None),
}


class ParameterError(ValueError):
    """Error base de parametros (subclase de `ValueError`, mapeable a 4xx)."""


class ParameterNotFoundError(ParameterError):
    """La clave no existe (`NOT_FOUND` al escribir)."""


class ParameterValidationError(ParameterError):
    """Valor no entero o fuera de rango (`VALIDATION_ERROR`)."""


_MISSING: object = object()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def daily_limit_key_for_profile(profile: str | None) -> str | None:
    """Clave por perfil (`None` si el perfil no es un token valido).

    Nunca lanza: un perfil inesperado (nulo, vacio, con minusculas o
    caracteres raros) retorna `None` y el llamante usa el limite
    generico. La normalizacion es `strip().upper()`.
    """
    if not isinstance(profile, str):
        return None
    token = profile.strip().upper()
    if not token or _PROFILE_TOKEN.fullmatch(token) is None:
        return None
    return DAILY_LIMIT_PROFILE_TEMPLATE.format(profile=token)


def _spec_for_key(key: str) -> tuple[int, int] | None:
    """`(default, minimo)` de una clave conocida (`None` si no la es)."""
    if key in _SPECS:
        return _SPECS[key]
    if (
        key.startswith("transfer.daily_limit_")
        and key.endswith("_minor")
        and key not in (DAILY_LIMIT_KEY,)
    ):
        token = key[len("transfer.daily_limit_") : -len("_minor")]
        if token and _PROFILE_TOKEN.fullmatch(token) is not None:
            return (DEFAULT_DAILY_LIMIT_MINOR, 0)
    if (
        key.startswith("loan.max_amount_")
        and key.endswith("_minor")
        and key not in (LOAN_MAX_AMOUNT_MINOR_KEY,)
    ):
        token = key[len("loan.max_amount_") : -len("_minor")]
        if token and _PROFILE_TOKEN.fullmatch(token) is not None:
            return (DEFAULT_LOAN_MAX_AMOUNT_MINOR, 0)
    return None


def _decimal_spec_for_key(key: str) -> tuple[Decimal, Decimal | None, Decimal | None] | None:
    """`(default, minimo, maximo)` de una clave decimal (`None` si no la es)."""
    return _DECIMAL_SPECS.get(key)


def _try_coerce_int(raw: object, minimum: int) -> int | None:
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


def _coerce_int(raw: object, default: int, minimum: int) -> int:
    """Entero validado (`default` si no es entero o es `< minimum`)."""
    candidate = _try_coerce_int(raw, minimum)
    return candidate if candidate is not None else default


def _try_coerce_decimal(
    raw: object,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal | None:
    """Decimal validado (`None` si no es numerico o viola las cotas).

    Acepta `Decimal`, `int` y `str` numerico: la semilla `0001` guardo
    `loan.max_dti_ratio` como texto `"0.4"` y debe leerse igual que el
    numero `0.4`. Tambien acepta `float` finito (asi llega un numero JSON
    por `psycopg`), convertido via `str()` para no arrastrar artefactos
    binarios. `bool`, `NaN`/`inf` y cualquier otro tipo -> `None` (rige
    el default, sin lanzar).
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
    if maximum is not None and candidate > maximum:
        return None
    return candidate


def _coerce_decimal(
    raw: object,
    default: Decimal,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    """Decimal validado (`default` si no es numerico o viola las cotas)."""
    candidate = _try_coerce_decimal(raw, minimum, maximum)
    return candidate if candidate is not None else default


def _fetch_raw(session: Session, key: str) -> object:
    """Valor crudo de `config.parameters.<key>` (`_MISSING` si no legible).

    SQL acotado a la tabla transversal dentro de un `SAVEPOINT`: tabla
    ausente, permiso o conexion caida no contaminan la transaccion del
    llamante (nunca lanza).
    """
    try:
        with session.begin_nested():
            return session.execute(
                sa.text("SELECT value_json FROM config.parameters WHERE key = :key"),
                {"key": key},
            ).scalar()
    except Exception:  # noqa: BLE001 - sin acceso rige el default
        logger.debug("parameters sin acceso (%s): fallback", key)
        return _MISSING


def get_parameter(session: Session, key: str, fallback: object) -> object:
    """Valor crudo de `config.parameters.<key>` o `fallback`.

    Sin coercion (para enteros validados usar `get_int_parameter`).
    Nunca lanza: clave invalida, fila ausente o tabla inaccesible
    retornan `fallback`.
    """
    if not isinstance(key, str) or not key.strip():
        return fallback
    raw = _fetch_raw(session, key.strip())
    if raw is None or raw is _MISSING:
        return fallback
    return raw


def get_int_parameter(session: Session, key: str, default: int, minimum: int = 0) -> int:
    """Entero de `config.parameters.<key>` o `default` (nunca lanza).

    Valor ausente, ilegible o no entero / `< minimum` -> `default`.
    """
    if not isinstance(key, str) or not key.strip():
        return default
    return _coerce_int(_fetch_raw(session, key.strip()), default, minimum)


def get_biometric_threshold_minor(session: Session) -> int:
    """Umbral de biometria en centimos (E2-T17: exige factor si se supera)."""
    default, minimum = _SPECS[BIOMETRIC_THRESHOLD_KEY]
    return get_int_parameter(session, BIOMETRIC_THRESHOLD_KEY, default, minimum)


def get_daily_limit_minor(session: Session, profile: str | None = "STANDARD") -> int:
    """Limite diario en centimos para un perfil de riesgo (E2-T17, CA-03).

    Cadena: clave por perfil -> clave generica -> constante. Perfil
    ausente/invalido o valor invalido cae al siguiente nivel; nunca lanza.
    """
    key = daily_limit_key_for_profile(profile)
    if key is not None:
        raw = _fetch_raw(session, key)
        if raw is not None and raw is not _MISSING:
            candidate = _try_coerce_int(raw, 0)
            if candidate is not None:
                return candidate
    default, minimum = _SPECS[DAILY_LIMIT_KEY]
    return get_int_parameter(session, DAILY_LIMIT_KEY, default, minimum)


def get_max_failed_attempts(session: Session) -> int:
    """Tope de intentos fallidos antes del bloqueo (E2-T17, D-05)."""
    default, minimum = _SPECS[AUTH_MAX_FAILED_ATTEMPTS_KEY]
    return get_int_parameter(session, AUTH_MAX_FAILED_ATTEMPTS_KEY, default, minimum)


def get_decimal_parameter(
    session: Session,
    key: str,
    default: Decimal,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    """Decimal de `config.parameters.<key>` o `default` (nunca lanza).

    Valor ausente, ilegible, no numerico o fuera de cotas -> `default`.
    Acepta el numero JSON (`0.4`), su forma texto (`"0.4"`, semilla `0001`
    de `loan.max_dti_ratio`), enteros y `Decimal`; jamas `float` en la
    salida (regla de oro 3).
    """
    base = default if isinstance(default, Decimal) else Decimal(0)
    if not isinstance(key, str) or not key.strip():
        return base
    return _coerce_decimal(_fetch_raw(session, key.strip()), base, minimum, maximum)


def loan_amount_key_for_profile(profile: str | None) -> str | None:
    """Clave de tope de monto por perfil (`None` si el perfil no es valido).

    Espejo de `daily_limit_key_for_profile` (E2-T18): nunca lanza; un
    perfil inesperado retorna `None` y el llamante usa el tope generico.
    """
    if not isinstance(profile, str):
        return None
    token = profile.strip().upper()
    if not token or _PROFILE_TOKEN.fullmatch(token) is None:
        return None
    return LOAN_MAX_AMOUNT_PROFILE_TEMPLATE.format(profile=token)


def get_loan_max_dti_ratio(session: Session) -> Decimal:
    """Relacion deuda/ingreso maxima del motor de credito (E3-T07)."""
    default, minimum, maximum = _DECIMAL_SPECS[LOAN_MAX_DTI_RATIO_KEY]
    return get_decimal_parameter(session, LOAN_MAX_DTI_RATIO_KEY, default, minimum, maximum)


def get_loan_max_amount_minor(session: Session, profile: str | None = None) -> int:
    """Tope de monto solicitado en centimos, con override por perfil (E3-T07).

    Cadena: clave por perfil -> clave generica -> constante. Igual que
    `get_daily_limit_minor`; nunca lanza.
    """
    key = loan_amount_key_for_profile(profile)
    if key is not None:
        raw = _fetch_raw(session, key)
        if raw is not None and raw is not _MISSING:
            candidate = _try_coerce_int(raw, 0)
            if candidate is not None:
                return candidate
    default, minimum = _SPECS[LOAN_MAX_AMOUNT_MINOR_KEY]
    return get_int_parameter(session, LOAN_MAX_AMOUNT_MINOR_KEY, default, minimum)


def get_loan_min_score(session: Session) -> int:
    """Score minimo del bureau para aprobar (E3-T07)."""
    default, minimum = _SPECS[LOAN_MIN_SCORE_KEY]
    return get_int_parameter(session, LOAN_MIN_SCORE_KEY, default, minimum)


def get_loan_review_margin(session: Session) -> Decimal:
    """Margen sobre el minimo que manda a revision manual (E3-T07)."""
    default, minimum, maximum = _DECIMAL_SPECS[LOAN_REVIEW_MARGIN_KEY]
    return get_decimal_parameter(session, LOAN_REVIEW_MARGIN_KEY, default, minimum, maximum)


def get_loan_max_debt_minor(session: Session) -> int:
    """Deuda total maxima del solicitante en centimos (E3-T07)."""
    default, minimum = _SPECS[LOAN_MAX_DEBT_MINOR_KEY]
    return get_int_parameter(session, LOAN_MAX_DEBT_MINOR_KEY, default, minimum)


def get_loan_reminder_days_before(session: Session) -> int:
    """Dias antes del vencimiento para el recordatorio de cuota (E3-T19).

    Ventana del job de recordatorios (HU12 CA-04): avisa las cuotas
    con `due_date == hoy + N`. Lectura best-effort con fallback
    (`DEFAULT_LOAN_REMINDER_DAYS_BEFORE` si la clave falta o el valor
    es invalido); nunca lanza.
    """
    default, minimum = _SPECS[LOAN_REMINDER_DAYS_BEFORE_KEY]
    return get_int_parameter(session, LOAN_REMINDER_DAYS_BEFORE_KEY, default, minimum)


def get_wallet_express_limit_minor(session: Session) -> int:
    """Limite base del pago express en centimos (E4-T02, HU13).

    Bajo este total el pago liquida sin reto del servidor; sobre el
    total exige biometria/PIN (gate del motor). Lectura best-effort
    con fallback (`DEFAULT_WALLET_EXPRESS_LIMIT_MINOR` si la clave
    falta o el valor es invalido); nunca lanza.
    """
    default, minimum = _SPECS[WALLET_EXPRESS_LIMIT_MINOR_KEY]
    return get_int_parameter(session, WALLET_EXPRESS_LIMIT_MINOR_KEY, default, minimum)


def get_wallet_daily_limit_minor(session: Session) -> int:
    """Limite diario de billetera en centimos (E4-T02, HU13).

    `acumulado + total > limite` -> `REJECTED` (`DAILY_LIMIT_EXCEEDED`)
    sin tocar saldos. MVP generico (sin variantes por perfil).
    Lectura best-effort con fallback
    (`DEFAULT_WALLET_DAILY_LIMIT_MINOR` si la clave falta o el valor
    es invalido); nunca lanza.
    """
    default, minimum = _SPECS[WALLET_DAILY_LIMIT_MINOR_KEY]
    return get_int_parameter(session, WALLET_DAILY_LIMIT_MINOR_KEY, default, minimum)


def get_wallet_qr_ttl_seconds(session: Session) -> int:
    """Vigencia del QR de cobro en segundos (E4-T06, HU14).

    `expires_at = now + ttl` en modo `DYNAMIC`. Lectura best-effort
    con fallback (`DEFAULT_WALLET_QR_TTL_SECONDS` si la clave falta o
    el valor es invalido); nunca lanza.
    """
    default, minimum = _SPECS[WALLET_QR_TTL_SECONDS_KEY]
    return get_int_parameter(session, WALLET_QR_TTL_SECONDS_KEY, default, minimum)


def get_wallet_service_reminder_days_before(session: Session) -> int:
    """Dias antes del vencimiento para el recordatorio de servicios (E4-T18).

    Ventana del job de recordatorios (HU16 CA-03): avisa las
    suscripciones cuyo vencimiento derivado cae en `hoy + N`.
    Lectura best-effort con fallback
    (`DEFAULT_WALLET_SERVICE_REMINDER_DAYS_BEFORE` si la clave falta
    o el valor es invalido); nunca lanza.
    """
    default, minimum = _SPECS[WALLET_SERVICE_REMINDER_DAYS_BEFORE_KEY]
    return get_int_parameter(session, WALLET_SERVICE_REMINDER_DAYS_BEFORE_KEY, default, minimum)


def get_loan_params(session: Session) -> dict[str, object]:
    """Foto generica de los parametros del motor de credito (E3-T07).

    La consume la fachada de evaluacion (`credits.service.evaluation`):
    lectura por llamada, sin cache (un cambio se refleja de inmediato).
    El override por perfil lo resuelve el llamante con
    `loan_amount_key_for_profile` + `get_parameter`. Nunca lanza.
    """
    return {
        LOAN_MAX_DTI_RATIO_KEY: get_loan_max_dti_ratio(session),
        LOAN_MAX_AMOUNT_MINOR_KEY: get_int_parameter(
            session, LOAN_MAX_AMOUNT_MINOR_KEY, *_SPECS[LOAN_MAX_AMOUNT_MINOR_KEY]
        ),
        LOAN_MIN_SCORE_KEY: get_int_parameter(
            session, LOAN_MIN_SCORE_KEY, *_SPECS[LOAN_MIN_SCORE_KEY]
        ),
        LOAN_REVIEW_MARGIN_KEY: get_loan_review_margin(session),
        LOAN_MAX_DEBT_MINOR_KEY: get_int_parameter(
            session, LOAN_MAX_DEBT_MINOR_KEY, *_SPECS[LOAN_MAX_DEBT_MINOR_KEY]
        ),
    }


def _coerce_updated_by(updated_by: uuid.UUID | str | None) -> uuid.UUID | None:
    if updated_by is None:
        return None
    if isinstance(updated_by, uuid.UUID):
        return updated_by
    try:
        return uuid.UUID(str(updated_by))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ParameterValidationError(
            f"updated_by debe ser UUID, recibido: {updated_by!r}"
        ) from exc


def _store_decimal(
    session: Session,
    clean_key: str,
    candidate: Decimal,
    updated_by: uuid.UUID | str | None,
) -> Decimal:
    """Persiste un decimal como numero JSON (`flush`, sin `commit`).

    `format(candidate, "f")` evita notacion cientifica: `CAST('0.4' AS
    jsonb)` guarda el numero `0.4` (no el texto `"0.4"` que sembro
    `0001`). En SQLite el `CAST` degrada a texto y la lectura lo acepta
    igual (ver `_try_coerce_decimal`). Exige fila existente (`NOT_FOUND`
    si no hay fila).
    """
    actor = _coerce_updated_by(updated_by)
    result = session.execute(
        sa.text(
            "UPDATE config.parameters SET value_json = CAST(:value AS jsonb), "
            "updated_at = :updated_at"
            + (", updated_by = :updated_by" if actor is not None else "")
            + " WHERE key = :key"
        ),
        {
            "value": format(candidate, "f"),
            "updated_at": _utcnow(),
            **({"updated_by": str(actor)} if actor is not None else {}),
            "key": clean_key,
        },
    )
    session.flush()
    if (result.rowcount or 0) == 0:
        raise ParameterNotFoundError(f"clave inexistente: {clean_key!r}")
    return candidate


def set_parameter(
    session: Session,
    *,
    key: str,
    value: int | Decimal | str,
    updated_by: uuid.UUID | str | None = None,
) -> int | Decimal:
    """Actualiza el valor de una clave existente (`flush`, sin `commit`).

    - Claves enteras: `value` debe ser `int` (no `bool`) dentro del rango
      (`VALIDATION_ERROR` si no cumple; comportamiento E2-T18 intacto).
    - Claves decimales (`loan.max_dti_ratio`, `loan.review_margin`,
      E3-T07): `value` acepta `Decimal`, `int` o `str` numerico (se
      almacena como numero JSON; `float` se rechaza para no arrastrar
      artefactos binarios: pasar `"0.4"`, no `0.4`); fuera de cotas ->
      `VALIDATION_ERROR`.
    - Exige fila existente (`NOT_FOUND` si la clave no esta sembrada:
      el espacio de claves lo gobiernan las migraciones).
    - Idempotente por `key` UQ: es un `UPDATE` (repetir no duplica;
      concurrencia = ultimo escritor gana, atomico por sentencia).
    - Trazable: fija `updated_at` y `updated_by` (solo si se informa;
      nunca guarda PII: es el UUID del operador).

    Retorna el valor almacenado (`int` o `Decimal`). Nunca crea claves
    ni tablas.
    """
    if not isinstance(key, str) or not key.strip():
        raise ParameterNotFoundError(f"clave inexistente: {key!r}")
    clean_key = key.strip()
    decimal_spec = _decimal_spec_for_key(clean_key)
    if decimal_spec is not None:
        _, minimum, maximum = decimal_spec
        if isinstance(value, float):
            raise ParameterValidationError(
                f"{clean_key} no acepta float (artefactos binarios): "
                f"pasar Decimal o texto, recibido: {value!r}"
            )
        candidate = _try_coerce_decimal(value, minimum, maximum)
        if candidate is None:
            raise ParameterValidationError(
                f"{clean_key} debe ser decimal numerico"
                + (f" >= {minimum}" if minimum is not None else "")
                + (f" <= {maximum}" if maximum is not None else "")
                + f", recibido: {value!r}"
            )
        stored = _store_decimal(session, clean_key, candidate, updated_by)
        return stored
    spec = _spec_for_key(clean_key)
    if spec is None:
        raise ParameterNotFoundError(f"clave inexistente: {clean_key!r}")
    _, minimum = spec
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParameterValidationError(f"{clean_key} debe ser entero, recibido: {value!r}")
    if value < minimum:
        raise ParameterValidationError(f"{clean_key} debe ser >= {minimum}, recibido: {value!r}")
    actor = _coerce_updated_by(updated_by)

    # `value_json` es `JSONB` en Postgres: se envia serializado con
    # `CAST(... AS jsonb)` (el `INSERT` de `0001`/`0019`/`0023` hace lo
    # mismo); en SQLite el `CAST` degrada a afinidad numerica y el valor
    # se lee como entero o texto (ambos los acepta la coercion). El
    # `UUID` se envia como texto (ambos dialectos lo aceptan).
    result = session.execute(
        sa.text(
            "UPDATE config.parameters SET value_json = CAST(:value AS jsonb), "
            "updated_at = :updated_at"
            + (", updated_by = :updated_by" if actor is not None else "")
            + " WHERE key = :key"
        ),
        {
            "value": json.dumps(value),
            "updated_at": _utcnow(),
            **({"updated_by": str(actor)} if actor is not None else {}),
            "key": clean_key,
        },
    )
    session.flush()
    if (result.rowcount or 0) == 0:
        raise ParameterNotFoundError(f"clave inexistente: {clean_key!r}")
    return value
