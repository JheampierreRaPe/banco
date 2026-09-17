"""Login con PIN, intentos y bloqueo temporal (`login_with_pin`; E1-T14, HU03 CA-02/CA-03).

Flujo (`POST /auth/login/pin {user_ref[, device_id/device_info/ip], pin}`):

1. Resuelve `user_ref` como UUID (malformado o inexistente -> generico
   `PinInvalidError`, sin filtrar existencia: misma rama que PIN erroneo).
2. Si la credencial esta bloqueada (`locked_until` futuro) -> `PinLockedError`
   (`ACCOUNT_LOCKED`, sin exponer cuanto falta ni intentos restantes).
   Un bloqueo ya vencido se limpia solo (desbloqueo automatico por tiempo)
   y el intento sigue su curso normal.
3. Verifica el PIN contra `credentials.pin_hash` con comparacion en tiempo
   constante (`hmac.compare_digest`). Si el usuario no existe o no tiene PIN,
   se verifica contra un hash ficticio (`_DUMMY_HASH`) para que el tiempo de
   respuesta no filtre existencia.
4. Exito: resetea `failed_attempts`/`locked_until` y emite tokens + sesion
   con el MISMO mecanismo que `service/device_login` (JWT via
   `create_access_token` + refresh opaco cuyo hash se persiste + evento
   `auth.login_succeeded` via outbox; constantes reutilizadas, no
   reinventadas). Fallo: incrementa `failed_attempts`; al alcanzar
   `MAX_FAILED_ATTEMPTS` fija `locked_until` y notifica `login_alert`
   best-effort (import perezoso como `otp_service`, sin PII en logs).
5. El PIN jamas se guarda en claro ni sale en logs; solo su hash PBKDF2.

DECISION CRIPTOGRAFICA (documentada, verificada en el `.venv`):

- `app/core/security.py` solo trae JWT (`create_access_token`): no hay
  hasher de PIN/contrasena en el repo.
- El `.venv` trae `PyJWT` pero NO `bcrypt`/`argon2`/`passlib`/`cryptography`
  (verificado con `importlib.util.find_spec` el 2026-09-17): no hay
  Argon2/bcrypt usable hoy, aunque `models.Credential` los mencione como
  aspiracion en su docstring.
- Hasher usado: **PBKDF2-HMAC-SHA256 via `hashlib`** (stdlib, sin
  dependencias nuevas), `210_000` iteraciones, salt de 16 bytes, formato
  `"pbkdf2-sha256$<iter>$<salt_hex>$<hash_hex>"`. Verificacion siempre con
  `hmac.compare_digest`. Cuando `bcrypt`/`argon2` entren al `.venv`, los
  hashes nuevos podran migrarse sin cambiar el contrato (el formato se
  autodetecta por prefijo; formatos desconocidos -> `False` generico).

REGLAS CONFIGURABLES (regla de oro 6): `MAX_FAILED_ATTEMPTS = 5` y
`LOCKOUT_SECONDS = 900` (15 min) son candidatas a `config.parameters`
(`auth.max_failed_attempts`, `auth.lockout_seconds`, claves ya previstas en
`docs/03b#config.parameters`); aun no existe infraestructura de parametros
en el repo (mismo patron que `device_login.REFRESH_TTL_SECONDS` y
`domain/nonce.py`: constante documentada + misma clave).

NO FILTRACION (decisiones):

- Usuario inexistente vs PIN erroneo: mismo codigo (`INVALID_CREDENTIALS`),
  mismo mensaje, mismo cuerpo (incluido `request_id` si el cliente lo fija)
  y mismo trabajo criptografico (una verificacion PBKDF2 en ambas ramas).
- `ACCOUNT_LOCKED` solo se devuelve para usuarios existentes bloqueados
  (revela que la cuenta existe, pero es inherente al aviso de bloqueo que
  exige HU03 CA-03; no se expone `locked_until`, intentos restantes ni
  precision quirurgica del tiempo que falta).
- El PIN viaja como texto libre (`1..128`): el esquema no valida formato de
  PIN (longitud patron) para no crear un oraculo 422-vs-401.

Convencion: `flush` sin `commit`; quien llama decide la transaccion.
Sin `float`, sin secretos/PIN en claro, sin PII en logs (solo contadores).
No toca `onboard_customer`, `kyc_proxy`, `otp_service` ni `activation`
(solo reutiliza el JWT de `core.security`, el repositorio `identity` y las
constantes de emision de `device_login`).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.modules.identity import repository as identity_repo
from app.modules.identity.service import device_login as device_login_service

logger = logging.getLogger(__name__)

#: Fallos consecutivos que disparan el bloqueo (candidato a
#: `config.parameters: auth.max_failed_attempts`, ya previsto en `03b`).
MAX_FAILED_ATTEMPTS = 5

#: Duracion del bloqueo temporal en segundos (candidato a
#: `config.parameters: auth.lockout_seconds`).
LOCKOUT_SECONDS = 900

#: Iteraciones PBKDF2-HMAC-SHA256 para el hash del PIN.
PBKDF2_ITERATIONS = 210_000

#: Prefijo de formato del hash del PIN (autodeteccion por prefijo).
PIN_HASH_PREFIX = "pbkdf2-sha256"

#: Mensaje generico estable (identico exista o no el usuario o el PIN).
INVALID_MESSAGE = "Credenciales invalidas"

#: Mensaje de bloqueo (sin precision del tiempo restante, sin contadores).
LOCKED_MESSAGE = "Cuenta bloqueada temporalmente por intentos fallidos"

#: Acciones de auditoria E1-T17 (exito reutiliza el evento de device_login).
AUDIT_LOGIN_SUCCEEDED = device_login_service.LOGIN_SUCCEEDED_EVENT
AUDIT_FAILED_ATTEMPT = "auth.failed_attempt"

#: Plantilla de la notificacion de bloqueo (canal `push`, la unica de login).
LOCK_TEMPLATE_CODE = "login_alert"
LOCK_CHANNEL = "push"


class PinInvalidError(ValueError):
    """PIN erroneo, usuario inexistente, `user_ref` malformado o credencial
    sin PIN: una sola clase/mensaje para no filtrar (-> 401
    `INVALID_CREDENTIALS`)."""


class PinLockedError(ValueError):
    """Cuenta bloqueada por intentos fallidos (-> 423 `ACCOUNT_LOCKED`)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _coerce_user_id(user_ref: object) -> uuid.UUID | None:
    """Interpreta `user_ref` como UUID (`None` si malformado, sin excepcion)."""
    if isinstance(user_ref, uuid.UUID):
        return user_ref
    try:
        return uuid.UUID(str(user_ref).strip())
    except (ValueError, AttributeError, TypeError):
        return None


def hash_pin(pin: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Deriva el hash almacenable de un PIN (PBKDF2-HMAC-SHA256).

    Formato: `"pbkdf2-sha256$<iter>$<salt_hex>$<hash_hex>"`. Nunca devuelve
    ni loguea el PIN en claro.
    """
    if not isinstance(pin, str) or not pin:
        raise ValueError("pin es obligatorio")
    if not isinstance(iterations, int) or iterations < 1:
        raise ValueError("iterations debe ser >= 1")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)
    return f"{PIN_HASH_PREFIX}${iterations}${salt.hex()}${digest.hex()}"


def verify_pin(pin: object, stored_hash: object) -> bool:
    """Verifica un PIN contra su hash en tiempo constante (`False` ante
    cualquier formato desconocido o entrada malformada: sin excepciones)."""
    if not isinstance(pin, str) or not pin:
        return False
    if not isinstance(stored_hash, str):
        return False
    try:
        prefix, iter_text, salt_hex, hash_hex = stored_hash.split("$")
    except ValueError:
        return False
    if prefix != PIN_HASH_PREFIX:
        return False
    try:
        iterations = int(iter_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    if iterations < 1 or len(salt) < 8 or len(expected) != 32:
        return False
    try:
        candidate = hashlib.pbkdf2_hmac(
            "sha256", pin.encode("utf-8"), salt, iterations
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(candidate, expected)


#: Hash ficticio para la rama ciega (usuario inexistente o sin PIN): se
#: verifica contra el para igualar el costo criptografico y no filtrar por
#: timing. Se genera una vez por proceso (el salt aleatorio no importa: solo
#: se usa para comparar y fallar en tiempo constante).
_DUMMY_HASH = hash_pin(secrets.token_hex(8))


def _notify_lock(
    session: Session,
    *,
    user_id: uuid.UUID,
    recipient: str,
    moment: datetime,
    sender=None,
) -> None:
    """Notifica el bloqueo via `login_alert` (best-effort, import perezoso).

    Sigue el patron de `otp_service._notify`: si falta canal/destino o el
    envio falla, se loguea sin PII y NO se revierte el bloqueo.
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
            "pin_login lock notify_failed channel=%s template=%s error=%s",
            LOCK_CHANNEL,
            LOCK_TEMPLATE_CODE,
            type(exc).__name__,
        )


def _emit_login_succeeded(
    session: Session, *, user_id: uuid.UUID, session_id: uuid.UUID, device_id: str | None
) -> None:
    """Enlista `auth.login_succeeded` via outbox (best-effort, no bloquea).

    Mismo evento y mecanismo que `device_login` (regla de oro 8, import
    perezoso como E1-T03): si el outbox fallara se loguea y el login igual
    se retorna, la sesion ya quedo persistida con `flush`.
    """
    try:
        from app.core.outbox import record as outbox_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("pin_login outbox no disponible: evento no enlistado")
        return
    try:
        outbox_record(
            session,
            aggregate_type=device_login_service.USER_AGGREGATE_TYPE,
            aggregate_id=user_id,
            event_type=device_login_service.LOGIN_SUCCEEDED_EVENT,
            payload={
                "user_id": str(user_id),
                "session_id": str(session_id),
                "device_id": device_id,
            },
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning("pin_login evento no enlistado error=%s", type(exc).__name__)


def _audit_auth_event(
    session: Session,
    *,
    user_id: uuid.UUID | None,
    device_id: str | None,
    ip: str | None,
    moment: datetime,
    action: str,
    session_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> None:
    """Registra `auth.login_succeeded` / `auth.failed_attempt` via fachada.

    E1-T17 (best-effort como E1-T03): si la auditoria falla se loguea y el
    login igual se retorna; nunca `commit` (solo `flush` via la fachada).
    Sin PII: solo IDs (`user_id`, `session_id`, `device_id`, `ip`, `at`,
    `method=pin`, `reason`); jamas el PIN ni su hash.
    """
    try:
        from app.modules.audit.service import record as audit_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("pin_login audit no disponible")
        return
    try:
        metadata: dict = {"method": "pin", "at": moment.isoformat()}
        if session_id is not None:
            metadata["session_id"] = str(session_id)
        if reason is not None:
            metadata["reason"] = reason
        audit_record(
            session,
            actor=user_id,
            action=action,
            entity=device_login_service.USER_AGGREGATE_TYPE,
            entity_id=user_id,
            metadata=metadata,
            device_id=device_id,
            ip=ip,
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 - best-effort documentado E1-T17
        logger.warning("pin_login audit no registrado error=%s", type(exc).__name__)


def login_with_pin(
    session: Session,
    *,
    user_ref: str,
    pin: str,
    device_id: str | None = None,
    device_info: dict | None = None,
    ip: str | None = None,
    now: datetime | None = None,
    notify_sender=None,
) -> dict:
    """Verifica el PIN y abre sesion (`flush`, sin `commit`).

    Exito: resetea `failed_attempts`/`locked_until`, crea la fila en
    `sessions` (refresh opaco, solo su hash se persiste), devuelve JWT corto
    (reutiliza `create_access_token`) + refresh + `session_id`, y enlista
    `auth.login_succeeded`. Fallo: incrementa `failed_attempts`; al llegar a
    `MAX_FAILED_ATTEMPTS` fija `locked_until = now + LOCKOUT_SECONDS`,
    notifica `login_alert` best-effort y responde `PinLockedError` (el 5to
    fallo ya es bloqueo). Un bloqueo vencido se limpia solo antes de
    verificar (desbloqueo automatico por tiempo).
    """
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()

    uid = _coerce_user_id(user_ref)
    user = identity_repo.get_user(session, uid) if uid is not None else None
    credential = (
        identity_repo.get_credential(session, user.id) if user is not None else None
    )

    if credential is not None and credential.locked_until is not None:
        locked_until = _as_aware(credential.locked_until)
        if locked_until > moment:
            # Bloqueo vigente: gana al PIN correcto (no se verifica nada).
            _audit_auth_event(
                session,
                user_id=user.id,
                device_id=device_id,
                ip=ip,
                moment=moment,
                action=AUDIT_FAILED_ATTEMPT,
                reason="locked",
            )
            raise PinLockedError(LOCKED_MESSAGE)
        # Bloqueo vencido: desbloqueo automatico por tiempo.
        credential.failed_attempts = 0
        credential.locked_until = None
        session.flush()

    stored = (
        credential.pin_hash
        if credential is not None and credential.pin_hash
        else None
    )
    if stored is None:
        # Rama ciega: mismo costo PBKDF2 que la rama real (no filtra por
        # timing) y mismo error que un PIN erroneo.
        verify_pin(pin if isinstance(pin, str) else "", _DUMMY_HASH)
        _audit_auth_event(
            session,
            user_id=user.id if user is not None else uid,
            device_id=device_id,
            ip=ip,
            moment=moment,
            action=AUDIT_FAILED_ATTEMPT,
            reason="invalid",
        )
        raise PinInvalidError(INVALID_MESSAGE)

    if verify_pin(pin, stored):
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
        _emit_login_succeeded(
            session, user_id=user.id, session_id=row.id, device_id=device_id
        )
        _audit_auth_event(
            session,
            user_id=user.id,
            device_id=device_id,
            ip=ip,
            moment=moment,
            action=AUDIT_LOGIN_SUCCEEDED,
            session_id=row.id,
        )
        logger.info("pin_login ok")
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
        }

    attempts = int(credential.failed_attempts or 0) + 1
    credential.failed_attempts = attempts
    if attempts >= MAX_FAILED_ATTEMPTS:
        credential.locked_until = moment + timedelta(seconds=LOCKOUT_SECONDS)
        session.flush()
        recipient = None
        if user is not None:
            recipient = user.phone or user.email or str(user.id)
        _notify_lock(
            session,
            user_id=user.id if user is not None else uid,
            recipient=recipient or "",
            moment=moment,
            sender=notify_sender,
        )
        _audit_auth_event(
            session,
            user_id=user.id if user is not None else uid,
            device_id=device_id,
            ip=ip,
            moment=moment,
            action=AUDIT_FAILED_ATTEMPT,
            reason="locked",
        )
        logger.info("pin_login bloqueado")
        raise PinLockedError(LOCKED_MESSAGE)
    session.flush()
    _audit_auth_event(
        session,
        user_id=user.id if user is not None else uid,
        device_id=device_id,
        ip=ip,
        moment=moment,
        action=AUDIT_FAILED_ATTEMPT,
        reason="invalid",
    )
    logger.info("pin_login fallo")
    raise PinInvalidError(INVALID_MESSAGE)


__all__ = [
    "AUDIT_FAILED_ATTEMPT",
    "AUDIT_LOGIN_SUCCEEDED",
    "INVALID_MESSAGE",
    "LOCK_CHANNEL",
    "LOCK_TEMPLATE_CODE",
    "LOCKOUT_SECONDS",
    "MAX_FAILED_ATTEMPTS",
    "PBKDF2_ITERATIONS",
    "PIN_HASH_PREFIX",
    "PinInvalidError",
    "PinLockedError",
    "hash_pin",
    "login_with_pin",
    "verify_pin",
]
