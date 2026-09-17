"""Idempotencia para endpoints que mueven dinero (E5-T04, HU17 CA-03).

Fuente: `docs/04-motor-transaccional-y-ledger.md#5-idempotencia`.
Deposito en `shared.idempotency_keys` via `app.modules.shared.repository`
(fachada del modulo dueno; sin acceder a tablas ajenas directamente salvo
por el repositorio compartido transversal). Sin publicar eventos aqui
(regla de oro 8) y sin `float` (montos en centimos enteros).

Protocolo (igual que `04#4-paso-1-resolver-idempotencia`):

- `Idempotency-Key` obligatoria en todo endpoint que mueve dinero;
  opcional (pero respetada si viene) en los que no mueven dinero.
- Misma clave + mismo cuerpo (SHA-256 canonico) -> se devuelve el
  `response_snapshot` guardado sin re-ejecutar el handler (sin doble debito).
- Misma clave + distinto cuerpo -> `409 Conflict`.
- Clave vencida (`expires_at` pasado) -> se trata como nueva (renueva TTL).

El middleware envuelve al handler: si hay hit, retorna el snapshot; si no,
ejecuta el handler, persiste (`flush`, sin `commit`: la transaccion la
decide el llamante) y retorna. El reclamo es atomico por UQ
(`key`, `user_id`): un doble claim simultaneo no duplica la ejecucion.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from sqlalchemy.orm import Session

from app.modules.shared.repository.idempotency import (
    compute_request_hash,
    find_key,
    is_expired,
    renew_expired,
    resolve_ttl_seconds,
    store_response,
    try_claim,
)

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

# Endpoints de dinero: exigen `Idempotency-Key` (regla de oro 5).
# Marca explicita para los que NO mueven dinero (cabecera opcional).
MONEY_ENDPOINTS: tuple[str, ...] = (
    "/transfers",
    "/payments",
    "/transactions",
)


class IdempotencyKeyMissingError(ValueError):
    """Falta `Idempotency-Key` en un endpoint que mueve dinero (-> 422)."""

    status_code = 422


class IdempotencyConflictError(ValueError):
    """Misma clave con distinto cuerpo, o marca en vuelo (-> 409)."""

    status_code = 409


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def moves_money(endpoint: str) -> bool:
    """Indica si la ruta mueve dinero (cabecera obligatoria).

    Comparacion por prefijo sobre `MONEY_ENDPOINTS`; las rutas fuera de
    esos prefijos se consideran de solo lectura (cabecera opcional).
    """
    path = str(endpoint or "")
    return any(path == p or path.startswith(p + "/") for p in MONEY_ENDPOINTS)


def require_idempotency_key(
    headers: Mapping[str, Any], *, endpoint: str
) -> str | None:
    """Exige `Idempotency-Key` si la ruta mueve dinero.

    Retorna la clave limpia, o `None` si la ruta no mueve dinero y no se
    envio cabecera (marca explicita: opcional solo fuera de dinero).
    Lanza `IdempotencyKeyMissingError` (-> 422) si falta en dinero.
    Acepta `headers` como mapping (insensible a mayusculas) o objeto
    con `.get` estilo Starlette.
    """
    found: Any = None
    try:
        get = getattr(headers, "get", None)
        if callable(get):
            found = get(IDEMPOTENCY_KEY_HEADER)
            if found is None and hasattr(headers, "get"):
                # Starlette `Headers` ya es insensible; dict plano: probar
                # variante en minusculas.
                found = get(IDEMPOTENCY_KEY_HEADER.lower())
        else:
            items = dict(headers)
            lowered = {str(k).lower(): v for k, v in items.items()}
            found = lowered.get(IDEMPOTENCY_KEY_HEADER.lower())
    except (AttributeError, TypeError, ValueError) as exc:
        raise IdempotencyKeyMissingError("cabecera Idempotency-Key invalida") from exc
    key = str(found).strip() if found is not None else ""
    if key:
        if len(key) > 80:
            raise IdempotencyKeyMissingError("Idempotency-Key supera 80 caracteres")
        return key
    if moves_money(endpoint):
        raise IdempotencyKeyMissingError(
            f"Idempotency-Key es obligatoria en {endpoint}"
        )
    return None


@dataclass(frozen=True)
class IdempotentResult:
    """Salida del middleware: respuesta + si fue replay (sin re-ejecutar)."""

    response: dict
    replayed: bool
    transaction_id: uuid.UUID | None = None


def execute_with_idempotency(
    session: Session,
    *,
    key: str,
    user_id: uuid.UUID | str | None,
    endpoint: str,
    method: str,
    body: dict | list | str | bytes | None,
    handler: Callable[[], tuple[dict, uuid.UUID | str | None]],
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> IdempotentResult:
    """Envuelve al handler con idempotencia (hace `flush`, no `commit`).

    - Reclamo atomico por UQ: si la fila es nueva, ejecuta `handler()`,
      persiste el snapshot y retorna `replayed=False`.
    - Si la fila existe y no vencio: mismo hash + snapshot -> retorna el
      snapshot (`replayed=True`) sin ejecutar; distinto hash -> 409; mismo
      hash sin snapshot (marca en vuelo por reclamo concurrente) -> 409.
    - Si vencio: renueva como nueva, ejecuta y retorna `replayed=False`.

    `handler` retorna `(respuesta_dict, transaction_id | None)` y no debe
    publicar eventos (regla de oro 8: el llamante usa outbox).
    """
    if not callable(handler):
        raise ValueError("handler debe ser callable")
    digest = compute_request_hash(body)
    ttl = resolve_ttl_seconds(ttl_seconds)
    moment = now or _utcnow()

    row, created = try_claim(
        session,
        key=key,
        user_id=user_id,
        endpoint=endpoint,
        method=method,
        request_hash=digest,
        ttl_seconds=ttl,
        now=moment,
    )
    if created:
        payload, transaction_id = handler()
        store_response(session, row, response_snapshot=payload, transaction_id=transaction_id)
        return IdempotentResult(
            response=dict(payload),
            replayed=False,
            transaction_id=row.transaction_id,
        )

    stored = find_key(session, key=key, user_id=user_id)
    if stored is None:  # pragma: no cover - carrera extrema
        raise IdempotencyConflictError("clave de idempotencia en conflicto")
    if is_expired(stored, now=moment):
        renew_expired(
            session,
            stored,
            request_hash=digest,
            endpoint=endpoint,
            method=method,
            ttl_seconds=ttl,
            now=moment,
        )
        payload, transaction_id = handler()
        store_response(
            session, stored, response_snapshot=payload, transaction_id=transaction_id
        )
        return IdempotentResult(
            response=dict(payload),
            replayed=False,
            transaction_id=stored.transaction_id,
        )
    if stored.request_hash != digest:
        raise IdempotencyConflictError(
            "Idempotency-Key ya usada con un cuerpo distinto"
        )
    if stored.response_snapshot is None:
        # Marca en vuelo: otro worker reclamo primero y aun no persiste la
        # respuesta. No re-ejecutar (sin doble debito): 409 para reintentar.
        raise IdempotencyConflictError(
            "operacion en curso para esta Idempotency-Key"
        )
    return IdempotentResult(
        response=dict(stored.response_snapshot),
        replayed=True,
        transaction_id=stored.transaction_id,
    )


def idempotency_dependency(*, endpoint: str):
    """Dependencia FastAPI que exige la cabecera segun la ruta.

    Uso: `key: str | None = Depends(idempotency_dependency(endpoint="/transfers"))`.
    Import perezoso de FastAPI para no acoplarlo en tests de dominio.
    """

    def _dependency(request) -> str | None:  # tipo laxo: evita import en tests
        return require_idempotency_key(request.headers, endpoint=endpoint)

    # Marca para documentar el contrato sin importar FastAPI aqui.
    _dependency.__idempotency_endpoint__ = endpoint  # type: ignore[attr-defined]
    return _dependency
