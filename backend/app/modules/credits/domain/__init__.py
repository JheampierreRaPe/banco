"""Dominio puro de credits (E3-T01): motor financiero sin BD ni I/O."""

from app.modules.credits.domain.financial import (
    FinancialValidationError,
    LoanSimulation,
    ScheduleRow,
    annual_rate_from_monthly,
    build_schedule,
    compute_tcea,
    french_payment,
    monthly_rate_from_annual,
    schedule_cashflows,
    simulate,
    with_insurance,
)

__all__ = [
    "FinancialValidationError",
    "LoanSimulation",
    "ScheduleRow",
    "annual_rate_from_monthly",
    "build_schedule",
    "compute_tcea",
    "french_payment",
    "monthly_rate_from_annual",
    "schedule_cashflows",
    "simulate",
    "with_insurance",
]
