"""Paso 1 del login en dispositivo nuevo: email + DNI/RUC -> OTP `LOGIN` (E1-T45, HU03/HU04).

Hueco que cierra (decision del dueno, cerrada): cuando NO hay `userRef` en
el dispositivo, el login ofrece "iniciar sesion en este dispositivo" con
`email + DNI (MISMA cuenta) -> OTP por email -> PIN -> asociar device +
abrir sesion`. `pin-reset` NO sirve de base: fuerza cambio de PIN, consume
OTP `RECOVERY` y no crea binding. Esta tarea cubre el paso 1 (solicitud de
OTP `LOGIN` con verificacion de misma cuenta); el paso 2 (validar OTP +
PIN + binding + sesion) es `E1-T46`.

Flujo (`POST /auth/login/device/request {email, doc_type, document_number}`):

1. Normaliza el email (`strip().lower()`, via `recovery.normalize_email`).
2. Rate-limit por `email+IP` ANTES de resolver existencia (misma ventana
   que `pin-reset`: `auth.recovery_verify_*` via
   `recovery._check_rate_limit` / `recovery._build_recovery_rate_key` con
   scope `"device_login"`; ver decision abajo): excederla ->
   `RecoveryRateLimitedError` (429, sin filtrar).
3. Resuelve el usuario por email normalizado; si no existe, `status !=
   "ACTIVE"` o sin email -> rama ciega (mismo 200, sin OTP, sin filtrar
   existencia/estado; audita `result="accepted"`).
4. Verifica que `email` y documento son de la MISMA cuenta con
   `hash_document_number(document_number)` + `hmac.compare_digest` contra
   `user.doc_number_hash` (mismo patron exacto que
   `service/pin_reset.py:222-231`); mismatch/documento vacio -> rama ciega
   (mismo 200). `doc_type` NO se cruza con `users.doc_type` (solo valida
   formato/longitud en el esquema, sin oraculo).
5. Resuelve la entrega con `activation.resolve_activation_delivery(
   email=user.email, phone=None, channel="email")`; si `None` -> rama ciega.
6. Cooldown: si hay OTP `LOGIN` `PENDING` vigente (`get_active_otp` con
   `expires_at > now`) -> reutiliza sin emitir ni notificar (idempotente).
7. Emite `otp_service.generate_otp(... purpose="LOGIN",
   destination=<email>)` y entrega best-effort con `notifications.send`
   (patron `service/recovery.py:349-390` / `_notify_recovery_code`:
   `channel="email"`, plantilla `otp_code_email`, `{code, ttl_minutes}`).
8. Audita best-effort (`auth.device_login_requested`, sin PII) via fachada
   `audit` (patron `recovery._audit_recovery_event`).

Decision de rate-limit (E1-T45, regla de oro 6): NO se crean claves nuevas
en `config.parameters`; se REUTILIZA la ventana de verificacion
`auth.recovery_verify_window_seconds` /
`auth.recovery_verify_max_requests` (semillas `60`/`10`, modulo `identity`)
con un scope propio (`"device_login"`) en la clave `email+IP` en memoria.
Motivo: el paso 1 es una verificacion previa al consumo del OTP (igual que
`pin-reset`), no una solicitud masiva como `recovery/request`; compartir la
ventana de verify evita una fila nueva de parametros y mantiene un solo
tope de verificaciones por `email+IP`. Prod: Redis/middleware (misma clave).

Sin enumeracion (regla dura): `request` responde SIEMPRE el mismo cuerpo
200 (constantes globales `accepted/ttl_seconds/resend_wait_seconds`),
exista o no la cuenta, coincida o no el documento, sea o no `ACTIVE`; el
rate-limit es la unica salida no-200 (429). Comparacion del documento en
tiempo constante (`hmac.compare_digest`); en la rama ciega por usuario
inexistente/no elegible se hace trabajo equivalente (consulta OTP
descartada + hash del documento ignorado) para no revelar por timing ni por
patron de acceso. El `document_number` nunca se loguea ni se persiste; el
email nunca se loguea.

Convencion: `flush` sin `commit`; el endpoint confirma (`commit`) en exito
(incluida la rama ciega: la auditoria hace `flush`) y revierte
(`rollback`) ante rate-limit o error inesperado. Sin `float`, sin PII/OTP
en logs (solo `result` y contadores). No toca `onboard_customer`,
`kyc_proxy`, `otp_service` ni `activation` (solo los consume via import).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.modules.identity import repository as identity_repo
from app.modules.identity.service import activation as activation_service
from app.modules.identity.service import otp_service
from app.modules.identity.service import recovery as recovery_service
from app.modules.identity.service.kyc_onboarding import hash_document_number

logger = logging.getLogger(__name__)

#: Proposito OTP de este flujo (ya incluido en `OTP_PURPOSES`, sin migracion).
LOGIN_PURPOSE = "LOGIN"

#: Canal de entrega (el UNICO permitido: nunca SMS).
EMAIL_CHANNEL = "email"

#: Plantilla del codigo por correo (`{code, ttl_minutes}`, canal `email`).
OTP_EMAIL_TEMPLATE_CODE = "otp_code_email"

#: Accion de auditoria (best-effort via fachada, sin PII).
AUDIT_DEVICE_LOGIN_REQUESTED = "auth.device_login_requested"

#: Tipo de agregado para la auditoria.
USER_AGGREGATE_TYPE = "user"

#: Scope propio del rate-limit en la clave `email+IP` (ventana compartida de
#: verify, sin claves nuevas en `config.parameters`; ver decision arriba).
_RATE_SCOPE = "device_login"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _audit_device_login_event(
    session: Session,
    *,
    user_id: uuid.UUID | None,
    ip: str | None,
    moment: datetime,
    result: str,
) -> None:
    """Registra `auth.device_login_requested` via fachada.

    Best-effort como E1-T17: si la auditoria falla se loguea y el flujo
    igual continua; nunca `commit` (solo `flush` via la fachada). Sin PII:
    solo `user_id` (cuando se conoce), `ip`, `at`, `result`; jamas el email,
    el documento, el OTP ni su hash.
    """
    try:
        from app.modules.audit.service import record as audit_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("device_login audit no disponible")
        return
    try:
        audit_record(
            session,
            actor=user_id,
            action=AUDIT_DEVICE_LOGIN_REQUESTED,
            entity=USER_AGGREGATE_TYPE,
            entity_id=user_id,
            metadata={"result": result, "at": moment.isoformat()},
            device_id=None,
            ip=ip,
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 - best-effort documentado E1-T17
        logger.warning("device_login audit no registrado error=%s", type(exc).__name__)


def _notify_login_code(
    session: Session,
    *,
    user_id: uuid.UUID,
    recipient: str,
    plain_code: str,
) -> None:
    """Entrega el codigo SOLO por email (best-effort, import perezoso).

    Mismo patron que `recovery._notify_recovery_code`: un fallo de envio se
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
            "device_login notify_failed channel=%s template=%s error=%s",
            EMAIL_CHANNEL,
            OTP_EMAIL_TEMPLATE_CODE,
            type(exc).__name__,
        )


def _success_body() -> dict:
    """Cuerpo 200 del `request` (constantes globales: identico exista o no)."""
    return {
        "accepted": True,
        "ttl_seconds": otp_service.OTP_TTL_SECONDS,
        "resend_wait_seconds": otp_service.OTP_RESEND_WAIT_SECONDS,
    }


def _blind_body(
    session: Session,
    *,
    user_id: uuid.UUID | None,
    ip: str | None,
    moment: datetime,
    document_number: object = None,
) -> dict:
    """Rama ciega: mismo cuerpo 200, sin OTP, sin filtrar (audita `accepted`).

    Hace trabajo equivalente a la rama real (consulta OTP descartada + hash
    del documento ignorado + comparacion constante descartada) para no
    revelar por timing ni por patron de acceso a BD si la cuenta existe.
    """
    identity_repo.get_active_otp(session, uuid.uuid4(), LOGIN_PURPOSE)
    try:
        if isinstance(document_number, str) and document_number.strip():
            candidate = hash_document_number(document_number.strip())
            hmac.compare_digest(candidate, hashlib.sha256(b"blind").hexdigest())
    except (ValueError, TypeError):
        pass
    _audit_device_login_event(session, user_id=user_id, ip=ip, moment=moment, result="accepted")
    logger.info("device_login requested result=%s", "accepted")
    return _success_body()


def request_device_login(
    session: Session,
    *,
    email: str,
    doc_type: str = "DNI",
    document_number: str,
    ip: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Solicita el OTP `LOGIN` del paso 1 (`flush`, sin `commit`).

    SIEMPRE retorna el mismo cuerpo 200 (sin enumeracion): el rate-limit se
    verifica ANTES de resolver existencia o comparar el documento; si no hay
    cuenta elegible (inexistente, no `ACTIVE`, sin email, documento que no
    coincide o sin entrega resoluble) no se emite OTP. En cooldown (OTP
    `LOGIN` `PENDING` vigente) se reutiliza sin emitir otro ni re-notificar
    (idempotente). La entrega es SOLO email (nunca SMS) y best-effort: un
    fallo no revierte la emision.
    """
    normalized = recovery_service.normalize_email(email)
    recovery_service._check_rate_limit(
        recovery_service._build_recovery_rate_key(_RATE_SCOPE, normalized, ip),
        recovery_service.recovery_verify_rate_limit_cfg(session),
    )
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()

    user = identity_repo.get_by_email(session, normalized)
    if user is None or user.status != "ACTIVE" or not user.email:
        # Rama ciega: mismo cuerpo, sin OTP, sin filtrar existencia/estado.
        return _blind_body(
            session,
            user_id=user.id if user is not None else None,
            ip=ip,
            moment=moment,
            document_number=document_number,
        )

    try:
        candidate_hash = hash_document_number(
            document_number.strip() if isinstance(document_number, str) else ""
        )
    except (ValueError, TypeError, AttributeError):
        # Documento vacio/invalido: generico, sin distinguir del resto.
        return _blind_body(session, user_id=user.id, ip=ip, moment=moment, document_number=None)
    stored_hash = user.doc_number_hash or ""
    if not stored_hash or not hmac.compare_digest(candidate_hash, stored_hash):
        return _blind_body(session, user_id=user.id, ip=ip, moment=moment, document_number=None)

    route = activation_service.resolve_activation_delivery(
        email=user.email,
        phone=None,
        channel=EMAIL_CHANNEL,
    )
    if route is None:
        # Sin email resoluble: 200 identico, sin OTP y nunca SMS.
        return _blind_body(session, user_id=user.id, ip=ip, moment=moment, document_number=None)

    recipient = route[2]
    active = identity_repo.get_active_otp(session, user.id, LOGIN_PURPOSE)
    if active is not None and _as_aware(active.expires_at) > moment:
        # Cooldown: se reutiliza el PENDING vigente (no duplica, no notifica).
        _audit_device_login_event(session, user_id=user.id, ip=ip, moment=moment, result="accepted")
        logger.info("device_login requested result=%s reused=%s", "accepted", True)
        return _success_body()

    _, plain = otp_service.generate_otp(
        session,
        user_id=user.id,
        purpose=LOGIN_PURPOSE,
        destination=recipient,
    )
    _notify_login_code(session, user_id=user.id, recipient=recipient, plain_code=plain)
    _audit_device_login_event(session, user_id=user.id, ip=ip, moment=moment, result="accepted")
    logger.info("device_login requested result=%s reused=%s", "accepted", False)
    return _success_body()


__all__ = [
    "AUDIT_DEVICE_LOGIN_REQUESTED",
    "EMAIL_CHANNEL",
    "LOGIN_PURPOSE",
    "OTP_EMAIL_TEMPLATE_CODE",
    "USER_AGGREGATE_TYPE",
    "request_device_login",
]
