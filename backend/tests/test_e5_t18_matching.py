"""Motor puro de matching con tolerancias (E5-T18 corte 1, HU19).

Cubre (`docs/tasks/E5-T18.md#pruebas-requeridas`, parte dominio):

- Camino feliz: match exacto (uno y lote) -> `matched_count = N`, 0 excepciones.
- `AMOUNT_DIFF`/`DATE_DIFF` dentro de tolerancia (incluye borde `diff == tol`);
  fuera de tolerancia (incluye tolerancia 0) -> excepcion tipada con detalle.
- Huerfanos: `MISSING_INTERNAL` / `MISSING_EXTERNAL`.
- `CURRENCY_MISMATCH` (manda sobre monto/fecha).
- Sin reutilizacion: un item/operacion se usa como maximo una vez.
- Determinismo: el orden de entrada no cambia el resultado.
- Validacion: tolerancias negativas, montos no enteros, moneda/fecha
  invalidas, IDs duplicados -> `TypeError` (tipo) / `ValueError` (valor).

Puro (sin BD). No mueve dinero; sin PII en logs (el dominio no loguea).
"""

from __future__ import annotations

from datetime import date

import pytest

from app.modules.reconciliation.domain import matching
from app.modules.reconciliation.domain.matching import (
    AMOUNT_DIFF,
    CURRENCY_MISMATCH,
    DATE_DIFF,
    EXACT,
    MISSING_EXTERNAL,
    MISSING_INTERNAL,
    ClearingEntry,
    InternalOperation,
    MatchResult,
    Tolerances,
    match_items,
)

D = date(2026, 11, 13)


def _op(op_id="op-1", ref="REF-1", amount=100_00, currency="PEN", value_date=D):
    return InternalOperation(
        op_id=op_id,
        external_ref=ref,
        amount_minor=amount,
        currency=currency,
        value_date=value_date,
    )


def _item(item_id="it-1", ref="REF-1", amount=100_00, currency="PEN", value_date=D):
    return ClearingEntry(
        item_id=item_id,
        external_ref=ref,
        amount_minor=amount,
        currency=currency,
        value_date=value_date,
    )


def test_exact_match_single():
    result = match_items([_op()], [_item()])
    assert isinstance(result, MatchResult)
    assert result.matched_count == 1
    assert result.exception_count == 0
    (match,) = result.matches
    assert (match.internal_id, match.clearing_id, match.qualifier) == (
        "op-1",
        "it-1",
        EXACT,
    )


def test_exact_match_batch():
    internal = [_op(f"op-{i}", ref=f"REF-{i}") for i in range(5)]
    clearing = [_item(f"it-{i}", ref=f"REF-{i}") for i in range(5)]
    result = match_items(internal, clearing)
    assert result.matched_count == 5
    assert result.exception_count == 0
    assert {m.qualifier for m in result.matches} == {EXACT}


def test_amount_diff_within_tolerance_matches():
    tol = Tolerances(amount_tolerance_minor=100, date_tolerance_days=0)
    result = match_items([_op(amount=100_00)], [_item(amount=100_50)], tol)
    assert result.matched_count == 1
    assert result.exception_count == 0
    assert result.matches[0].qualifier == AMOUNT_DIFF


def test_amount_diff_on_boundary_matches():
    tol = Tolerances(amount_tolerance_minor=100, date_tolerance_days=0)
    result = match_items([_op(amount=100_00)], [_item(amount=101_00)], tol)
    assert result.matched_count == 1
    assert result.matches[0].qualifier == AMOUNT_DIFF


def test_amount_diff_outside_tolerance_is_exception():
    tol = Tolerances(amount_tolerance_minor=100, date_tolerance_days=0)
    result = match_items([_op()], [_item(amount=102_00)], tol)
    assert result.matched_count == 0
    assert result.exception_count == 1
    exc = result.exceptions[0]
    assert exc.reason == AMOUNT_DIFF
    assert exc.internal_id == "op-1"
    assert exc.clearing_id == "it-1"
    assert exc.amount_diff_minor == 200


def test_amount_diff_with_zero_tolerance_is_exception():
    result = match_items([_op()], [_item(amount=100_01)], Tolerances())
    assert result.matched_count == 0
    assert result.exceptions[0].reason == AMOUNT_DIFF


def test_date_diff_within_tolerance_matches():
    tol = Tolerances(amount_tolerance_minor=0, date_tolerance_days=2)
    result = match_items(
        [_op(value_date=date(2026, 11, 13))],
        [_item(value_date=date(2026, 11, 15))],
        tol,
    )
    assert result.matched_count == 1
    assert result.matches[0].qualifier == DATE_DIFF


def test_date_diff_on_boundary_matches():
    tol = Tolerances(amount_tolerance_minor=0, date_tolerance_days=2)
    result = match_items(
        [_op(value_date=date(2026, 11, 13))],
        [_item(value_date=date(2026, 11, 11))],
        tol,
    )
    assert result.matched_count == 1
    assert result.matches[0].qualifier == DATE_DIFF


def test_date_diff_outside_tolerance_is_exception():
    tol = Tolerances(amount_tolerance_minor=0, date_tolerance_days=2)
    result = match_items(
        [_op(value_date=date(2026, 11, 13))],
        [_item(value_date=date(2026, 11, 16))],
        tol,
    )
    assert result.matched_count == 0
    (exc,) = result.exceptions
    assert exc.reason == DATE_DIFF
    assert exc.date_diff_days == 3
    assert exc.amount_diff_minor is None


def test_missing_internal_and_external():
    result = match_items([_op(op_id="op-a", ref="REF-A")], [_item(item_id="it-b", ref="REF-B")])
    assert result.matched_count == 0
    assert result.exception_count == 2
    by_reason = {e.reason: e for e in result.exceptions}
    assert by_reason[MISSING_INTERNAL].clearing_id == "it-b"
    assert by_reason[MISSING_INTERNAL].internal_id is None
    assert by_reason[MISSING_EXTERNAL].internal_id == "op-a"
    assert by_reason[MISSING_EXTERNAL].clearing_id is None


def test_currency_mismatch_wins_over_amount_and_date():
    result = match_items(
        [_op(amount=100_00, currency="PEN", value_date=D)],
        [_item(amount=999_99, currency="USD", value_date=date(2026, 1, 1))],
        Tolerances(amount_tolerance_minor=10**9, date_tolerance_days=10**4),
    )
    assert result.matched_count == 0
    (exc,) = result.exceptions
    assert exc.reason == CURRENCY_MISMATCH
    assert (exc.internal_id, exc.clearing_id) == ("op-1", "it-1")


@pytest.mark.parametrize("tol", [Tolerances(), None, {}, {"amount_tolerance_minor": 0}])
def test_zero_tolerance_by_default(tol):
    result = match_items([_op()], [_item(amount=100_01)], tol)
    assert result.matched_count == 0
    assert result.exceptions[0].reason == AMOUNT_DIFF


def test_tolerance_changes_outcome_same_data():
    internal = [_op()]
    clearing = [_item(amount=100_50)]
    strict = match_items(internal, clearing, Tolerances())
    lax = match_items(
        internal, clearing, Tolerances(amount_tolerance_minor=50, date_tolerance_days=0)
    )
    assert strict.exception_count == 1
    assert lax.matched_count == 1
    assert lax.matches[0].qualifier == AMOUNT_DIFF


def test_amount_takes_precedence_over_date_within_tolerance():
    tol = Tolerances(amount_tolerance_minor=100, date_tolerance_days=2)
    result = match_items(
        [_op(amount=100_00, value_date=date(2026, 11, 13))],
        [_item(amount=100_50, value_date=date(2026, 11, 14))],
        tol,
    )
    assert result.matched_count == 1
    assert result.matches[0].qualifier == AMOUNT_DIFF


def test_amount_takes_precedence_over_date_outside_tolerance():
    tol = Tolerances(amount_tolerance_minor=10, date_tolerance_days=0)
    result = match_items(
        [_op(amount=100_00, value_date=date(2026, 11, 13))],
        [_item(amount=101_00, value_date=date(2026, 11, 20))],
        tol,
    )
    assert result.matched_count == 0
    (exc,) = result.exceptions
    assert exc.reason == AMOUNT_DIFF
    assert exc.amount_diff_minor == 100
    assert exc.date_diff_days == 7


def test_date_reason_when_only_date_fails():
    tol = Tolerances(amount_tolerance_minor=100, date_tolerance_days=0)
    result = match_items(
        [_op(amount=100_50, value_date=date(2026, 11, 13))],
        [_item(amount=100_00, value_date=date(2026, 11, 14))],
        tol,
    )
    assert result.matched_count == 0
    (exc,) = result.exceptions
    assert exc.reason == DATE_DIFF
    assert exc.date_diff_days == 1
    assert exc.amount_diff_minor == -50


def test_clearing_item_used_at_most_once():
    tol = Tolerances()
    result = match_items(
        [_op(op_id="op-1", amount=100_00)],
        [_item(item_id="it-1", amount=100_00), _item(item_id="it-2", amount=100_00)],
        tol,
    )
    assert result.matched_count == 1
    assert result.exception_count == 1
    assert result.exceptions[0].reason == MISSING_INTERNAL
    assert result.exceptions[0].clearing_id in {"it-1", "it-2"}


def test_internal_op_used_at_most_once():
    result = match_items(
        [_op(op_id="op-1"), _op(op_id="op-2")],
        [_item(item_id="it-1")],
    )
    assert result.matched_count == 1
    assert result.exception_count == 1
    assert result.exceptions[0].reason == MISSING_EXTERNAL
    assert result.exceptions[0].internal_id in {"op-1", "op-2"}


def test_best_candidate_wins_same_ref():
    tol = Tolerances(amount_tolerance_minor=10, date_tolerance_days=0)
    result = match_items(
        [_op(op_id="op-far", amount=500_00), _op(op_id="op-near", amount=100_05)],
        [_item(item_id="it-1", amount=100_00)],
        tol,
    )
    assert result.matched_count == 1
    assert result.matches[0].internal_id == "op-near"
    assert result.matches[0].qualifier == AMOUNT_DIFF
    assert result.exceptions[0].reason == MISSING_EXTERNAL
    assert result.exceptions[0].internal_id == "op-far"


def test_deterministic_under_input_permutation():
    internal = [_op(f"op-{i}", ref=f"REF-{i % 3}") for i in range(6)]
    clearing = [_item(f"it-{i}", ref=f"REF-{i % 3}") for i in range(6)]
    tol = Tolerances(amount_tolerance_minor=5, date_tolerance_days=1)
    first = match_items(internal, clearing, tol)
    second = match_items(list(reversed(internal)), list(reversed(clearing)), tol)
    assert first == second
    assert match_items(internal, clearing, tol) == first


def test_empty_inputs():
    result = match_items([], [], Tolerances())
    assert result.matched_count == 0
    assert result.exception_count == 0


def test_mapping_tolerances():
    result = match_items(
        [_op()],
        [_item(amount=100_50)],
        {"amount_tolerance_minor": 50, "date_tolerance_days": 0},
    )
    assert result.matched_count == 1
    assert result.matches[0].qualifier == AMOUNT_DIFF


@pytest.mark.parametrize(
    ("kwargs", "exc"),
    [
        ({"amount_tolerance_minor": -1}, ValueError),
        ({"date_tolerance_days": -1}, ValueError),
        ({"amount_tolerance_minor": 10.5}, TypeError),
        ({"amount_tolerance_minor": True}, TypeError),
    ],
)
def test_invalid_tolerances_rejected(kwargs, exc):
    with pytest.raises(exc):
        Tolerances(**kwargs)


@pytest.mark.parametrize(
    "make_bad",
    [
        lambda: _op(amount=100.0),
        lambda: _op(amount=True),
        lambda: _op(amount=0),
        lambda: _op(currency="pen"),
        lambda: _op(currency="PESOS"),
        lambda: _item(amount="10000"),
        lambda: _item(currency="US"),
    ],
)
def test_invalid_entries_rejected(make_bad):
    with pytest.raises((TypeError, ValueError)):
        make_bad()


def test_datetime_value_date_rejected():
    from datetime import datetime

    with pytest.raises(TypeError):
        _op(value_date=datetime(2026, 11, 13, 12, 0, 0))


def test_duplicate_ids_rejected():
    with pytest.raises(ValueError, match="op_id duplicado"):
        match_items([_op(op_id="dup"), _op(op_id="dup", ref="REF-2")], [_item()])
    with pytest.raises(ValueError, match="item_id duplicado"):
        match_items([_op()], [_item(item_id="dup"), _item(item_id="dup", ref="REF-2")])


def test_wrong_container_types_rejected():
    with pytest.raises(TypeError):
        match_items([{"op_id": "op-1"}], [_item()])
    with pytest.raises(TypeError):
        match_items([_op()], [{"item_id": "it-1"}])
    with pytest.raises(TypeError):
        match_items([_op()], [_item()], tolerances=[1, 2])


def test_no_transactions_import_in_domain():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "modules"
        / "reconciliation"
        / "domain"
        / "matching.py"
    ).read_text(encoding="utf-8")
    lines = source.splitlines()
    import_lines = [
        line
        for line in lines
        if line.strip().startswith(("import ", "from ")) and "transactions" in line
    ]
    assert import_lines == []
    assert "float(" not in source
    assert "logging" not in source
    assert matching.__name__.endswith("matching")
