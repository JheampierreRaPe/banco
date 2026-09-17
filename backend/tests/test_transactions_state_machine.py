"""Pruebas unitarias de la maquina de estados (E5-T01). Dominio puro, sin BD."""

import pytest

from app.modules.transactions.domain import (
    ActorType,
    ConcurrencyError,
    InvalidTransitionError,
    UnauthorizedActorError,
    can_transition,
    guarded_transition,
    is_terminal,
    next_states,
    requires_hold_release,
    transition,
    validate_amount_minor,
    validate_currency,
)
from app.modules.transactions.domain import (
    TransactionStatus as S,
)


def test_happy_path_initiated_to_settled():
    path = [S.INITIATED, S.VALIDATED, S.AUTHORIZED, S.FUNDS_HELD, S.POSTED, S.SETTLED]
    state = path[0]
    for nxt in path[1:]:
        assert can_transition(state, nxt)
        state = transition(state, nxt, ActorType.SYSTEM)
    assert state is S.SETTLED


def test_happy_path_via_pending_authorization():
    assert transition(S.VALIDATED, S.PENDING_AUTHORIZATION) is S.PENDING_AUTHORIZATION
    assert transition(S.PENDING_AUTHORIZATION, S.AUTHORIZED, ActorType.USER) is S.AUTHORIZED


def test_illegal_transition_raises_explicitly():
    with pytest.raises(InvalidTransitionError):
        transition(S.INITIATED, S.SETTLED)
    with pytest.raises(InvalidTransitionError):
        transition(S.SETTLED, S.FAILED)  # SETTLED solo va a CONCILIATED/REVERSED
    with pytest.raises(InvalidTransitionError):
        transition(S.REJECTED, S.VALIDATED)  # terminal sin salida
    with pytest.raises(InvalidTransitionError):
        transition(S.SETTLED, S.SETTLED)  # doble procesamiento: reintento rechazado
    assert not can_transition(S.INITIATED, S.SETTLED)
    assert not can_transition("NOPE", S.VALIDATED)


def test_failed_from_funds_held_and_posted_releases_hold():
    assert transition(S.FUNDS_HELD, S.FAILED) is S.FAILED
    assert transition(S.POSTED, S.FAILED) is S.FAILED
    assert requires_hold_release(S.FAILED)
    assert not requires_hold_release(S.REJECTED)
    assert not requires_hold_release(S.SETTLED)


def test_reversed_from_posted_settled_conciliated():
    assert transition(S.POSTED, S.REVERSED) is S.REVERSED
    assert transition(S.SETTLED, S.REVERSED) is S.REVERSED
    assert transition(S.SETTLED, S.CONCILIATED) is S.CONCILIATED
    assert transition(S.CONCILIATED, S.REVERSED, ActorType.ANALYST) is S.REVERSED


@pytest.mark.parametrize(
    "origin", [S.INITIATED, S.VALIDATED, S.PENDING_AUTHORIZATION, S.AUTHORIZED]
)
def test_rejected_only_before_hold(origin):
    assert transition(origin, S.REJECTED) is S.REJECTED


def test_rejected_after_hold_is_invalid():
    with pytest.raises(InvalidTransitionError):
        transition(S.FUNDS_HELD, S.REJECTED)
    with pytest.raises(InvalidTransitionError):
        transition(S.POSTED, S.REJECTED)


def test_terminal_states():
    for terminal in (S.REJECTED, S.FAILED, S.REVERSED):
        assert is_terminal(terminal)
        assert next_states(terminal) == ()
    assert not is_terminal(S.SETTLED)
    assert not is_terminal(S.CONCILIATED)  # puede ir a REVERSED ante descuadre


def test_unauthorized_actor_rejected():
    with pytest.raises(UnauthorizedActorError):
        transition(S.INITIATED, S.VALIDATED, ActorType.USER)
    with pytest.raises(UnauthorizedActorError):
        transition(S.CONCILIATED, S.REVERSED, ActorType.USER)


def test_concurrency_guard_detects_double_processing():
    new_state, new_version = guarded_transition(
        S.POSTED, S.SETTLED, expected_version=3, current_version=3
    )
    assert new_state is S.SETTLED
    assert new_version == 4
    with pytest.raises(ConcurrencyError):
        guarded_transition(S.POSTED, S.SETTLED, expected_version=3, current_version=4)


def test_amount_and_currency_rules():
    assert validate_amount_minor(100) == 100
    with pytest.raises(ValueError):
        validate_amount_minor(0)
    with pytest.raises(ValueError):
        validate_amount_minor(-5)
    with pytest.raises(TypeError):
        validate_amount_minor(10.5)  # nunca float
    assert validate_currency("PEN") == "PEN"
    with pytest.raises(ValueError):
        validate_currency("pen")
    with pytest.raises(ValueError):
        validate_currency("USDD")
