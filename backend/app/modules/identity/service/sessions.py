"""Gestion de sesiones: refresh rotativo, inactividad y logout (E1-T15, HU03 CA-04).

`POST /auth/refresh {refresh_token}` verifica el hash + vigencia + no
revocado + inactividad, y emite un refresh NUEVO rotativo invalidando el
anterior. `POST /auth/logout {refresh_token}` revoca la sesion + refresh.

Rotacion: el refresh presentado se revoca y se emite uno nuevo (solo su
hash SHA-256 se persiste, jamas el token en claro). Reutilizar un refresh
ya revocado se trata como posible robo: se revocan TODAS las sesiones
activas del usuario (toda la cadena) y se responde error.

Inactividad: ventana deslizante medida desde `session.created_at`
(momento de emision del refresh vigente: cada rotacion exitosa la
reinicia). Si `now - created_at > inactivity_seconds`, la sesion se cierra
(se revoca) y el refresh se rechaza. Default `INACTIVITY_SECONDS = 180`
(3 min), candidato a `config.parameters: session.inactivity_seconds`
(clave ya prevista en `docs/03b#3.1` semillas; regla de oro 6).

Revocacion: vive en la tabla `identity.sessions` (`revoked_at`).
El `.venv` NO trae cliente `redis`/`fakeredis` (verificado con
`importlib.util.find_spec` el 2026-09-17), asi que no hay lista negra en
Redis hoy: la revocacion es efectiva e inmediata por consulta a la tabla
en cada `refresh`. Redis (lista negra con TTL) queda como mejora futura
sin cambiar el contrato.

Convencion: `flush` sin `commit`; quien llama decide la transaccion.
Sin `float`, sin secretos/PII en logs (solo contadores/tipos). No toca
`device_login`/`pin_login`/`onboard`/`otp`/`activation`: solo reutiliza el
formato de emision (`device_login.REFRESH_TTL_SECONDS`,
`create_access_token`) y el repositorio `identity`.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.modules.identity import repository as identity_repo
from app.modules.identity.service import device_login as device_login_service

logger = logging.getLogger(__name__)

#: Inactividad maxima en segundos antes de cerrar la sesion (3 min).
#: Candidato a `config.parameters: session.inactivity_seconds` (clave ya
#: prevista en `docs/03b#3.1`); aun no existe infraestructura de parametros
#: en el repo (mismo patron que `device_login.REFRESH_TTL_SECONDS`).
INACTIVITY_SECONDS = 180

#: Mensajes estables (sin filtrar existencia ni estado).
INVALID_MESSAGE = "Refresh invalido"
EXPIRED_MESSAGE = "Refresh vencido, inicie sesion de nuevo"
INACTIVE_MESSAGE = "Sesion cerrada por inactividad, inicie sesion de nuevo"
REUSED_MESSAGE = "Refresh reutilizado, sesion revocada por seguridad"

#: Acciones de auditoria E1-T17 (exito reutiliza el evento de device_login).
AUDIT_LOGIN_SUCCEEDED = device_login_service.LOGIN_SUCCEEDED_EVENT
AUDIT_FAILED_ATTEMPT = "auth.failed_attempt"


class RefreshInvalidError(ValueError):
    """Refresh malformado o desconocido (-> 401 `INVALID_REFRESH`)."""


class RefreshExpiredError(ValueError):
    """Refresh con `expires_at` pasado (-> 401 `REFRESH_EXPIRED`)."""


class SessionInactiveError(ValueError):
    """Inactividad excedida: sesion cerrada (-> 401 `SESSION_INACTIVE`)."""


class RefreshReuseError(ValueError):
    """Reuso de un refresh revocado: posible robo (-> 401 `REFRESH_REUSED`)."""


def _audit_auth_event(
    session: Session,
    *,
    user_id,
    device_id: str | None,
    ip: str | None,
    moment: datetime,
    action: str,
    session_id=None,
    reason: str | None = None,
) -> None:
    """Registra `auth.login_succeeded` / `auth.failed_attempt` via fachada.

    E1-T17 (best-effort como E1-T03): si la auditoria falla se loguea y el
    flujo igual continua; nunca `commit` (solo `flush` via la fachada).
    Sin PII: solo IDs (`user_id`, `session_id`, `device_id`, `ip`, `at`,
    `method=refresh`, `reason`); jamas el token en claro ni su hash.
    """
    try:
        from app.modules.audit.service import record as audit_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("session audit no disponible")
        return
    try:
        metadata: dict = {"method": "refresh", "at": moment.isoformat()}
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
        logger.warning("session audit no registrado error=%s", type(exc).__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def refresh_session(
    session: Session,
    *,
    refresh_token: str,
    now: datetime | None = None,
    inactivity_seconds: int = INACTIVITY_SECONDS,
) -> dict:
    """Rota el refresh: valida, revoca el presentado y emite uno nuevo.

    Orden de verificacion: formato conocido -> no revocado (reuso =
    posible robo: revoca TODA la cadena del usuario) -> vigencia
    (`expires_at`; vencido cierra la sesion) -> inactividad (excedida
    cierra la sesion). Exito: revoca el presentado, crea la fila sucesora
    (mismo `user_id`/`device_id`/`device_info`/`ip`, nuevo `expires_at`) y
    devuelve JWT corto + refresh nuevo. `flush`, sin `commit`.
    """
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()
    if (
        not isinstance(refresh_token, str)
        or not refresh_token.strip()
        or not isinstance(inactivity_seconds, int)
        or inactivity_seconds < 0
    ):
        _audit_auth_event(
            session,
            user_id=None,
            device_id=None,
            ip=None,
            moment=moment,
            action=AUDIT_FAILED_ATTEMPT,
            reason="invalid",
        )
        raise RefreshInvalidError(INVALID_MESSAGE)

    try:
        presented_hash = identity_repo.hash_refresh_token(refresh_token.strip())
    except ValueError as exc:
        _audit_auth_event(
            session,
            user_id=None,
            device_id=None,
            ip=None,
            moment=moment,
            action=AUDIT_FAILED_ATTEMPT,
            reason="invalid",
        )
        raise RefreshInvalidError(INVALID_MESSAGE) from exc
    row = identity_repo.get_session_by_refresh_hash(session, presented_hash)
    if row is None:
        _audit_auth_event(
            session,
            user_id=None,
            device_id=None,
            ip=None,
            moment=moment,
            action=AUDIT_FAILED_ATTEMPT,
            reason="invalid",
        )
        raise RefreshInvalidError(INVALID_MESSAGE)

    if row.revoked_at is not None:
        # Reuso de revocado: posible robo del refresh. Se revoca TODA la
        # cadena (todas las sesiones activas del usuario) y se responde
        # error; el sucesor legitimo tambien muere (el usuario reingresa).
        revoked = identity_repo.revoke_user_sessions(session, row.user_id, moment)
        logger.warning("refresh reuso detectado cadena_revocada=%d", revoked)
        _audit_auth_event(
            session,
            user_id=row.user_id,
            device_id=row.device_id,
            ip=row.ip,
            moment=moment,
            action=AUDIT_FAILED_ATTEMPT,
            reason="reused",
        )
        raise RefreshReuseError(REUSED_MESSAGE)

    if _as_aware(row.expires_at) <= moment:
        # Vencido: se cierra la sesion (no se extiende) y se responde error.
        identity_repo.revoke_session(session, row, moment)
        logger.info("refresh vencido sesion_cerrada")
        _audit_auth_event(
            session,
            user_id=row.user_id,
            device_id=row.device_id,
            ip=row.ip,
            moment=moment,
            action=AUDIT_FAILED_ATTEMPT,
            reason="expired",
        )
        raise RefreshExpiredError(EXPIRED_MESSAGE)

    created_at = row.created_at
    if created_at is not None:
        idle = (moment - _as_aware(created_at)).total_seconds()
        if idle > inactivity_seconds:
            # Inactividad excedida: se cierra la sesion y se responde error.
            identity_repo.revoke_session(session, row, moment)
            logger.info("sesion cerrada por inactividad")
            _audit_auth_event(
                session,
                user_id=row.user_id,
                device_id=row.device_id,
                ip=row.ip,
                moment=moment,
                action=AUDIT_FAILED_ATTEMPT,
                reason="inactive",
            )
            raise SessionInactiveError(INACTIVE_MESSAGE)

    identity_repo.revoke_session(session, row, moment)
    fresh = secrets.token_urlsafe(32)
    successor = identity_repo.create_session(
        session,
        row.user_id,
        identity_repo.hash_refresh_token(fresh),
        moment + timedelta(seconds=device_login_service.REFRESH_TTL_SECONDS),
        device_id=row.device_id,
        device_info=dict(row.device_info) if row.device_info is not None else None,
        ip=row.ip,
    )
    access_token = create_access_token(subject=str(row.user_id))
    logger.info("refresh rotado")
    _audit_auth_event(
        session,
        user_id=row.user_id,
        device_id=row.device_id,
        ip=row.ip,
        moment=moment,
        action=AUDIT_LOGIN_SUCCEEDED,
        session_id=successor.id,
    )
    try:
        refresh_in = max(0, int((_as_aware(successor.expires_at) - moment).total_seconds()))
    except (AttributeError, TypeError):
        refresh_in = device_login_service.REFRESH_TTL_SECONDS
    return {
        "access_token": access_token,
        "refresh_token": fresh,
        "token_type": "Bearer",
        "session_id": str(successor.id),
        "expires_in": refresh_in,
    }


def logout_session(
    session: Session,
    *,
    refresh_token: str,
    now: datetime | None = None,
) -> dict:
    """Revoca la sesion del refresh presentado (`flush`, sin `commit`).

    Idempotente estilo RFC 7009 §2.2.1: un refresh desconocido o ya
    revocado tambien responde exito (`revoked=False`) para no crear un
    oraculo de existencia. La revocacion vive en `sessions.revoked_at`
    (sin Redis en el repo: mejora futura documentada en el modulo).
    """
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise RefreshInvalidError(INVALID_MESSAGE)
    try:
        presented_hash = identity_repo.hash_refresh_token(refresh_token.strip())
    except ValueError as exc:
        raise RefreshInvalidError(INVALID_MESSAGE) from exc
    row = identity_repo.get_session_by_refresh_hash(session, presented_hash)
    if row is None:
        logger.info("logout sin sesion")
        return {"revoked": False, "session_id": None}
    if row.revoked_at is not None:
        return {"revoked": False, "session_id": str(row.id)}
    identity_repo.revoke_session(session, row, moment)
    logger.info("logout ok")
    return {"revoked": True, "session_id": str(row.id)}


__all__ = [
    "AUDIT_FAILED_ATTEMPT",
    "AUDIT_LOGIN_SUCCEEDED",
    "EXPIRED_MESSAGE",
    "INACTIVE_MESSAGE",
    "INACTIVITY_SECONDS",
    "INVALID_MESSAGE",
    "REUSED_MESSAGE",
    "RefreshExpiredError",
    "RefreshInvalidError",
    "RefreshReuseError",
    "SessionInactiveError",
    "logout_session",
    "refresh_session",
]
