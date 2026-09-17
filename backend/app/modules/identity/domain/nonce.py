"""Nonces de un solo uso para el login con dispositivo (E1-T13, HU03 CA-01).

Dominio puro: sin BD, sin endpoints, testeable sin Postgres. El `nonce` lo
emite `POST /auth/login/challenge`; la app lo firma con la clave del
dispositivo (biometria local, desbloqueo en almacenamiento seguro) y el
servidor lo verifica en `POST /auth/login/facial` contra
`device_bindings.public_key` (D07: sin liveness del servidor aqui).

Reglas:

- Aleatorio seguro (`secrets.token_urlsafe`, 32 bytes de entropia).
- Un solo uso: `consume_nonce` marca `used=True`; reutilizarlo lanza
  `NonceReuseError` (el servicio lo mapea a error generico, sin filtrar).
- TTL corto: `NONCE_TTL_SECONDS = 120`. Movible a `config.parameters`
  (`auth.nonce_ttl_seconds`, misma clave y mismo valor) cuando config
  adopte la lectura (regla de oro 6); hoy es constante documentada porque
  ningun modulo del repo lee `parameters` via fachada aun (mismo criterio
  que `otp_service.OTP_TTL_SECONDS`).
- Sin `float`, sin secretos/PII en logs (este modulo no loguea), sin tocar
  otras capas (solo lo consume `service/device_login.py`).

NOTA de almacenamiento: los nonces viven en memoria del proceso (mapa con
candado, con purga oportunista de vencidos y tope blando). Es suficiente
para el MVP monolito; en produccion multirreplica van en Redis con
`SET NX EX` + borrado atomico al consumir (misma semantica: un solo uso +
TTL). La migracion `0015_identity_login` NO crea tabla de nonces a
proposito: el nonce jamas se persiste (solo su consumo via `sessions`).
"""

from __future__ import annotations

import secrets
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

#: Vigencia del nonce en segundos. TTL corto (HU03: la ventana de firma
#: debe ser minima). Candidata a `config.parameters` (`auth.nonce_ttl_seconds`,
#: mismo valor `120`) cuando config la adopte (regla de oro 6).
NONCE_TTL_SECONDS = 120

#: Entropia del nonce en bytes (`token_urlsafe(32)` -> ~43 caracteres).
NONCE_ENTROPY_BYTES = 32

#: Tope blando del mapa en memoria (purga de vencidos al superarlo).
_MAX_NONCES = 10_000


class NonceError(ValueError):
    """Error base de nonce (subclase de `ValueError` para mapear a 4xx)."""


class NonceNotFoundError(NonceError):
    """Nonce inexistente (nunca emitido o ya purgado)."""


class NonceReuseError(NonceError):
    """Nonce ya consumido: un solo uso, reutilizarlo es error."""


class NonceExpiredError(NonceError):
    """Nonce vencido (TTL agotado)."""


@dataclass
class NonceEntry:
    """Nonce emitido pendiente de firma (solo memoria, nunca se persiste)."""

    nonce: str
    user_id: uuid.UUID
    device_id: str | None
    expires_at: datetime
    used: bool = field(default=False)


_NONCES: dict[str, NonceEntry] = {}
_NONCES_LOCK = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    """Normaliza a tz-aware UTC (defensa ante relojes naive en pruebas)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _purge_expired_locked(now: datetime) -> None:
    """Elimina vencidos (con candado ya tomado; purga oportunista)."""
    if len(_NONCES) < _MAX_NONCES:
        expired = [key for key, entry in _NONCES.items() if entry.expires_at <= now]
    else:  # Mapa lleno: purga agresiva (vencidos + ya usados).
        expired = [
            key
            for key, entry in _NONCES.items()
            if entry.expires_at <= now or entry.used
        ]
    for key in expired:
        _NONCES.pop(key, None)


def issue_nonce(
    user_id: uuid.UUID | str,
    device_id: str | None = None,
    *,
    now: datetime | None = None,
    ttl_seconds: int | None = None,
) -> tuple[str, datetime]:
    """Emite un nonce de un solo uso (`(nonce, expires_at)`).

    `user_id` queda ligado al nonce: `login_with_device` resuelve el
    usuario desde aqui (el `facial` no necesita confiar en un `user_ref`
    del cliente). `ttl_seconds` solo existe para pruebas; en produccion
    rige `NONCE_TTL_SECONDS`.
    """
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"user_id debe ser UUID, recibido: {user_id!r}") from exc
    if device_id is not None and (
        not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 128
    ):
        raise ValueError("device_id debe ser texto de 1..128 caracteres")
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()
    if ttl_seconds is None:
        ttl_seconds = NONCE_TTL_SECONDS
    if (
        not isinstance(ttl_seconds, int)
        or isinstance(ttl_seconds, bool)
        or ttl_seconds <= 0
    ):
        raise ValueError("ttl_seconds debe ser int > 0")

    nonce = secrets.token_urlsafe(NONCE_ENTROPY_BYTES)
    entry = NonceEntry(
        nonce=nonce,
        user_id=uid,
        device_id=device_id.strip() if device_id is not None else None,
        expires_at=moment + timedelta(seconds=ttl_seconds),
    )
    with _NONCES_LOCK:
        _purge_expired_locked(moment)
        _NONCES[nonce] = entry
    return nonce, entry.expires_at


def consume_nonce(nonce: object, *, now: datetime | None = None) -> NonceEntry:
    """Consume un nonce (un solo uso; lo marca `used`).

    Exito: retorna el `NonceEntry` (con `user_id` ligado en la emision).
    Fallos: `NonceNotFoundError` (inexistente), `NonceReuseError` (ya
    usado), `NonceExpiredError` (TTL agotado; se purga al detectarlo).
    """
    if not isinstance(nonce, str) or not nonce.strip():
        raise NonceNotFoundError("desafio inexistente o ya utilizado")
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()
    with _NONCES_LOCK:
        entry = _NONCES.get(nonce.strip())
        if entry is None:
            raise NonceNotFoundError("desafio inexistente o ya utilizado")
        if entry.used:
            raise NonceReuseError("desafio ya utilizado")
        if entry.expires_at <= moment:
            _NONCES.pop(entry.nonce, None)
            raise NonceExpiredError("desafio vencido, solicite uno nuevo")
        entry.used = True
        return entry


def reset_nonces() -> None:
    """Limpia el mapa en memoria (uso en pruebas)."""
    with _NONCES_LOCK:
        _NONCES.clear()


__all__ = [
    "NONCE_ENTROPY_BYTES",
    "NONCE_TTL_SECONDS",
    "NonceEntry",
    "NonceError",
    "NonceExpiredError",
    "NonceNotFoundError",
    "NonceReuseError",
    "consume_nonce",
    "issue_nonce",
    "reset_nonces",
]
