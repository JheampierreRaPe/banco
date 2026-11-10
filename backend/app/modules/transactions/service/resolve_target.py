"""Lookup previo de destino por DNI/celular (E2-T27, HU06).

Paso de solo lectura anterior a la transferencia: dado el DNI o el celular
del destinatario, resuelve su cuenta `ACTIVE` y devuelve la referencia
(`target_account_id`) + el titular enmascarado, sin que el cliente conozca
el numero de cuenta. `POST /transfers/third-party` NO se extiende: consume
esa referencia y revalida propiedad/estado en el motor (TOCTOU: si la cuenta
cambia entre lookup y transferencia, gana la validacion del motor).

Frontera (regla de oro 4, D-01): `identity` y `accounts` solo por sus
fachadas de `service/` (imports perezosos); jamas se importan sus modelos
ni repositorios. Sin PII en logs (reglas 7/9): ni documento, ni celular, ni
numero de cuenta; solo el desenlace (`found`/`ambiguous`, conteos).

Reglas:
- Exactamente UNO de `target_document` / `target_phone` (ambos o ninguno
  -> `ResolveTargetValidationError` => 422).
- `0` cuentas `ACTIVE` -> `TargetNotFoundError` (neutro, mismo cuerpo que
  un documento/celular inexistente: anti-enumeracion).
- `1` cuenta `ACTIVE` -> `200` con la referencia + mascaras.
- `>= 2` cuentas `ACTIVE` -> `TargetAmbiguousError` (no se elige una).
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Tipos aceptados en `target_document` (contrato del brief E2-T27).
RESOLVE_DOC_TYPES: tuple[str, ...] = ("DNI", "CE", "PASSPORT")

#: Longitud exacta del DNI (solo digitos); CE/PASSPORT aceptan 6-12 alfanumericos.
DNI_LENGTH = 8
_FOREIGN_DOC_RE = re.compile(r"[0-9A-Za-z]{6,12}")

#: Celular normalizado (solo digitos): rango aceptado.
PHONE_MIN_DIGITS = 7
PHONE_MAX_DIGITS = 15

#: Mensaje neutro (identico exista o no el titular / tenga o no cuenta ACTIVE).
TARGET_NOT_FOUND_MESSAGE = "destino no encontrado"
#: Mensaje de ambiguedad (sin revelar cuantas ni cuales cuentas hay).
TARGET_AMBIGUOUS_MESSAGE = "el destino tiene varias cuentas activas; use el numero de cuenta"


class ResolveTargetError(ValueError):
    """Error base del lookup previo (el router lo traduce a `AppError`)."""


class ResolveTargetValidationError(ResolveTargetError):
    """Entrada invalida (formato o combinacion) => 422 VALIDATION_ERROR."""


class TargetNotFoundError(ResolveTargetError):
    """Destino no resoluble (neutro) => 404 TARGET_NOT_FOUND."""


class TargetAmbiguousError(ResolveTargetError):
    """Titular con varias cuentas ACTIVE => 409 TARGET_AMBIGUOUS."""


def normalize_phone(value: object) -> str:
    """Celular a solo digitos (puro; `+51 987-654-321` -> `"51987654321"`)."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\D+", "", value)


def validate_document(doc_type: object, number: object) -> tuple[str, str]:
    """Valida `target_document` y retorna `(tipo, digitos)` normalizados.

    `DNI` = 8 digitos exactos; `CE`/`PASSPORT` = 6-12 alfanumericos (se
    normalizan a mayusculas sin separadores para el hash). Otro tipo o
    formato -> `ResolveTargetValidationError` (422, sin oraculo).
    """
    kind = doc_type.strip().upper() if isinstance(doc_type, str) else ""
    if kind not in RESOLVE_DOC_TYPES:
        raise ResolveTargetValidationError(
            f"target_document.type debe ser uno de {list(RESOLVE_DOC_TYPES)}"
        )
    raw = number.strip() if isinstance(number, str) else ""
    if kind == "DNI":
        if re.fullmatch(r"[0-9]{8}", raw) is None:
            raise ResolveTargetValidationError("target_document.number: DNI debe tener 8 digitos")
        return kind, raw
    cleaned = re.sub(r"[^0-9A-Za-z]", "", raw).upper()
    if _FOREIGN_DOC_RE.fullmatch(cleaned) is None:
        raise ResolveTargetValidationError(
            "target_document.number con formato invalido (6-12 alfanumericos)"
        )
    return kind, cleaned


def validate_phone(value: object) -> str:
    """Valida `target_phone` y retorna los digitos normalizados (422 si no)."""
    digits = normalize_phone(value)
    if not digits or not (PHONE_MIN_DIGITS <= len(digits) <= PHONE_MAX_DIGITS):
        raise ResolveTargetValidationError(
            "target_phone con formato invalido " f"({PHONE_MIN_DIGITS}-{PHONE_MAX_DIGITS} digitos)"
        )
    return digits


def _coerce_document(value: Any) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    get = getattr(value, "model_dump", None)
    if callable(get):
        dumped = get()
        return dumped if isinstance(dumped, dict) else None
    return None


def resolve_transfer_target(
    session: Session,
    *,
    target_document: Any | None = None,
    target_phone: Any | None = None,
) -> dict:
    """Resuelve el destino y retorna la referencia + mascaras (solo lectura).

    Retorna `{target_account_id (UUID), holder_name_masked,
    account_number_masked ("****dddd"), currency}`. Sin PII en logs ni en
    la respuesta (solo UUIDs, mascaras y titular enmascarado).
    """
    from app.adapters.holder_lookup import mask_holder_name
    from app.modules.accounts.service import list_active_by_user, mask_account_number
    from app.modules.identity.service import target_lookup as identity_lookup

    document = _coerce_document(target_document)
    phone = target_phone.strip() if isinstance(target_phone, str) else target_phone
    has_document = document is not None and (
        document.get("type") is not None or document.get("number") is not None
    )
    has_phone = isinstance(phone, str) and phone != ""
    if has_document == has_phone:
        raise ResolveTargetValidationError(
            "se exige exactamente uno de target_document / target_phone"
        )
    if has_document:
        assert document is not None
        _, digits = validate_document(document.get("type"), document.get("number"))
        holder = identity_lookup.resolve_user_by_document(session, digits)
    else:
        validated_phone = validate_phone(phone)
        holder = identity_lookup.resolve_user_by_phone(session, validated_phone)
    if holder is None:
        logger.info("resolve_target outcome=not_found")
        raise TargetNotFoundError(TARGET_NOT_FOUND_MESSAGE)
    active = list_active_by_user(session, holder["user_id"])
    if len(active) == 0:
        # Neutro: identico a un titular inexistente (anti-enumeracion).
        logger.info("resolve_target outcome=not_found")
        raise TargetNotFoundError(TARGET_NOT_FOUND_MESSAGE)
    if len(active) > 1:
        logger.info("resolve_target outcome=ambiguous active_count=%d", len(active))
        raise TargetAmbiguousError(TARGET_AMBIGUOUS_MESSAGE)
    account = active[0]
    business = (holder.get("business_name") or "").strip()
    if business:
        display = business
    else:
        display = f"{holder.get('first_name') or ''} {holder.get('last_name') or ''}".strip()
    result = {
        "target_account_id": (
            account["account_id"]
            if isinstance(account["account_id"], uuid.UUID)
            else uuid.UUID(str(account["account_id"]))
        ),
        "holder_name_masked": mask_holder_name(display),
        "account_number_masked": mask_account_number(str(account["account_number"])),
        "currency": str(account["currency"]),
    }
    logger.info("resolve_target outcome=found")
    return result


__all__ = [
    "DNI_LENGTH",
    "PHONE_MAX_DIGITS",
    "PHONE_MIN_DIGITS",
    "RESOLVE_DOC_TYPES",
    "TARGET_AMBIGUOUS_MESSAGE",
    "TARGET_NOT_FOUND_MESSAGE",
    "ResolveTargetError",
    "ResolveTargetValidationError",
    "TargetAmbiguousError",
    "TargetNotFoundError",
    "normalize_phone",
    "resolve_transfer_target",
    "validate_document",
    "validate_phone",
]
