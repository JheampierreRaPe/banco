"""Consulta del titular por documento (E1-T35, HU01).

Delega en el adaptador `DocumentLookupProvider`: valida `type`/`number`
ANTES de cualquier red, consulta y **normaliza** `first_name`/`last_name`
(persona natural) o `business_name` (razon social, RUC de persona juridica).
Descarta cualquier PII que no se use; sin persistencia, sin eventos.

E1-T37: precheck "documento ya registrado" (DNI y RUC) ANTES de la red:
`lookup_holder` acepta el seam `is_registered` y lanza
`DocumentAlreadyRegisteredError` (409) sin invocar al proveedor cuando el
`doc_number_hash` HMAC ya existe en `identity.users`.

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
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.adapters.document_lookup_provider import DocumentLookupProvider, clean_text, hash_document
from app.modules.identity import repository as identity_repo
from app.modules.identity.service.kyc_onboarding import hash_document_number

logger = logging.getLogger(__name__)

#: Tipos de documento aceptados (validacion temprana, espejo del schema).
DOC_LOOKUP_TYPES: tuple[str, ...] = ("DNI", "RUC")

#: Longitudes exactas por tipo (solo digitos).
DOC_LOOKUP_LENGTHS: dict[str, int] = {"DNI": 8, "RUC": 11}


class DocumentLookupValidationError(ValueError):
    """Entrada `type`/`number` invalida -> HTTP 422 (sin llamar al proveedor)."""


class DocumentAlreadyRegisteredError(ValueError):
    """Documento ya registrado (`doc_number_hash` existe) -> HTTP 409.

    Familia propia del lookup (E1-T37): el router la traduce a
    `DUPLICATE_DOCUMENT` ("El documento ya se encuentra registrado") sin
    haber invocado al proveedor. No lleva el numero ni datos del titular.
    """


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


def document_is_registered(session: Session, number: str) -> bool:
    """`True` si el numero ya esta registrado en `identity.users` (E1-T37).

    Hashea con `kyc_onboarding.hash_document_number` (HMAC-SHA256 con
    `DOC_HASH_PEPPER`, determinista y alineado al UNIQUE
    `uq_users_doc_number_hash`); prohibido usar el `hash_document` del
    adaptador (ese solo correlaciona logs). Solo lectura, sin PII en logs.
    """
    doc_hash = hash_document_number(number)
    found = identity_repo.get_by_doc_hash(session, doc_hash) is not None
    logger.info("doc_lookup precheck has_match=%s", found)
    return found


def lookup_holder(
    provider: DocumentLookupProvider,
    *,
    doc_type: object,
    number: object,
    is_registered: Callable[[str], bool] | None = None,
) -> dict:
    """Valida, prechequea duplicado, consulta al proveedor y normaliza.

    Orden: `validate_lookup_input` (422) -> precheck (409) -> proveedor
    (200/404/503/504). Respuesta:
    `{document_type, first_name, last_name, business_name}`.
    La ausencia de datos del proveedor se propaga como
    `DocumentNotFoundError` (el router la traduce a 404 neutro).

    `is_registered(number)`: seam del precheck "documento ya registrado"
    (E1-T37, DNI y RUC). Recibe los digitos ya validados y, si devuelve
    `True`, se lanza `DocumentAlreadyRegisteredError` SIN invocar al
    proveedor. En produccion el router siempre lo inyecta (via
    `document_is_registered` con la sesion de BD); los tests unitarios
    puros pueden omitirlo (sin red ni BD).
    """
    kind, digits = validate_lookup_input(doc_type, number)
    session_id = uuid.uuid4().hex
    logger.info(
        "doc_lookup validated session_id=%s doc_type=%s doc_hash=%s",
        session_id,
        kind,
        hash_document(digits),
    )
    if is_registered is not None and is_registered(digits):
        logger.info(
            "doc_lookup duplicate session_id=%s doc_type=%s",
            session_id,
            kind,
        )
        raise DocumentAlreadyRegisteredError("El documento ya se encuentra registrado")
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
    "DocumentAlreadyRegisteredError",
    "DocumentLookupValidationError",
    "document_is_registered",
    "lookup_holder",
    "validate_lookup_input",
]
