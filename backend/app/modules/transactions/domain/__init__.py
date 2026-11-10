"""Dominio puro del modulo transactions (E5-T01)."""

from .state_machine import (
    TERMINAL_STATES,
    ActorType,
    ConcurrencyError,
    InvalidTransitionError,
    TransactionStatus,
    UnauthorizedActorError,
    authorized_actors,
    can_transition,
    guarded_transition,
    is_terminal,
    next_states,
    requires_hold_release,
    transition,
    validate_amount_minor,
    validate_currency,
)

__all__ = [
    "TERMINAL_STATES",
    "ActorType",
    "ConcurrencyError",
    "InvalidTransitionError",
    "TransactionStatus",
    "UnauthorizedActorError",
    "authorized_actors",
    "can_transition",
    "guarded_transition",
    "is_terminal",
    "next_states",
    "requires_hold_release",
    "transition",
    "validate_amount_minor",
    "validate_currency",
]
