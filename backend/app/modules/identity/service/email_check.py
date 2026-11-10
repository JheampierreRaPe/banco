"""Precheck de email para el paso "Continuar" del KYC (E1-T40, HU01).

`email_is_registered(session, email) -> bool`: normaliza el correo
(`strip().lower()`, equivalente a `kyc_onboarding._normalize_email`) y
consulta `identity_repo.get_by_email` (`repository/__init__.py:214`).
Solo lectura: no persiste, no llama al proveedor KYC ni a ningun
proveedor externo.

Anti-oraculo (decision del dueno, item 2): este precheck SI revela
existencia (igual que el 409 de documento de E1-T37); se mitiga con
rate-limit previo en el router (ventana en memoria compartida con el
proxy KYC) y mensaje neutro sin eco del email. Sin PII en logs: solo
`has_match` booleano, nunca el email ni derivados correlacionables
(fix MENOR: se retiro el hash corto del correo por pseudonimo
correlacionable sin sal; tampoco se reutiliza el hash de tokens de
bitacora porque sigue siendo SHA sin sal y el precheck no necesita
correlacion entre lineas).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.modules.identity import repository as identity_repo

logger = logging.getLogger(__name__)


def _normalize_email(email: str) -> str:
    """Normaliza el correo (trim + minusculas; el schema lo exige no vacio)."""
    if not isinstance(email, str) or not email.strip():
        raise ValueError("email es obligatorio")
    return email.strip().lower()


def email_is_registered(session: Session, email: str) -> bool:
    """`True` si el email (normalizado) ya esta registrado en `identity.users`.

    Solo lectura via `get_by_email`; sin persistencia ni llamadas externas.
    En logs solo `has_match` booleano (jamas el email en claro ni hashes
    derivados del email).
    """
    normalized = _normalize_email(email)
    found = identity_repo.get_by_email(session, normalized) is not None
    logger.info("email_check has_match=%s", found)
    return found


__all__ = ["email_is_registered"]
