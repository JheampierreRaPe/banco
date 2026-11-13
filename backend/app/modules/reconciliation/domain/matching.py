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
- El cruce maximiza la **cardinalidad por ``external_ref``** (matching
  bipartito maximo) sobre **todas** las aristas que clasifican como
  ``Match`` (exactas + toleradas) en una unica pasada, con Kuhn
  determinista (items por ``item_id`` y vecinos en orden fijo: exactos
  primero como desempate, luego ``op_id``). Garantizado: cardinalidad
  maxima (ningun otro emparejamiento con aristas ``Match`` tiene mas
  pares). No garantizado formalmente: preferencia de exactos; a igual
  cardinalidad los exactos van primero en el orden de vecinos, lo que es
  un desempate determinista (en la practica conserva los exactos cuando
  hay empate) pero no una prueba de maximalidad lexicografica: en un
  barrido exhaustivo del revisor (8192 instancias 2x2) hubo 204 casos con
  menos ``EXACT`` que otro maximo alternativo y 120 casos con un maximo
  alternativo 100% exacto. Los sobrantes se clasifican como excepcion.
  Asi dos tolerados cruzados (``it-a``/``op-2`` + ``it-b``/``op-1``) ganan
  sobre un unico par mejor puntuado (``it-a``/``op-1``) que dejaria un
  falso positivo, y un exacto que compite con tolerados no se pierde
  cuando existe un maximo que lo incluye (casos A/B del validador). Si el
  corte 2 quiere consistencia total de exactos puede adoptar min-cost
  max-flow (coste 0 exacto / 1 tolerado) manteniendo la cardinalidad
  maxima; el corte 1 no lo implementa (Kuhn + desempate alcanza).
- Determinismo: a igualdad de cardinalidad, el resultado es estable porque
  items y operaciones se ordenan por ID y los vecinos se exploran en orden
  fijo (exactos primero, luego ``op_id``; Kuhn en orden fijo); el resultado
  sale ordenado por IDs.
- Indice de candidatos: por cada ``external_ref`` las operaciones se ordenan
  por monto y cada item usa ``bisect`` sobre la ventana
  ``[monto - tol, monto + tol]`` (mas filtro de moneda/signo/fecha) para no
  generar todos los pares cuando la tolerancia es 0/estrecha (``O(n log n +
  k)`` en vez de ``O(n^2)``). Con tolerancia enorme la ventana cubre todo el
  grupo y rige la cota de seguridad.
- Cota de seguridad: ``MAX_CANDIDATE_PAIRS`` (default ``250_000`` pares
  candidatos evaluables por grupo de ``external_ref``, aprox ``500x500``)
  evita que un grupo patologico de refs repetidas agote memoria/tiempo:
  superarla lanza ``ValueError`` claro en vez de tardar minutos. La cota
  cuenta **solo pares que podrian ser ``Match``**: en la pasada de
  matching, pares en la ventana de monto que ademas pasan moneda/signo y
  quedan dentro de la tolerancia de fecha (equivalente a aristas ``Match``;
  un grupo 501x501 con todas las monedas distintas cuenta 0 y no aborta);
  en la pasada de excepciones, pares libres con misma moneda, mismo signo
  y fecha dentro de tolerancia (sin filtrar por distancia de monto, que es
  lo que el ``min()`` evalua; coherente con el matching y documentado
  aqui). El ``ValueError`` nunca incluye el valor de ``external_ref``
  (solo conteos + hash corto SHA-256 de 8 hex para traza) y el corte 2
  debe mapearlo a corrida ``FAILED`` (o degradacion por grupo); el default
  debe poder configurarse por entorno (hoy es una constante del dominio).
  Motivo: las refs deben ser casi unicas (decision corte 2: referencia
  unica por operacion); un grupo gigante con tolerancia amplia o con miles
  de sobrantes es un error de datos, no un cruce normal. Configurable via
  ``max_candidate_pairs`` de :func:`match_items`.
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
#: ``external_ref`` antes de fallar con ``ValueError``. Con ``250_000`` un
#: cruce normal (refs casi unicas, o 300x300 = 90_000 pares en el peor caso)
#: pasa holgado, pero un grupo patologico (p. ej. ``1000x1000`` con tolerancia
#: enorme = 1M pares) falla rapido en vez de agotar memoria/tiempo. Las refs
#: deben ser casi unicas (el corte 2 decidir entre referencia unica por
#: operacion o matching global); un grupo gigante con tolerancia amplia es un
#: error de datos.
MAX_CANDIDATE_PAIRS = 250_000


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
) -> MatchResult:
    """Cruza clearing contra operaciones internas (puro y determinista).

    Maximiza la cardinalidad del emparejamiento por ``external_ref`` en una
    unica pasada bipartita sobre **todas** las aristas ``Match`` (exactas +
    toleradas) con Kuhn determinista (items por ``item_id``, vecinos con
    exactos primero y luego ``op_id``). Garantiza cardinalidad maxima; la
    preferencia de exactos es solo desempate determinista, no garantia
    formal. Los sobrantes se clasifican como excepcion con el mejor
    candidato libre (``CURRENCY_MISMATCH``/fuera de tolerancia/
    ``MISSING_INTERNAL``). El orden de entrada no afecta el resultado: todo
    se ordena antes de emparejar y el resultado sale ordenado por IDs.

    La fase de matching usa un indice por monto (``bisect`` sobre la ventana
    de tolerancia) para no generar ``O(n^2)`` pares con tolerancia 0/estrecha;
    solo los pares que pasan moneda/signo/fecha (aristas ``Match``) cuentan
    para ``max_candidate_pairs``; si los superan se lanza ``ValueError`` (cota
    de seguridad para refs repetidas patologicas; el mensaje lleva conteos +
    hash corto de la ref, nunca su valor). La pasada de excepciones aplica la
    misma cota por grupo contando solo pares libres evaluables (misma moneda,
    mismo signo, fecha dentro de tolerancia) antes de buscar el mejor
    candidato, con el mismo estilo de ``ValueError`` sin la ref literal. El
    corte 2 debe mapear ese ``ValueError`` a corrida ``FAILED`` (o
    degradacion por grupo) y poder configurar el default por entorno.
    """
    tol = _coerce_tolerances(tolerances)
    if isinstance(max_candidate_pairs, bool) or not isinstance(max_candidate_pairs, int):
        raise TypeError("max_candidate_pairs debe ser un entero >= 0")
    if max_candidate_pairs < 0:
        raise ValueError("max_candidate_pairs debe ser >= 0")

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

    for ref in sorted(set(ops_by_ref) | set(items_by_ref)):
        group_ops = ops_by_ref.get(ref, [])
        group_items = items_by_ref.get(ref, [])
        if not group_ops or not group_items:
            continue
        # Pasada unica: cardinalidad maxima sobre todas las aristas Match.
        _match_group_max_cardinality(
            group_ops,
            group_items,
            tol,
            matches,
            used_ops,
            matched_items,
            max_candidate_pairs,
        )

    # Cota de la pasada de excepciones: cuenta solo pares libres evaluables
    # (misma moneda, mismo signo, fecha dentro de tolerancia; la distancia de
    # monto la evalua el min() sin filtrar). Evita el cartesiano por grupo
    # pero no aborta cuando ningun par podria ser Match (p. ej. monedas
    # distintas -> todo CURRENCY_MISMATCH). Sin la ref literal en el mensaje.
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
                    raise ValueError(
                        "grupo de referencia "
                        f"{_ref_hash(ref)} supera la cota de seguridad en excepciones "
                        f"({evaluable} pares evaluables > {max_candidate_pairs}): refs "
                        "repetidas patologicas; reduzca la tolerancia o exija "
                        "referencia unica por operacion"
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


def _match_group_max_cardinality(
    group_ops: list[InternalOperation],
    group_items: list[ClearingEntry],
    tol: Tolerances,
    matches: list[Match],
    used_ops: set[str],
    matched_items: set[str],
    max_candidate_pairs: int,
) -> None:
    """Maximiza pares ``Match`` (exactos + tolerados) con Kuhn determinista.

    Construye la adyacencia solo con pares que :func:`_classify` califica
    como ``Match``, usando la ventana de monto (``bisect``) + filtros de
    moneda/signo/fecha; los vecinos se ordenan con exactos primero y luego
    ``op_id`` (desempate determinista, no garantia formal de maximalidad
    lexicografica: ver cuantificacion 204/8192 y 120/8192 en el modulo; el
    corte 2 puede adoptar min-cost max-flow si quiere consistencia total).
    Solo los pares que pasan moneda/signo/fecha (aristas ``Match``) cuentan
    para ``max_candidate_pairs``: un grupo con todas las monedas distintas
    cuenta 0 y no aborta. Si se supera lanza ``ValueError`` sin la ref
    literal (conteos + hash corto). Despues corre aumentos de
    camino en orden fijo (items por ``item_id``), lo que garantiza
    cardinalidad maxima sobre el grafo de aristas ``Match``. El aumento es
    Kuhn DFS iterativo con pila explicita (misma semantica que el recursivo)
    para no agotar la pila de Python en cadenas largas de aumento.
    """
    ops_sorted = sorted(group_ops, key=lambda o: o.amount_minor)
    op_amounts = [op.amount_minor for op in ops_sorted]
    op_by_id = {op.op_id: op for op in ops_sorted}
    items_sorted = sorted(group_items, key=lambda e: e.item_id)

    adjacency: dict[str, list[str]] = {}
    candidate_count = 0
    for entry in items_sorted:
        low = entry.amount_minor - tol.amount_tolerance_minor
        high = entry.amount_minor + tol.amount_tolerance_minor
        left = bisect.bisect_left(op_amounts, low)
        right = bisect.bisect_right(op_amounts, high)
        neighbours: list[tuple[int, str]] = []
        for op in ops_sorted[left:right]:
            if op.op_id in used_ops:  # pragma: no cover - grupos frescos
                continue
            if op.currency != entry.currency:
                continue
            if (op.amount_minor > 0) != (entry.amount_minor > 0):
                continue
            outcome = _classify(op, entry, tol)
            if isinstance(outcome, Match):
                neighbours.append((0 if outcome.qualifier == EXACT else 1, op.op_id))
                candidate_count += 1
                if candidate_count > max_candidate_pairs:
                    raise ValueError(
                        "grupo de referencia "
                        f"{_ref_hash(entry.external_ref)} supera la cota de seguridad "
                        f"({candidate_count} pares evaluables > "
                        f"{max_candidate_pairs}): refs repetidas con tolerancia amplia; "
                        "reduzca la tolerancia o exija referencia unica por operacion"
                    )
        neighbours.sort()
        adjacency[entry.item_id] = [op_id for _, op_id in neighbours]

    entry_by_id = {e.item_id: e for e in items_sorted}
    match_for_op: dict[str, str] = {}

    def _augment(start_item_id: str, visited: set[str]) -> bool:
        # Kuhn DFS iterativo con pila explicita: misma semantica que el
        # recursivo (mismo orden de vecinos, mismo `visited` por raiz y
        # misma reasignacion al desenrollar), sin limite de recursion.
        # Cada marco guarda el item y el indice del proximo vecino;
        # `incoming_op[depth]` es la arista por la que se llego a ese item
        # (None en la raiz). Al hallar una op libre se reasigna toda la
        # cadena: la op libre al item actual y cada op padre a su item.
        stack_items: list[str] = [start_item_id]
        stack_next: list[int] = [0]
        stack_incoming: list[str | None] = [None]
        while stack_items:
            item_id = stack_items[-1]
            idx = stack_next[-1]
            neighbours = adjacency[item_id]
            if idx >= len(neighbours):
                stack_items.pop()
                stack_next.pop()
                stack_incoming.pop()
                continue
            stack_next[-1] = idx + 1
            op_id = neighbours[idx]
            if op_id in visited:
                continue
            visited.add(op_id)
            current = match_for_op.get(op_id)
            if current is None:
                match_for_op[op_id] = item_id
                for depth in range(len(stack_items) - 1, 0, -1):
                    parent_op = stack_incoming[depth]
                    assert parent_op is not None
                    match_for_op[parent_op] = stack_items[depth - 1]
                return True
            stack_items.append(current)
            stack_next.append(0)
            stack_incoming.append(op_id)
        return False

    for entry in items_sorted:
        _augment(entry.item_id, set())

    for op_id, item_id in sorted(match_for_op.items(), key=lambda kv: (kv[1], kv[0])):
        used_ops.add(op_id)
        matched_items.add(item_id)
        op = op_by_id[op_id]
        entry = entry_by_id[item_id]
        outcome = _classify(op, entry, tol)
        if isinstance(outcome, Match):
            matches.append(outcome)
        # pragma: no cover - la adyacencia solo contiene Match;
        # si _classify discrepara seria un bug de construccion.


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
