"""Consulta del titular por documento (E1-T35, HU01).

Delega en el adaptador `DocumentLookupProvider`: valida `type`/`number`
ANTES de cualquier red, consulta y **normaliza** `first_name`/`last_name`
(persona natural) o `business_name` (razon social, RUC de persona juridica).
Descarta cualquier PII que no se use; sin persistencia, sin eventos.

Rate limit: reutiliza el patron en memoria de `kyc_proxy`
(`check_rate_limit`/`build_rate_key`) desde el router; en produccion
multirreplica va a Redis/middleware (igual que KYC).

Sin PII en logs: el numero solo se correlaciona hasheado
(`hash_document` del adaptador); los nombres devueltos jamas se loguean.
"""

from __future__ import annotations

import logging
import re
import uuid

from app.adapters.document_lookup_provider import DocumentLookupProvider, clean_text, hash_document

logger = logging.getLogger(__name__)

#: Tipos de documento aceptados (validacion temprana, espejo del schema).
DOC_LOOKUP_TYPES: tuple[str, ...] = ("DNI", "RUC")

#: Longitudes exactas por tipo (solo digitos).
DOC_LOOKUP_LENGTHS: dict[str, int] = {"DNI": 8, "RUC": 11}


class DocumentLookupValidationError(ValueError):
    """Entrada `type`/`number` invalida -> HTTP 422 (sin llamar al proveedor)."""


def validate_lookup_input(doc_type: object, number: object) -> tuple[str, str]:
    """Valida `type in {DNI,RUC}` y `number` solo digitos con longitud exacta."""
    kind = doc_type.strip().upper() if isinstance(doc_type, str) else ""
    digits = number.strip() if isinstance(number, str) else ""
    if kind not in DOC_LOOKUP_TYPES:
        raise DocumentLookupValidationError(f"type debe ser uno de {DOC_LOOKUP_TYPES}")
    if not digits or re.fullmatch(r"[0-9]+", digits) is None:
        raise DocumentLookupValidationError("number debe contener solo digitos")
    expected = DOC_LOOKUP_LENGTHS[kind]
    if len(digits) != expected:
        raise DocumentLookupValidationError(f"number: {kind} debe tener {expected} digitos")
    return kind, digits


def lookup_holder(provider: DocumentLookupProvider, *, doc_type: object, number: object) -> dict:
    """Valida, consulta al proveedor y devuelve el titular normalizado.

    Respuesta: `{document_type, first_name, last_name, business_name}`.
    La ausencia de datos del proveedor se propaga como
    `DocumentNotFoundError` (el router la traduce a 404 neutro).
    """
    kind, digits = validate_lookup_input(doc_type, number)
    session_id = uuid.uuid4().hex
    logger.info(
        "doc_lookup validated session_id=%s doc_type=%s doc_hash=%s",
        session_id,
        kind,
        hash_document(digits),
    )
    holder = provider.lookup_document(session_id=session_id, doc_type=kind, number=digits)
    data = {
        "document_type": kind,
        "first_name": clean_text(holder.first_name),
        "last_name": clean_text(holder.last_name),
        "business_name": clean_text(holder.business_name),
    }
    # Solo conteos/tipo en logs: nunca nombres ni numero en claro.
    logger.info(
        "doc_lookup result session_id=%s doc_type=%s has_names=%s has_business=%s",
        session_id,
        kind,
        bool(data["first_name"] or data["last_name"]),
        bool(data["business_name"]),
    )
    return data


__all__ = [
    "DOC_LOOKUP_LENGTHS",
    "DOC_LOOKUP_TYPES",
    "DocumentLookupValidationError",
    "lookup_holder",
    "validate_lookup_input",
]
