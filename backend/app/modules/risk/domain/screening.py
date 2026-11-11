"""Dominio puro de cotejo contra listas restrictivas (E6-T07, `risk`, HU21).

Funciones sin E/S ni dependencias: normalizacion de texto y similitud
determinista (misma entrada -> mismo `match_score`). El adaptador
(`app/adapters/sanctions_lists.py`) las consume; aqui no hay red, BD ni
secretos.
"""

from __future__ import annotations

import re
import unicodedata

#: Umbral por defecto de `risk.screening_threshold`: `matched=true` solo si
#: `match_score >= threshold`. Espejo documentado en `docs/02#9`; el valor
#: efectivo vive en `SanctionsListsSettings.screening_threshold`
#: (`SANCTIONS_SCREENING_THRESHOLD`) y, cuando exista el caso de uso
#: (`E6-T09`), podra leerse de `config.parameters`.
DEFAULT_SCREENING_THRESHOLD = 0.85

#: Escala del prefijo comun en Jaro-Winkler (estandar `0.1`, tope 4 chars).
_JW_PREFIX_SCALE = 0.1
_JW_PREFIX_MAX = 4

_PUNCT_RE = re.compile(r"[^A-Z0-9 ]+")


def normalize_text(value: object) -> str:
    """Normaliza un texto para comparacion difusa.

    Mayusculas, sin acentos/diacriticos, sin puntuacion, espacios simples
    colapsados y sin bordes. Puro y determinista; `None`/no-texto -> `""`.
    """
    if not isinstance(value, str):
        return ""
    ascii_text = unicodedata.normalize("NFKD", value)
    ascii_text = ascii_text.encode("ascii", "ignore").decode("ascii")
    upper = ascii_text.upper()
    no_punct = _PUNCT_RE.sub(" ", upper)
    return " ".join(no_punct.split())


def _jaro(first: str, second: str) -> float:
    """Similitud de Jaro en `[0, 1]` (pura, determinista, sin dependencias)."""
    if first == second:
        return 1.0
    len_a, len_b = len(first), len(second)
    if len_a == 0 or len_b == 0:
        return 0.0
    window = max(len_a, len_b) // 2 - 1
    window = max(window, 0)
    matched_a = [False] * len_a
    matched_b = [False] * len_b
    matches = 0
    for i, char_a in enumerate(first):
        start = max(0, i - window)
        end = min(len_b, i + window + 1)
        for j in range(start, end):
            if not matched_b[j] and second[j] == char_a:
                matched_a[i] = True
                matched_b[j] = True
                matches += 1
                break
    if matches == 0:
        return 0.0
    transpositions = 0
    cursor = 0
    for i in range(len_a):
        if not matched_a[i]:
            continue
        while not matched_b[cursor]:
            cursor += 1
        if first[i] != second[cursor]:
            transpositions += 1
        cursor += 1
    transpositions //= 2
    return (matches / len_a + matches / len_b + (matches - transpositions) / matches) / 3.0


def jaro_winkler(first: str, second: str) -> float:
    """Jaro-Winkler en `[0, 1]`: Jaro + bono por prefijo comun (tope 4).

    Puro y determinista; entradas ya normalizadas (pero tolera crudos:
    normaliza de nuevo por seguridad).
    """
    norm_a = normalize_text(first)
    norm_b = normalize_text(second)
    if norm_a == norm_b:
        # Incluye el caso ambos vacios: identicos -> `1.0` (el llamante
        # decide si el vacio es comparable; `name_similarity` lo trata).
        return 1.0
    if not norm_a or not norm_b:
        return 0.0
    jaro = _jaro(norm_a, norm_b)
    prefix = 0
    for char_a, char_b in zip(norm_a, norm_b):
        if char_a != char_b or prefix >= _JW_PREFIX_MAX:
            break
        prefix += 1
    return min(1.0, jaro + prefix * _JW_PREFIX_SCALE * (1.0 - jaro))


def name_similarity(first: str, second: str) -> float:
    """Similitud entre dos nombres en `[0, 1]`, tolerante al orden.

    Toma el maximo entre comparar las cadenas normalizadas completas y
    compararlas con tokens ordenados (`"QUISPE AURELIO"` == `"AURELIO
    QUISPE"` -> `1.0`). Determinista; vacio contra no-vacio -> `0.0`.
    """
    norm_a = normalize_text(first)
    norm_b = normalize_text(second)
    if not norm_a or not norm_b:
        return 0.0
    direct = jaro_winkler(norm_a, norm_b)
    sorted_a = " ".join(sorted(norm_a.split()))
    sorted_b = " ".join(sorted(norm_b.split()))
    if sorted_a == sorted_b:
        return 1.0
    return max(direct, jaro_winkler(sorted_a, sorted_b))


def is_match(score: object, threshold: float = DEFAULT_SCREENING_THRESHOLD) -> bool:
    """`True` si `score >= threshold` (el limite cuenta como coincidencia)."""
    try:
        candidate = float(score)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return candidate >= threshold


__all__ = [
    "DEFAULT_SCREENING_THRESHOLD",
    "is_match",
    "jaro_winkler",
    "name_similarity",
    "normalize_text",
]
