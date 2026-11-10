"""Reseteo de PIN con email+DNI+OTP (`reset_pin`; E1-T34, HU02/HU04, SCR-005).

Hueco que cierra: `recovery.verify` (E1-T33) devuelve `{user_ref,
device_bound}` SIN abrir sesion, pero el usuario que no recuerda su PIN no
puede abrir la unica sesion (`POST /auth/login/pin`). Este caso de uso fija
el PIN con el OTP `RECOVERY` emitido por `recovery.request`.

Flujo (`POST /auth/pin-reset {email, doc_number, [doc_type,] code, pin}`):

1. Valida el formato del PIN (`4-6` digitos numericos) y del documento
   (`doc_type in {DNI,RUC}`, `doc_number` solo digitos con longitud exacta
   DNI 8 / RUC 11; `ValueError` -> 422) ANTES de consumir nada: un input
   inutilizable no debe quemar un codigo de
   un solo uso. No crea oraculo: el 422 solo habla del formato del propio
   input, nunca del estado de la cuenta.
2. Rate-limit por `email+IP` ANTES de resolver existencia (misma
   ventana que `recovery.verify`, via
   `recovery.check_pin_reset_rate_limit`): excederla -> 429, sin filtrar.
3. Resuelve el usuario por email normalizado (`strip().lower()`); si no
   existe o `status != 'ACTIVE'` -> `PinResetInvalidError` (401 generico,
   sin revelar existencia/estado). La rama ciega consulta el OTP pendiente
   (descartado) para igualar el patron de acceso a BD.
4. Valida el DNI/RUC contra `users.doc_number_hash` con el hash HMAC-SHA256
   server-side existente (`kyc_onboarding.hash_document_number` sobre la
   forma normalizada, comparacion con `hmac.compare_digest`); no coincide
   (o el documento es vacio/invalido) -> mismo 401 generico. El documento
   jamas se loguea ni se persiste. `doc_type` NO se cruza con
   `users.doc_type` (E1-T40: solo valida formato, sin oraculo).
5. Exige credencial existente y consume el OTP `RECOVERY` via
   `otp_service.validate_otp` (hash/TTL/max intentos, un solo uso):
   codigo incorrecto/vencido/bloqueado/sin OTP -> mismo 401 generico
   (vencido y bloqueado colapsan al generico para no filtrar; el log
   interno conserva la causa real).
6. Al exito, en la MISMA transaccion: `credentials.pin_hash =
   hash_pin(pin)` (PBKDF2 de `pin_login`, sin reinventar cripto),
   `failed_attempts=0`, `locked_until=None` (resetea el bloqueo), fila en
   `access_recovery` (`method='OTP'`, **`new_credential_set=true`**: es un
   cambio de credencial real, a diferencia de `recovery.verify`) y
   auditoria `auth.pin_reset` best-effort via fachada. Retorna
   `{user_ref, pin_set: true}`. NO crea `sessions` ni emite tokens.

Convencion: `flush` sin `commit`; el endpoint confirma (`commit`) en exito
Y ante error de negocio tipado (el contador de intentos del OTP DEBE
persistir, como `api/recovery.py`), y revierte (`rollback`) ante error
inesperado o PIN malformado (422, antes de tocar estado). Sin `float`, sin
PII/DNI/OTP/PIN en logs ni respuestas (solo `result` y contadores), sin
secretos. No toca `onboard_customer`, `kyc_proxy`, `activation`,
`pin_setup` ni `device_login` (solo reutiliza `hash_pin`, `validate_otp`,
`hash_document_number` y el repositorio `identity`).
"""

from __future__ import annotations

import hmac
import logging
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.modules.identity import repository as identity_repo
from app.modules.identity.repository import recovery as recovery_repo
from app.modules.identity.service import otp_service
from app.modules.identity.service import pin_login as pin_login_service
from app.modules.identity.service import recovery as recovery_service
from app.modules.identity.service.kyc_onboarding import hash_document_number

logger = logging.getLogger(__name__)

#: Proposito OTP de este flujo (el emitido por `recovery.request`).
RESET_PURPOSE = "RECOVERY"

#: Metodo registrado en `access_recovery` (`03b#4.8`).
RESET_METHOD = "OTP"

#: Mensaje generico estable (identico para email no registrado, no elegible,
#: DNI que no coincide, sin OTP, codigo incorrecto, OTP vencido o bloqueado
#: por intentos: sin enumeracion ni oraculo).
INVALID_MESSAGE = "Codigo de reseteo invalido"

#: Mensaje de rate-limit (ventana por `email+IP` excedida).
RATE_LIMIT_MESSAGE = recovery_service.RATE_LIMIT_MESSAGE

#: Accion de auditoria (best-effort via fachada, sin PII).
AUDIT_PIN_RESET = "auth.pin_reset"

#: Tipo de agregado para la auditoria (el mismo que `pin_login`).
USER_AGGREGATE_TYPE = "user"

#: Formato del PIN: 4-6 digitos numericos (sin oraculo: se valida antes de
#: tocar estado y el 422 es generico; misma regla que `pin_setup`).
_PIN_RE = re.compile(r"^\d{4,6}$")

#: Tipos de documento aceptados en `/pin-reset` (E1-T40, paridad con el
#: lookup E1-T35; gobiernan SOLO la validacion de formato/longitud).
_PIN_DOC_TYPES: tuple[str, ...] = ("DNI", "RUC")

#: Longitudes exactas por tipo (solo digitos; E1-T40).
_PIN_DOC_LENGTHS: dict[str, int] = {"DNI": 8, "RUC": 11}


class PinResetInvalidError(ValueError):
    """Email no registrado, usuario no elegible, DNI que no coincide, sin
    OTP pendiente, codigo incorrecto, OTP vencido, OTP bloqueado por
    intentos o credencial ausente: una sola clase/mensaje para no filtrar
    (-> 401 `INVALID_PIN_RESET`). Vencido y bloqueado colapsan aqui a
    proposito (mismo patron anti-oraculo que `recovery`): el log interno
    conserva la causa, pero el usuario recibe siempre el generico."""


def _validate_pin_format(pin: object) -> str:
    """Exige PIN de 4-6 digitos numericos (`ValueError` -> 422 generico)."""
    if not isinstance(pin, str) or not _PIN_RE.match(pin):
        raise ValueError("pin debe ser de 4 a 6 digitos numericos")
    return pin


def _validate_doc_format(doc_type: object, doc_number: object) -> tuple[str, str]:
    """Exige `doc_type in {DNI,RUC}` y `doc_number` solo digitos exactos.

    Validacion defensiva (E1-T40, paridad con el lookup E1-T35): el esquema
    ya la aplica en HTTP, pero los llamados directos al servicio tambien
    quedan cubiertos. `ValueError` -> 422 `INVALID_PIN_FORMAT` en el router,
    ANTES de tocar estado (sin quemar el OTP). Retorna la forma canonica
    (`doc_type` en mayusculas, `doc_number` con trim).
    """
    kind = doc_type.strip().upper() if isinstance(doc_type, str) else ""
    if kind not in _PIN_DOC_TYPES:
        raise ValueError(f"doc_type debe ser uno de {_PIN_DOC_TYPES}")
    digits = doc_number.strip() if isinstance(doc_number, str) else ""
    if not digits or re.fullmatch(r"[0-9]+", digits) is None:
        raise ValueError("doc_number debe contener solo digitos")
    expected = _PIN_DOC_LENGTHS[kind]
    if len(digits) != expected:
        raise ValueError(f"doc_number: {kind} debe tener {expected} digitos")
    return kind, digits


def _audit_pin_reset(session: Session, *, user_id: uuid.UUID, ip: str | None) -> None:
    """Registra `auth.pin_reset` via fachada (best-effort como `pin_login`).

    Nunca `commit` (solo `flush` via la fachada). Sin PII: solo IDs
    (`user_id`, `ip`, `at`, `result`); jamas el email, el DNI, el OTP ni el
    PIN o su hash.
    """
    try:
        from app.modules.audit.service import record as audit_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("pin_reset audit no disponible")
        return
    try:
        audit_record(
            session,
            actor=user_id,
            action=AUDIT_PIN_RESET,
            entity=USER_AGGREGATE_TYPE,
            entity_id=user_id,
            metadata={"result": "ok", "at": _utcnow().isoformat()},
            ip=ip,
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning("pin_reset audit no registrado error=%s", type(exc).__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def reset_pin(
    session: Session,
    *,
    email: str,
    doc_number: str,
    code: str,
    pin: str,
    doc_type: str = "DNI",
    ip: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Fija el PIN con email+DNI/RUC+OTP `RECOVERY` (`flush`, sin `commit`).

    Exito: consume el OTP (un solo uso), fija `pin_hash`, resetea
    `failed_attempts`/`locked_until`, registra `access_recovery` con
    `new_credential_set=true` y audita; retorna `{"user_ref", "pin_set":
    True}`. Fallos: formato de PIN o de documento (`doc_type`/longitud)
    debil -> `ValueError` (422, antes de tocar estado); ventana por
    `email+IP` excedida -> `RecoveryRateLimitedError` (429, antes de la
    existencia); cualquier otra combinacion invalida ->
    `PinResetInvalidError` (401 generico).

    E1-T40: `doc_type` gobierna SOLO la validacion de formato/longitud
    (DNI 8 / RUC 11 digitos); la resolucion sigue siendo `email`
    normalizado + `doc_number_hash` HMAC comparado en tiempo constante
    (NO se cruza con `users.doc_type`, para no crear un oraculo ni cambiar
    el contrato anti-enumeracion).
    """
    _validate_pin_format(pin)
    _, doc_number = _validate_doc_format(doc_type, doc_number)
    normalized = recovery_service.normalize_email(email)
    recovery_service.check_pin_reset_rate_limit(normalized, ip, session)
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()

    user = identity_repo.get_by_email(session, normalized)
    if user is None:
        # Rama ciega: se consulta el OTP pendiente (descartado) para igualar
        # el patron de acceso a BD antes de responder generico.
        identity_repo.get_active_otp(session, uuid.uuid4(), RESET_PURPOSE)
        logger.info("pin_reset result=%s", "invalid")
        raise PinResetInvalidError(INVALID_MESSAGE)
    if user.status != "ACTIVE":
        logger.info("pin_reset result=%s", "invalid")
        raise PinResetInvalidError(INVALID_MESSAGE)

    try:
        candidate_hash = hash_document_number(doc_number)
    except (ValueError, TypeError):
        # Documento vacio/invalido: generico, sin distinguir del resto.
        logger.info("pin_reset result=%s", "invalid")
        raise PinResetInvalidError(INVALID_MESSAGE) from None
    stored_hash = user.doc_number_hash or ""
    if not stored_hash or not hmac.compare_digest(candidate_hash, stored_hash):
        logger.info("pin_reset result=%s", "invalid")
        raise PinResetInvalidError(INVALID_MESSAGE)

    credential = identity_repo.get_credential(session, user.id)
    if credential is None:
        # Sin credencial no hay donde fijar el PIN: generico SIN quemar el
        # OTP (orden anti-oraculo: elegibilidad completa antes de consumir).
        logger.info("pin_reset result=%s", "invalid")
        raise PinResetInvalidError(INVALID_MESSAGE)

    try:
        otp_service.validate_otp(session, user_id=user.id, purpose=RESET_PURPOSE, code=code)
    except otp_service.OtpExpiredError as exc:
        # Colapso anti-oraculo: vencido -> mismo 401 generico (la causa
        # real solo queda en el log interno).
        logger.info("pin_reset result=%s cause=%s", "invalid", "expired")
        raise PinResetInvalidError(INVALID_MESSAGE) from exc
    except otp_service.OtpAttemptsExceededError as exc:
        # Colapso anti-oraculo: bloqueado por intentos -> mismo 401
        # generico (la fila queda `EXPIRED`: el bloqueo persiste aunque la
        # respuesta no lo anuncie).
        logger.info("pin_reset result=%s cause=%s", "invalid", "locked")
        raise PinResetInvalidError(INVALID_MESSAGE) from exc
    except (otp_service.OtpError, ValueError) as exc:
        # `OtpNotFound`/`OtpInvalid` + codigo malformado: generico.
        logger.info("pin_reset result=%s", "invalid")
        raise PinResetInvalidError(INVALID_MESSAGE) from exc

    credential.pin_hash = pin_login_service.hash_pin(pin)
    credential.failed_attempts = 0
    credential.locked_until = None
    session.flush()
    recovery_repo.record_access_recovery(
        session,
        user.id,
        method=RESET_METHOD,
        verification_result={"result": "ok", "at": moment.isoformat()},
        device_id=None,
        new_credential_set=True,
        notified_channels=[recovery_service.EMAIL_CHANNEL],
    )
    _audit_pin_reset(session, user_id=user.id, ip=ip)
    logger.info("pin_reset result=%s", "ok")
    return {"user_ref": str(user.id), "pin_set": True}


__all__ = [
    "AUDIT_PIN_RESET",
    "INVALID_MESSAGE",
    "RATE_LIMIT_MESSAGE",
    "RESET_METHOD",
    "RESET_PURPOSE",
    "USER_AGGREGATE_TYPE",
    "PinResetInvalidError",
    "reset_pin",
]
