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
import re
import uuid
from datetime import UTC, datetime

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

#: Defaults = semillas (`0001`/`0019`/`0023`): migrar no cambia conducta.
DEFAULT_BIOMETRIC_THRESHOLD_MINOR = 1_000_000
DEFAULT_DAILY_LIMIT_MINOR = 500_000
DEFAULT_MAX_FAILED_ATTEMPTS = 5

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
    return None


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


def set_parameter(
    session: Session,
    *,
    key: str,
    value: int,
    updated_by: uuid.UUID | str | None = None,
) -> int:
    """Actualiza el valor de una clave existente (`flush`, sin `commit`).

    - Valida tipo/rango segun la clave (`VALIDATION_ERROR` si no es
      entero o viola el minimo; `bool` se rechaza).
    - Exige fila existente (`NOT_FOUND` si la clave no esta sembrada:
      el espacio de claves lo gobiernan las migraciones).
    - Idempotente por `key` UQ: es un `UPDATE` (repetir no duplica;
      concurrencia = ultimo escritor gana, atomico por sentencia).
    - Trazable: fija `updated_at` y `updated_by` (solo si se informa;
      nunca guarda PII: es el UUID del operador).

    Retorna el entero almacenado. Nunca crea claves ni tablas.
    """
    if not isinstance(key, str) or not key.strip():
        raise ParameterNotFoundError(f"clave inexistente: {key!r}")
    clean_key = key.strip()
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
