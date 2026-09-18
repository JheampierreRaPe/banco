"""Fijado inicial del PIN con OTP de activacion (`setup_pin`; addendum E1-T14, HU02/HU03).

Hueco que cierra: `onboard_customer` crea la credencial SIN `pin_hash`
(`initial_pin_hash=None`) y no existia ningun endpoint para fijarlo, asi que
el login con PIN (`POST /auth/login/pin`, E1-T14) jamas podia funcionar para
usuarios creados por la app. Este caso de uso fija el PIN una sola vez.

Flujo (`POST /auth/pin/setup {user_ref, code, pin}`):

1. Valida el formato del PIN (`4-6` digitos numericos; `ValueError` -> 422)
   ANTES de consumir nada: un PIN inutilizable no debe quemar un codigo de
   un solo uso. No crea oraculo: el 422 solo habla del formato del propio
   input, nunca del estado de la cuenta.
2. Valida el OTP `ACTIVATION` via `otp_service.validate_otp` (lo consume:
   marca `USED` + enlista `user.activated` via outbox, sin duplicar aqui).
   Codigo invalido/expirado/usado o usuario inexistente -> un solo
   `PinSetupInvalidError` (`INVALID_SETUP_CODE`, mismo cuerpo: no filtra
   existencia ni estado). El OTP valido ES la autorizacion (sin sesion
   previa); por eso el 409 de abajo solo se alcanza con OTP valido (no se
   puede sondear el estado del PIN sin codigo).
3. Si la credencial ya tiene `pin_hash` -> `PinAlreadySetError`
   (`PIN_ALREADY_SET`, 409): el PIN solo se fija una vez (no hay re-fijado).
4. Si no, fija `pin_hash = hash_pin(pin)` (PBKDF2 de `pin_login`, sin
   reinventar cripto) y retorna `{user_id, status}` (forma de E1-T10).

Convencion: `flush` sin `commit`; quien llama decide la transaccion (el
endpoint confirma en exito y ante 409 para que el consumo del OTP persista;
revierte ante 401 como E1-T10). Sin `float`, sin PIN en claro en logs ni
respuestas (solo su hash), sin PII en logs. Auditoria `auth.pin_setup`
best-effort via fachada `audit` (import perezoso, como `pin_login`). No
toca `onboard_customer`, `kyc_proxy`, `activation` ni `login_with_pin` (solo
reutiliza `hash_pin`, `validate_otp` y el repositorio `identity`).
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy.orm import Session

from app.modules.identity import repository as identity_repo
from app.modules.identity.service import otp_service
from app.modules.identity.service import pin_login as pin_login_service

logger = logging.getLogger(__name__)

#: Proposito OTP de este flujo (el mismo que activa E1-T10 en HU02).
SETUP_PURPOSE = "ACTIVATION"

#: Mensaje generico estable (identico exista o no el usuario o el OTP).
INVALID_MESSAGE = "Codigo de configuracion invalido"

#: Mensaje de PIN ya fijado (solo con OTP valido: no filtra sin codigo).
ALREADY_SET_MESSAGE = "El PIN ya fue configurado"

#: Accion de auditoria E1-T17 (best-effort via fachada `audit`).
AUDIT_PIN_SETUP = "auth.pin_setup"

#: Tipo de agregado para la auditoria (el mismo que `pin_login`).
USER_AGGREGATE_TYPE = "user"

#: Formato del PIN: 4-6 digitos numericos (sin oraculo: se valida antes de
#: tocar estado y el 422 es generico).
_PIN_RE = re.compile(r"^\d{4,6}$")


class PinSetupInvalidError(ValueError):
    """Codigo invalido/expirado/usado, usuario inexistente, `user_ref`
    malformado o credencial ausente: una sola clase/mensaje para no filtrar
    (-> 401 `INVALID_SETUP_CODE`)."""


class PinAlreadySetError(ValueError):
    """La credencial ya tiene PIN (-> 409 `PIN_ALREADY_SET`)."""


def _coerce_user_id(user_ref: object) -> uuid.UUID | None:
    """Interpreta `user_ref` como UUID (`None` si malformado, sin excepcion)."""
    if isinstance(user_ref, uuid.UUID):
        return user_ref
    try:
        return uuid.UUID(str(user_ref).strip())
    except (ValueError, AttributeError, TypeError):
        return None


def _validate_pin_format(pin: object) -> str:
    """Exige PIN de 4-6 digitos numericos (`ValueError` -> 422 generico)."""
    if not isinstance(pin, str) or not _PIN_RE.match(pin):
        raise ValueError("pin debe ser de 4 a 6 digitos numericos")
    return pin


def _audit_pin_setup(session: Session, *, user_id: uuid.UUID) -> None:
    """Registra `auth.pin_setup` via fachada (best-effort como `pin_login`).

    Nunca `commit` (solo `flush` via la fachada). Sin PII: solo IDs; jamas
    el PIN ni su hash.
    """
    try:
        from app.modules.audit.service import record as audit_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("pin_setup audit no disponible")
        return
    try:
        audit_record(
            session,
            actor=user_id,
            action=AUDIT_PIN_SETUP,
            entity=USER_AGGREGATE_TYPE,
            entity_id=user_id,
            metadata={"method": "otp-setup"},
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 - best-effort documentado E1-T17
        logger.warning("pin_setup audit no registrado error=%s", type(exc).__name__)


def setup_pin(session: Session, *, user_ref: str, code: str, pin: str) -> dict:
    """Fija el PIN inicial una sola vez (`flush`, sin `commit`).

    Exito: consume el OTP `ACTIVATION`, fija `pin_hash` y retorna
    `{"user_id", "status"}` (el `status` actual del usuario). Fallos:
    formato de PIN debil -> `ValueError` (422); codigo/usuario invalidos ->
    `PinSetupInvalidError` (401 generico); PIN ya fijado ->
    `PinAlreadySetError` (409).
    """
    uid = _coerce_user_id(user_ref)
    if uid is None:
        raise PinSetupInvalidError(INVALID_MESSAGE)
    _validate_pin_format(pin)

    try:
        otp_service.validate_otp(session, user_id=uid, purpose=SETUP_PURPOSE, code=code)
    except (otp_service.OtpError, ValueError) as exc:
        # `OtpNotFound/Invalid/Expired/AttemptsExceeded` + codigo malformado
        # (servicio directo): generico, sin filtrar existencia ni estado.
        raise PinSetupInvalidError(INVALID_MESSAGE) from exc

    user = identity_repo.get_user(session, uid)
    credential = identity_repo.get_credential(session, uid) if user is not None else None
    if user is None or credential is None:
        # Inalcanzable con OTP valido en la practica (el OTP cuelga del
        # usuario), pero generico por si la cuenta se borro entremedio.
        raise PinSetupInvalidError(INVALID_MESSAGE)
    if credential.pin_hash:
        raise PinAlreadySetError(ALREADY_SET_MESSAGE)

    credential.pin_hash = pin_login_service.hash_pin(pin)
    session.flush()
    _audit_pin_setup(session, user_id=user.id)
    logger.info("pin_setup ok")
    return {"user_id": str(user.id), "status": user.status}


__all__ = [
    "ALREADY_SET_MESSAGE",
    "AUDIT_PIN_SETUP",
    "INVALID_MESSAGE",
    "SETUP_PURPOSE",
    "USER_AGGREGATE_TYPE",
    "PinAlreadySetError",
    "PinSetupInvalidError",
    "setup_pin",
]
