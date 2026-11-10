"""Resolucion de titulares por documento o celular (E2-T27, HU06).

Fachada de lectura que consume `transactions` para el lookup previo
(`POST /transfers/third-party/resolve-target`), sin importar modelos ni
repositorios de `identity` desde otros modulos (regla de oro 4, D-01).

- Documento: se resuelve por `doc_number_hash` (HMAC server-side con pepper
  via `kyc_onboarding.hash_document_number`); el numero en claro nunca se
  persiste ni se loguea (reglas de oro 7/9).
- Celular: se compara normalizado (solo digitos, sin espacios ni guiones);
  el valor en claro nunca se loguea.

Solo lectura (sin `flush`/`commit`). Retorna vistas minimas
(`user_id`, `first_name`, `last_name`, `business_name`) o `None`.
"""

from __future__ import annotations

import logging
import re

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.service.kyc_onboarding import hash_document_number

logger = logging.getLogger(__name__)

_NON_DIGIT_RE = re.compile(r"\D+")


def normalize_phone(value: object) -> str:
    """Normaliza un celular a solo digitos (puro, sin BD).

    `"+51 987-654-321"` -> `"51987654321"`. No valido nada mas aqui:
    el formato lo valida `transactions` (422); la fachada solo compara.
    """
    if not isinstance(value, str):
        return ""
    return _NON_DIGIT_RE.sub("", value)


def _public_view(user: User) -> dict:
    """Vista minima del titular (sin PII completa: nombres para enmascarar)."""
    return {
        "user_id": user.id,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "business_name": user.business_name,
    }


def resolve_user_by_document(session: Session, number: str) -> dict | None:
    """Resuelve el titular por numero de documento (`None` si no existe).

    Hashea con el mismo HMAC server-side del alta (`hash_document_number`)
    y lee por `get_by_doc_hash`. Solo lectura; en logs solo `has_match`
    (jamas el numero ni el hash).
    """
    try:
        digest = hash_document_number(number)
    except (TypeError, ValueError):
        logger.info("target_lookup by_document has_match=%s", False)
        return None
    user = identity_repo.get_by_doc_hash(session, digest)
    logger.info("target_lookup by_document has_match=%s", user is not None)
    return _public_view(user) if user is not None else None


def resolve_user_by_phone(session: Session, phone: str) -> dict | None:
    """Resuelve el titular por celular normalizado (`None` si no existe).

    Compara digitos contra digitos (el `phone` persistido puede tener
    formato). Solo lectura; en logs solo `has_match` (jamas el numero).
    """
    digits = normalize_phone(phone)
    if not digits:
        logger.info("target_lookup by_phone has_match=%s", False)
        return None
    candidates = session.scalars(sa.select(User).where(User.phone.is_not(None))).all()
    for user in candidates:
        if normalize_phone(user.phone) == digits and normalize_phone(user.phone):
            logger.info("target_lookup by_phone has_match=%s", True)
            return _public_view(user)
    logger.info("target_lookup by_phone has_match=%s", False)
    return None


__all__ = [
    "normalize_phone",
    "resolve_user_by_document",
    "resolve_user_by_phone",
]
