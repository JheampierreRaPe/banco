"""Cronograma definitivo del prestamo (E3-T18, HU12 CA-02).

Dominio puro: sin BD, sin I/O, sin logs con PII, sin estado global.
Reutiliza el motor financiero de E3-T01 (`financial.build_schedule` /
`financial._add_months`): no duplica formulas ni tasas (regla de oro 6;
toda regla variable viene del producto/expediente, nunca cableada).

Todo el dinero se expresa en centimos enteros (`int` minor);
internamente el motor usa `Decimal` con `ROUND_HALF_UP`. Jamas `float`.

El cronograma definitivo es el del motor **con fechas**: `installment_no`
1..N, vencimientos mensuales deterministas derivados de
`first_due_date` (el caso de uso E3-T17 la deriva del desembolso con
:func:`first_due_date_for`), y por cuota
``principal + interest + insurance == total`` con
``sum(principal) == principal`` y saldo final 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.modules.credits.domain import financial
from app.modules.credits.domain.financial import FinancialValidationError

__all__ = [
    "DefinitiveInstallment",
    "ScheduleValidationError",
    "build_definitive_schedule",
    "first_due_date_for",
]


class ScheduleValidationError(ValueError):
    """Dato del cronograma definitivo invalido (`VALIDATION_ERROR`)."""

    code = "VALIDATION_ERROR"


@dataclass(frozen=True)
class DefinitiveInstallment:
    """Una cuota del cronograma definitivo (forma de `loan_schedules`).

    `due_date` siempre tiene fecha (a diferencia de la simulacion
    previa al desembolso, que la deja en `None`).
    """

    installment_no: int
    due_date: date
    principal_minor: int
    interest_minor: int
    insurance_minor: int
    total_minor: int


def first_due_date_for(disbursed_on: date) -> date:
    """Primer vencimiento de un desembolso: un mes despues (mismo dia, con tope)."""
    if isinstance(disbursed_on, bool) or not isinstance(disbursed_on, date):
        raise ScheduleValidationError("disbursed_on debe ser date")
    return financial._add_months(disbursed_on, 1)


def build_definitive_schedule(
    *,
    principal_minor: int,
    annual_rate: Decimal | int | str,
    term_months: int,
    insurance_rate: Decimal | int | str = Decimal(0),
    first_due_date: date,
) -> tuple[DefinitiveInstallment, ...]:
    """Cronograma definitivo determinista (misma entrada -> mismo cronograma).

    Delega el calculo a `financial.build_schedule` (cuota francesa +
    desgravamen mensual, ultima cuota absorbe el residuo) y verifica
    los invariantes de cuadre antes de devolver:
    `principal+interest+insurance == total` por fila,
    `sum(principal) == principal_minor` y fechas mensuales exactas
    desde `first_due_date`.

    `annual_rate` es la TEA del producto (o su TCEA si `tea` es `NULL`,
    mismo criterio que el simulador E3-T03); `insurance_rate` es la
    tasa MENSUAL de desgravamen (`0` no altera la cuota).
    """
    if isinstance(first_due_date, bool) or not isinstance(first_due_date, date):
        raise ScheduleValidationError("first_due_date debe ser date")
    if isinstance(principal_minor, bool) or not isinstance(principal_minor, int):
        raise ScheduleValidationError("principal_minor debe ser entero de centimos")
    if isinstance(term_months, bool) or not isinstance(term_months, int):
        raise ScheduleValidationError("term_months debe ser entero de meses")
    for name, value in (("annual_rate", annual_rate), ("insurance_rate", insurance_rate)):
        if isinstance(value, float):
            raise ScheduleValidationError(f"{name} no acepta float (dinero entero)")
    try:
        rows = financial.build_schedule(
            principal_minor,
            annual_rate,
            term_months,
            insurance_rate,
            first_due_date,
        )
    except FinancialValidationError as exc:
        raise ScheduleValidationError(str(exc)) from exc
    if len(rows) != term_months:  # pragma: no cover - defensa en profundidad
        raise ScheduleValidationError("el motor devolvio un cronograma incompleto")
    if sum(row.principal_minor for row in rows) != principal_minor:
        raise ScheduleValidationError(  # pragma: no cover - defensa en profundidad
            "el cronograma no cuadra: sum(principal) != principal_minor"
        )
    out: list[DefinitiveInstallment] = []
    for pos, row in enumerate(rows, start=1):
        if row.due_date != financial._add_months(first_due_date, pos - 1):
            raise ScheduleValidationError(  # pragma: no cover - defensa en profundidad
                "el cronograma no cuadra: fechas no mensuales"
            )
        if row.principal_minor + row.interest_minor + row.insurance_minor != row.total_minor:
            raise ScheduleValidationError(  # pragma: no cover - defensa en profundidad
                f"la cuota {pos} no cuadra: principal+interest+insurance != total"
            )
        assert row.due_date is not None
        out.append(
            DefinitiveInstallment(
                installment_no=row.installment_no,
                due_date=row.due_date,
                principal_minor=row.principal_minor,
                interest_minor=row.interest_minor,
                insurance_minor=row.insurance_minor,
                total_minor=row.total_minor,
            )
        )
    return tuple(out)
