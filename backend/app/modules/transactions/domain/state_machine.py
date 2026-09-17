"""Maquina de estados del motor transaccional (E5-T01, HU17).

Dominio puro: sin base de datos ni HTTP. Solo el motor cambia de estado;
cada cambio deja historia (``transaction_status_history``) en la capa de
servicio/persistencia (fuera de este modulo).

Referencias (context pack):
- ``docs/04-motor-transaccional-y-ledger.md`` #3 (maquina de estados) y #6 (concurrencia).
- ``docs/03b-diccionario-de-datos.md`` #6.1 (``transactions``) y #6.2 (``transaction_status_history``).
"""

from __future__ import annotations

import re
from enum import Enum


class TransactionStatus(str, Enum):
    INITIATED = "INITIATED"
    VALIDATED = "VALIDATED"
    PENDING_AUTHORIZATION = "PENDING_AUTHORIZATION"
    AUTHORIZED = "AUTHORIZED"
    FUNDS_HELD = "FUNDS_HELD"
    POSTED = "POSTED"
    SETTLED = "SETTLED"
    CONCILIATED = "CONCILIATED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    REVERSED = "REVERSED"


class ActorType(str, Enum):
    SYSTEM = "SYSTEM"
    USER = "USER"
    ANALYST = "ANALYST"


class InvalidTransitionError(ValueError):
    """Transicion no listada: debe fallar de forma explicita."""


class UnauthorizedActorError(ValueError):
    """Actor no autorizado para la transicion."""


class ConcurrencyError(ValueError):
    """La version esperada no coincide (optimistic locking, doble procesamiento)."""


# Transiciones validas. Toda transicion no listada es invalida.
# - REJECTED (negocio, sin movimiento): solo antes de retener fondos.
# - FAILED (tecnico, libera holds): solo tras retener (FUNDS_HELD, POSTED).
# - REVERSED (compensacion con asiento inverso): POSTED, SETTLED, CONCILIATED.
# - CONCILIATED -> REVERSED: la conciliacion detecto descuadre.
_TRANSITIONS: dict[TransactionStatus, frozenset[TransactionStatus]] = {
    TransactionStatus.INITIATED: frozenset(
        {TransactionStatus.VALIDATED, TransactionStatus.REJECTED}
    ),
    TransactionStatus.VALIDATED: frozenset(
        {
            TransactionStatus.PENDING_AUTHORIZATION,
            TransactionStatus.AUTHORIZED,
            TransactionStatus.REJECTED,
        }
    ),
    TransactionStatus.PENDING_AUTHORIZATION: frozenset(
        {TransactionStatus.AUTHORIZED, TransactionStatus.REJECTED}
    ),
    TransactionStatus.AUTHORIZED: frozenset(
        {TransactionStatus.FUNDS_HELD, TransactionStatus.REJECTED}
    ),
    TransactionStatus.FUNDS_HELD: frozenset(
        {TransactionStatus.POSTED, TransactionStatus.FAILED}
    ),
    TransactionStatus.POSTED: frozenset(
        {
            TransactionStatus.SETTLED,
            TransactionStatus.FAILED,
            TransactionStatus.REVERSED,
        }
    ),
    TransactionStatus.SETTLED: frozenset(
        {TransactionStatus.CONCILIATED, TransactionStatus.REVERSED}
    ),
    TransactionStatus.CONCILIATED: frozenset({TransactionStatus.REVERSED}),
    TransactionStatus.REJECTED: frozenset(),
    TransactionStatus.FAILED: frozenset(),
    TransactionStatus.REVERSED: frozenset(),
}

# Actores autorizados por transicion. Solo el motor cambia de estado; el actor
# indica quien origino el cambio que el motor registra en el historial.
_ACTORS: dict[tuple[TransactionStatus, TransactionStatus], frozenset[ActorType]] = {
    (TransactionStatus.INITIATED, TransactionStatus.VALIDATED): frozenset(
        {ActorType.SYSTEM}
    ),
    (TransactionStatus.INITIATED, TransactionStatus.REJECTED): frozenset(
        {ActorType.SYSTEM}
    ),
    (TransactionStatus.VALIDATED, TransactionStatus.PENDING_AUTHORIZATION): frozenset(
        {ActorType.SYSTEM}
    ),
    (TransactionStatus.VALIDATED, TransactionStatus.AUTHORIZED): frozenset(
        {ActorType.SYSTEM}
    ),
    (TransactionStatus.VALIDATED, TransactionStatus.REJECTED): frozenset(
        {ActorType.SYSTEM}
    ),
    (TransactionStatus.PENDING_AUTHORIZATION, TransactionStatus.AUTHORIZED): frozenset(
        {ActorType.USER, ActorType.SYSTEM}
    ),
    (TransactionStatus.PENDING_AUTHORIZATION, TransactionStatus.REJECTED): frozenset(
        {ActorType.SYSTEM, ActorType.USER}
    ),
    (TransactionStatus.AUTHORIZED, TransactionStatus.FUNDS_HELD): frozenset(
        {ActorType.SYSTEM}
    ),
    (TransactionStatus.AUTHORIZED, TransactionStatus.REJECTED): frozenset(
        {ActorType.SYSTEM}
    ),
    (TransactionStatus.FUNDS_HELD, TransactionStatus.POSTED): frozenset(
        {ActorType.SYSTEM}
    ),
    (TransactionStatus.FUNDS_HELD, TransactionStatus.FAILED): frozenset(
        {ActorType.SYSTEM}
    ),
    (TransactionStatus.POSTED, TransactionStatus.SETTLED): frozenset(
        {ActorType.SYSTEM}
    ),
    (TransactionStatus.POSTED, TransactionStatus.FAILED): frozenset(
        {ActorType.SYSTEM}
    ),
    (TransactionStatus.POSTED, TransactionStatus.REVERSED): frozenset(
        {ActorType.SYSTEM, ActorType.ANALYST}
    ),
    (TransactionStatus.SETTLED, TransactionStatus.CONCILIATED): frozenset(
        {ActorType.SYSTEM}
    ),
    (TransactionStatus.SETTLED, TransactionStatus.REVERSED): frozenset(
        {ActorType.SYSTEM, ActorType.ANALYST}
    ),
    (TransactionStatus.CONCILIATED, TransactionStatus.REVERSED): frozenset(
        {ActorType.SYSTEM, ActorType.ANALYST}
    ),
}

#: Estados terminales estrictos (sin salida). CONCILIATED es terminal salvo
#: descuadre (puede ir a REVERSED); ver `is_terminal`.
TERMINAL_STATES = frozenset(
    {TransactionStatus.REJECTED, TransactionStatus.FAILED, TransactionStatus.REVERSED}
)

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def next_states(current: TransactionStatus | str) -> tuple[TransactionStatus, ...]:
    """Estados alcanzables desde `current` (tupla ordenada por nombre)."""
    status = TransactionStatus(current)
    return tuple(sorted(_TRANSITIONS[status], key=lambda s: s.value))


def can_transition(
    current: TransactionStatus | str, target: TransactionStatus | str
) -> bool:
    """Indica si la transicion esta listada. No lanza; `transition` si lo hace."""
    try:
        from_status = TransactionStatus(current)
        to_status = TransactionStatus(target)
    except ValueError:
        return False
    return to_status in _TRANSITIONS[from_status]


def authorized_actors(
    current: TransactionStatus | str, target: TransactionStatus | str
) -> tuple[ActorType, ...]:
    """Actores autorizados para la transicion (vacio si es invalida)."""
    try:
        key = (TransactionStatus(current), TransactionStatus(target))
    except ValueError:
        return ()
    return tuple(sorted(_ACTORS.get(key, frozenset()), key=lambda a: a.value))


def is_terminal(status: TransactionStatus | str) -> bool:
    """`True` si el estado no admite mas transiciones.

    CONCILIATED puede pasar a REVERSED ante descuadre, por lo que no es
    estrictamente terminal (`False`); REJECTED/FAILED/REVERSED son `True`.
    """
    return TransactionStatus(status) in TERMINAL_STATES


def requires_hold_release(target: TransactionStatus | str) -> bool:
    """`True` si entrar a `target` exige liberar holds.

    Un FAILED siempre libera holds (2100 -> 2000); la liberacion la ejecuta
    la capa de servicio, aqui solo se detecta.
    """
    return TransactionStatus(target) is TransactionStatus.FAILED


def validate_amount_minor(value: int) -> int:
    """Valida `amount_minor`: entero positivo (centimos, nunca float)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"amount_minor debe ser entero, recibido: {value!r}")
    if value <= 0:
        raise ValueError(f"amount_minor debe ser > 0, recibido: {value}")
    return value


def validate_currency(code: str) -> str:
    """Valida moneda ISO-4217 (3 letras mayusculas, p. ej. PEN, USD)."""
    if not isinstance(code, str) or not _CURRENCY_RE.match(code):
        raise ValueError(f"currency debe ser ISO-4217 (3 letras mayusculas), recibido: {code!r}")
    return code


def transition(
    current: TransactionStatus | str,
    target: TransactionStatus | str,
    actor: ActorType | str = ActorType.SYSTEM,
) -> TransactionStatus:
    """Aplica una transicion valida y devuelve el nuevo estado.

    Lanza:
    - `InvalidTransitionError` si la transicion no esta listada (incluye
      reintentos/doble procesamiento: p. ej. SETTLED -> SETTLED).
    - `UnauthorizedActorError` si el actor no esta autorizado.
    """
    try:
        from_status = TransactionStatus(current)
    except ValueError as exc:
        raise InvalidTransitionError(f"estado origen desconocido: {current!r}") from exc
    try:
        to_status = TransactionStatus(target)
    except ValueError as exc:
        raise InvalidTransitionError(f"estado destino desconocido: {target!r}") from exc

    if to_status not in _TRANSITIONS[from_status]:
        raise InvalidTransitionError(
            f"transicion invalida: {from_status.value} -> {to_status.value}"
        )

    try:
        actor_type = ActorType(actor)
    except ValueError as exc:
        raise UnauthorizedActorError(f"actor desconocido: {actor!r}") from exc
    allowed = _ACTORS.get((from_status, to_status), frozenset())
    if actor_type not in allowed:
        allowed_names = ", ".join(sorted(a.value for a in allowed)) or "ninguno"
        raise UnauthorizedActorError(
            f"actor {actor_type.value} no autorizado para "
            f"{from_status.value} -> {to_status.value} (permitidos: {allowed_names})"
        )
    return to_status


def guarded_transition(
    current: TransactionStatus | str,
    target: TransactionStatus | str,
    actor: ActorType | str = ActorType.SYSTEM,
    *,
    expected_version: int,
    current_version: int,
) -> tuple[TransactionStatus, int]:
    """Transicion con optimistic locking (`version` de `transactions`).

    Detecta doble procesamiento concurrente: si `current_version` difiere de
    `expected_version`, otro proceso ya avanzo la transaccion y se rechaza
    antes de cambiar de estado. Devuelve `(nuevo_estado, nueva_version)`.
    """
    if current_version != expected_version:
        raise ConcurrencyError(
            f"version obsoleta: esperada {expected_version}, actual {current_version} "
            "(posible doble procesamiento)"
        )
    new_status = transition(current, target, actor)
    return new_status, current_version + 1
