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
- El cruce maximiza la **cardinalidad por ``external_ref``** en un unico
  flujo de costo minimo por grupo: entre todos los emparejamientos de
  cardinalidad maxima sobre **todas** las aristas que clasifican como
  ``Match`` (exactas + toleradas) se elige el que **maximiza el numero de
  pares ``EXACT``** (coste 0 exacto / 1 tolerado). El motor es un
  min-cost max-flow determinista (SSAP por conjuntos de caminos minimos:
  Dijkstra con potenciales + flujo bloqueante sobre aristas de coste
  reducido 0; nodos en orden fijo por ID y desempates por ID): a igualdad
  de entrada siempre produce el mismo resultado. Garantizado: cardinalidad
  maxima Y maximo de ``EXACT`` entre los maximos (verificado contra fuerza
  bruta en ``backend/tests/test_e5_t18b_reconciliation_run.py``). Los
  sobrantes se clasifican como excepcion. Asi dos tolerados cruzados
  (``it-a``/``op-2`` + ``it-b``/``op-1``) ganan sobre un unico par mejor
  puntuado (``it-a``/``op-1``) que dejaria un falso positivo, y un exacto
  que compite con tolerados no se pierde cuando existe un maximo que lo
  incluye (casos A/B del validador); ademas, a igual cardinalidad, el
  emparejamiento con mas exactos gana siempre (p. ej. el caso del revisor:
  ops ``[100,100,300]`` vs items ``[100,400,300]`` con ``tol=100`` da
  **2 ``EXACT``** + ``AMOUNT_DIFF`` del item huerfano, no 1 exacto +
  1 tolerado).
- Determinismo: el resultado es estable porque items y operaciones se
  ordenan por ID, los nodos del flujo van en orden fijo (items por
  ``item_id``, ops por ``op_id``) y los desempates son por ID; el
  resultado sale ordenado por IDs.
- Indice de candidatos: por cada ``external_ref`` las operaciones se ordenan
  por monto y cada item usa ``bisect`` sobre la ventana
  ``[monto - tol, monto + tol]`` (mas filtro de moneda/signo/fecha) para no
  generar todos los pares cuando la tolerancia es 0/estrecha (``O(n log n +
  k)`` en vez de ``O(n^2)``). Con tolerancia enorme la ventana cubre todo el
  grupo y rige la cota de seguridad.
- Cota de seguridad: ``MAX_CANDIDATE_PAIRS`` (default ``250_000`` pares
  candidatos evaluables por grupo de ``external_ref``, aprox ``500x500``)
  evita que un grupo patologico de refs repetidas agote memoria/tiempo:
  superarla lanza ``CandidateBudgetExceeded`` (subclase de ``ValueError``)
  claro en vez de tardar minutos. La cota cuenta **solo pares que podrian
  ser ``Match``**: en la pasada de matching, pares en la ventana de monto
  que ademas pasan moneda/signo y quedan dentro de la tolerancia de fecha
  (equivalente a aristas ``Match``; un grupo 501x501 con todas las monedas
  distintas cuenta 0 y no aborta); en la pasada de excepciones, pares
  libres con misma moneda, mismo signo y fecha dentro de tolerancia (sin
  filtrar por distancia de monto, que es lo que el ``min()`` evalua;
  coherente con el matching y documentado aqui). El error nunca incluye el
  valor de ``external_ref`` (solo conteos + hash corto SHA-256 de 8 hex) y
  el servicio lo mapea a corrida ``FAILED`` (motivo
  ``CANDIDATE_BUDGET_EXCEEDED``); el default se configura por entorno via
  ``reconciliation.max_candidate_pairs`` (hoy es la constante del dominio).
  Motivo: las refs deben ser casi unicas (decision corte 2: referencia
  unica por operacion); un grupo gigante con tolerancia amplia o con miles
  de sobrantes es un error de datos, no un cruce normal. Configurable via
  ``max_candidate_pairs`` de :func:`match_items`.
- Presupuesto total de corrida: ``max_total_pairs`` de :func:`match_items`
  (``None`` = sin limite) acota la suma de pares evaluables de
  **todos** los grupos (pasada de matching + pasada de excepciones); al
  superarlo lanza ``CandidateBudgetExceeded`` sin ninguna ref (mensaje de
  corrida, solo conteos). El servicio lo configura por entorno via
  ``reconciliation.max_candidate_pairs_per_run``.
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
``amount_tolerance_minor`` y ``date_tolerance_days``; las claves **ausentes**
caen a ``0`` (no se exigen ambas); cualquier otra clave (desconocida) se
rechaza con ``TypeError`` (un typo no debe desactivar la tolerancia en
silencio). El corte 2 debe acotar la tolerancia leida de ``config.parameters``
(ver nota de parcialidad en ``docs/tasks/E5-T18.md``).
Nota de signo/fachada: el corte 2 debe verificar como la fachada de
``transactions`` representa los reversos (monto negativo vs monto positivo +
``type='REVERSAL'``) antes de adaptar los dicts a estos tipos (ver
``docs/tasks/E5-T18.md`` y ``docs/modules/README.md``).

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

import bisect
import hashlib
import heapq
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

#: Cota de seguridad: maximo de pares candidatos evaluables por grupo de
#: ``external_ref`` antes de fallar con ``CandidateBudgetExceeded``. Con
#: ``250_000`` un cruce normal (refs casi unicas, o 300x300 = 90_000 pares
#: en el peor caso) pasa holgado, pero un grupo patologico (p. ej.
#: ``1000x1000`` con tolerancia enorme = 1M pares) falla rapido en vez de
#: agotar memoria/tiempo. Las refs deben ser casi unicas (el corte 2
#: decidir entre referencia unica por operacion o matching global); un
#: grupo gigante con tolerancia amplia es un error de datos.
MAX_CANDIDATE_PAIRS = 250_000


class CandidateBudgetExceeded(ValueError):
    """Cota de pares evaluables superada (grupo o corrida).

    Subclase de ``ValueError`` (quien esperaba ``ValueError`` sigue
    capturandolo). El mensaje nunca incluye el valor de ``external_ref``
    (solo conteos + hash corto en la cota por grupo; solo conteos en la
    cota por corrida): el servicio la mapea a corrida ``FAILED`` con
    motivo ``CANDIDATE_BUDGET_EXCEEDED``, sin parciales.
    """


def _ref_hash(ref: str) -> str:
    """Hash corto (SHA-256, 8 hex) de un ``external_ref`` para trazar sin exponerlo.

    Los ``ValueError`` de la cota nunca incluyen el valor de la referencia
    (podria ser sensible si se propaga a logs); solo conteos + este hash.
    """

    return hashlib.sha256(ref.encode("utf-8")).hexdigest()[:8]


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
    - ``AMOUNT_DIFF`` exige ``amount_diff_minor`` no nulo y **distinto de
      cero**; ``DATE_DIFF`` exige ``date_diff_days`` no nulo y **distinto de
      cero** (un detalle cero significaria que esa dimension coincide y el
      motivo seria otro).
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
            if self.amount_diff_minor == 0:
                raise ValueError("AMOUNT_DIFF exige amount_diff_minor distinto de cero")
        elif self.reason == DATE_DIFF:
            if self.date_diff_days is None:
                raise ValueError("DATE_DIFF exige date_diff_days no nulo")
            if self.date_diff_days == 0:
                raise ValueError("DATE_DIFF exige date_diff_days distinto de cero")
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
    """Resultado determinista del cruce (orden estable por IDs).

    Acepta tuplas o listas en el constructor (las listas se convierten a
    tuplas) pero siempre expone tuplas inmutables en lectura.
    """

    matches: tuple[Match, ...] = field(default_factory=tuple)
    exceptions: tuple[ReconException, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "matches", tuple(self.matches))
        object.__setattr__(self, "exceptions", tuple(self.exceptions))
        for match in self.matches:
            if not isinstance(match, Match):
                raise TypeError("matches debe contener Match")
        for exc in self.exceptions:
            if not isinstance(exc, ReconException):
                raise TypeError("exceptions debe contener ReconException")

    @property
    def matched_count(self) -> int:
        return len(self.matches)

    @property
    def exception_count(self) -> int:
        return len(self.exceptions)


def _coerce_tolerances(tolerances: Tolerances | Mapping[str, object] | None) -> Tolerances:
    """Normaliza tolerancias; claves aceptadas: ``TOLERANCE_KEYS``.

    Las claves **ausentes** caen a ``0`` (no se exigen ambas: ``{}`` equivale
    a tolerancia cero). Cualquier clave desconocida se rechaza con
    ``TypeError`` para que un typo no desactive la tolerancia en silencio.
    Los valores se validan como enteros ``>= 0`` (``TypeError``/``ValueError``).
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
    *,
    max_candidate_pairs: int = MAX_CANDIDATE_PAIRS,
    max_total_pairs: int | None = None,
) -> MatchResult:
    """Cruza clearing contra operaciones internas (puro y determinista).

    Maximiza la cardinalidad del emparejamiento por ``external_ref`` con un
    flujo de costo minimo (coste 0 exacto / 1 tolerado): a igual
    cardinalidad gana el emparejamiento con mas pares ``EXACT``. Los
    sobrantes se clasifican como excepcion con el mejor candidato libre
    (``CURRENCY_MISMATCH``/fuera de tolerancia/``MISSING_INTERNAL``). El
    orden de entrada no afecta el resultado: todo se ordena antes de
    emparejar y el resultado sale ordenado por IDs.

    La fase de matching usa un indice por monto (``bisect`` sobre la ventana
    de tolerancia) para no generar ``O(n^2)`` pares con tolerancia 0/estrecha;
    solo los pares que pasan moneda/signo/fecha (aristas ``Match``) cuentan
    para ``max_candidate_pairs``; si los superan se lanza
    ``CandidateBudgetExceeded`` (cota de seguridad para refs repetidas
    patologicas; el mensaje lleva conteos + hash corto de la ref, nunca su
    valor). ``max_total_pairs`` (``None`` = sin limite) acota ademas la suma
    de pares evaluables de todos los grupos (matching + excepciones); al
    superarlo se lanza ``CandidateBudgetExceeded`` sin ninguna ref. El
    servicio mapea ese error a corrida ``FAILED`` (motivo
    ``CANDIDATE_BUDGET_EXCEEDED``) y configura ambos defaults por entorno
    (``reconciliation.max_candidate_pairs`` /
    ``reconciliation.max_candidate_pairs_per_run``).
    """
    tol = _coerce_tolerances(tolerances)
    if isinstance(max_candidate_pairs, bool) or not isinstance(max_candidate_pairs, int):
        raise TypeError("max_candidate_pairs debe ser un entero >= 0")
    if max_candidate_pairs < 0:
        raise ValueError("max_candidate_pairs debe ser >= 0")
    if max_total_pairs is not None:
        if isinstance(max_total_pairs, bool) or not isinstance(max_total_pairs, int):
            raise TypeError("max_total_pairs debe ser un entero >= 0 o None")
        if max_total_pairs < 0:
            raise ValueError("max_total_pairs debe ser >= 0 o None")

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

    ops_by_ref: dict[str, list[InternalOperation]] = {}
    for op in sorted(internals, key=lambda o: o.op_id):
        ops_by_ref.setdefault(op.external_ref, []).append(op)
    items_by_ref: dict[str, list[ClearingEntry]] = {}
    for entry in sorted(entries, key=lambda e: e.item_id):
        items_by_ref.setdefault(entry.external_ref, []).append(entry)

    matches: list[Match] = []
    used_ops: set[str] = set()
    matched_items: set[str] = set()
    # Presupuesto total de la corrida: pares evaluables acumulados de todos
    # los grupos (matching + excepciones). ``None`` = sin limite.
    total_state: list[int] | None = [0, max_total_pairs] if max_total_pairs is not None else None

    for ref in sorted(set(ops_by_ref) | set(items_by_ref)):
        group_ops = ops_by_ref.get(ref, [])
        group_items = items_by_ref.get(ref, [])
        if not group_ops or not group_items:
            continue
        # Flujo de costo minimo: cardinalidad maxima + maximo de EXACT.
        _match_group_min_cost_flow(
            group_ops,
            group_items,
            tol,
            matches,
            used_ops,
            matched_items,
            max_candidate_pairs,
            total_state,
        )

    # Cota de la pasada de excepciones: cuenta solo pares libres evaluables
    # (misma moneda, mismo signo, fecha dentro de tolerancia; la distancia de
    # monto la evalua el min() sin filtrar). Evita el cartesiano por grupo
    # pero no aborta cuando ningun par podria ser Match (p. ej. monedas
    # distintas -> todo CURRENCY_MISMATCH). Sin la ref literal en el mensaje.
    # Ademas carga el presupuesto total de la corrida.
    for ref in sorted(set(ops_by_ref) | set(items_by_ref)):
        free_ops = [op for op in ops_by_ref.get(ref, []) if op.op_id not in used_ops]
        free_items = [e for e in items_by_ref.get(ref, []) if e.item_id not in matched_items]
        if not free_ops or not free_items:
            continue
        evaluable = 0
        for op in free_ops:
            for e in free_items:
                if op.currency != e.currency:
                    continue
                if (op.amount_minor > 0) != (e.amount_minor > 0):
                    continue
                if abs((e.value_date - op.value_date).days) > tol.date_tolerance_days:
                    continue
                evaluable += 1
                if evaluable > max_candidate_pairs:
                    raise CandidateBudgetExceeded(
                        "grupo de referencia "
                        f"{_ref_hash(ref)} supera la cota de seguridad en excepciones "
                        f"({evaluable} pares evaluables > {max_candidate_pairs}): refs "
                        "repetidas patologicas; reduzca la tolerancia o exija "
                        "referencia unica por operacion"
                    )
                if total_state is not None:
                    total_state[0] += 1
                    if total_state[0] > total_state[1]:
                        raise CandidateBudgetExceeded(
                            "corrida supera la cota de seguridad total "
                            f"(presupuesto total {total_state[0]} pares evaluables > "
                            f"{total_state[1]}): tolerancias amplias o referencias "
                            "repetidas; reduzca la tolerancia o exija referencia "
                            "unica por operacion"
                        )

    # Pasada final (excepciones): sobrantes; el mejor candidato libre
    # clasifica la excepcion con cercania (moneda > fuera de tolerancia).
    exceptions: list[ReconException] = []
    for entry in sorted(
        (e for e in entries if e.item_id not in matched_items),
        key=lambda e: (e.external_ref, e.item_id),
    ):
        candidates = [
            op for op in ops_by_ref.get(entry.external_ref, []) if op.op_id not in used_ops
        ]
        if not candidates:
            exceptions.append(ReconException(reason=MISSING_INTERNAL, clearing_id=entry.item_id))
            continue
        best = min(candidates, key=lambda op: _pair_key(op, entry, tol))
        used_ops.add(best.op_id)
        outcome = _classify(best, entry, tol)
        if isinstance(outcome, Match):  # pragma: no cover - red de seguridad
            matches.append(outcome)  # pragma: no cover - las fases ya consumieron todo Match
            matched_items.add(entry.item_id)  # pragma: no cover
        else:
            exceptions.append(outcome)

    unused = sorted(
        (op for ops in ops_by_ref.values() for op in ops if op.op_id not in used_ops),
        key=lambda o: (o.external_ref, o.op_id),
    )
    for op in unused:
        exceptions.append(ReconException(reason=MISSING_EXTERNAL, internal_id=op.op_id))

    matches.sort(key=lambda m: (m.clearing_id, m.internal_id))
    exceptions.sort(key=lambda e: (e.reason, e.clearing_id or "", e.internal_id or ""))
    return MatchResult(matches=tuple(matches), exceptions=tuple(exceptions))


def _match_group_min_cost_flow(
    group_ops: list[InternalOperation],
    group_items: list[ClearingEntry],
    tol: Tolerances,
    matches: list[Match],
    used_ops: set[str],
    matched_items: set[str],
    max_candidate_pairs: int,
    total_state: list[int] | None,
) -> None:
    """Empareja un grupo con flujo de costo minimo (exacto 0 / tolerado 1).

    Construye la adyacencia solo con pares que :func:`_classify` califica
    como ``Match``, usando la ventana de monto (``bisect``) + filtros de
    moneda/signo/fecha; los vecinos se ordenan con exactos primero y luego
    ``op_id`` (orden de insercion determinista). Solo los pares que pasan
    moneda/signo/fecha (aristas ``Match``) cuentan para
    ``max_candidate_pairs``: un grupo con todas las monedas distintas
    cuenta 0 y no aborta. Si se supera lanza ``CandidateBudgetExceeded``
    sin la ref literal (conteos + hash corto). ``total_state`` (``[usados,
    limite]`` o ``None``) acumula el presupuesto total de la corrida y
    tambien lanza ``CandidateBudgetExceeded`` (sin ninguna ref) al
    superarlo.

    El cruce es un min-cost max-flow (:func:`_min_cost_flow_matches`):
    garantiza cardinalidad maxima sobre el grafo de aristas ``Match`` y,
    entre los maximos, el maximo numero de pares ``EXACT``. Determinista:
    nodos en orden fijo por ID y desempates por ID.
    """
    ops_sorted = sorted(group_ops, key=lambda o: o.amount_minor)
    op_amounts = [op.amount_minor for op in ops_sorted]
    op_by_id = {op.op_id: op for op in ops_sorted}
    items_sorted = sorted(group_items, key=lambda e: e.item_id)

    neighbours: dict[str, list[tuple[int, str]]] = {}
    candidate_count = 0
    for entry in items_sorted:
        low = entry.amount_minor - tol.amount_tolerance_minor
        high = entry.amount_minor + tol.amount_tolerance_minor
        left = bisect.bisect_left(op_amounts, low)
        right = bisect.bisect_right(op_amounts, high)
        item_neighbours: list[tuple[int, str]] = []
        for op in ops_sorted[left:right]:
            if op.op_id in used_ops:  # pragma: no cover - grupos frescos
                continue
            if op.currency != entry.currency:
                continue
            if (op.amount_minor > 0) != (entry.amount_minor > 0):
                continue
            outcome = _classify(op, entry, tol)
            if isinstance(outcome, Match):
                item_neighbours.append((0 if outcome.qualifier == EXACT else 1, op.op_id))
                candidate_count += 1
                if candidate_count > max_candidate_pairs:
                    raise CandidateBudgetExceeded(
                        "grupo de referencia "
                        f"{_ref_hash(entry.external_ref)} supera la cota de seguridad "
                        f"({candidate_count} pares evaluables > "
                        f"{max_candidate_pairs}): refs repetidas con tolerancia amplia; "
                        "reduzca la tolerancia o exija referencia unica por operacion"
                    )
                if total_state is not None:
                    total_state[0] += 1
                    if total_state[0] > total_state[1]:
                        raise CandidateBudgetExceeded(
                            "corrida supera la cota de seguridad total "
                            f"(presupuesto total {total_state[0]} pares evaluables > "
                            f"{total_state[1]}): tolerancias amplias o referencias "
                            "repetidas; reduzca la tolerancia o exija referencia "
                            "unica por operacion"
                        )
        item_neighbours.sort()
        neighbours[entry.item_id] = item_neighbours

    match_for_op = _min_cost_flow_matches(
        [e.item_id for e in items_sorted],
        sorted(op_by_id),
        neighbours,
    )

    entry_by_id = {e.item_id: e for e in items_sorted}
    for op_id, item_id in sorted(match_for_op.items(), key=lambda kv: (kv[1], kv[0])):
        used_ops.add(op_id)
        matched_items.add(item_id)
        op = op_by_id[op_id]
        entry = entry_by_id[item_id]
        outcome = _classify(op, entry, tol)
        if isinstance(outcome, Match):
            matches.append(outcome)
        # pragma: no cover - la adyacencia solo contiene Match;
        # si _classify discrepa seria un bug de construccion.


def _min_cost_flow_matches(
    item_ids: list[str],
    op_ids: list[str],
    neighbours: dict[str, list[tuple[int, str]]],
) -> dict[str, str]:
    """Flujo de costo minimo item -> op (cardinalidad maxima + max EXACT).

    Red: fuente -> items (cap 1, coste 0), item -> op (cap 1, coste 0
    exacto / 1 tolerado), op -> sumidero (cap 1, coste 0). Cada fase
    calcula el camino minimo vigente (Dijkstra con potenciales), actualiza
    potenciales y empuja un flujo bloqueante por aristas de coste reducido
    0 (cada camino empujado es minimo vigente: solo se saturan aristas de
    coste 0 y sus inversas tambien cuestan 0, asi que los potenciales
    siguen validos). Al no quedar camino s-t el flujo es maximo y, por
    aumentar siempre por caminos minimos, de coste minimo: cardinalidad
    maxima con maximo numero de ``EXACT``. Nodos en orden fijo por ID y
    desempates por ID: determinista (misma entrada -> mismo resultado sin
    importar el orden de entrada).
    """
    item_pos = {iid: 1 + k for k, iid in enumerate(item_ids)}
    op_pos = {oid: 1 + len(item_ids) + k for k, oid in enumerate(op_ids)}
    op_id_by_pos = {pos: oid for oid, pos in op_pos.items()}
    node_count = 1 + len(item_ids) + len(op_ids) + 1
    src = 0
    dst = node_count - 1
    # Arista mutable [destino, indice_inversa, capacidad, coste].
    graph: list[list[list]] = [[] for _ in range(node_count)]

    def _add_edge(fr: int, to: int, cap: int, cost: int) -> None:
        graph[fr].append([to, len(graph[to]), cap, cost])
        graph[to].append([fr, len(graph[fr]) - 1, 0, -cost])

    for iid in item_ids:
        _add_edge(src, item_pos[iid], 1, 0)
    for oid in op_ids:
        _add_edge(op_pos[oid], dst, 1, 0)
    for iid in item_ids:
        u = item_pos[iid]
        for cost, oid in neighbours.get(iid, ()):
            _add_edge(u, op_pos[oid], 1, cost)

    INF = 10**18
    potential = [0] * node_count
    while True:
        dist = [INF] * node_count
        dist[src] = 0
        heap = [(0, src)]
        while heap:
            d, u = heapq.heappop(heap)
            if d != dist[u]:
                continue
            if u == dst:
                break
            for e in graph[u]:
                if e[2] <= 0:
                    continue
                nd = d + e[3] + potential[u] - potential[e[0]]
                if nd < dist[e[0]]:
                    dist[e[0]] = nd
                    heapq.heappush(heap, (nd, e[0]))
        if dist[dst] == INF:
            break
        for v in range(node_count):
            if dist[v] < INF:
                potential[v] += dist[v]
        # Flujo bloqueante sobre aristas de coste reducido 0.
        edge_ptr = [0] * node_count
        seen = [0] * node_count
        stamp = 0
        while _push_blocking_unit(graph, edge_ptr, seen, src, dst, potential, stamp + 1):
            stamp += 1

    matched: dict[str, str] = {}
    for iid in item_ids:
        u = item_pos[iid]
        for e in graph[u]:
            # En la adyacencia de un item, las aristas hacia nodos-op son
            # exactamente las directas item -> op (cap inicial 1): cap 0 =
            # flujo 1 (emparejado). Las inversas item -> fuente no apuntan
            # a nodos-op.
            if e[0] in op_id_by_pos and e[2] == 0:
                matched[op_id_by_pos[e[0]]] = iid
    return matched


def _push_blocking_unit(
    graph: list[list[list]],
    edge_ptr: list[int],
    seen: list[int],
    src: int,
    dst: int,
    potential: list[int],
    stamp: int,
) -> bool:
    """Empuja 1 unidad s-t por aristas de coste reducido 0 (DFS iterativo).

    ``edge_ptr`` (arco actual por nodo) persiste entre llamadas de la misma
    fase: cada arista se recorre una vez en amortizado. ``seen`` con ``stamp``
    evita ciclos sin realocar por busqueda. Retorna ``True`` si empujo.
    """
    path_nodes = [src]
    path_edges: list[tuple[int, int]] = []  # (nodo_origen, indice_arista)
    seen[src] = stamp
    while path_nodes:
        u = path_nodes[-1]
        if u == dst:
            for fr, ei in path_edges:
                e = graph[fr][ei]
                e[2] -= 1
                graph[e[0]][e[1]][2] += 1
            return True
        advanced = False
        while edge_ptr[u] < len(graph[u]):
            ei = edge_ptr[u]
            e = graph[u][ei]
            v = e[0]
            if e[2] > 0 and seen[v] != stamp and e[3] + potential[u] - potential[v] == 0:
                edge_ptr[u] = ei + 1
                path_nodes.append(v)
                path_edges.append((u, ei))
                seen[v] = stamp
                advanced = True
                break
            edge_ptr[u] = ei + 1
        if not advanced:
            path_nodes.pop()
            if path_edges:
                path_edges.pop()
    return False


def _pair_key(
    op: InternalOperation, entry: ClearingEntry, tol: Tolerances
) -> tuple[int, int, int, str]:
    """Prioridad de emparejo (determinista).

    Nivel: ``EXACT`` (0) > tolerado (1) > ``CURRENCY_MISMATCH`` (2) > fuera de
    tolerancia (3); a igual nivel, menor diferencia absoluta primero
    (monto y luego fecha, tambien en ``CURRENCY_MISMATCH`` para que gane la
    operacion mas cercana y no el ``op_id`` alfabetico) y ``op_id`` como
    desempate final. La moneda distinta manda sobre un monto/fecha fuera de
    tolerancia, pero nunca sobre un ``Match``.
    """
    amount_diff = entry.amount_minor - op.amount_minor
    date_diff = (entry.value_date - op.value_date).days
    if entry.currency != op.currency:
        return (2, abs(amount_diff), abs(date_diff), op.op_id)
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
