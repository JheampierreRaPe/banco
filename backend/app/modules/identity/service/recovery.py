"""Recuperacion de acceso por email: solicitar OTP y validar OTP sin sesion (E1-T33, HU04).

Flujo (pre-sesion, sin `user_ref` ni `device_id` locales: tras reinstalar o
borrar datos el usuario solo conoce su email registrado):

1. `POST /auth/recovery/request {email}` -> normaliza el email, aplica
   rate-limit por `email+IP` ANTES de resolver existencia, y si el usuario
   existe, esta `ACTIVE` y tiene email, emite (o reutiliza en cooldown) un
   OTP `purpose='RECOVERY'` notificado SOLO por email. Responde SIEMPRE 200
   con el MISMO cuerpo exista o no el email (sin enumeracion).
2. `POST /auth/recovery/verify {email, code[, device_id/device_public_key/
   platform/biometric_type]}` -> valida el OTP via
   `otp_service.validate_otp` (hash/TTL/max intentos, un solo uso), inserta
   `access_recovery` (`method='OTP'`, `new_credential_set=false`) y
   registra/refresca el `device_binding` del dispositivo nuevo best-effort.
   Devuelve `{user_ref, device_bound}` SIN abrir sesion (SCR-005): la unica
   sesion la abre `POST /auth/login/pin` (lo persiste `F-T29` via
   `SessionIdentityStore.userRef` + sesion activa).

Reutilizacion (sin reinventar, sin modificar lo existente):

- OTP: `otp_service.generate_otp` / `validate_otp` con `purpose='RECOVERY'`
  (`OTP_PURPOSES` ya lo incluye; `otp_codes` ya trae `code_hash`,
  `expires_at`, `attempts`/`max_attempts`, `status`). El codigo en claro
  solo existe en el retorno de `generate_otp` para notificarlo: jamas se
  persiste, loguea ni responde.
- Entrega: `activation.resolve_activation_delivery(..., phone=None,
  channel="email")` fuerza email y devuelve `None` si no hay email:
  nunca cae a SMS. Plantilla `otp_code_email` (`{code, ttl_minutes}`),
  notificada via fachada `notifications.send` best-effort (un fallo de
  envio se loguea sin PII/codigo y NO revierte la emision, como E1-T26).
- Usuario: `identity_repo.get_by_email` (email normalizado
  `strip().lower()`, como `kyc_onboarding._normalize_email`).
- Sesion/tokens: NO se emiten aqui (SCR-005, E1-T33): la unica sesion la
  abre `pin_login.login_with_pin` (`identity_repo.create_session` +
  `identity_repo.hash_refresh_token` + `create_access_token` +
  `auth.login_succeeded` via outbox viven solo ahi).
- Binding: `identity_repo.get_binding` / `touch_binding` /
  `register_binding` en savepoint best-effort (un fallo no revierte la
  sesion); `platform`/`biometric_type` invalidos se ignoran (`None`).

Sin enumeracion (regla dura): `request` responde 200 identico exista o no
el email; `verify` responde el mismo 401 generico para "email no
registrado", "no elegible", "sin OTP pendiente", "codigo incorrecto",
"OTP vencido" y "OTP bloqueado por intentos agotados" (vencido y
bloqueado colapsan al generico para no filtrar existencia; el log
interno conserva la causa real).

Orden validacion/consumo en `verify` (decision anti-oraculo): la
elegibilidad (`users.status == 'ACTIVE'`) se verifica ANTES de
`validate_otp`: un usuario no elegible responde 401 generico SIN quemar el
OTP (ni abrir sesion). La igualdad con la rama ciega (email inexistente)
es de cuerpo/codigo; el tiempo solo se aproxima (misma limitacion
documentada que `activation`: en prod, padding constante + WAF).

REGLAS CONFIGURABLES (regla de oro 6): `RECOVERY_REQUEST_*` y
`RECOVERY_VERIFY_*` (ventana/maximo del rate-limit en memoria) se leen de
`config.parameters` (E1-T34/SCR-005, migracion `0019`:
`auth.recovery_request_window_seconds` / `auth.recovery_request_max_requests`
/ `auth.recovery_verify_window_seconds` / `auth.recovery_verify_max_requests`,
semillas `60`/`10`/`60`/`10`, modulo `identity`) con lectura best-effort via
`otp_service.read_int_parameter` (SQL directo a `config.parameters` en
SAVEPOINT, fallback al valor de entorno y luego a la constante); sin acceso
o valor invalido rige el entorno/constante (comportamiento identico).
`ttl_seconds`/`resend_wait_seconds` reusan las constantes resueltas de
`otp_service`.

Convencion: `flush` sin `commit`; el endpoint confirma (`commit`) en exito
y ante error de negocio tipado (el contador de intentos del OTP DEBE
persistir, como `api/pin_login.py`), y revierte (`rollback`) ante error
inesperado. Sin `float`, sin PII/OTP en logs (solo `result` y contadores).
No toca `onboard_customer`, `kyc_proxy`, `otp_service` ni `activation`
(solo los consume via import).
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.modules.identity import repository as identity_repo
from app.modules.identity.repository import recovery as recovery_repo
from app.modules.identity.service import activation as activation_service
from app.modules.identity.service import device_login as device_login_service
from app.modules.identity.service import otp_service

logger = logging.getLogger(__name__)

#: Proposito OTP de este flujo (ya incluido en `OTP_PURPOSES`).
RECOVERY_PURPOSE = "RECOVERY"

#: Canal de entrega (el UNICO permitido para recuperacion: nunca SMS).
EMAIL_CHANNEL = "email"

#: Plantilla del codigo por correo (`{code, ttl_minutes}`, canal `email`).
OTP_EMAIL_TEMPLATE_CODE = "otp_code_email"

#: Metodo registrado en `access_recovery` (`03b#4.8`).
RECOVERY_METHOD = "OTP"

#: Mensaje generico estable de `verify` (identico exista o no el usuario,
#: haya o no OTP pendiente, el codigo sea incorrecto, el OTP este vencido
#: o bloqueado por intentos agotados: sin enumeracion ni oraculo).
INVALID_MESSAGE = "Codigo de recuperacion invalido"

#: Mensaje de rate-limit (request, verify) e intentos del OTP agotados.
RATE_LIMIT_MESSAGE = "Limite de intentos excedido, intente mas tarde"

#: Acciones de auditoria E1-T17 (best-effort via fachada, sin PII).
AUDIT_RECOVERY_REQUESTED = "auth.recovery_requested"
AUDIT_ACCESS_RECOVERED = "auth.access_recovered"


class RecoveryInvalidError(ValueError):
    """Email no registrado, usuario no elegible, sin OTP pendiente, codigo
    incorrecto, OTP vencido o OTP bloqueado por intentos agotados: una
    sola clase/mensaje para no filtrar (-> 401 `INVALID_RECOVERY_CODE`).
    Vencido y bloqueado colapsan aqui a proposito (fix anti-oraculo
    E1-T31): el log interno conserva la causa (`expired`/`locked`), pero
    el usuario recibe siempre el generico."""


class RecoveryRateLimitedError(RuntimeError):
    """Ventana de rate-limit (`request`/`verify` por `email+IP`) excedida
    (-> 429 `RATE_LIMITED`, verificada antes de la existencia, sin
    filtrar). Los intentos del OTP agotados ya NO usan esta clase:
    colapsan al 401 generico (el bloqueo persiste en la fila)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def normalize_email(email: object) -> str:
    """Normaliza el email (`strip().lower()`, como `kyc_onboarding`)."""
    if not isinstance(email, str):
        raise TypeError("email debe ser texto")
    return email.strip().lower()


# ------------------------------------------------------- Rate limit en memoria
_BUCKETS: dict[str, list[float]] = {}
_BUCKETS_LOCK = threading.Lock()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


#: Claves del rate-limit en `config.parameters` (E1-T34/SCR-005, semillas
#: `60`/`10`, modulo `identity`; sin acceso rigen entorno/constante).
_PARAM_REQUEST_WINDOW_KEY = "auth.recovery_request_window_seconds"
_PARAM_REQUEST_MAX_KEY = "auth.recovery_request_max_requests"
_PARAM_VERIFY_WINDOW_KEY = "auth.recovery_verify_window_seconds"
_PARAM_VERIFY_MAX_KEY = "auth.recovery_verify_max_requests"


def recovery_request_rate_limit_cfg(session: Session | None = None) -> tuple[int, int]:
    """`(ventana_s, maximo)` de `POST /auth/recovery/request`.

    E1-T34/SCR-005 (regla de oro 6): `config.parameters`
    (`auth.recovery_request_window_seconds`,
    `auth.recovery_request_max_requests`) con fallback al entorno
    (`RECOVERY_REQUEST_RATE_LIMIT_*`) y luego a `(60, 10)`. Sin sesion
    (p. ej. pruebas) solo rige entorno/constante.
    """
    window_default = _env_int("RECOVERY_REQUEST_RATE_LIMIT_WINDOW_SECONDS", 60)
    max_default = _env_int("RECOVERY_REQUEST_RATE_LIMIT_MAX_REQUESTS", 10)
    if session is None:
        return (window_default, max_default)
    return (
        otp_service.read_int_parameter(session, _PARAM_REQUEST_WINDOW_KEY, window_default),
        otp_service.read_int_parameter(session, _PARAM_REQUEST_MAX_KEY, max_default),
    )


def recovery_verify_rate_limit_cfg(session: Session | None = None) -> tuple[int, int]:
    """`(ventana_s, maximo)` de `POST /auth/recovery/verify` (y `/auth/pin-reset`).

    E1-T34/SCR-005 (regla de oro 6): `config.parameters`
    (`auth.recovery_verify_window_seconds`,
    `auth.recovery_verify_max_requests`) con fallback al entorno
    (`RECOVERY_VERIFY_RATE_LIMIT_*`) y luego a `(60, 10)`.
    """
    window_default = _env_int("RECOVERY_VERIFY_RATE_LIMIT_WINDOW_SECONDS", 60)
    max_default = _env_int("RECOVERY_VERIFY_RATE_LIMIT_MAX_REQUESTS", 10)
    if session is None:
        return (window_default, max_default)
    return (
        otp_service.read_int_parameter(session, _PARAM_VERIFY_WINDOW_KEY, window_default),
        otp_service.read_int_parameter(session, _PARAM_VERIFY_MAX_KEY, max_default),
    )


def check_pin_reset_rate_limit(email: str, ip: str | None, session: Session | None = None) -> None:
    """Rate-limit de `POST /auth/pin-reset` por `email+IP` (E1-T34).

    Reutiliza la ventana de verify (`auth.recovery_verify_*`): E1-T34 no
    crea claves propias para `/pin-reset` (solo las 8 de su tabla). Se
    verifica ANTES de la existencia (anti-enumeracion, mismo patron que
    `recovery`). Excederla lanza `RecoveryRateLimitedError` (-> 429).
    """
    _check_rate_limit(
        _build_recovery_rate_key("pin_reset", email, ip),
        recovery_verify_rate_limit_cfg(session),
    )


def _build_recovery_rate_key(scope: str, email: str, ip: str | None) -> str:
    """Clave por `email+IP` (hash: sin PII en el mapa)."""
    raw = f"{scope}|{(email or '').strip().lower()}|{(ip or '-').strip() or '-'}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _check_rate_limit(key: str, cfg: tuple[int, int]) -> None:
    """Ventana deslizante en memoria; excederla lanza `RecoveryRateLimitedError`.

    NOTA produccion: con multirreplica este mapa local no se comparte;
    mover a Redis o a un middleware de rate limiting (misma clave).
    """
    window_s, maximum = cfg
    now = time.monotonic()
    with _BUCKETS_LOCK:
        hits = [t for t in _BUCKETS.get(key, []) if now - t < window_s]
        if len(hits) >= maximum:
            _BUCKETS[key] = hits
            logger.warning("recovery rate_limited hits=%d", len(hits))
            raise RecoveryRateLimitedError(RATE_LIMIT_MESSAGE)
        hits.append(now)
        _BUCKETS[key] = hits


def reset_recovery_rate_limits() -> None:
    """Limpia las ventanas (uso en pruebas)."""
    with _BUCKETS_LOCK:
        _BUCKETS.clear()


# ------------------------------------------------------- Auditoria / eventos
def _audit_recovery_event(
    session: Session,
    *,
    user_id: uuid.UUID | None,
    device_id: str | None,
    ip: str | None,
    moment: datetime,
    action: str,
    result: str,
) -> None:
    """Registra `auth.recovery_requested` / `auth.access_recovered` via fachada.

    Best-effort como E1-T17: si la auditoria falla se loguea y el flujo
    igual continua; nunca `commit` (solo `flush` via la fachada). Sin PII:
    solo `user_id` (cuando se conoce), `device_id`, `ip`, `at`, `result`;
    jamas el email, el OTP ni su hash.
    """
    try:
        from app.modules.audit.service import record as audit_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("recovery audit no disponible")
        return
    try:
        audit_record(
            session,
            actor=user_id,
            action=action,
            entity=device_login_service.USER_AGGREGATE_TYPE,
            entity_id=user_id,
            metadata={"result": result, "at": moment.isoformat()},
            device_id=device_id,
            ip=ip,
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 - best-effort documentado E1-T17
        logger.warning("recovery audit no registrado error=%s", type(exc).__name__)


def _notify_recovery_code(
    session: Session,
    *,
    user_id: uuid.UUID,
    recipient: str,
    plain_code: str,
) -> None:
    """Entrega el codigo SOLO por email (best-effort, import perezoso).

    Mismo patron que `activation._notify_resend`: un fallo de envio se
    loguea sin PII ni codigo y NO revierte la emision del OTP.
    """
    try:
        from app.modules.notifications.service import send as notifications_send

        notifications_send(
            session,
            channel=EMAIL_CHANNEL,
            recipient=recipient,
            template_code=OTP_EMAIL_TEMPLATE_CODE,
            data={
                "code": plain_code,
                "ttl_minutes": max(1, otp_service.OTP_TTL_SECONDS // 60),
            },
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning(
            "recovery notify_failed channel=%s template=%s error=%s",
            EMAIL_CHANNEL,
            OTP_EMAIL_TEMPLATE_CODE,
            type(exc).__name__,
        )


def _bind_device_best_effort(
    session: Session,
    *,
    user_id: uuid.UUID,
    device_id: str | None,
    device_public_key: str | None,
    platform: str | None,
    biometric_type: str | None,
    moment: datetime,
) -> str | None:
    """Registra o refresca el binding del dispositivo nuevo (E1-T31).

    Mismo patron que `pin_login._bind_device_best_effort` (reutiliza
    `identity_repo.get_binding` / `touch_binding` / `register_binding`;
    `platform`/`biometric_type` invalidos se ignoran): solo actua si llegan
    `device_id` y `device_public_key`. Todo va en un savepoint
    (`begin_nested`): un fallo de persistencia se revierte SOLO aqui y el
    verify igual se retorna (best-effort).

    Retorna `"registered"`, `"touched"`, `"failed"` o `None` si no habia
    intento (sin `device_id`/`device_public_key`).
    """
    if not device_id or not device_public_key:
        return None
    key = device_public_key.strip() if isinstance(device_public_key, str) else ""
    if not key:
        return None
    safe_platform = platform if platform in identity_repo.DEVICE_PLATFORMS else None
    safe_biometric = biometric_type if biometric_type in identity_repo.BIOMETRIC_TYPES else None
    result = "failed"
    try:
        with session.begin_nested():
            existing = identity_repo.get_binding(session, user_id, device_id)
            if existing is not None:
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
        logger.warning("recovery device_binding %s error=%s", result, type(exc).__name__)
    return result


def _success_body() -> dict:
    """Cuerpo 200 de `request` (constantes globales: identico exista o no)."""
    return {
        "accepted": True,
        "ttl_seconds": otp_service.OTP_TTL_SECONDS,
        "resend_wait_seconds": otp_service.OTP_RESEND_WAIT_SECONDS,
    }


# ------------------------------------------------------- Solicitud
def request_recovery(
    session: Session,
    *,
    email: str,
    ip: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Solicita el OTP de recuperacion (`flush`, sin `commit`).

    SIEMPRE retorna el mismo cuerpo 200 (sin enumeracion): el rate-limit se
    verifica ANTES de resolver el email; si no hay usuario elegible
    (inexistente, no `ACTIVE` o sin email) no se emite OTP. En cooldown (OTP
    `RECOVERY` `PENDING` vigente) se reutiliza sin emitir otro ni
    re-notificar (idempotencia). La entrega es SOLO email (nunca SMS) y
    best-effort: un fallo no revierte la emision.
    """
    normalized = normalize_email(email)
    _check_rate_limit(
        _build_recovery_rate_key("request", normalized, ip),
        recovery_request_rate_limit_cfg(session),
    )
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()

    user = identity_repo.get_by_email(session, normalized)
    if user is None or user.status != "ACTIVE" or not user.email:
        # Rama ciega: mismo cuerpo, sin OTP, sin filtrar existencia/estado.
        _audit_recovery_event(
            session,
            user_id=user.id if user is not None else None,
            device_id=None,
            ip=ip,
            moment=moment,
            action=AUDIT_RECOVERY_REQUESTED,
            result="accepted",
        )
        logger.info("recovery requested result=%s", "accepted")
        return _success_body()

    route = activation_service.resolve_activation_delivery(
        email=user.email,
        phone=None,
        channel=EMAIL_CHANNEL,
    )
    if route is None:
        # Sin email resoluble: 200 identico, sin OTP y nunca SMS.
        _audit_recovery_event(
            session,
            user_id=user.id,
            device_id=None,
            ip=ip,
            moment=moment,
            action=AUDIT_RECOVERY_REQUESTED,
            result="accepted",
        )
        logger.info("recovery requested result=%s", "accepted")
        return _success_body()

    recipient = route[2]
    active = identity_repo.get_active_otp(session, user.id, RECOVERY_PURPOSE)
    if active is not None and _as_aware(active.expires_at) > moment:
        # Cooldown: se reutiliza el PENDING vigente (no duplica, no notifica).
        _audit_recovery_event(
            session,
            user_id=user.id,
            device_id=None,
            ip=ip,
            moment=moment,
            action=AUDIT_RECOVERY_REQUESTED,
            result="accepted",
        )
        logger.info("recovery requested result=%s reused=%s", "accepted", True)
        return _success_body()

    _, plain = otp_service.generate_otp(
        session,
        user_id=user.id,
        purpose=RECOVERY_PURPOSE,
        destination=recipient,
    )
    _notify_recovery_code(session, user_id=user.id, recipient=recipient, plain_code=plain)
    _audit_recovery_event(
        session,
        user_id=user.id,
        device_id=None,
        ip=ip,
        moment=moment,
        action=AUDIT_RECOVERY_REQUESTED,
        result="accepted",
    )
    logger.info("recovery requested result=%s reused=%s", "accepted", False)
    return _success_body()


# ------------------------------------------------------- Verificacion
def verify_recovery(
    session: Session,
    *,
    email: str,
    code: str,
    device_id: str | None = None,
    device_public_key: str | None = None,
    platform: str | None = None,
    biometric_type: str | None = None,
    ip: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Valida el OTP sin abrir sesion (`flush`, sin `commit`).

    El rate-limit se verifica ANTES de la existencia (misma respuesta para
    todos). La elegibilidad (`ACTIVE`) se verifica ANTES de consumir el OTP:
    un usuario no elegible responde 401 generico sin quemar el codigo (orden
    anti-oraculo documentado). En el exito, en la MISMA transaccion: consumo
    del OTP (un solo uso), `access_recovery` (`method='OTP'`,
    `new_credential_set=false`) y binding del dispositivo nuevo best-effort
    (`device_bound` solo `True` si se REGISTRO un binding nuevo;
    `touched`/fallo/ausencia -> `False`). NO crea `sessions`, NO emite
    tokens y NO enlista `auth.login_succeeded` (SCR-005, E1-T33): la unica
    sesion la abre `POST /auth/login/pin`. Retorna `{user_ref, device_bound}`
    (`user_ref = str(user.id)`).
    """
    normalized = normalize_email(email)
    _check_rate_limit(
        _build_recovery_rate_key("verify", normalized, ip),
        recovery_verify_rate_limit_cfg(session),
    )
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()

    user = identity_repo.get_by_email(session, normalized)
    if user is None:
        # Rama ciega: se consulta el OTP pendiente (descartado) para igualar
        # el patron de acceso a BD antes de responder generico.
        identity_repo.get_active_otp(session, uuid.uuid4(), RECOVERY_PURPOSE)
        logger.info("recovery verify result=%s", "invalid")
        raise RecoveryInvalidError(INVALID_MESSAGE)
    if user.status != "ACTIVE":
        logger.info("recovery verify result=%s", "invalid")
        raise RecoveryInvalidError(INVALID_MESSAGE)

    try:
        otp_service.validate_otp(session, user_id=user.id, purpose=RECOVERY_PURPOSE, code=code)
    except otp_service.OtpExpiredError as exc:
        # Colapso anti-oraculo: vencido -> mismo 401 generico (la causa
        # real solo queda en el log interno).
        logger.info("recovery verify result=%s cause=%s", "invalid", "expired")
        raise RecoveryInvalidError(INVALID_MESSAGE) from exc
    except otp_service.OtpAttemptsExceededError as exc:
        # Colapso anti-oraculo: bloqueado por intentos -> mismo 401
        # generico (la fila queda `EXPIRED`: el bloqueo persiste aunque la
        # respuesta no lo anuncie).
        logger.info("recovery verify result=%s cause=%s", "invalid", "locked")
        raise RecoveryInvalidError(INVALID_MESSAGE) from exc
    except (otp_service.OtpError, ValueError) as exc:
        # `OtpNotFound`/`OtpInvalid` + codigo malformado: generico.
        logger.info("recovery verify result=%s", "invalid")
        raise RecoveryInvalidError(INVALID_MESSAGE) from exc

    recovery_repo.record_access_recovery(
        session,
        user.id,
        method=RECOVERY_METHOD,
        verification_result={"result": "ok", "at": moment.isoformat()},
        device_id=device_id,
        new_credential_set=False,
        notified_channels=[EMAIL_CHANNEL],
    )
    binding_result = _bind_device_best_effort(
        session,
        user_id=user.id,
        device_id=device_id,
        device_public_key=device_public_key,
        platform=platform,
        biometric_type=biometric_type,
        moment=moment,
    )
    _audit_recovery_event(
        session,
        user_id=user.id,
        device_id=device_id,
        ip=ip,
        moment=moment,
        action=AUDIT_ACCESS_RECOVERED,
        result="ok",
    )
    logger.info("recovery verify result=%s", "ok")
    return {
        "user_ref": str(user.id),
        "device_bound": binding_result == "registered",
    }


__all__ = [
    "AUDIT_ACCESS_RECOVERED",
    "AUDIT_RECOVERY_REQUESTED",
    "EMAIL_CHANNEL",
    "INVALID_MESSAGE",
    "OTP_EMAIL_TEMPLATE_CODE",
    "RATE_LIMIT_MESSAGE",
    "RECOVERY_METHOD",
    "RECOVERY_PURPOSE",
    "RecoveryInvalidError",
    "RecoveryRateLimitedError",
    "check_pin_reset_rate_limit",
    "normalize_email",
    "recovery_request_rate_limit_cfg",
    "recovery_verify_rate_limit_cfg",
    "request_recovery",
    "reset_recovery_rate_limits",
    "verify_recovery",
]
