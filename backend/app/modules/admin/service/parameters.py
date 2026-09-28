"""Caso de uso `admin/parameters`: fachada de lectura/escritura auditada (E2-T26).

`admin` NO es dueno del esquema de `config.parameters`: solo lo expone con
auditoria (`docs/modules/README.md#admin`). Toda la semantica de valores
vive en la fachada duena `app.modules.shared.config` (E2-T18):

- Lectura de valores: exclusivamente via sus getters tipados
  (`get_biometric_threshold_minor`, `get_daily_limit_minor`,
  `get_max_failed_attempts`, `get_parameter`). Lectura por llamada, sin
  cache en proceso: un cambio se refleja de inmediato, sin despliegue.
- Escritura: exclusivamente via `set_parameter` (valida tipo/rango segun
  la clave, exige fila existente, fija `updated_at`/`updated_by`). Este
  modulo solo confirma la transaccion (`commit`); jamas emite
  `UPDATE`/`INSERT`/`DELETE` contra `config.parameters`.

La unica SQL propia es un `SELECT` acotado de metadatos (key/module/
description/updated_by/updated_at) para componer el listado `GET` que pide
el contrato; NUNCA interpreta valores (el valor efectivo siempre lo
resuelve la fachada) y es best-effort: si la tabla no es legible, se listan
las claves conocidas con sus defaults (nunca lanza).

Auditoria y PII (reglas de oro 7 y 10): cada escritura registra actor
(UUID del JWT) y timestamp via la fachada; los logs de este modulo llevan
solo la clave (jamas valores).

Sin `float`; sin importar modelos de otros modulos (no existe modelo ORM
de parametros: la tabla la gestiona la migracion `0001`).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.shared.config import (
    AUTH_MAX_FAILED_ATTEMPTS_KEY,
    BIOMETRIC_THRESHOLD_KEY,
    DAILY_LIMIT_KEY,
    DAILY_LIMIT_PROFILE_TEMPLATE,
    SEEDED_PROFILES,
    ParameterNotFoundError,
    ParameterValidationError,
    daily_limit_key_for_profile,
    get_biometric_threshold_minor,
    get_daily_limit_minor,
    get_max_failed_attempts,
    get_parameter,
    set_parameter,
)

logger = logging.getLogger(__name__)

#: Claves escalares conocidas (presentacion + fallback de listado).
KNOWN_KEYS: tuple[str, ...] = (
    BIOMETRIC_THRESHOLD_KEY,
    DAILY_LIMIT_KEY,
    *(DAILY_LIMIT_PROFILE_TEMPLATE.format(profile=p) for p in SEEDED_PROFILES),
    AUTH_MAX_FAILED_ATTEMPTS_KEY,
)

#: Modulo presentacional por clave (refleja las semillas `0001`/`0019`/`0023`).
_KEY_MODULES: dict[str, str] = {
    BIOMETRIC_THRESHOLD_KEY: "transactions",
    DAILY_LIMIT_KEY: "transactions",
    AUTH_MAX_FAILED_ATTEMPTS_KEY: "identity",
}

#: Descripcion presentacional por clave (la columna `description` manda si existe).
_KEY_DESCRIPTIONS: dict[str, str] = {
    BIOMETRIC_THRESHOLD_KEY: "Monto en centimos desde el cual se exige biometria",
    DAILY_LIMIT_KEY: "Limite diario generico en centimos",
    AUTH_MAX_FAILED_ATTEMPTS_KEY: "Intentos fallidos antes del bloqueo temporal",
}

_MISSING: object = object()


def _module_for_key(key: str) -> str:
    """Modulo presentacional de una clave (prefijo `transfer.`/`auth.`)."""
    if key in _KEY_MODULES:
        return _KEY_MODULES[key]
    if key.startswith("transfer.daily_limit_") and key.endswith("_minor"):
        return "transactions"
    return key.split(".", 1)[0] if "." in key else "config"


def _description_for_key(key: str) -> str | None:
    if key in _KEY_DESCRIPTIONS:
        return _KEY_DESCRIPTIONS[key]
    if key.startswith("transfer.daily_limit_") and key.endswith("_minor"):
        return "Limite diario por perfil de riesgo en centimos"
    return None


def _coerce_uuid(raw: object) -> uuid.UUID | None:
    if raw is None:
        return None
    if isinstance(raw, uuid.UUID):
        return raw
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def _coerce_datetime(raw: object) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


def _present_raw(raw: object) -> object:
    """Normaliza un valor crudo para el listado (solo presentacion).

    No valida rangos (eso es de la fachada al escribir): enteros y textos
    numericos se muestran como `int`; cualquier otro JSON se muestra tal
    cual.
    """
    if raw is None or raw is _MISSING:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else raw
    if isinstance(raw, str):
        text = raw.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return raw
        if isinstance(parsed, bool):
            return parsed
        if isinstance(parsed, int):
            return parsed
        if isinstance(parsed, float) and parsed.is_integer():
            return int(parsed)
        return parsed
    return raw


def _resolve_value(session: Session, key: str) -> object:
    """Valor efectivo de una clave, siempre via la fachada duena."""
    if key == BIOMETRIC_THRESHOLD_KEY:
        return get_biometric_threshold_minor(session)
    if key == AUTH_MAX_FAILED_ATTEMPTS_KEY:
        return get_max_failed_attempts(session)
    if key == DAILY_LIMIT_KEY:
        return get_daily_limit_minor(session)
    if key.startswith("transfer.daily_limit_") and key.endswith("_minor"):
        token = key[len("transfer.daily_limit_") : -len("_minor")]
        profile = token if daily_limit_key_for_profile(token) == key else None
        if profile is not None or token in SEEDED_PROFILES:
            return get_daily_limit_minor(session, token)
        return get_daily_limit_minor(session, token)
    return _present_raw(get_parameter(session, key, None))


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _read_meta_rows(
    session: Session,
    *,
    module: str | None = None,
    prefix: str | None = None,
) -> list[dict] | None:
    """Filas de metadatos (`SELECT` acotado, solo lectura; `None` si no legible)."""
    where = []
    params: dict[str, object] = {}
    if module:
        where.append("module = :module")
        params["module"] = module
    if prefix:
        where.append("key LIKE :prefix ESCAPE '\\'")
        params["prefix"] = f"{_escape_like(prefix)}%"
    sql = (
        "SELECT key, module, description, updated_by, updated_at "
        "FROM config.parameters"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY key"
    )
    try:
        with session.begin_nested():
            rows = session.execute(sa.text(sql), params).mappings().all()
    except Exception:  # noqa: BLE001 - listado best-effort, nunca lanza
        logger.debug("admin parameters sin acceso a metadatos: fallback")
        return None
    return [dict(row) for row in rows]


def _matches(module_value: str, key: str, *, module: str | None, prefix: str | None) -> bool:
    return (module is None or module_value == module) and (prefix is None or key.startswith(prefix))


def list_parameters(
    session: Session,
    *,
    module: str | None = None,
    prefix: str | None = None,
) -> list[dict]:
    """Lista parametros con valor efectivo + metadatos (solo lectura).

    Filtros opcionales `module` (igualdad) y `prefix` (prefijo de `key`):
    un filtro sin coincidencias retorna `[]` con `200` (no `404`).
    """
    clean_module = module.strip() if isinstance(module, str) and module.strip() else None
    clean_prefix = prefix.strip() if isinstance(prefix, str) and prefix.strip() else None
    rows = _read_meta_rows(session, module=clean_module, prefix=clean_prefix)
    if rows is None:
        rows = [
            {
                "key": key,
                "module": _module_for_key(key),
                "description": None,
                "updated_by": None,
                "updated_at": None,
            }
            for key in KNOWN_KEYS
            if _matches(_module_for_key(key), key, module=clean_module, prefix=clean_prefix)
        ]
    items: list[dict] = []
    for row in rows:
        key = str(row.get("key"))
        items.append(
            {
                "key": key,
                "value_json": _resolve_value(session, key),
                "module": str(row.get("module") or _module_for_key(key)),
                "description": row.get("description") or _description_for_key(key),
                "updated_by": _coerce_uuid(row.get("updated_by")),
                "updated_at": _coerce_datetime(row.get("updated_at")),
            }
        )
    logger.debug(
        "admin parameters list module=%s prefix=%s count=%s",
        clean_module,
        clean_prefix,
        len(items),
    )
    return items


def update_parameter(
    session: Session,
    *,
    key: str,
    value: int,
    updated_by: uuid.UUID | str | None = None,
) -> dict:
    """Actualiza el valor de una clave existente via la fachada duena.

    Delega tipo/rango/existencia a `set_parameter` (`NOT_FOUND` si la clave
    no esta sembrada; `VALIDATION_ERROR` si el valor es invalido) y confirma
    la transaccion. Idempotente por naturaleza: misma clave + mismo valor =
    mismo estado (`UPDATE` sobre `key` UQ; concurrencia = ultimo escritor
    gana, atomico por sentencia).

    Retorna `{key, value_json, updated_at}`. Audita actor/timestamp sin PII
    (los logs llevan solo la clave, jamas el valor).
    """
    if not isinstance(key, str) or not key.strip():
        raise ParameterNotFoundError(f"clave inexistente: {key!r}")
    clean_key = key.strip()
    actor = _coerce_uuid(updated_by) if updated_by is not None else None
    if updated_by is not None and actor is None:
        raise ParameterValidationError(f"updated_by debe ser UUID, recibido: {updated_by!r}")
    stored = set_parameter(session, key=clean_key, value=value, updated_by=actor)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    updated_at: datetime | None = None
    try:
        with session.begin_nested():
            raw_updated_at = session.execute(
                sa.text("SELECT updated_at FROM config.parameters WHERE key = :key"),
                {"key": clean_key},
            ).scalar()
        updated_at = _coerce_datetime(raw_updated_at) or datetime.now(UTC)
    except Exception:  # noqa: BLE001 - best-effort, el valor ya quedo confirmado
        logger.debug("admin parameters sin relectura de updated_at (%s)", clean_key)
        updated_at = datetime.now(UTC)
    logger.info("admin parameter updated key=%s actor=%s", clean_key, actor)
    return {"key": clean_key, "value_json": stored, "updated_at": updated_at}


__all__ = [
    "KNOWN_KEYS",
    "list_parameters",
    "update_parameter",
]
