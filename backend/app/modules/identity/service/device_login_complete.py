"""Paso 2 del login en dispositivo nuevo: OTP `LOGIN` + PIN -> binding + sesion.

(E1-T46, HU03; consumido por `F-T56`/`F-T57`.)

Hueco que cierra (decision del dueno, cerrada): cuando NO hay `userRef` en el
dispositivo, el paso 1 (`POST /auth/login/device/request`, E1-T45) emite el OTP
`LOGIN` tras verificar que `email + documento` son de la MISMA cuenta; este paso
2 valida OTP + PIN en la MISMA transaccion, asocia el dispositivo (binding) y
RECIEN entonces abre sesion. `pin-reset` NO sirve de base: fuerza cambio de PIN,
consume OTP `RECOVERY` y no crea binding ni sesion.

Flujo (`POST /auth/login/device/complete`):

1. Valida el formato del PIN (`4-6` digitos, patron `pin_reset`) y del documento
   (`doc_type in {DNI,RUC}`, longitud exacta; `ValueError` -> 422) ANTES de tocar
   estado: un input inutilizable no debe quemar un codigo de un solo uso ni mover
   contadores. No crea oraculo: el 422 solo habla del formato del propio input,
   nunca del estado de la cuenta.
2. Rate-limit por `email+IP` ANTES de resolver existencia (misma ventana
   compartida del paso 1: `auth.recovery_verify_*` via
   `recovery._check_rate_limit` / `recovery._build_recovery_rate_key` con scope
   `"device_login"`; ver decision abajo): excederla -> `RecoveryRateLimitedError`
   (429, sin filtrar). Compartir la ventana con la solicitud evita zurrar el OTP
   con intentos ilimitados de `complete`.
3. Resuelve el usuario por email normalizado; si no existe, `status != "ACTIVE"`
   o sin email -> rama ciega + `DeviceLoginInvalidError` (401 generico). La rama
   ciega hace trabajo equivalente (lectura OTP descartada con `uuid4`, hash del
   documento ignorado y verificacion PBKDF2 contra hash ficticio) para no filtrar
   por timing ni por patron de acceso.
4. Verifica que `email` y documento son de la MISMA cuenta con
   `hash_document_number` + `hmac.compare_digest` (mismo patron exacto que
   `service/pin_reset.py:222-231`); mismatch -> mismo 401 generico.
5. Exige credencial existente; sin ella -> mismo 401 (con verificacion ficticia
   para igualar costo).
6. **Lockout primero** (patron `login_with_pin`): si `locked_until` vigente ->
   `DeviceLoginLockedError` (423 `ACCOUNT_LOCKED`, sin precision del tiempo);
   si vencio, se limpia solo y el intento sigue su curso.
7. **Atomico, PIN primero:** se verifica el PIN con `pin_login.verify_pin`
   (PBKDF2, `hmac.compare_digest`); si falla, se incrementa `failed_attempts`
   (al 5to se fija `locked_until`, se notifica `login_alert` best-effort y el
   fallo ya es 423) y se responde 401 generico SIN tocar el OTP. Solo con el PIN
   correcto se valida el OTP con `otp_service.validate_otp(..., purpose="LOGIN")`
   (un solo uso, TTL, tope de intentos por fila); cualquier fallo de OTP
   (inexistente/usado/vencido/bloqueado/incorrecto) colapsa al MISMO 401 (el log
   interno conserva la causa real).
8. En exito, misma transaccion: resetea `failed_attempts`/`locked_until`, crea
   la sesion (refresh opaco, solo su hash se persiste) + JWT corto via
   `create_access_token`, registra/actualiza el binding best-effort en savepoint
   (un fallo NO revierte la sesion), enlista `auth.login_succeeded` via outbox y
   audita. Retorna tokens + `user_ref` + `biometric_enabled`.

ORDEN OTP/PIN (decision documentada): PIN primero, OTP despues. Motivo:
`validate_otp` marca `USED` al validar (un solo uso, con `flush` inmediato); si
el OTP se validara primero, un OTP valido con PIN erroneo quedaria consumido y
el usuario legitimo perderia su codigo. Verificando el PIN primero, un OTP
valido con PIN erroneo NO se consume (sigue `PENDING`, intentos OTP intactos) y
el fallo solo mueve el contador de lockout del PIN. La contrapartida (PIN
correcto + OTP erroneo) avanza los intentos OTP via `validate_otp` y colapsa al
mismo 401: ambos contadores persisten via el `commit` del router en errores de
negocio (patron `api/pin_login.py` / `api/pin_reset.py`).

BINDING `REVOKED` (decision del dueno, E1-T42, aplicada): si ya existe binding
para (`user_id`, `device_id`) se reescribe su `public_key` con la clave vigente
+ se refresca `last_used_at`, PRESERVANDO `status` (un `REVOKED` NO se reactiva;
   no se usa `register_binding` tal cual porque fuerza `ACTIVE`); si no existe,
   `register_binding` (alta `ACTIVE`). **La sesion se abre igual aunque el
   binding este `REVOKED`**: el paso 2 autentica a la persona (OTP + PIN), no al
   dispositivo; revivir o no el binding es decision aparte y el error/salida no
   revela el estado (misma respuesta 200). La clave se persiste tal cual
   (compatibilidad `hmac:`/PEM, sin validar formato).

Decision de rate-limit (E1-T46, regla de oro 6): NO se crean claves nuevas en
`config.parameters`; se REUTILIZA la ventana de verificacion
`auth.recovery_verify_window_seconds` / `auth.recovery_verify_max_requests`
(semillas `60`/`10`, modulo `identity`) con el MISMO scope del paso 1
(`"device_login"`) en la clave `email+IP` en memoria: `request` y `complete`
comparten el tope de verificaciones por `email+IP` (el paso 2 tambien consume
verificaciones del OTP). Prod: Redis/middleware (misma clave).

Sin enumeracion (regla dura): un unico 401 `INVALID_LOGIN` colapsa cuenta
inexistente, no elegible, documento que no coincide, sin credencial, OTP
invalido/vencido/bloqueado y PIN erroneo (mismo cuerpo, sin sesion). `423`
`ACCOUNT_LOCKED` es inherente al aviso de bloqueo (igual que `login/pin`), sin
precision del tiempo restante. Comparacion del documento en tiempo constante.

Convencion: `flush` sin `commit`; el endpoint confirma (`commit`) en exito Y
ante error de negocio tipado (los contadores de lockout/OTP DEBEN persistir) y
revierte (`rollback`) ante error de formato (422, antes de tocar estado) o fallo
inesperado. Sin `float`, sin PII/secretos en logs (solo `result`/`cause` y
contadores; jamas email, documento, OTP, PIN, tokens ni `device_public_key`). No
toca `onboard_customer`, `kyc_proxy`, `otp_service` ni `activation` (solo los
consume via import); reutiliza el JWT de `core.security`, el repositorio
`identity` y las constantes de emision de `device_login`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.modules.identity import repository as identity_repo
from app.modules.identity.service import device_login as device_login_service
from app.modules.identity.service import otp_service
from app.modules.identity.service import pin_login as pin_login_service
from app.modules.identity.service import recovery as recovery_service
from app.modules.identity.service.kyc_onboarding import hash_document_number

logger = logging.getLogger(__name__)

#: Proposito OTP de este flujo (ya incluido en `OTP_PURPOSES`, sin migracion;
#: lo emite el paso 1, E1-T45).
LOGIN_PURPOSE = "LOGIN"

#: Mensaje generico estable (identico para cuenta inexistente/no elegible,
#: documento que no coincide, sin credencial, OTP invalido/vencido/bloqueado y
#: PIN erroneo: sin enumeracion ni oraculo).
INVALID_MESSAGE = "Datos de acceso invalidos"

#: Mensaje de bloqueo (sin precision del tiempo restante, sin contadores).
LOCKED_MESSAGE = "Cuenta bloqueada temporalmente por intentos fallidos"

#: Accion de auditoria del flujo (best-effort via fachada, sin PII; el paso 1
#: usa `auth.device_login_requested`).
AUDIT_DEVICE_LOGIN = "auth.device_login"

#: Accion de auditoria del alta/uso del binding (misma que `pin_login`, E1-T27).
AUDIT_DEVICE_BINDING = pin_login_service.AUDIT_DEVICE_BINDING

#: Tipo de agregado para la auditoria/outbox (el mismo que `pin_login`).
USER_AGGREGATE_TYPE = device_login_service.USER_AGGREGATE_TYPE

#: Evento de sesion abierta (via outbox, ya existente; regla de oro 8).
LOGIN_SUCCEEDED_EVENT = device_login_service.LOGIN_SUCCEEDED_EVENT

#: Plantilla/canal de la notificacion de bloqueo (los mismos de `pin_login`).
LOCK_TEMPLATE_CODE = pin_login_service.LOCK_TEMPLATE_CODE
LOCK_CHANNEL = pin_login_service.LOCK_CHANNEL

#: Formato del PIN: 4-6 digitos numericos (patron `pin_reset`; ver decision en
#: el docstring del modulo y en `schemas/device_login_complete.py`).
_PIN_RE = re.compile(r"^\d{4,6}$")

#: Tipos de documento aceptados (paridad con el paso 1 E1-T45 y `pin_reset`
#: E1-T40; gobiernan SOLO la validacion de formato/longitud).
_PIN_DOC_TYPES: tuple[str, ...] = ("DNI", "RUC")

#: Longitudes exactas por tipo (solo digitos).
_PIN_DOC_LENGTHS: dict[str, int] = {"DNI": 8, "RUC": 11}

#: Scope del rate-limit en la clave `email+IP`: el MISMO del paso 1 (E1-T45)
#: para compartir la ventana `auth.recovery_verify_*` (sin claves nuevas).
_RATE_SCOPE = "device_login"


class DeviceLoginInvalidError(ValueError):
    """Cuenta inexistente/no elegible, documento que no coincide, sin credencial,
    OTP invalido/vencido/bloqueado o PIN erroneo: una sola clase/mensaje para no
    filtrar (-> 401 `INVALID_LOGIN`)."""


class DeviceLoginLockedError(ValueError):
    """Cuenta bloqueada por intentos fallidos de PIN (-> 423 `ACCOUNT_LOCKED`)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validate_pin_format(pin: object) -> str:
    """Exige PIN de 4-6 digitos numericos (`ValueError` -> 422 generico)."""
    if not isinstance(pin, str) or not _PIN_RE.match(pin):
        raise ValueError("pin debe ser de 4 a 6 digitos numericos")
    return pin


def _validate_doc_format(doc_type: object, document_number: object) -> tuple[str, str]:
    """Exige `doc_type in {DNI,RUC}` y documento solo digitos exactos.

    Validacion defensiva (paridad con el paso 1 E1-T45 y `pin_reset` E1-T40): el
    esquema ya la aplica en HTTP, pero los llamados directos al servicio tambien
    quedan cubiertos. `ValueError` -> 422 en el router, ANTES de tocar estado
    (sin quemar el OTP). Retorna la forma canonica (`doc_type` en mayusculas,
    documento con trim).
    """
    kind = doc_type.strip().upper() if isinstance(doc_type, str) else ""
    if kind not in _PIN_DOC_TYPES:
        raise ValueError(f"doc_type debe ser uno de {_PIN_DOC_TYPES}")
    digits = document_number.strip() if isinstance(document_number, str) else ""
    if not digits or re.fullmatch(r"[0-9]+", digits) is None:
        raise ValueError("document_number debe contener solo digitos")
    expected = _PIN_DOC_LENGTHS[kind]
    if len(digits) != expected:
        raise ValueError(f"document_number: {kind} debe tener {expected} digitos")
    return kind, digits


def _resolve_max_attempts(session: Session) -> int:
    """Umbral de intentos: `auth.max_failed_attempts` > constante de `pin_login`.

    Regla de oro 6 (misma lectura best-effort que `pin_login._resolve_max_attempts`,
    via `otp_service.read_int_parameter`, que es publica): sin acceso o valor
    invalido rige `pin_login.MAX_FAILED_ATTEMPTS`.
    """
    return otp_service.read_int_parameter(
        session, "auth.max_failed_attempts", pin_login_service.MAX_FAILED_ATTEMPTS
    )


def _resolve_lockout_seconds(session: Session) -> int:
    """Duracion del bloqueo: `auth.lockout_seconds` > constante de `pin_login`."""
    return otp_service.read_int_parameter(
        session, "auth.lockout_seconds", pin_login_service.LOCKOUT_SECONDS
    )


#: Hash ficticio para la rama ciega (mismo patron que `pin_login._DUMMY_HASH`:
#: se verifica contra el para igualar el costo PBKDF2 y no filtrar por timing).
_DUMMY_HASH = pin_login_service.hash_pin(secrets.token_hex(8))


def _blind_invalid(
    session: Session,
    *,
    pin: object,
    document_number: object = None,
) -> None:
    """Trabajo equivalente de la rama ciega + 401 generico (sin filtrar).

    Consulta el OTP pendiente con un `user_id` aleatorio (descartada), hashea el
    documento (ignorado) y verifica el PIN contra el hash ficticio (descartada)
    para que el tiempo/patron de acceso no revele si la cuenta existe. Siempre
    lanza `DeviceLoginInvalidError`; nunca `commit` aqui (el router confirma).
    """
    identity_repo.get_active_otp(session, uuid.uuid4(), LOGIN_PURPOSE)
    try:
        if isinstance(document_number, str) and document_number.strip():
            candidate = hash_document_number(document_number.strip())
            hmac.compare_digest(candidate, hashlib.sha256(b"blind").hexdigest())
    except (ValueError, TypeError):
        pass
    pin_login_service.verify_pin(pin if isinstance(pin, str) else "", _DUMMY_HASH)
    logger.info("device_login_complete result=%s", "invalid")
    raise DeviceLoginInvalidError(INVALID_MESSAGE)


def _notify_lock(
    session: Session,
    *,
    user_id: uuid.UUID,
    recipient: str,
    moment: datetime,
    sender=None,
) -> None:
    """Notifica el bloqueo via `login_alert` (best-effort, import perezoso).

    Mismo patron que `pin_login._notify_lock`: si falta canal/destino o el envio
    falla, se loguea sin PII y NO se revierte el bloqueo.
    """
    if not recipient or not str(recipient).strip():
        return
    try:
        from app.modules.notifications.service import send as notifications_send

        notifications_send(
            session,
            channel=LOCK_CHANNEL,
            recipient=str(recipient).strip(),
            template_code=LOCK_TEMPLATE_CODE,
            data={"device": "canal PIN", "at": moment.isoformat()},
            user_id=user_id,
            sender=sender,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning(
            "device_login_complete lock notify_failed channel=%s template=%s error=%s",
            LOCK_CHANNEL,
            LOCK_TEMPLATE_CODE,
            type(exc).__name__,
        )


def _emit_login_succeeded(
    session: Session, *, user_id: uuid.UUID, session_id: uuid.UUID, device_id: str | None
) -> None:
    """Enlista `auth.login_succeeded` via outbox (best-effort, no bloquea).

    Mismo evento y mecanismo que `pin_login`/`device_login` (regla de oro 8,
    import perezoso): si el outbox fallara se loguea y el login igual se
    retorna, la sesion ya quedo persistida con `flush`.
    """
    try:
        from app.core.outbox import record as outbox_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("device_login_complete outbox no disponible: evento no enlistado")
        return
    try:
        outbox_record(
            session,
            aggregate_type=USER_AGGREGATE_TYPE,
            aggregate_id=user_id,
            event_type=LOGIN_SUCCEEDED_EVENT,
            payload={
                "user_id": str(user_id),
                "session_id": str(session_id),
                "device_id": device_id,
            },
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning("device_login_complete evento no enlistado error=%s", type(exc).__name__)


def _audit_device_login_event(
    session: Session,
    *,
    user_id: uuid.UUID | None,
    device_id: str | None,
    ip: str | None,
    moment: datetime,
    result: str,
    session_id: uuid.UUID | None = None,
) -> None:
    """Registra `auth.device_login` via fachada (best-effort, sin PII).

    Mismo patron que `pin_login._audit_auth_event`: si la auditoria falla se
    loguea y el flujo igual continua; nunca `commit` (solo `flush` via la
    fachada). Sin PII: solo IDs (`user_id`, `session_id`, `device_id`, `ip`,
    `at`, `result`); jamas email, documento, OTP, PIN, tokens ni clave publica.
    """
    try:
        from app.modules.audit.service import record as audit_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("device_login_complete audit no disponible")
        return
    try:
        metadata: dict = {"result": result, "at": moment.isoformat()}
        if session_id is not None:
            metadata["session_id"] = str(session_id)
        audit_record(
            session,
            actor=user_id,
            action=AUDIT_DEVICE_LOGIN,
            entity=USER_AGGREGATE_TYPE,
            entity_id=user_id,
            metadata=metadata,
            device_id=device_id,
            ip=ip,
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning("device_login_complete audit no registrado error=%s", type(exc).__name__)


def _audit_binding_event(
    session: Session,
    *,
    user_id: uuid.UUID,
    device_id: str,
    moment: datetime,
    result: str,
    platform: str | None,
    biometric_type: str | None,
) -> None:
    """Audita el alta/uso del binding (patron `pin_login`, best-effort).

    Sin secretos: solo IDs y metadatos del resultado
    (`registered`/`touched`/`failed`, `platform`, `biometric_type`); jamas el
    `device_public_key`.
    """
    try:
        from app.modules.audit.service import record as audit_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("device_login_complete audit no disponible")
        return
    try:
        metadata: dict = {"result": result, "at": moment.isoformat()}
        if platform is not None:
            metadata["platform"] = platform
        if biometric_type is not None:
            metadata["biometric_type"] = biometric_type
        audit_record(
            session,
            actor=user_id,
            action=AUDIT_DEVICE_BINDING,
            entity=USER_AGGREGATE_TYPE,
            entity_id=user_id,
            metadata=metadata,
            device_id=device_id,
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning(
            "device_login_complete binding audit no registrado error=%s", type(exc).__name__
        )


def _bind_device_best_effort(
    session: Session,
    *,
    user_id: uuid.UUID,
    device_id: str,
    device_public_key: str,
    platform: str | None,
    biometric_type: str | None,
    moment: datetime,
) -> str:
    """Registra o actualiza el binding tras OTP + PIN validos (patron E1-T27/E1-T42).

    Si ya existe binding para (`user_id`, `device_id`) reescribe `public_key`
    con la clave vigente y refresca `last_used_at`, PRESERVANDO `status` (un
    `REVOKED` no se reactiva por este login: decision del dueno E1-T42; no se usa
    `register_binding` aqui porque fuerza `ACTIVE`); si no existe,
    `register_binding` (alta `ACTIVE`). La clave se persiste tal cual
    (compatibilidad `hmac:`/PEM, sin validar formato). `platform`/
    `biometric_type` invalidos se ignoran (`None`).

    Todo va en un savepoint (`begin_nested`): un fallo de persistencia se
    revierte SOLO aqui y la sesion/los tokens sobreviven (best-effort); se
    audita el resultado. Retorna `"registered"`, `"touched"` o `"failed"`.
    """
    key = device_public_key.strip() if isinstance(device_public_key, str) else ""
    safe_platform = platform if platform in identity_repo.DEVICE_PLATFORMS else None
    safe_biometric = biometric_type if biometric_type in identity_repo.BIOMETRIC_TYPES else None
    result = "failed"
    try:
        with session.begin_nested():
            existing = identity_repo.get_binding(session, user_id, device_id)
            if existing is not None:
                # Rebind con `status` preservado: un `REVOKED` no revive (E1-T42).
                existing.public_key = key
                if safe_platform is not None:
                    existing.platform = safe_platform
                if safe_biometric is not None:
                    existing.biometric_type = safe_biometric
                session.flush()
                identity_repo.touch_binding(session, existing, moment)
                result = "touched"
            else:
                row = identity_repo.register_binding(
                    session,
                    user_id,
                    device_id,
                    key,
                    platform=safe_platform,
                    biometric_type=safe_biometric,
                )
                identity_repo.touch_binding(session, row, moment)
                result = "registered"
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning(
            "device_login_complete device_binding %s error=%s", result, type(exc).__name__
        )
    _audit_binding_event(
        session,
        user_id=user_id,
        device_id=device_id,
        moment=moment,
        result=result,
        platform=safe_platform,
        biometric_type=safe_biometric,
    )
    return result


def complete_device_login(
    session: Session,
    *,
    email: str,
    doc_type: str = "DNI",
    document_number: str,
    code: str,
    pin: str,
    device_id: str,
    device_public_key: str,
    platform: str | None = None,
    biometric_type: str | None = None,
    device_info: dict | None = None,
    ip: str | None = None,
    now: datetime | None = None,
    notify_sender=None,
) -> dict:
    """Valida OTP `LOGIN` + PIN, asocia el dispositivo y abre sesion.

    `flush` sin `commit`; quien llama decide la transaccion. En exito retorna
    `{access_token, refresh_token, token_type, session_id, expires_in, user_ref,
    biometric_enabled}`. Cualquier combinacion invalida -> `DeviceLoginInvalidError`
    (401 generico); bloqueo de PIN vigente -> `DeviceLoginLockedError` (423);
    ventana por `email+IP` excedida -> `RecoveryRateLimitedError` (429);
    formato debil -> `ValueError` (422, antes de tocar estado).
    """
    _validate_pin_format(pin)
    _, doc_number = _validate_doc_format(doc_type, document_number)
    normalized = recovery_service.normalize_email(email)
    recovery_service._check_rate_limit(
        recovery_service._build_recovery_rate_key(_RATE_SCOPE, normalized, ip),
        recovery_service.recovery_verify_rate_limit_cfg(session),
    )
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()

    user = identity_repo.get_by_email(session, normalized)
    if user is None or user.status != "ACTIVE" or not user.email:
        # Rama ciega: mismo 401, sin OTP, sin filtrar existencia/estado.
        _blind_invalid(session, pin=pin, document_number=document_number)

    try:
        candidate_hash = hash_document_number(doc_number)
    except (ValueError, TypeError):
        # Documento vacio/invalido: generico, sin distinguir del resto.
        logger.info("device_login_complete result=%s", "invalid")
        raise DeviceLoginInvalidError(INVALID_MESSAGE) from None
    stored_hash = user.doc_number_hash or ""
    if not stored_hash or not hmac.compare_digest(candidate_hash, stored_hash):
        logger.info("device_login_complete result=%s", "invalid")
        raise DeviceLoginInvalidError(INVALID_MESSAGE)

    credential = identity_repo.get_credential(session, user.id)
    if credential is None:
        # Sin credencial no hay PIN que verificar: generico SIN quemar el OTP
        # (orden anti-oraculo: elegibilidad completa antes de consumir).
        pin_login_service.verify_pin(pin if isinstance(pin, str) else "", _DUMMY_HASH)
        logger.info("device_login_complete result=%s", "invalid")
        raise DeviceLoginInvalidError(INVALID_MESSAGE)

    if credential.locked_until is not None:
        locked_until = _as_aware(credential.locked_until)
        if locked_until > moment:
            # Bloqueo vigente: gana al OTP/PIN correctos (no se verifica nada).
            _audit_device_login_event(
                session,
                user_id=user.id,
                device_id=device_id,
                ip=ip,
                moment=moment,
                result="locked",
            )
            raise DeviceLoginLockedError(LOCKED_MESSAGE)
        # Bloqueo vencido: desbloqueo automatico por tiempo.
        credential.failed_attempts = 0
        credential.locked_until = None
        session.flush()

    stored = credential.pin_hash if credential.pin_hash else None
    if stored is None:
        # Credencial sin PIN: mismo costo PBKDF2 (rama ciega) y mismo 401.
        pin_login_service.verify_pin(pin if isinstance(pin, str) else "", _DUMMY_HASH)
        _audit_device_login_event(
            session,
            user_id=user.id,
            device_id=device_id,
            ip=ip,
            moment=moment,
            result="invalid",
        )
        raise DeviceLoginInvalidError(INVALID_MESSAGE)

    if not pin_login_service.verify_pin(pin, stored):
        # PIN erroneo: mueve SOLO el lockout del PIN; el OTP NO se toca (sigue
        # `PENDING`, intentos intactos). Al 5to fallo el bloqueo ya rige (423).
        attempts = int(credential.failed_attempts or 0) + 1
        credential.failed_attempts = attempts
        if attempts >= _resolve_max_attempts(session):
            credential.locked_until = moment + timedelta(seconds=_resolve_lockout_seconds(session))
            session.flush()
            recipient = user.phone or user.email or str(user.id)
            _notify_lock(
                session,
                user_id=user.id,
                recipient=recipient or "",
                moment=moment,
                sender=notify_sender,
            )
            _audit_device_login_event(
                session,
                user_id=user.id,
                device_id=device_id,
                ip=ip,
                moment=moment,
                result="locked",
            )
            logger.info("device_login_complete result=%s", "locked")
            raise DeviceLoginLockedError(LOCKED_MESSAGE)
        session.flush()
        _audit_device_login_event(
            session,
            user_id=user.id,
            device_id=device_id,
            ip=ip,
            moment=moment,
            result="invalid",
        )
        logger.info("device_login_complete result=%s", "invalid")
        raise DeviceLoginInvalidError(INVALID_MESSAGE)

    # PIN correcto: RECIEN ahora se consume el OTP (un solo uso). Cualquier
    # fallo (inexistente/usado/vencido/bloqueado/incorrecto) colapsa al mismo
    # 401; la causa real solo queda en el log interno (anti-oraculo).
    try:
        otp_service.validate_otp(session, user_id=user.id, purpose=LOGIN_PURPOSE, code=code)
    except otp_service.OtpExpiredError as exc:
        logger.info("device_login_complete result=%s cause=%s", "invalid", "expired")
        raise DeviceLoginInvalidError(INVALID_MESSAGE) from exc
    except otp_service.OtpAttemptsExceededError as exc:
        logger.info("device_login_complete result=%s cause=%s", "invalid", "locked")
        raise DeviceLoginInvalidError(INVALID_MESSAGE) from exc
    except (otp_service.OtpError, ValueError) as exc:
        logger.info("device_login_complete result=%s", "invalid")
        raise DeviceLoginInvalidError(INVALID_MESSAGE) from exc

    credential.failed_attempts = 0
    credential.locked_until = None
    session.flush()

    refresh = secrets.token_urlsafe(32)
    row = identity_repo.create_session(
        session,
        user.id,
        identity_repo.hash_refresh_token(refresh),
        moment + timedelta(seconds=device_login_service.REFRESH_TTL_SECONDS),
        device_id=device_id,
        device_info=device_info,
        ip=ip,
    )
    access_token = create_access_token(subject=str(user.id))
    _emit_login_succeeded(session, user_id=user.id, session_id=row.id, device_id=device_id)
    _audit_device_login_event(
        session,
        user_id=user.id,
        device_id=device_id,
        ip=ip,
        moment=moment,
        result="ok",
        session_id=row.id,
    )
    _bind_device_best_effort(
        session,
        user_id=user.id,
        device_id=device_id,
        device_public_key=device_public_key,
        platform=platform,
        biometric_type=biometric_type,
        moment=moment,
    )
    logger.info("device_login_complete result=%s", "ok")
    try:
        aware_exp = _as_aware(row.expires_at)
        refresh_in = max(0, int((aware_exp - moment).total_seconds()))
    except (AttributeError, TypeError):
        refresh_in = device_login_service.REFRESH_TTL_SECONDS
    return {
        "access_token": access_token,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "session_id": str(row.id),
        "expires_in": refresh_in,
        # E1-T39 (paridad con `login_with_pin`): consentimiento vigente (la
        # credencial ya se obtuvo arriba; solo en la rama de exito).
        "biometric_enabled": credential.biometric_enabled is True,
        # El cliente persiste el `userRef` con este valor (cliente delgado).
        "user_ref": str(user.id),
    }


__all__ = [
    "AUDIT_DEVICE_BINDING",
    "AUDIT_DEVICE_LOGIN",
    "INVALID_MESSAGE",
    "LOCKED_MESSAGE",
    "LOGIN_PURPOSE",
    "LOGIN_SUCCEEDED_EVENT",
    "USER_AGGREGATE_TYPE",
    "DeviceLoginInvalidError",
    "DeviceLoginLockedError",
    "complete_device_login",
]
