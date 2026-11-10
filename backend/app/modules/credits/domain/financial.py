"""Motor de calculo financiero puro (E3-T01, HU09).

Dominio puro: sin BD, sin I/O, sin logs con PII, sin estado global.
Todo el dinero se expresa en centimos enteros (``int`` minor); internamente
se usa :class:`~decimal.Decimal` con redondeo ``ROUND_HALF_UP``. Jamas ``float``.

Convenciones (decision del dueno, 2026-10-09):
- Cuota por amortizacion francesa (cuota fija) a partir de la TEA del producto.
- Tasa del periodo mensual equivalente: ``i = (1 + TEA) ** (1/12) - 1``
  (capitalizacion mensual equivalente a la efectiva anual).
- Redondeo half-up a centimo en cada componente; la ultima cuota absorbe el
  residuo para que ``sum(principal) == P`` y el saldo final sea 0.
- Seguro de desgravamen: ``insurance_rate`` es la tasa MENSUAL aplicada sobre
  el saldo insoluto al inicio de cada periodo. Si es 0, la cuota no cambia.
- TCEA: TIR mensual del flujo (desembolso ``-P`` en t=0 y cuotas totales
  ``total_minor`` en t=1..n, que incluyen el seguro) anualizada como
  ``TCEA = (1 + irr_mensual) ** 12 - 1``. Sin seguro ni comisiones, la TCEA
  coincide con la TEA (salvo ruido de redondeo <= 1 centimo por cuota).
- Se exponen TEA (tasa de interes del producto) y TCEA (costo total con
  desgravamen) por separado: ver :func:`simulate`.
"""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext

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

_PREC = 28
_MAX_TCEA_ITERATIONS = 500
_TCEA_TOLERANCE = Decimal("1e-14")


class FinancialValidationError(ValueError):
    """Error tipado del dominio financiero (mapea a ``VALIDATION_ERROR``)."""


@dataclass(frozen=True)
class ScheduleRow:
    """Una cuota del cronograma (contrato de salida, forma de ``loan_schedules``).

    ``balance_minor`` es el saldo de capital despues de aplicar la cuota
    (0 en la ultima); no se persiste en ``loan_schedules`` pero permite
    recalcular el seguro y verificar invariantes sin estado externo.
    """

    installment_no: int
    due_date: date | None
    principal_minor: int
    interest_minor: int
    insurance_minor: int
    total_minor: int
    balance_minor: int


@dataclass(frozen=True)
class LoanSimulation:
    """Resultado agregado de :func:`simulate`: expone TEA y TCEA."""

    payment_minor: int
    schedule: tuple[ScheduleRow, ...]
    tea: Decimal
    tcea: Decimal
    total_principal_minor: int
    total_interest_minor: int
    total_insurance_minor: int
    total_paid_minor: int


def _check_no_float(**kwargs: object) -> None:
    for name, value in kwargs.items():
        if isinstance(value, float):
            raise FinancialValidationError(f"{name} no acepta float (dinero entero)")


def _to_rate(value: Decimal | int | str, name: str) -> Decimal:
    _check_no_float(**{name: value})
    try:
        rate = value if isinstance(value, Decimal) else Decimal(value)
    except Exception as exc:
        raise FinancialValidationError(f"{name} invalida: {value!r}") from exc
    if rate < 0:
        raise FinancialValidationError(f"{name} no puede ser negativa")
    return rate


def _to_positive_int(value: int, name: str) -> int:
    _check_no_float(**{name: value})
    if isinstance(value, bool) or not isinstance(value, int):
        raise FinancialValidationError(f"{name} debe ser entero")
    if value <= 0:
        raise FinancialValidationError(f"{name} debe ser > 0")
    return value


def _round_minor(exact_minor: Decimal) -> int:
    """Redondeo half-up a centimo entero."""
    with localcontext() as ctx:
        ctx.prec = _PREC
        return int(exact_minor.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def monthly_rate_from_annual(annual_rate: Decimal | int | str) -> Decimal:
    """Tasa mensual equivalente a una tasa efectiva anual: ``(1+TEA)**(1/12)-1``."""
    tea = _to_rate(annual_rate, "annual_rate")
    if tea == 0:
        return Decimal(0)
    with localcontext() as ctx:
        ctx.prec = _PREC
        monthly = (Decimal(1) + tea) ** (Decimal(1) / Decimal(12)) - Decimal(1)
        return +monthly


def annual_rate_from_monthly(monthly_rate: Decimal | int | str) -> Decimal:
    """Anualiza una tasa mensual: ``(1+i)**12-1`` (TEA desde i, o TCEA desde la TIR)."""
    i = _to_rate(monthly_rate, "monthly_rate")
    if i == 0:
        return Decimal(0)
    with localcontext() as ctx:
        ctx.prec = _PREC
        return +((Decimal(1) + i) ** Decimal(12) - Decimal(1))


def french_payment(
    principal_minor: int,
    annual_rate: Decimal | int | str,
    term_months: int,
) -> int:
    """Cuota francesa base (sin seguro), redondeada half-up a centimo.

    ``cuota = P * i / (1 - (1+i)**-n)`` con ``i`` mensual equivalente a la TEA.
    Con tasa 0, ``P / n`` redondeada (el cronograma ajusta la ultima cuota).
    """
    principal = _to_positive_int(principal_minor, "principal_minor")
    term = _to_positive_int(term_months, "term_months")
    tea = _to_rate(annual_rate, "annual_rate")
    with localcontext() as ctx:
        ctx.prec = _PREC
        balance = Decimal(principal)
        if tea == 0:
            return _round_minor(balance / Decimal(term))
        i = monthly_rate_from_annual(tea)
        factor = (Decimal(1) + i) ** Decimal(term)
        payment = balance * i * factor / (factor - Decimal(1))
        return _round_minor(payment)


def _add_months(base: date, months: int) -> date:
    month = base.month - 1 + months
    year = base.year + month // 12
    month = month % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(base.day, last_day))


def build_schedule(
    principal_minor: int,
    annual_rate: Decimal | int | str,
    term_months: int,
    insurance_rate: Decimal | int | str = Decimal(0),
    first_due_date: date | None = None,
) -> list[ScheduleRow]:
    """Cronograma frances con seguro de desgravamen opcional.

    - Interes del periodo = saldo insoluto * ``i`` (redondeado half-up).
    - Amortizacion = cuota - interes; la ULTIMA cuota absorbe el residuo
      (``principal = saldo``) para que ``sum(principal) == P`` y saldo final 0.
    - ``insurance_rate``: tasa MENSUAL sobre el saldo insoluto al inicio del
      periodo; ``0`` no altera la cuota. El seguro recalcula ``total_minor``.
    - Invariante por fila: ``principal + interest + insurance == total``.
    """
    principal = _to_positive_int(principal_minor, "principal_minor")
    term = _to_positive_int(term_months, "term_months")
    tea = _to_rate(annual_rate, "annual_rate")
    insurance = _to_rate(insurance_rate, "insurance_rate")
    if first_due_date is not None and not isinstance(first_due_date, date):
        raise FinancialValidationError("first_due_date debe ser date o None")

    with localcontext() as ctx:
        ctx.prec = _PREC
        i = monthly_rate_from_annual(tea) if tea != 0 else Decimal(0)
        payment = french_payment(principal, tea, term)

        rows: list[ScheduleRow] = []
        balance = principal
        for k in range(1, term + 1):
            balance_dec = Decimal(balance)
            interest = _round_minor(balance_dec * i) if i != 0 else 0
            ins = _round_minor(balance_dec * insurance) if insurance != 0 else 0
            if k < term:
                amort = payment - interest
                amort = max(amort, 0)
                amort = min(amort, balance)
                total = amort + interest + ins
                balance -= amort
            else:
                amort = balance
                total = amort + interest + ins
                balance = 0
            rows.append(
                ScheduleRow(
                    installment_no=k,
                    due_date=(
                        _add_months(first_due_date, k - 1) if first_due_date is not None else None
                    ),
                    principal_minor=amort,
                    interest_minor=interest,
                    insurance_minor=ins,
                    total_minor=total,
                    balance_minor=balance,
                )
            )
        return rows


def with_insurance(
    schedule: Sequence[ScheduleRow],
    insurance_rate: Decimal | int | str,
) -> list[ScheduleRow]:
    """Recalcula el seguro de un cronograma (tasa mensual sobre saldo inicial).

    El capital/interes no cambian (la cuota base francesa es fija); solo se
    recalculan ``insurance_minor`` y ``total_minor``. Con tasa 0 devuelve filas
    con seguro 0 (cuota intacta).
    """
    insurance = _to_rate(insurance_rate, "insurance_rate")
    rows = list(schedule)
    if not rows:
        raise FinancialValidationError("schedule vacio")
    with localcontext() as ctx:
        ctx.prec = _PREC
        principal_total = sum(r.principal_minor for r in rows)
        if principal_total <= 0:
            raise FinancialValidationError("schedule sin principal")
        out: list[ScheduleRow] = []
        paid_before = 0
        for row in rows:
            balance_start = principal_total - paid_before
            ins = _round_minor(Decimal(balance_start) * insurance) if insurance != 0 else 0
            out.append(
                ScheduleRow(
                    installment_no=row.installment_no,
                    due_date=row.due_date,
                    principal_minor=row.principal_minor,
                    interest_minor=row.interest_minor,
                    insurance_minor=ins,
                    total_minor=row.principal_minor + row.interest_minor + ins,
                    balance_minor=row.balance_minor,
                )
            )
            paid_before += row.principal_minor
        return out


def schedule_cashflows(principal_minor: int, schedule: Sequence[ScheduleRow]) -> list[int]:
    """Flujo del prestamo: ``[-P, total_1, ..., total_n]`` en centimos."""
    principal = _to_positive_int(principal_minor, "principal_minor")
    rows = list(schedule)
    if not rows:
        raise FinancialValidationError("schedule vacio")
    return [-principal] + [r.total_minor for r in rows]


def compute_tcea(cashflows: Sequence[int | Decimal]) -> Decimal:
    """TCEA por TIR del flujo: TIR mensual ``r`` tal que ``VPN(r) = 0``.

    ``cashflows[0] < 0`` (desembolso) y al menos una cuota ``> 0``.
    ``TCEA = (1 + r) ** 12 - 1``. Biseccion con ``Decimal`` (sin ``float``);
    flujo sin costo (todo cero salvo el desembolso compensado) -> ``0``.
    """
    if len(cashflows) < 2:
        raise FinancialValidationError("cashflows requiere desembolso + al menos 1 cuota")
    for pos, value in enumerate(cashflows):
        _check_no_float(**{f"cashflows[{pos}]": value})
    try:
        flows = [c if isinstance(c, Decimal) else Decimal(c) for c in cashflows]
    except Exception as exc:
        raise FinancialValidationError("cashflows con valores invalidos") from exc
    if flows[0] >= 0:
        raise FinancialValidationError("cashflows[0] debe ser el desembolso (< 0)")
    if not any(c > 0 for c in flows[1:]):
        raise FinancialValidationError("cashflows sin cuotas positivas")

    with localcontext() as ctx:
        ctx.prec = _PREC

        def npv(rate: Decimal) -> Decimal:
            total = Decimal(0)
            base = Decimal(1) + rate
            discount = Decimal(1)
            for flow in flows:
                total += flow / discount if discount != 0 else Decimal(0)
                discount *= base
            return total

        if npv(Decimal(0)) == 0:
            return Decimal(0)
        if npv(Decimal(0)) < 0:
            raise FinancialValidationError("flujo sin TIR positiva resoluble")

        low = Decimal(0)
        high = Decimal(1)
        while npv(high) > 0:
            high *= Decimal(2)
            if high > Decimal(10):
                raise FinancialValidationError("TIR fuera de rango")
        for _ in range(_MAX_TCEA_ITERATIONS):
            mid = (low + high) / Decimal(2)
            if high - low <= _TCEA_TOLERANCE:
                break
            if npv(mid) > 0:
                low = mid
            else:
                high = mid
        irr = (low + high) / Decimal(2)
        return annual_rate_from_monthly(irr)


def simulate(
    principal_minor: int,
    annual_rate: Decimal | int | str,
    term_months: int,
    insurance_rate: Decimal | int | str = Decimal(0),
    first_due_date: date | None = None,
) -> LoanSimulation:
    """Simulacion completa: cuota, cronograma, TEA y TCEA (con desgravamen)."""
    principal = _to_positive_int(principal_minor, "principal_minor")
    term = _to_positive_int(term_months, "term_months")
    tea = _to_rate(annual_rate, "annual_rate")
    insurance = _to_rate(insurance_rate, "insurance_rate")
    schedule = build_schedule(principal, tea, term, insurance, first_due_date)
    tcea = compute_tcea(schedule_cashflows(principal, schedule))
    return LoanSimulation(
        payment_minor=french_payment(principal, tea, term),
        schedule=tuple(schedule),
        tea=tea,
        tcea=tcea,
        total_principal_minor=sum(r.principal_minor for r in schedule),
        total_interest_minor=sum(r.interest_minor for r in schedule),
        total_insurance_minor=sum(r.insurance_minor for r in schedule),
        total_paid_minor=sum(r.total_minor for r in schedule),
    )
