"""Alta de cliente + persistencia KYC en `POST /auth/kyc/submit` (E1-T24, HU01/HU02).

Orquesta, en la MISMA transaccion (misma sesion, `flush` sin `commit`), el
resultado del KYC (`kyc_proxy.submit_kyc`) con el alta de cliente
(`onboard_customer`, E1-T03) y la persistencia del intento
(`save_verification`, E1-T04):

- `overall_result=true`: valida duplicados (documento/email), crea el
  `User`/`Credential`/cuenta/subcuentas/OTP/`kyc.completed` via
  `onboard_customer` y guarda la verificacion asociada al usuario.
- `overall_result=false`: guarda el intento fallido (con motivo) y NO crea
  usuario/cuenta ni emite `kyc.completed`.

El numero de documento se hashea server-side con HMAC-SHA256 (pepper
`DOC_HASH_PEPPER` de entorno) sobre la forma normalizada; jamas se persiste
ni se loguea en claro ni el pepper: solo `doc_number_hash` y
`doc_number_masked`. La normalizacion es determinista (mayusculas, sin
separadores) porque hay UNIQUE sobre el hash. Sin `float` de dinero, sin
frames, sin PII en logs. La transaccion la cierra el endpoint (`commit` en
exito, `rollback` ante fallo): todo-o-nada.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.kyc_provider import hash_token
from app.modules.identity import repository as identity_repo
from app.modules.identity.service import onboard_customer

logger = logging.getLogger(__name__)

#: Motivo de fallo por defecto cuando el proveedor no entrega `detail_code`.
REJECTION_REASON = "kyc_not_passed"

#: Longitud maxima de `doc_number_masked` (`03b#4.1`: VARCHAR(20)).
MASKED_MAX = 20

#: Variable de entorno con el pepper del hash del documento (HMAC-SHA256).
DOC_HASH_PEPPER_ENV = "DOC_HASH_PEPPER"

#: Pepper de desarrollo (solo si `DOC_HASH_PEPPER` no esta definido). En
#: produccion SIEMPRE debe configurarse uno propio, largo y secreto; jamas se
#: loguea. Cambiarlo invalida los hashes previos (por eso se define en BD).
_DEV_DOC_HASH_PEPPER = "banca-online-dev-doc-hash-pepper"

_NON_ALNUM = re.compile(r"[^0-9A-Za-z]")


class DuplicateDocumentError(ValueError):
    """`doc_number_hash` ya registrado -> HTTP 409 `DUPLICATE_DOCUMENT`."""


class DuplicateEmailError(ValueError):
    """`email` ya registrado -> HTTP 409 `DUPLICATE_EMAIL`."""


def normalize_document_number(document_number: str) -> str:
    """Forma canonica (sin separadores, mayusculas) para hashear/enmascarar."""
    if not isinstance(document_number, str):
        raise TypeError("document.number debe ser texto")
    return _NON_ALNUM.sub("", document_number).upper()


def _document_hash_pepper() -> bytes:
    """Pepper del hash: `DOC_HASH_PEPPER` o el fallback de desarrollo.

    Nunca se loguea. En produccion se configura por entorno; el fallback solo
    evita fallos en local/pruebas (documentado como riesgo aceptado).
    """
    configured = os.environ.get(DOC_HASH_PEPPER_ENV)
    return (configured if configured else _DEV_DOC_HASH_PEPPER).encode("utf-8")


def hash_document_number(document_number: str) -> str:
    """Hash HMAC-SHA256 hex (64 chars) del numero normalizado; sin claro.

    Determinista para el mismo numero y pepper (hay UNIQUE sobre el hash);
    con `DOC_HASH_PEPPER` distinto el hash cambia (no reversible por fuerza
    bruta sin el pepper). El pepper jamas se persiste ni se loguea.
    """
    normalized = normalize_document_number(document_number)
    if not normalized:
        raise ValueError("document.number es obligatorio")
    return hmac.new(_document_hash_pepper(), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def mask_document_number(document_number: str) -> str:
    """Version enmascarada (conserva los ultimos 4 caracteres)."""
    normalized = normalize_document_number(document_number)
    if not normalized:
        return ""
    if len(normalized) <= 4:
        return "*" * len(normalized)
    return ("*" * (len(normalized) - 4) + normalized[-4:])[-MASKED_MAX:]


def _normalize_email(email: str) -> str:
    """Normaliza el correo (trim + minusculas; el schema lo exige no vacio)."""
    if not isinstance(email, str) or not email.strip():
        raise ValueError("applicant.email es obligatorio")
    return email.strip().lower()


def _reject_duplicates(session: Session, *, doc_hash: str, email: str) -> None:
    """409 tipado por campo, sin revelar datos de otro usuario."""
    if identity_repo.get_by_doc_hash(session, doc_hash) is not None:
        logger.info("kyc onboarding duplicate field=document")
        raise DuplicateDocumentError("documento ya registrado")
    if email and identity_repo.get_by_email(session, email) is not None:
        logger.info("kyc onboarding duplicate field=email")
        raise DuplicateEmailError("email ya registrado")


def _duplicate_from_integrity_error(exc: IntegrityError) -> ValueError | None:
    """Traduce una violacion UNIQUE de `users` al 409 tipado por campo.

    Cubre la carrera check-then-insert (dos altas concurrentes pasan el
    chequeo previo): la BD es la ultima garantia. Devuelve `None` si la
    violacion no es de `email` ni de `doc_number_hash` (el llamante re-lanza).
    """
    text = str(getattr(exc, "orig", None) or exc).lower()
    if "email" in text or "uq_users_email" in text:
        return DuplicateEmailError("email ya registrado")
    if "doc_number_hash" in text or "uq_users_doc_number_hash" in text:
        return DuplicateDocumentError("documento ya registrado")
    return None


def persist_kyc_submission(
    session: Session,
    *,
    kyc_result: dict[str, Any],
    doc_type: str,
    document_number: str,
    first_name: str,
    last_name: str,
    business_name: str | None = None,
    email: str,
    phone: str | None = None,
    challenge_token: str | None = None,
) -> dict[str, Any]:
    """Persiste el resultado del KYC y da de alta al cliente si paso.

    `kyc_result` es el retorno de `kyc_proxy.submit_kyc`
    (`overall_result`/`detail_code`/`distance`). Con exito: alta via
    `onboard_customer` + verificacion en la misma sesion. Con fallo:
    verificacion con `user_id=None` y `failure_reason`. Cualquier fallo de
    las fachadas `accounts`/`ledger`/`outbox` propaga para el rollback del
    llamante (sin usuario a medias). Retorno listo para `KycSubmitData`.

    E1-T36: `business_name` (razon social del `applicant`) se propaga al
    alta; la validacion por tipo vive en `kyc_proxy.validate_applicant`
    (el endpoint la exige ANTES del proveedor).
    """
    if not isinstance(kyc_result, dict):
        raise TypeError(f"kyc_result debe ser dict, recibido: {kyc_result!r}")
    overall = kyc_result.get("overall_result")
    if not isinstance(overall, bool):
        raise TypeError("kyc_result.overall_result debe ser bool")
    detail_code = str(kyc_result.get("detail_code") or "")
    distance = float(kyc_result.get("distance") or 0.0)

    doc_hash = hash_document_number(document_number)
    doc_masked = mask_document_number(document_number)
    normalized_email = _normalize_email(email)
    token_hash = hash_token(challenge_token) if challenge_token else None

    if overall:
        _reject_duplicates(session, doc_hash=doc_hash, email=normalized_email)
        try:
            # Savepoint: aisla el alta para poder mapear la violacion UNIQUE
            # de la carrera (check-then-insert) sin envenenar la sesion.
            with session.begin_nested():
                onboarded = onboard_customer(
                    session,
                    kyc_result={"overall_result": True, "doc_number_hash": doc_hash},
                    first_name=first_name,
                    last_name=last_name,
                    doc_type=doc_type,
                    business_name=business_name,
                    doc_number_masked=doc_masked,
                    email=normalized_email,
                    phone=phone,
                )
        except IntegrityError as exc:
            duplicate = _duplicate_from_integrity_error(exc)
            if duplicate is None:
                raise
            logger.info("kyc onboarding duplicate_race field=%s", type(duplicate).__name__)
            raise duplicate from exc
        except identity_repo.DuplicateDocumentError as exc:
            raise DuplicateDocumentError("documento ya registrado") from exc
        user_id: uuid.UUID | None = onboarded["user_id"]
        account_id: uuid.UUID | None = onboarded["account_id"]
        status = str(onboarded["status"])
        failure_reason = None
    else:
        user_id = None
        account_id = None
        status = "REJECTED"
        failure_reason = detail_code or REJECTION_REASON

    identity_repo.save_verification(
        session,
        user_id=user_id,
        overall_result=overall,
        document_json={"doc_number_masked": doc_masked, "detail_code": detail_code},
        face_match_json={"distance": distance},
        challenge_token_hash=token_hash,
        failure_reason=failure_reason,
    )
    logger.info(
        "kyc onboarding overall=%s status=%s has_user=%s",
        overall,
        status,
        user_id is not None,
    )
    return {
        "overall_result": overall,
        "detail_code": detail_code,
        "distance": distance,
        "user_id": str(user_id) if user_id is not None else None,
        "status": status,
        "account_id": str(account_id) if account_id is not None else None,
    }


__all__ = [
    "DOC_HASH_PEPPER_ENV",
    "MASKED_MAX",
    "REJECTION_REASON",
    "DuplicateDocumentError",
    "DuplicateEmailError",
    "hash_document_number",
    "mask_document_number",
    "normalize_document_number",
    "persist_kyc_submission",
]
