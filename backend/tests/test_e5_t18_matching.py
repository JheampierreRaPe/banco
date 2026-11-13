"""Motor puro de matching con tolerancias (E5-T18 corte 1, HU19).

Cubre (`docs/tasks/E5-T18.md#pruebas-requeridas`, parte dominio):

- Camino feliz: match exacto (uno y lote) -> `matched_count = N`, 0 excepciones.
- `AMOUNT_DIFF`/`DATE_DIFF` dentro de tolerancia (incluye borde `diff == tol`);
  fuera de tolerancia (incluye tolerancia 0) -> excepcion tipada con detalle.
- Huerfanos: `MISSING_INTERNAL` / `MISSING_EXTERNAL`.
- `CURRENCY_MISMATCH` manda sobre monto/fecha fuera de tolerancia, nunca sobre un `Match`.
- Dos pasadas: un `Match` (exacto/tolerado) gana aunque un item anterior solo diera excepcion.
- Signo: montos distintos de cero (negativos = reversos/ajustes); `Match` exige mismo
  signo; `amount_diff_minor = clearing - internal`, `date_diff_days = clearing - internal`.
- Tolerancias mapping con claves estrictas (`TypeError` ante typo); moneda ISO-4217 ASCII.
- Invariantes de `Match`/`ReconException` por motivo; guardas de IDs/refs/moneda.
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
    assert result.matches[0].clearing_id == "it-1"
    assert result.exceptions[0].reason == MISSING_INTERNAL
    assert result.exceptions[0].clearing_id == "it-2"


def test_internal_op_used_at_most_once():
    result = match_items(
        [_op(op_id="op-1"), _op(op_id="op-2")],
        [_item(item_id="it-1")],
    )
    assert result.matched_count == 1
    assert result.exception_count == 1
    assert result.matches[0].internal_id == "op-1"
    assert result.exceptions[0].reason == MISSING_EXTERNAL
    assert result.exceptions[0].internal_id == "op-2"


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
    import itertools

    internal = [_op(f"op-{i}", ref="REF-1", amount=100_00 + i) for i in range(3)]
    clearing = [_item(f"it-{i}", ref="REF-1", amount=100_00 + i) for i in range(3)]
    tol = Tolerances(amount_tolerance_minor=5, date_tolerance_days=1)
    expected = match_items(internal, clearing, tol)
    for perm_in, perm_out in itertools.product(
        itertools.permutations(internal), itertools.permutations(clearing)
    ):
        assert match_items(list(perm_in), list(perm_out), tol) == expected
    assert match_items(internal, clearing, tol) == expected


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


# --- Regresion PR #25: la pasada 1 (Match) gana aunque el item anterior solo de excepcion ---


def test_greedy_exact_wins_over_out_of_tolerance_item_first():
    tol = Tolerances(amount_tolerance_minor=10, date_tolerance_days=0)
    result = match_items(
        [_op(op_id="op-1", ref="REF-9", amount=100_00)],
        [
            _item(item_id="it-a", ref="REF-9", amount=200_00),
            _item(item_id="it-b", ref="REF-9", amount=100_00),
        ],
        tol,
    )
    assert result.matched_count == 1
    assert (result.matches[0].internal_id, result.matches[0].clearing_id) == ("op-1", "it-b")
    assert result.matches[0].qualifier == EXACT
    assert result.exception_count == 1
    assert result.exceptions[0].reason == MISSING_INTERNAL
    assert result.exceptions[0].clearing_id == "it-a"


def test_greedy_exact_wins_over_currency_mismatch_item_first():
    result = match_items(
        [_op(op_id="op-1", ref="REF-9", amount=100_00, currency="PEN")],
        [
            _item(item_id="it-a", ref="REF-9", amount=100_00, currency="USD"),
            _item(item_id="it-b", ref="REF-9", amount=100_00, currency="PEN"),
        ],
    )
    assert result.matched_count == 1
    assert (result.matches[0].internal_id, result.matches[0].clearing_id) == ("op-1", "it-b")
    assert result.matches[0].qualifier == EXACT
    assert result.exception_count == 1
    assert result.exceptions[0].reason == MISSING_INTERNAL
    assert result.exceptions[0].clearing_id == "it-a"


def test_tolerated_wins_over_out_of_tolerance_item_first():
    tol = Tolerances(amount_tolerance_minor=10, date_tolerance_days=0)
    result = match_items(
        [_op(op_id="op-1", ref="REF-9", amount=100_00)],
        [
            _item(item_id="it-a", ref="REF-9", amount=500_00),
            _item(item_id="it-b", ref="REF-9", amount=100_05),
        ],
        tol,
    )
    assert result.matched_count == 1
    assert (result.matches[0].internal_id, result.matches[0].clearing_id) == ("op-1", "it-b")
    assert result.matches[0].qualifier == AMOUNT_DIFF
    assert result.exceptions[0].reason == MISSING_INTERNAL
    assert result.exceptions[0].clearing_id == "it-a"


# --- Signo: montos negativos y politica de mismo signo ---


def test_negative_amounts_match_exact():
    result = match_items(
        [_op(op_id="op-neg", amount=-100_00)],
        [_item(item_id="it-neg", amount=-100_00)],
    )
    assert result.matched_count == 1
    assert result.matches[0].qualifier == EXACT


def test_negative_amounts_within_tolerance_match():
    tol = Tolerances(amount_tolerance_minor=100, date_tolerance_days=0)
    result = match_items(
        [_op(op_id="op-neg", amount=-100_00)],
        [_item(item_id="it-neg", amount=-99_50)],
        tol,
    )
    assert result.matched_count == 1
    assert result.matches[0].qualifier == AMOUNT_DIFF


def test_opposite_signs_never_match_even_with_huge_tolerance():
    tol = Tolerances(amount_tolerance_minor=10**9, date_tolerance_days=10**4)
    result = match_items(
        [_op(op_id="op-1", amount=100_00)],
        [_item(item_id="it-1", amount=-100_00)],
        tol,
    )
    assert result.matched_count == 0
    assert result.exception_count == 1
    exc = result.exceptions[0]
    assert exc.reason == AMOUNT_DIFF
    assert exc.amount_diff_minor == -200_00


def test_zero_amount_rejected():
    with pytest.raises(ValueError, match="distinto de cero"):
        _op(amount=0)
    with pytest.raises(ValueError, match="distinto de cero"):
        _item(amount=0)


# --- Prioridad moneda vs tolerancia ---


def test_currency_mismatch_beats_out_of_tolerance_amount():
    result = match_items(
        [
            _op(op_id="op-usd", ref="REF-9", amount=500_00, currency="USD"),
            _op(op_id="op-pen", ref="REF-9", amount=100_00, currency="PEN"),
        ],
        [_item(item_id="it-1", ref="REF-9", amount=100_00, currency="USD")],
        Tolerances(),
    )
    assert result.matched_count == 0
    assert result.exception_count == 2
    by_internal = {e.internal_id: e for e in result.exceptions}
    assert by_internal["op-pen"].reason == CURRENCY_MISMATCH
    assert by_internal["op-pen"].clearing_id == "it-1"
    assert by_internal["op-usd"].reason == MISSING_EXTERNAL


def test_diff_sign_convention_clearing_minus_internal():
    result = match_items([_op(amount=100_00)], [_item(amount=99_00)], Tolerances())
    assert result.matched_count == 0
    (exc,) = result.exceptions
    assert exc.reason == AMOUNT_DIFF
    assert exc.amount_diff_minor == -100


# --- Tolerancias mapping: typo y valores ---


def test_mapping_with_unknown_key_rejected():
    with pytest.raises(TypeError, match="desconocidas"):
        match_items([_op()], [_item()], {"amount_toleranse_minor": 50})
    with pytest.raises(TypeError, match="desconocidas"):
        match_items(
            [_op()],
            [_item()],
            {"amount_tolerance_minor": 50, "date_tolerance_days": 0, "typo": 1},
        )


def test_mapping_with_invalid_values_rejected():
    with pytest.raises(ValueError):
        match_items([_op()], [_item()], {"amount_tolerance_minor": -1})
    with pytest.raises(TypeError):
        match_items([_op()], [_item()], {"amount_tolerance_minor": 1.5})
    with pytest.raises(TypeError):
        match_items([_op()], [_item()], {"date_tolerance_days": True})


# --- Moneda ISO-4217 ASCII ---


@pytest.mark.parametrize("bad", ["ПЕН", "ÁBC", "peN", "PEN1", "PE", "P EN", "P-EN"])
def test_non_ascii_or_malformed_currency_rejected(bad):
    with pytest.raises(ValueError):
        _op(currency=bad)
    with pytest.raises(ValueError):
        _item(currency=bad)


def test_currency_non_text_rejected():
    with pytest.raises(TypeError):
        _op(currency=123)
    with pytest.raises(TypeError):
        _item(currency=None)


# --- Guardas: _require_id / _require_ref / Match / ReconException ---


@pytest.mark.parametrize("bad", [123, None, ["op-1"]])
def test_require_id_non_text_rejected(bad):
    from app.modules.reconciliation.domain.matching import _require_id

    with pytest.raises(TypeError):
        _require_id(bad, name="op_id")


@pytest.mark.parametrize("bad", ["", "   "])
def test_require_id_empty_rejected(bad):
    from app.modules.reconciliation.domain.matching import _require_id

    with pytest.raises(ValueError):
        _require_id(bad, name="op_id")


@pytest.mark.parametrize("bad", [123, None])
def test_require_ref_non_text_rejected(bad):
    from app.modules.reconciliation.domain.matching import _require_ref

    with pytest.raises(TypeError):
        _require_ref(bad)


@pytest.mark.parametrize("bad", ["", "   "])
def test_require_ref_empty_rejected(bad):
    from app.modules.reconciliation.domain.matching import _require_ref

    with pytest.raises(ValueError):
        _require_ref(bad)


def test_match_ids_validated():
    from app.modules.reconciliation.domain.matching import Match

    with pytest.raises(TypeError):
        Match(internal_id=123, clearing_id="it-1")
    with pytest.raises(ValueError):
        Match(internal_id="", clearing_id="it-1")
    with pytest.raises(ValueError):
        Match(internal_id="op-1", clearing_id="   ")
    with pytest.raises(ValueError, match="qualifier invalido"):
        Match(internal_id="op-1", clearing_id="it-1", qualifier="NOPE")


def test_recon_exception_invariants():
    from app.modules.reconciliation.domain.matching import Match, ReconException

    assert Match(internal_id="op-1", clearing_id="it-1").qualifier == EXACT
    with pytest.raises(ValueError, match="reason invalido"):
        ReconException(reason="NOPE", internal_id="op-1")
    with pytest.raises(ValueError, match="al menos un lado"):
        ReconException(reason=AMOUNT_DIFF, amount_diff_minor=1)
    with pytest.raises(TypeError):
        ReconException(reason=MISSING_INTERNAL, clearing_id=123)
    with pytest.raises(ValueError):
        ReconException(reason=MISSING_INTERNAL, clearing_id="   ")


@pytest.mark.parametrize(
    "exc",
    [
        {"reason": MISSING_INTERNAL, "internal_id": "op-1", "clearing_id": "it-1"},
        {"reason": MISSING_INTERNAL, "clearing_id": None},
        {"reason": MISSING_INTERNAL},
        {"reason": MISSING_EXTERNAL, "internal_id": "op-1", "clearing_id": "it-1"},
        {"reason": MISSING_EXTERNAL, "internal_id": None},
        {"reason": MISSING_EXTERNAL},
        {"reason": AMOUNT_DIFF, "internal_id": "op-1", "clearing_id": "it-1"},
        {
            "reason": AMOUNT_DIFF,
            "internal_id": "op-1",
            "clearing_id": "it-1",
            "amount_diff_minor": True,
        },
        {
            "reason": DATE_DIFF,
            "internal_id": "op-1",
            "clearing_id": "it-1",
            "date_diff_days": None,
        },
        {"reason": CURRENCY_MISMATCH, "internal_id": "op-1", "clearing_id": None},
        {"reason": CURRENCY_MISMATCH, "internal_id": None, "clearing_id": "it-1"},
    ],
)
def test_recon_exception_reason_shape_rejected(exc):
    from app.modules.reconciliation.domain.matching import ReconException

    with pytest.raises(ValueError):
        ReconException(**exc)


def test_recon_exception_valid_shapes_accepted():
    from app.modules.reconciliation.domain.matching import ReconException

    assert ReconException(reason=MISSING_INTERNAL, clearing_id="it-1").internal_id is None
    assert ReconException(reason=MISSING_EXTERNAL, internal_id="op-1").clearing_id is None
    assert (
        ReconException(
            reason=AMOUNT_DIFF, internal_id="op-1", clearing_id="it-1", amount_diff_minor=-100
        ).amount_diff_minor
        == -100
    )
    assert (
        ReconException(
            reason=DATE_DIFF, internal_id="op-1", clearing_id="it-1", date_diff_days=3
        ).date_diff_days
        == 3
    )
    assert (
        ReconException(reason=CURRENCY_MISMATCH, internal_id="op-1", clearing_id="it-1").reason
        == CURRENCY_MISMATCH
    )


def test_exceptions_sorted_by_reason_then_ids():
    result = match_items(
        [_op(op_id="op-a", ref="REF-A"), _op(op_id="op-b", ref="REF-B", amount=100_00)],
        [
            _item(item_id="it-z", ref="REF-Z"),
            _item(item_id="it-b", ref="REF-B", amount=200_00),
        ],
        Tolerances(),
    )
    assert result.matched_count == 0
    assert result.exception_count == 3
    keys = [(e.reason, e.clearing_id or "", e.internal_id or "") for e in result.exceptions]
    assert keys == sorted(keys)
    reasons = [e.reason for e in result.exceptions]
    assert reasons.count(MISSING_EXTERNAL) == 1
    assert reasons.count(MISSING_INTERNAL) == 1
    assert reasons.count(AMOUNT_DIFF) == 1


# --- Fix PR #25 (2026-11-13): cardinalidad maxima entre tolerados ---


def test_max_cardinality_cross_tolerated_beats_greedy():
    """El greedy por puntaje elegia it-a/op-1 y dejaba un falso positivo.

    matching bipartito maximo cruza it-a/op-2 + it-b/op-1 = 2 matches.
    Falla sin el fix (1 match + 1 excepcion) y pasa con el.
    """
    tol = Tolerances(amount_tolerance_minor=10, date_tolerance_days=0)
    result = match_items(
        [
            _op(op_id="op-1", ref="REF-X", amount=100_00),
            _op(op_id="op-2", ref="REF-X", amount=100_12),
        ],
        [
            _item(item_id="it-a", ref="REF-X", amount=100_02),
            _item(item_id="it-b", ref="REF-X", amount=99_90),
        ],
        tol,
    )
    assert result.matched_count == 2
    assert result.exception_count == 0
    assert {(m.clearing_id, m.internal_id) for m in result.matches} == {
        ("it-a", "op-2"),
        ("it-b", "op-1"),
    }
    assert {m.qualifier for m in result.matches} == {AMOUNT_DIFF}


def test_max_cardinality_stable_under_permutation():
    import itertools

    internal = [
        _op(op_id="op-1", ref="REF-X", amount=100_00),
        _op(op_id="op-2", ref="REF-X", amount=100_12),
    ]
    clearing = [
        _item(item_id="it-a", ref="REF-X", amount=100_02),
        _item(item_id="it-b", ref="REF-X", amount=99_90),
    ]
    tol = Tolerances(amount_tolerance_minor=10, date_tolerance_days=0)
    expected = match_items(internal, clearing, tol)
    for perm_in, perm_out in itertools.product(
        itertools.permutations(internal), itertools.permutations(clearing)
    ):
        assert match_items(list(perm_in), list(perm_out), tol) == expected


def test_exact_buckets_pair_lexicographically():
    # Matching unico (F1): entre exactos equivalentes cualquier biyeccion es
    # maxima; se exige cardinalidad, calificadores y determinismo (no una
    # pareja concreta de la implementacion anterior por cubetas).
    result = match_items(
        [_op(op_id="op-2"), _op(op_id="op-1")],
        [_item(item_id="it-b"), _item(item_id="it-a")],
    )
    assert result.matched_count == 2
    assert result.exception_count == 0
    assert {m.qualifier for m in result.matches} == {EXACT}
    assert {m.clearing_id for m in result.matches} == {"it-a", "it-b"}
    assert {m.internal_id for m in result.matches} == {"op-1", "op-2"}
    expected = result
    import itertools as _it

    internal = [_op(op_id="op-2"), _op(op_id="op-1")]
    clearing = [_item(item_id="it-b"), _item(item_id="it-a")]
    for perm_in, perm_out in _it.product(_it.permutations(internal), _it.permutations(clearing)):
        assert match_items(list(perm_in), list(perm_out)) == expected


def test_recon_exception_zero_diffs_rejected():
    from app.modules.reconciliation.domain.matching import ReconException

    with pytest.raises(ValueError, match="distinto de cero"):
        ReconException(
            reason=AMOUNT_DIFF,
            internal_id="op-1",
            clearing_id="it-1",
            amount_diff_minor=0,
        )
    with pytest.raises(ValueError, match="distinto de cero"):
        ReconException(reason=DATE_DIFF, internal_id="op-1", clearing_id="it-1", date_diff_days=0)


def test_mapping_missing_keys_default_to_zero():
    from app.modules.reconciliation.domain.matching import _coerce_tolerances

    assert _coerce_tolerances({}) == Tolerances()
    assert _coerce_tolerances({"amount_tolerance_minor": 50}) == Tolerances(
        amount_tolerance_minor=50, date_tolerance_days=0
    )
    result = match_items([_op()], [_item(amount=100_50)], {"amount_tolerance_minor": 50})
    assert result.matched_count == 1
    assert result.matches[0].qualifier == AMOUNT_DIFF


def test_currency_mismatch_closest_operation_wins():
    result = match_items(
        [
            _op(op_id="op-z", ref="REF-C", amount=100_00, currency="USD"),
            _op(op_id="op-a", ref="REF-C", amount=999_00, currency="EUR"),
        ],
        [_item(item_id="it-1", ref="REF-C", amount=100_00, currency="PEN")],
    )
    assert result.matched_count == 0
    assert result.exception_count == 2
    by_internal = {e.internal_id: e for e in result.exceptions}
    assert by_internal["op-z"].reason == CURRENCY_MISMATCH
    assert by_internal["op-z"].clearing_id == "it-1"
    assert by_internal["op-a"].reason == MISSING_EXTERNAL


def test_candidate_safety_bound_raises_value_error():
    tol = Tolerances(amount_tolerance_minor=10**9, date_tolerance_days=10**4)
    internal = [_op(op_id=f"op-{i}", ref="REF-G", amount=100_00 + i) for i in range(5)]
    clearing = [_item(item_id=f"it-{i}", ref="REF-G", amount=200_00 + i) for i in range(5)]
    with pytest.raises(ValueError, match="cota de seguridad"):
        match_items(internal, clearing, tol, max_candidate_pairs=10)
    with pytest.raises(ValueError, match="max_candidate_pairs"):
        match_items(internal, clearing, tol, max_candidate_pairs=-1)
    with pytest.raises(TypeError, match="max_candidate_pairs"):
        match_items(internal, clearing, tol, max_candidate_pairs=True)


def test_narrow_tolerance_does_not_build_quadratic_pairs():
    import time
    from unittest.mock import patch

    import app.modules.reconciliation.domain.matching as matching_mod

    # Desplazamiento +3: entra a la fase tolerada (AMOUNT_DIFF), no a EXACT,
    # para que `_classify` realmente se ejerza (~n llamadas, no 0 ni n^2).
    internal = [_op(op_id=f"op-{i:03d}", ref="REF-P", amount=100_00 + i * 100) for i in range(300)]
    clearing = [
        _item(item_id=f"it-{i:03d}", ref="REF-P", amount=100_00 + i * 100 + 3) for i in range(300)
    ]
    tol = Tolerances(amount_tolerance_minor=10, date_tolerance_days=0)
    calls = 0
    real_classify = matching_mod._classify

    def counting_classify(op, entry, t):
        nonlocal calls
        calls += 1
        return real_classify(op, entry, t)

    start = time.perf_counter()
    with patch.object(matching_mod, "_classify", side_effect=counting_classify):
        result = match_items(internal, clearing, tol)
    elapsed = time.perf_counter() - start
    assert result.matched_count == 300
    assert result.exception_count == 0
    assert {m.qualifier for m in result.matches} == {AMOUNT_DIFF}
    # Camino indexado: ~O(n) clasificaciones, no O(n^2) = 90_000 pares;
    # y > 0 para demostrar que la fase tolerada se ejercio.
    assert 300 <= calls < 5000
    assert elapsed < 5.0


def test_match_result_coerces_lists_to_tuples():
    from app.modules.reconciliation.domain.matching import Match, MatchResult, ReconException

    result = MatchResult(
        matches=[Match(internal_id="op-1", clearing_id="it-1")],
        exceptions=[ReconException(reason=MISSING_EXTERNAL, internal_id="op-2")],
    )
    assert isinstance(result.matches, tuple)
    assert isinstance(result.exceptions, tuple)
    assert result.matched_count == 1
    assert result.exception_count == 1
    assert result == MatchResult(matches=result.matches, exceptions=result.exceptions)


# --- Fix validador F1 (2026-11-13): exacto que compite con tolerados ---


def test_max_cardinality_exact_competing_with_tolerated_case_a():
    """Caso A del validador: exacto it-a/op-1 compite; el maximo es 2.

    Falla sin el fix (1 match + 1 excepcion: el exacto greedy bloquea);
    pasa con matching unico (it-a/op-2 + it-b/op-1).
    """
    tol = Tolerances(amount_tolerance_minor=1, date_tolerance_days=0)
    result = match_items(
        [
            _op(op_id="op-1", ref="REF-A", amount=100_00),
            _op(op_id="op-2", ref="REF-A", amount=100_01),
        ],
        [
            _item(item_id="it-a", ref="REF-A", amount=100_00),
            _item(item_id="it-b", ref="REF-A", amount=99_99),
        ],
        tol,
    )
    assert result.matched_count == 2
    assert result.exception_count == 0
    assert {(m.clearing_id, m.internal_id) for m in result.matches} == {
        ("it-a", "op-2"),
        ("it-b", "op-1"),
    }


def test_max_cardinality_exact_competing_with_tolerated_case_b():
    """Caso B del validador: 3 ops vs 3 items con tol 1; el maximo es 3.

    Falla sin el fix (2 matches: exactos primero dejan un tolerado huerfano).
    """
    tol = Tolerances(amount_tolerance_minor=1, date_tolerance_days=0)
    result = match_items(
        [
            _op(op_id="op-1", ref="REF-B", amount=100_00),
            _op(op_id="op-2", ref="REF-B", amount=100_00),
            _op(op_id="op-3", ref="REF-B", amount=100_01),
        ],
        [
            _item(item_id="it-1", ref="REF-B", amount=100_00),
            _item(item_id="it-2", ref="REF-B", amount=100_01),
            _item(item_id="it-3", ref="REF-B", amount=100_02),
        ],
        tol,
    )
    assert result.matched_count == 3
    assert result.exception_count == 0


def test_max_cardinality_2x2_sweep_no_suboptimal():
    """Barrido 2x2: ninguna instancia pequeña pierde cardinalidad.

    Compara contra fuerza bruta (maximo exacto por enumeracion) en una malla
    de montos {100_00, 100_01} x tolerancias {0, 1}: 0 suboptimas.
    """
    import itertools

    amounts = [100_00, 100_01]
    suboptimal = 0
    checked = 0
    for tol_minor in (0, 1):
        tol = Tolerances(amount_tolerance_minor=tol_minor, date_tolerance_days=0)
        for ops_amts in itertools.product(amounts, repeat=2):
            for items_amts in itertools.product(amounts, repeat=2):
                internal = [
                    _op(op_id=f"op-{i}", ref="REF-S", amount=a) for i, a in enumerate(ops_amts)
                ]
                clearing = [
                    _item(item_id=f"it-{i}", ref="REF-S", amount=a)
                    for i, a in enumerate(items_amts)
                ]
                result = match_items(internal, clearing, tol)
                # Maximo por fuerza bruta sobre aristas Match.
                from app.modules.reconciliation.domain.matching import _classify as _c

                edges = [
                    (i, j)
                    for i, op in enumerate(internal)
                    for j, it in enumerate(clearing)
                    if isinstance(_c(op, it, tol), matching.Match)
                ]
                best = 0
                for r in range(len(edges) + 1):
                    for combo in itertools.combinations(edges, r):
                        oi = [x for x, _ in combo]
                        ji = [x for _, x in combo]
                        if len(set(oi)) == len(oi) and len(set(ji)) == len(ji):
                            best = max(best, len(combo))
                checked += 1
                if result.matched_count != best:
                    suboptimal += 1
    assert checked > 0
    assert suboptimal == 0


# --- Fix validador F2 (2026-11-13): cota tambien en la pasada de excepciones ---


def test_exceptions_pass_bound_fails_fast():
    """Grupo patologico sin matches: la pasada de excepciones falla rapido.

    500 libres x 500 libres = 250_000+ con cota 10 -> ValueError inmediato,
    no minutos de `min()` cartesiano.
    """
    import time

    tol = Tolerances()
    internal = [_op(op_id=f"op-{i:04d}", ref="REF-Q", amount=100_00 + i) for i in range(500)]
    clearing = [_item(item_id=f"it-{i:04d}", ref="REF-Q", amount=900_00 + i) for i in range(500)]
    start = time.perf_counter()
    with pytest.raises(ValueError, match="cota de seguridad"):
        match_items(internal, clearing, tol, max_candidate_pairs=10)
    assert time.perf_counter() - start < 5.0


# --- Fix validador F5 (2026-11-13): cobertura MatchResult ---


def test_pair_key_priority_levels():
    from app.modules.reconciliation.domain.matching import _pair_key

    tol = Tolerances(amount_tolerance_minor=10, date_tolerance_days=2)
    assert _pair_key(_op(), _item(), tol)[0] == 0
    assert _pair_key(_op(), _item(amount=100_05), tol)[0] == 1
    assert _pair_key(_op(), _item(amount=100_05, currency="USD"), tol)[0] == 2
    assert _pair_key(_op(), _item(amount=999_00), tol)[0] == 3


def test_match_result_rejects_invalid_elements():
    from app.modules.reconciliation.domain.matching import MatchResult, ReconException

    with pytest.raises(TypeError, match="matches debe contener Match"):
        MatchResult(matches=("no-un-match",), exceptions=())
    with pytest.raises(TypeError, match="exceptions debe contener ReconException"):
        MatchResult(
            matches=(),
            exceptions=(ReconException(reason=MISSING_EXTERNAL, internal_id="op-1"), "x"),
        )
