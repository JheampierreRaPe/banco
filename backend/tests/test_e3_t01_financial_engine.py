"""E3-T01: motor financiero puro (cuota francesa, cronograma, TCEA). Sin BD."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.modules.credits.domain.financial import (
    FinancialValidationError,
    build_schedule,
    compute_tcea,
    french_payment,
    monthly_rate_from_annual,
    schedule_cashflows,
    simulate,
    with_insurance,
)


def _assert_invariants(principal: int, rows) -> None:
    assert sum(r.principal_minor for r in rows) == principal
    assert rows[-1].balance_minor == 0
    assert all(r.balance_minor >= 0 for r in rows)
    for r in rows:
        assert isinstance(r.principal_minor, int)
        assert isinstance(r.interest_minor, int)
        assert isinstance(r.insurance_minor, int)
        assert isinstance(r.total_minor, int)
        assert r.principal_minor + r.interest_minor + r.insurance_minor == r.total_minor


def test_reference_quota_tea20_12m():
    # Caso de referencia: S/10,000, TEA 20%, 12 meses -> cuota S/918.57.
    assert french_payment(1_000_000, Decimal("0.20"), 12) == 91857


def test_reference_quota_tea12_12m():
    # Caso de referencia: S/1,200, TEA 12%, 12 meses -> cuota S/106.28 (tol. 1 cent).
    assert abs(french_payment(120_000, Decimal("0.12"), 12) - 10628) <= 1


@pytest.mark.parametrize(
    "principal,tea,term",
    [
        (1_000_000, Decimal("0.20"), 12),
        (500_000, Decimal("0.35"), 24),
        (250_000, Decimal("0.10"), 6),
        (100_000, Decimal(0), 3),  # tasa 0
        (100_000, Decimal("0.15"), 1),  # plazo 1
        (7, Decimal("0.50"), 12),  # microcredito: redondeo extremo
    ],
)
def test_schedule_invariants(principal, tea, term):
    rows = build_schedule(principal, tea, term)
    assert len(rows) == term
    _assert_invariants(principal, rows)
    base = french_payment(principal, tea, term)
    for r in rows[:-1]:
        # Cuota fija; en el degenerado P < n (agotamiento anticipado) las filas
        # de cola quedan en cero con saldo 0 en lugar de la cuota base.
        assert (r.principal_minor + r.interest_minor == base) or (
            r.total_minor == 0 and r.balance_minor == 0
        )
    assert [r.installment_no for r in rows] == list(range(1, term + 1))


def test_zero_rate_splits_principal():
    rows = build_schedule(100_000, Decimal(0), 3)
    assert [r.total_minor for r in rows] == [33333, 33333, 33334]
    assert all(r.interest_minor == 0 for r in rows)
    _assert_invariants(100_000, rows)


def test_single_installment():
    rows = build_schedule(100_000, Decimal("0.15"), 1)
    assert len(rows) == 1
    assert rows[0].principal_minor == 100_000
    assert rows[0].balance_minor == 0
    expected_interest = french_payment(100_000, Decimal("0.15"), 1) - 100_000
    assert rows[0].total_minor == 100_000 + expected_interest


def test_zero_insurance_leaves_payment_unchanged():
    base = build_schedule(1_000_000, Decimal("0.20"), 12)
    insured = build_schedule(1_000_000, Decimal("0.20"), 12, Decimal("0.0005"))
    assert all(r.insurance_minor == 0 for r in base)
    assert sum(r.total_minor for r in insured) > sum(r.total_minor for r in base)
    _assert_invariants(1_000_000, insured)


def test_with_insurance_matches_build_schedule():
    base = build_schedule(600_000, Decimal("0.25"), 18)
    recalculated = with_insurance(base, Decimal("0.0004"))
    direct = build_schedule(600_000, Decimal("0.25"), 18, Decimal("0.0004"))
    assert [(r.insurance_minor, r.total_minor) for r in recalculated] == [
        (r.insurance_minor, r.total_minor) for r in direct
    ]
    assert all(r.insurance_minor >= 0 for r in recalculated)


def test_tcea_without_insurance_matches_tea():
    sim = simulate(1_000_000, Decimal("0.20"), 12)
    assert sim.tea == Decimal("0.20")
    assert abs(sim.tcea - Decimal("0.20")) < Decimal("0.0005")


def test_tcea_with_insurance_above_tea():
    plain = simulate(1_000_000, Decimal("0.20"), 12)
    insured = simulate(1_000_000, Decimal("0.20"), 12, Decimal("0.0005"))
    assert insured.tcea > plain.tcea
    # La TCEA incluye el desgravamen: el costo total sube.
    assert insured.total_paid_minor > plain.total_paid_minor


def test_compute_tcea_single_period():
    # Flujo [-100, 110]: TIR mensual 10% -> TCEA = 1.1^12 - 1 ~= 2.138428.
    tcea = compute_tcea([-100, 110])
    assert abs(tcea - Decimal("2.138428")) < Decimal("0.000001")


def test_compute_tcea_zero_cost():
    assert compute_tcea([-100, 0, 100]) == Decimal(0)


def test_determinism():
    kwargs = {"principal_minor": 750_000, "annual_rate": Decimal("0.18"), "term_months": 15}
    assert build_schedule(**kwargs) == build_schedule(**kwargs)
    assert simulate(**kwargs) == simulate(**kwargs)


def test_due_dates_monthly_with_month_end_clamp():
    rows = build_schedule(300_000, Decimal("0.12"), 3, first_due_date=date(2026, 1, 31))
    assert [r.due_date for r in rows] == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]


def test_schedule_cashflows_shape():
    rows = build_schedule(200_000, Decimal("0.10"), 4)
    flows = schedule_cashflows(200_000, rows)
    assert flows[0] == -200_000
    assert flows[1:] == [r.total_minor for r in rows]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"principal_minor": 0, "annual_rate": Decimal("0.1"), "term_months": 12},
        {"principal_minor": -100, "annual_rate": Decimal("0.1"), "term_months": 12},
        {"principal_minor": 100, "annual_rate": Decimal("0.1"), "term_months": 0},
        {"principal_minor": 100, "annual_rate": Decimal("0.1"), "term_months": -3},
        {"principal_minor": 100, "annual_rate": Decimal("-0.01"), "term_months": 12},
        {
            "principal_minor": 100,
            "annual_rate": Decimal("0.1"),
            "term_months": 12,
            "insurance_rate": Decimal("-0.001"),
        },
    ],
)
def test_validation_errors(kwargs):
    with pytest.raises(FinancialValidationError):
        build_schedule(**kwargs)
    with pytest.raises(ValueError):
        build_schedule(**kwargs)


def test_float_is_rejected():
    with pytest.raises(FinancialValidationError):
        french_payment(100_000, 0.20, 12)  # type: ignore[arg-type]
    with pytest.raises(FinancialValidationError):
        compute_tcea([-100.0, 110.0])  # type: ignore[list-item]


def test_empty_schedule_rejected():
    with pytest.raises(FinancialValidationError):
        with_insurance([], Decimal("0.001"))
    with pytest.raises(FinancialValidationError):
        schedule_cashflows(100, [])
    with pytest.raises(FinancialValidationError):
        compute_tcea([-100])


def test_monthly_rate_roundtrip():
    tea = Decimal("0.24")
    assert abs(monthly_rate_from_annual(tea) - Decimal("0.018087")) < Decimal("0.000001")
    from app.modules.credits.domain.financial import annual_rate_from_monthly

    assert abs(annual_rate_from_monthly(monthly_rate_from_annual(tea)) - tea) < Decimal(
        "0.000000001"
    )
