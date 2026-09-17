"""Repositorio de claves de idempotencia del schema `shared` (E5-T04, HU17 CA-03).

Capa de datos sin endpoints ni logica de negocio: solo SQLAlchemy sobre el
schema propio. Sin publicar eventos aqui (regla de oro 8) y sin `float`.

Convencion (igual que `outbox.py`): las funciones hacen `flush` y no
`commit`; quien llama decide la transaccion. Asi la clave se persiste junto
al cambio de negocio en la misma transaccion (un rollback revierte ambos).

TTL: `DEFAULT_TTL_SECONDS` (24 h) es el default; `resolve_ttl_seconds`
lee `IDEMPOTENCY_TTL_SECONDS` del entorno (sin secretos, solo un numero de
segundos). E2/fase config podra moverlo a `config.parameters` si aplica.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.shared.models import IdempotencyKey

# Default 24 h (`docs/04#5-idempotencia`: "TTL configurable (p. ej. 24 h)").
DEFAULT_TTL_SECONDS = 24 * 3600
# Nombre de la variable de entorno que sobreescribe el TTL (solo segundos,
# sin secretos). E2/fase config podra moverlo a `config.parameters`.
TTL_ENV_VAR = "IDEMPOTENCY_TTL_SECONDS"

# Limites segun `03b#2.1`.
MAX_KEY_LENGTH = 80
MAX_ENDPOINT_LENGTH = 150
MAX_METHOD_LENGTH = 10


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _naive(moment: datetime) -> datetime:
    """SQLite devuelve datetimes naive y Postgres aware: comparar sin tz."""
    return moment.replace(tzinfo=None) if moment.tzinfo is not None else moment


def resolve_ttl_seconds(explicit: int | None = None) -> int:
    """TTL efectivo en segundos: explicito > entorno > default 24 h."""
    if explicit is not None:
        if explicit < 1:
            raise ValueError("ttl_seconds debe ser >= 1")
        return explicit
    raw = os.getenv(TTL_ENV_VAR)
    if raw is None or not str(raw).strip():
        return DEFAULT_TTL_SECONDS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{TTL_ENV_VAR} debe ser entero en segundos") from exc
    if value < 1:
        raise ValueError(f"{TTL_ENV_VAR} debe ser >= 1")
    return value


def compute_request_hash(body: dict | list | str | bytes | None) -> str:
    """SHA-256 del cuerpo canonico (dominio puro, sin BD).

    Dicts/listas se serializan a JSON canonico (`sort_keys`, separadores
    compactos): el mismo cuerpo con distinto orden de claves da el mismo
    hash. `str`/`bytes` se hashean tal cual (UTF-8). `None` = cuerpo vacio.
    """
    if body is None:
        canonical = b""
    elif isinstance(body, bytes):
        canonical = body
    elif isinstance(body, str):
        canonical = body.encode("utf-8")
    elif isinstance(body, (dict, list)):
        try:
            canonical = json.dumps(
                body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"cuerpo no es JSON-serializable: {exc}") from exc
    else:
        raise ValueError(
            f"cuerpo debe ser dict/list/str/bytes/None, recibido: {type(body).__name__}"
        )
    return hashlib.sha256(canonical).hexdigest()


def _coerce_uuid(value: uuid.UUID | str | None, field: str) -> uuid.UUID | None:
    """UUID logico: acepta UUID, str (UUID en texto) o None."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def _validate_claim_fields(key: str, endpoint: str, method: str) -> tuple[str, str, str]:
    clean_key = str(key).strip() if isinstance(key, str) else ""
    if not clean_key:
        raise ValueError("key es obligatoria")
    if len(clean_key) > MAX_KEY_LENGTH:
        raise ValueError("key supera 80 caracteres")
    clean_endpoint = str(endpoint).strip() if isinstance(endpoint, str) else ""
    if not clean_endpoint:
        raise ValueError("endpoint es obligatorio")
    if len(clean_endpoint) > MAX_ENDPOINT_LENGTH:
        raise ValueError("endpoint supera 150 caracteres")
    clean_method = str(method).strip().upper() if isinstance(method, str) else ""
    if not clean_method:
        raise ValueError("method es obligatorio")
    if len(clean_method) > MAX_METHOD_LENGTH:
        raise ValueError("method supera 10 caracteres")
    return clean_key, clean_endpoint, clean_method


def _user_filter(column, user_id: uuid.UUID | None):
    """`user_id` nulo (servicios tecnicos) compara con `IS NULL`."""
    return column.is_(None) if user_id is None else column == user_id


def find_key(
    session: Session, *, key: str, user_id: uuid.UUID | str | None
) -> IdempotencyKey | None:
    """Busca la fila por (`key`, `user_id`), vencida o no. Solo lectura."""
    clean_key, _, _ = _validate_claim_fields(key, endpoint="/x", method="GET")
    uid = _coerce_uuid(user_id, "user_id")
    stmt = sa.select(IdempotencyKey).where(
        IdempotencyKey.key == clean_key, _user_filter(IdempotencyKey.user_id, uid)
    )
    return session.scalar(stmt)


def is_expired(row: IdempotencyKey, *, now: datetime | None = None) -> bool:
    """`True` si `expires_at` ya paso (dominio puro sobre la fila)."""
    moment = now or _utcnow()
    return _naive(row.expires_at) <= _naive(moment)


def try_claim(
    session: Session,
    *,
    key: str,
    user_id: uuid.UUID | str | None,
    endpoint: str,
    method: str,
    request_hash: str,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> tuple[IdempotencyKey, bool]:
    """Reclamo atomico por (`key`, `user_id`) UQ: no duplica.

    Retorna `(fila, creada)`. Si la fila ya existe retorna la existente con
    `creada=False` (el llamante decide: replay, 409 o renovacion por
    expiracion). Usa savepoint para no revertir el trabajo pendiente de la
    sesion ante el conflicto de UQ. Hace `flush`, no `commit`.

    `request_hash` debe ser hex SHA-256 (64 chars); se valida formato.
    """
    clean_key, clean_endpoint, clean_method = _validate_claim_fields(key, endpoint, method)
    uid = _coerce_uuid(user_id, "user_id")
    digest = str(request_hash or "").strip().lower()
    if len(digest) != 64:
        raise ValueError("request_hash debe ser SHA-256 hex (64 caracteres)")
    try:
        int(digest, 16)
    except ValueError as exc:
        raise ValueError("request_hash debe ser hexadecimal") from exc
    ttl = resolve_ttl_seconds(ttl_seconds)
    moment = now or _utcnow()
    row = IdempotencyKey(
        key=clean_key,
        user_id=uid,
        endpoint=clean_endpoint,
        method=clean_method,
        request_hash=digest,
        response_snapshot=None,
        transaction_id=None,
        expires_at=moment + timedelta(seconds=ttl),
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        existing = find_key(session, key=clean_key, user_id=uid)
        if existing is None:  # pragma: no cover - carrera extrema
            raise
        return existing, False
    return row, True


def renew_expired(
    session: Session,
    row: IdempotencyKey,
    *,
    request_hash: str,
    endpoint: str | None = None,
    method: str | None = None,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> IdempotencyKey:
    """Renueva una fila vencida como si fuera nueva: nuevo hash, nuevo TTL,
    snapshot y `transaction_id` limpiados. Hace `flush`, no `commit`."""
    digest = str(request_hash or "").strip().lower()
    if len(digest) != 64:
        raise ValueError("request_hash debe ser SHA-256 hex (64 caracteres)")
    ttl = resolve_ttl_seconds(ttl_seconds)
    moment = now or _utcnow()
    row.request_hash = digest
    if endpoint is not None:
        _, clean_endpoint, _ = _validate_claim_fields("k", endpoint, "GET")
        row.endpoint = clean_endpoint
    if method is not None:
        _, _, clean_method = _validate_claim_fields("k", "/x", method)
        row.method = clean_method
    row.response_snapshot = None
    row.transaction_id = None
    row.expires_at = moment + timedelta(seconds=ttl)
    session.flush()
    return row


def store_response(
    session: Session,
    row: IdempotencyKey,
    *,
    response_snapshot: dict,
    transaction_id: uuid.UUID | str | None = None,
) -> IdempotencyKey:
    """Persiste el snapshot de respuesta y la operacion asociada.

    `response_snapshot` debe ser dict JSON-serializable (sin `float` para
    dinero: montos en centimos enteros). Hace `flush`, no `commit`.
    """
    if not isinstance(response_snapshot, dict):
        raise TypeError("response_snapshot debe ser dict JSON-serializable")
    try:
        json.dumps(response_snapshot)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"response_snapshot no es JSON-serializable: {exc}") from exc
    row.response_snapshot = dict(response_snapshot)
    row.transaction_id = _coerce_uuid(transaction_id, "transaction_id")
    session.flush()
    return row
