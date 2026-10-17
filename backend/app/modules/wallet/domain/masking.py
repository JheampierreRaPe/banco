"""Enmascarado del nombre del dueno del QR (E4-T24, HU14/HU13).

`payee_name_masked` es la **unica** representacion del nombre del dueno
que viaja en las respuestas (`GET /wallet/qr/{id}` y
`POST /payments/qr/preview`); el nombre completo jamas viaja ni se
loguea (reglas de oro 7 y 9). Todo el modulo es puro y determinista
(testeable sin BD); la resolucion por fachada es best-effort con
fallback `None` (ausencia tolerada, CA-04).

Formato exacto (decision del dueno, documentada en `docs/05#6.5`):

- Entradas: `nombre + un apellido` (se usa el **primer** apellido
  disponible), o `business_name` (razon social) como una sola parte.
- Cada parte: primer token (antes del primer espacio), `strip`,
  mayusculas; se muestran los **primeros 4** caracteres y el resto se
  oculta con el sufijo `•••` (tres bullets U+2022); una parte de
  longitud `<= 4` se muestra **completa** (nada que ocultar).
- Partes separadas por **un espacio** (`" "`).
- Ejemplo: `Diego Gonzales` -> `DIEG••• GONZ•••`; `Ana` -> `ANA`;
  razon social `EMPRESA SAC` -> `EMPR•••` (solo el primer token).

Frontera (regla de oro 4): `wallet` no importa modelos de `identity`;
el nombre se resuelve por la fachada de solo lectura
`identity.service.profile.get_profile` con **import perezoso** (patron
D-03, como `credits.service.contract_signing` ->
`identity.service.holder_contact`). Sin PII en logs: solo decisiones
(`resuelto`/`no resoluble`), jamas valores.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Caracteres visibles por parte (decision del dueno E4-T24).
VISIBLE_CHARS = 4

#: Sufijo que oculta el resto de cada parte (tres bullets U+2022).
MASK_SUFFIX = "•••"

#: Separador entre partes (un espacio).
PART_SEPARATOR = " "


def _first_token(value: object) -> str:
    """Primer token de un valor (`strip` + colapsa espacios internos)."""
    text = str(value or "").strip()
    if not text:
        return ""
    return " ".join(text.split()).split(" ")[0]


def mask_part(part: object) -> str:
    """Enmascara una parte: `upper`, primeros 4 + `•••` (`<= 4` completa)."""
    token = _first_token(part).upper()
    if not token:
        return ""
    if len(token) <= VISIBLE_CHARS:
        return token
    return token[:VISIBLE_CHARS] + MASK_SUFFIX


def mask_payee_name(
    first_name: object = None,
    last_name: object = None,
    business_name: object = None,
) -> str | None:
    """Nombre del dueno enmascarado (`nombre + un apellido` o razon social).

    - Con `first_name`: partes = [nombre, primer apellido disponible];
      el apellido se toma de `last_name` (su primer token).
    - Sin `first_name` pero con `business_name` (RUC juridica): una
      sola parte con el primer token de la razon social.
    - Sin dato resoluble -> `None` (fallback neutro, CA-04).
    """
    given = _first_token(first_name)
    if given:
        parts = [mask_part(given)]
        surname = _first_token(last_name)
        if surname:
            parts.append(mask_part(surname))
        return PART_SEPARATOR.join(p for p in parts if p) or None
    business = _first_token(business_name)
    if business:
        return mask_part(business) or None
    return None


def resolve_payee_name_masked(
    session: Any,
    user_id: uuid.UUID | str | None,
    *,
    profile_resolver: Callable[..., Any] | None = None,
) -> str | None:
    """Resuelve `payee_name_masked` por la fachada `identity` (best-effort).

    Usa `identity.service.profile.get_profile` (import perezoso, D-03)
    salvo que se inyecte `profile_resolver` (tests). Cualquier fallo
    (usuario sin fila, fachada caida, perfil vacio) -> `None` sin
    romper la respuesta; sin PII en logs (solo la decision).
    """
    try:
        resolver = profile_resolver
        if resolver is None:  # pragma: no cover - se inyecta en pruebas
            from app.modules.identity.service import profile as profile_service

            resolver = profile_service.get_profile
        profile = resolver(session, user_id)
    except Exception:  # noqa: BLE001 - lectura best-effort: sin nombre hay fallback
        logger.debug("payee name no resoluble")
        return None
    if not isinstance(profile, dict):
        logger.debug("payee name no resoluble")
        return None
    try:
        masked = mask_payee_name(
            profile.get("first_name"),
            profile.get("last_name"),
            profile.get("business_name"),
        )
    except Exception:  # noqa: BLE001 - lectura best-effort: perfil inesperado
        logger.debug("payee name no resoluble")
        return None
    if masked is None:
        logger.debug("payee name no resoluble")
    return masked


__all__ = [
    "MASK_SUFFIX",
    "PART_SEPARATOR",
    "VISIBLE_CHARS",
    "mask_part",
    "mask_payee_name",
    "resolve_payee_name_masked",
]
