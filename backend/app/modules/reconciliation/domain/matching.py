"""Motor puro de matching de conciliacion (E5-T18 corte 1, HU19).

Cruza items de clearing contra operaciones internas del motor por
**referencia externa**, monto, moneda y fecha valor, aplicando tolerancias
configurables, y clasifica cada par como ``MATCHED`` o ``EXCEPTION`` con un
motivo tipado.

Reglas (``docs/tasks/E5-T18.md#reglas-de-negocio``):

- La referencia externa manda: solo se emparejan entradas que comparten
  ``external_ref``. Cada item/operacion se usa **como maximo una vez**.
- Prioridad de emparejo (de mejor a peor): ``EXACT`` (0) > tolerado
  ``AMOUNT_DIFF``/``DATE_DIFF`` (1) > ``CURRENCY_MISMATCH`` (2) > fuera de
  tolerancia (3). La moneda distinta manda sobre un monto/fecha fuera de
  tolerancia, pero nunca sobre un ``Match`` (exacto o tolerado).
- El cruce corre en **dos pasadas**: la pasada 1 solo resuelve los pares que
  clasifican como ``Match`` (exacto o tolerado) y consume alli la operacion;
  la pasada 2 empareja los sobrantes y los clasifica como excepcion. Asi un
  ``EXACT`` gana aunque un item lexicograficamente anterior solo diera
  excepcion con la misma operacion.
- Politica de signo: los montos son enteros **distintos de cero** (positivos
  para cargos/abonos, negativos para reversos/contracargos/ajustes). Un
  ``Match`` exige **mismo signo** en ambos lados; la tolerancia compara la
  **diferencia absoluta**; signos opuestos nunca son ``Match`` (se clasifican
  como ``AMOUNT_DIFF``/excepcion).
- Convencion de signos de los detalles: ``amount_diff_minor = clearing -
  internal`` y ``date_diff_days = clearing_date - internal_date``. Ejemplo:
  operacion ``100_00`` vs item ``99_00`` -> ``amount_diff_minor = -100``.
- Monto distinto con ``abs(diff) <= amount_tolerance_minor`` (y mismo signo)
  -> ``AMOUNT_DIFF``; fecha distinta con ``abs(diff) <= date_tolerance_days``
  -> ``DATE_DIFF``.
- Monto/fecha fuera de tolerancia -> ``EXCEPTION`` con el motivo de la
  primera dimension que falla (monto antes que fecha) y el detalle numerico.
- Item de clearing sin operacion interna -> ``MISSING_INTERNAL``.
- Operacion interna sin item de clearing -> ``MISSING_EXTERNAL``.

Tolerancias (``docs/16#2`` regla 6): se reciben como parametro
(:class:`Tolerances`, default ``0``/``0``); nunca cableadas aqui. El corte 2
las leera de ``config.parameters`` (``reconciliation.amount_tolerance_minor``,
``reconciliation.date_tolerance_days``). Claves aceptadas en mapping:
``amount_tolerance_minor`` y ``date_tolerance_days``; cualquier otra clave se
rechaza con ``TypeError`` (un typo no debe desactivar la tolerancia en
silencio). El corte 2 debe acotar la tolerancia leida de ``config.parameters``
(ver nota de parcialidad en ``docs/tasks/E5-T18.md``).

Garantias del dominio:

- **Puro y determinista**: mismos datos + mismas tolerancias -> mismo
  resultado, sin importar el orden de entrada. Sin I/O, sin reloj, sin azar.
- **Dinero en enteros**: montos y tolerancias son ``int`` (centimos); se
  rechaza ``float``/``bool``/``str`` con ``TypeError``.
- **Sin PII**: solo viajan IDs, referencias externas y montos; este modulo
  no escribe logs.
- **Sin imports de otros modulos**: no toca modelos de ``transactions``;
  el corte 2 adaptara los dicts de la fachada de lectura a estos tipos.

Nota de alcance (corte 1): ``external_ref`` es obligatorio y no vacio en
ambos lados; la politica de fallback para referencias vacias/ausentes y el
acotado de tolerancias desde ``config.parameters`` son decision del corte 2.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

EXACT = "EXACT"
AMOUNT_DIFF = "AMOUNT_DIFF"
DATE_DIFF = "DATE_DIFF"
MISSING_INTERNAL = "MISSING_INTERNAL"
MISSING_EXTERNAL = "MISSING_EXTERNAL"
CURRENCY_MISMATCH = "CURRENCY_MISMATCH"

#: Calificadores de un par conciliado (``Match.qualifier``).
MATCH_QUALIFIERS = (EXACT, AMOUNT_DIFF, DATE_DIFF)

#: Motivos tipados de una excepcion (``ReconException.reason``).
EXCEPTION_REASONS = (
    AMOUNT_DIFF,
    DATE_DIFF,
    MISSING_INTERNAL,
    MISSING_EXTERNAL,
    CURRENCY_MISMATCH,
)

#: Claves aceptadas al recibir tolerancias como mapping.
TOLERANCE_KEYS = frozenset({"amount_tolerance_minor", "date_tolerance_days"})


def _require_id(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} debe ser texto")
    if not value.strip():
        raise ValueError(f"{name} debe ser un texto no vacio")
    return value.strip()


def _require_ref(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("external_ref debe ser texto")
    if not value.strip():
        raise ValueError("external_ref debe ser un texto no vacio")
    return value.strip()


def _require_amount(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} debe ser un entero en centimos (sin float)")
    if value == 0:
        raise ValueError(f"{name} debe ser distinto de cero (positivo o negativo)")
    return value


def _require_currency(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("currency debe ser texto ISO-4217 en mayusculas")
    if re.fullmatch(r"[A-Z]{3}", value) is None:
        raise ValueError("currency debe ser ISO-4217 en mayusculas (p. ej. 'PEN')")
    return value


def _require_value_date(value: object) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError("value_date debe ser datetime.date (sin hora)")
    return value


def _require_tolerance(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} debe ser un entero >= 0 (sin float)")
    if value < 0:
        raise ValueError(f"{name} debe ser >= 0")
    return value


@dataclass(frozen=True)
class Tolerances:
    """Tolerancias configurables del cruce (corte 2: desde ``config.parameters``).

    Claves aceptadas: ``amount_tolerance_minor`` y ``date_tolerance_days``
    (enteros ``>= 0``). Ver :func:`_coerce_tolerances` para el mapping.
    """

    amount_tolerance_minor: int = 0
    date_tolerance_days: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "amount_tolerance_minor",
            _require_tolerance(self.amount_tolerance_minor, name="amount_tolerance_minor"),
        )
        object.__setattr__(
            self,
            "date_tolerance_days",
            _require_tolerance(self.date_tolerance_days, name="date_tolerance_days"),
        )


@dataclass(frozen=True)
class InternalOperation:
    """Operacion interna candidata del motor (vista minima, sin ORM ajeno).

    ``amount_minor`` es un entero distinto de cero: positivo (cargo/abono) o
    negativo (reverso/contracargo/ajuste). Un ``Match`` exige mismo signo en
    ambos lados (ver politica de signo en el modulo).
    """

    op_id: str
    external_ref: str
    amount_minor: int
    currency: str
    value_date: date

    def __post_init__(self) -> None:
        object.__setattr__(self, "op_id", _require_id(self.op_id, name="op_id"))
        object.__setattr__(self, "external_ref", _require_ref(self.external_ref))
        object.__setattr__(
            self, "amount_minor", _require_amount(self.amount_minor, name="amount_minor")
        )
        object.__setattr__(self, "currency", _require_currency(self.currency))
        object.__setattr__(self, "value_date", _require_value_date(self.value_date))


@dataclass(frozen=True)
class ClearingEntry:
    """Item de clearing a cruzar (vista minima, sin ORM).

    ``amount_minor`` es un entero distinto de cero: positivo o negativo
    (reversos/ajustes del clearing). Ver politica de signo en el modulo.
    """

    item_id: str
    external_ref: str
    amount_minor: int
    currency: str
    value_date: date

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _require_id(self.item_id, name="item_id"))
        object.__setattr__(self, "external_ref", _require_ref(self.external_ref))
        object.__setattr__(
            self, "amount_minor", _require_amount(self.amount_minor, name="amount_minor")
        )
        object.__setattr__(self, "currency", _require_currency(self.currency))
        object.__setattr__(self, "value_date", _require_value_date(self.value_date))


@dataclass(frozen=True)
class Match:
    """Par conciliado (cada ID aparece como maximo una vez en el resultado)."""

    internal_id: str
    clearing_id: str
    qualifier: str = EXACT

    def __post_init__(self) -> None:
        object.__setattr__(self, "internal_id", _require_id(self.internal_id, name="internal_id"))
        object.__setattr__(self, "clearing_id", _require_id(self.clearing_id, name="clearing_id"))
        if self.qualifier not in MATCH_QUALIFIERS:
            raise ValueError(f"qualifier invalido: {self.qualifier!r}")


@dataclass(frozen=True)
class ReconException:
    """Item u operacion no conciliados, con motivo tipado y detalle numerico.

    Convencion de signos: ``amount_diff_minor = clearing - internal`` y
    ``date_diff_days = clearing_date - internal_date`` (ej.: op ``100_00`` vs
    item ``99_00`` -> ``amount_diff_minor = -100``).

    Invariantes por motivo:

    - IDs presentes siempre no vacios.
    - ``MISSING_INTERNAL`` exige ``internal_id is None`` y clearing presente;
      ``MISSING_EXTERNAL`` al reves.
    - ``AMOUNT_DIFF`` exige ``amount_diff_minor`` no nulo; ``DATE_DIFF`` exige
      ``date_diff_days`` no nulo.
    - ``CURRENCY_MISMATCH`` exige ambos lados presentes.
    """

    reason: str
    internal_id: str | None = None
    clearing_id: str | None = None
    amount_diff_minor: int | None = None
    date_diff_days: int | None = None

    def __post_init__(self) -> None:
        if self.reason not in EXCEPTION_REASONS:
            raise ValueError(f"reason invalido: {self.reason!r}")
        if self.internal_id is not None:
            object.__setattr__(
                self, "internal_id", _require_id(self.internal_id, name="internal_id")
            )
        if self.clearing_id is not None:
            object.__setattr__(
                self, "clearing_id", _require_id(self.clearing_id, name="clearing_id")
            )
        if self.reason == MISSING_INTERNAL:
            if self.internal_id is not None:
                raise ValueError("MISSING_INTERNAL exige internal_id is None")
            if self.clearing_id is None:
                raise ValueError("MISSING_INTERNAL exige clearing_id presente")
        elif self.reason == MISSING_EXTERNAL:
            if self.clearing_id is not None:
                raise ValueError("MISSING_EXTERNAL exige clearing_id is None")
            if self.internal_id is None:
                raise ValueError("MISSING_EXTERNAL exige internal_id presente")
        elif self.reason == AMOUNT_DIFF:
            if self.amount_diff_minor is None:
                raise ValueError("AMOUNT_DIFF exige amount_diff_minor no nulo")
        elif self.reason == DATE_DIFF:
            if self.date_diff_days is None:
                raise ValueError("DATE_DIFF exige date_diff_days no nulo")
        elif self.reason == CURRENCY_MISMATCH and (
            self.internal_id is None or self.clearing_id is None
        ):
            raise ValueError("CURRENCY_MISMATCH exige ambos lados")
        if self.internal_id is None and self.clearing_id is None:
            raise ValueError("la excepcion debe referenciar al menos un lado")
        for diff_name in ("amount_diff_minor", "date_diff_days"):
            diff = getattr(self, diff_name)
            if diff is not None and (isinstance(diff, bool) or not isinstance(diff, int)):
                raise ValueError(f"{diff_name} debe ser entero o None")


@dataclass(frozen=True)
class MatchResult:
    """Resultado determinista del cruce (orden estable por IDs)."""

    matches: tuple[Match, ...] = field(default_factory=tuple)
    exceptions: tuple[ReconException, ...] = field(default_factory=tuple)

    @property
    def matched_count(self) -> int:
        return len(self.matches)

    @property
    def exception_count(self) -> int:
        return len(self.exceptions)


def _coerce_tolerances(tolerances: Tolerances | Mapping[str, object] | None) -> Tolerances:
    """Normaliza tolerancias; claves aceptadas: ``TOLERANCE_KEYS``.

    Cualquier clave desconocida se rechaza con ``TypeError`` para que un typo
    no desactive la tolerancia en silencio. Los valores se validan como
    enteros ``>= 0`` (``TypeError``/``ValueError``).
    """
    if tolerances is None:
        return Tolerances()
    if isinstance(tolerances, Tolerances):
        return tolerances
    if isinstance(tolerances, Mapping):
        unknown = sorted(set(tolerances.keys()) - TOLERANCE_KEYS)
        if unknown:
            raise TypeError(
                f"claves de tolerancia desconocidas: {unknown!r}; "
                f"aceptadas: {sorted(TOLERANCE_KEYS)!r}"
            )
        return Tolerances(
            amount_tolerance_minor=tolerances.get("amount_tolerance_minor", 0),
            date_tolerance_days=tolerances.get("date_tolerance_days", 0),
        )
    raise TypeError("tolerances debe ser Tolerances, mapping o None")


def match_items(
    internal: Sequence[InternalOperation],
    clearing: Sequence[ClearingEntry],
    tolerances: Tolerances | Mapping[str, object] | None = None,
) -> MatchResult:
    """Cruza clearing contra operaciones internas (puro y determinista).

    Empareja por ``external_ref`` (uno a uno, sin reutilizar) en **dos
    pasadas**: la pasada 1 resuelve todos los pares que clasifican como
    ``Match`` (exacto o tolerado, mejor candidato primero) y consume alli la
    operacion; la pasada 2 empareja los sobrantes y los clasifica como
    excepcion (``CURRENCY_MISMATCH``/fuera de tolerancia/``MISSING_INTERNAL``).
    Asi un ``EXACT`` gana aunque un item lexicograficamente anterior solo
    diera excepcion con la misma operacion. El orden de entrada no afecta el
    resultado: todo se ordena antes de emparejar y el resultado sale ordenado
    por IDs.
    """
    tol = _coerce_tolerances(tolerances)

    internals = list(internal)
    entries = list(clearing)
    for op in internals:
        if not isinstance(op, InternalOperation):
            raise TypeError("internal debe contener InternalOperation")
    for entry in entries:
        if not isinstance(entry, ClearingEntry):
            raise TypeError("clearing debe contener ClearingEntry")

    seen_ops = set()
    for op in internals:
        if op.op_id in seen_ops:
            raise ValueError(f"op_id duplicado: {op.op_id!r}")
        seen_ops.add(op.op_id)
    seen_items = set()
    for entry in entries:
        if entry.item_id in seen_items:
            raise ValueError(f"item_id duplicado: {entry.item_id!r}")
        seen_items.add(entry.item_id)

    by_ref: dict[str, list[InternalOperation]] = {}
    for op in sorted(internals, key=lambda o: o.op_id):
        by_ref.setdefault(op.external_ref, []).append(op)

    matches: list[Match] = []
    exceptions: list[ReconException] = []
    used_ops: set[str] = set()
    matched_items: set[str] = set()

    # Pasada 1: solo pares que clasifican como Match; el mejor candidato
    # global gana, sin importar el orden lexicografico de los items.
    matchable: list[
        tuple[tuple[tuple[int, int, int, str], str], InternalOperation, ClearingEntry, Match]
    ] = []
    for entry in entries:
        for op in by_ref.get(entry.external_ref, []):
            outcome = _classify(op, entry, tol)
            if isinstance(outcome, Match):
                matchable.append(((_pair_key(op, entry, tol), entry.item_id), op, entry, outcome))
    matchable.sort(key=lambda scored: (scored[0][0], scored[0][1]))
    for _, op, entry, outcome in matchable:
        if op.op_id in used_ops or entry.item_id in matched_items:
            continue
        used_ops.add(op.op_id)
        matched_items.add(entry.item_id)
        matches.append(outcome)

    # Pasada 2: sobrantes; el mejor candidato libre clasifica excepcion.
    for entry in sorted(
        (e for e in entries if e.item_id not in matched_items),
        key=lambda e: (e.external_ref, e.item_id),
    ):
        candidates = [op for op in by_ref.get(entry.external_ref, []) if op.op_id not in used_ops]
        if not candidates:
            exceptions.append(ReconException(reason=MISSING_INTERNAL, clearing_id=entry.item_id))
            continue
        best = min(candidates, key=lambda op: _pair_key(op, entry, tol))
        used_ops.add(best.op_id)
        outcome = _classify(best, entry, tol)
        if isinstance(outcome, Match):  # pragma: no cover - red de seguridad
            matches.append(outcome)  # pragma: no cover - la pasada 1 ya consumo todo Match
            matched_items.add(entry.item_id)  # pragma: no cover
        else:
            exceptions.append(outcome)

    unused = sorted(
        (op for ops in by_ref.values() for op in ops if op.op_id not in used_ops),
        key=lambda o: (o.external_ref, o.op_id),
    )
    for op in unused:
        exceptions.append(ReconException(reason=MISSING_EXTERNAL, internal_id=op.op_id))

    matches.sort(key=lambda m: (m.clearing_id, m.internal_id))
    exceptions.sort(key=lambda e: (e.reason, e.clearing_id or "", e.internal_id or ""))
    return MatchResult(matches=tuple(matches), exceptions=tuple(exceptions))


def _pair_key(
    op: InternalOperation, entry: ClearingEntry, tol: Tolerances
) -> tuple[int, int, int, str]:
    """Prioridad de emparejo (determinista).

    Nivel: ``EXACT`` (0) > tolerado (1) > ``CURRENCY_MISMATCH`` (2) > fuera de
    tolerancia (3); a igual nivel, menor diferencia absoluta primero y
    ``op_id`` como desempate. La moneda distinta manda sobre un monto/fecha
    fuera de tolerancia, pero nunca sobre un ``Match``.
    """
    amount_diff = entry.amount_minor - op.amount_minor
    date_diff = (entry.value_date - op.value_date).days
    if entry.currency != op.currency:
        return (2, 0, 0, op.op_id)
    same_sign = (op.amount_minor > 0) == (entry.amount_minor > 0)
    amount_ok = same_sign and abs(amount_diff) <= tol.amount_tolerance_minor
    date_ok = abs(date_diff) <= tol.date_tolerance_days
    if amount_diff == 0 and date_diff == 0:
        return (0, 0, 0, op.op_id)
    if amount_ok and date_ok:
        return (1, abs(amount_diff), abs(date_diff), op.op_id)
    return (3, abs(amount_diff), abs(date_diff), op.op_id)


def _classify(
    op: InternalOperation, entry: ClearingEntry, tol: Tolerances
) -> Match | ReconException:
    """Clasifica un par con su motivo y detalle numerico.

    Politica de signo: un ``Match`` exige mismo signo; signos opuestos nunca
    son ``Match`` (``AMOUNT_DIFF``). Convencion: ``amount_diff_minor =
    clearing - internal`` (ej.: op ``100_00`` vs item ``99_00`` -> ``-100``)
    y ``date_diff_days = clearing_date - internal_date``.
    """
    if entry.currency != op.currency:
        return ReconException(
            reason=CURRENCY_MISMATCH,
            internal_id=op.op_id,
            clearing_id=entry.item_id,
        )
    amount_diff = entry.amount_minor - op.amount_minor
    date_diff = (entry.value_date - op.value_date).days
    if (op.amount_minor > 0) != (entry.amount_minor > 0):
        return ReconException(
            reason=AMOUNT_DIFF,
            internal_id=op.op_id,
            clearing_id=entry.item_id,
            amount_diff_minor=amount_diff,
            date_diff_days=date_diff if date_diff != 0 else None,
        )
    amount_ok = abs(amount_diff) <= tol.amount_tolerance_minor
    date_ok = abs(date_diff) <= tol.date_tolerance_days
    if amount_diff == 0 and date_diff == 0:
        return Match(internal_id=op.op_id, clearing_id=entry.item_id, qualifier=EXACT)
    if amount_ok and date_ok:
        qualifier = AMOUNT_DIFF if amount_diff != 0 else DATE_DIFF
        return Match(internal_id=op.op_id, clearing_id=entry.item_id, qualifier=qualifier)
    reason = AMOUNT_DIFF if not amount_ok else DATE_DIFF
    return ReconException(
        reason=reason,
        internal_id=op.op_id,
        clearing_id=entry.item_id,
        amount_diff_minor=amount_diff if amount_diff != 0 else None,
        date_diff_days=date_diff if date_diff != 0 else None,
    )
