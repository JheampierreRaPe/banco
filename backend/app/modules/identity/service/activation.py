"""Orquestacion de activacion y reenvio de OTP (E1-T10, HU02 CA-02/CA-03/CA-04).

`activate_account(session, user_ref, code)`: valida el OTP `ACTIVATION` via
`otp_service.validate_otp` y, si OK, transiciona
`users.status: PENDING_ACTIVATION -> ACTIVE` en la misma sesion (`flush`,
sin `commit`; el endpoint confirma). El evento `user.activated` ya lo
enlista `validate_otp` via outbox en la misma sesion: aqui NO se re-emite
(verificado: `otp_service.validate_otp` enlista solo con
`purpose == "ACTIVATION"`).

`resend_activation_otp(session, user_ref, channel)`: emite un codigo nuevo
via `otp_service.resend_otp` (invalida el anterior, espera minima y maximo
de reenvios del ciclo) y notifica best-effort via la fachada
`notifications.send` (import perezoso, mismo patron que E1-T03/E1-T08: un
fallo de envio se loguea sin PII ni codigo y NO revierte el reenvio).

Reglas transversales:

- No filtrar existencia: usuario inexistente o `user_ref` malformado
  responden IDENTICO a codigo invalido (`ActivationInvalidError`, mismo
  mensaje). Limitacion documentada: la igualdad es de cuerpo/codigo; el
  tiempo solo se aproxima (en prod, padding de tiempo constante + WAF).
- El codigo OTP jamas sale en respuestas ni logs (solo viaja al canal de
  entrega dentro del cuerpo de la notificacion, que es su proposito).
- Rate limit propio en memoria (ventana + maximo por env, mismo patron que
  `kyc_proxy.check_rate_limit` pero con buckets independientes): suficiente
  para el MVP monolito; en produccion multirreplica va en Redis/middleware
  (ver `check_activation_rate_limit`).
- Sin `float`, sin secretos/PII en logs (los logs solo llevan contadores y
  tipos de error). No toca `onboard_customer`, `kyc_proxy` ni `otp_service`
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
from app.modules.identity.service import otp_service

logger = logging.getLogger(__name__)

#: Proposito OTP de este flujo (el unico que activa cuentas).
ACTIVATION_PURPOSE = "ACTIVATION"

#: Plantilla de entrega del codigo (canal `sms`, placeholders
#: `{code, ttl_minutes}` segun `notifications.domain.templates`).
OTP_TEMPLATE_CODE = "otp_code"

#: Canal por defecto del reenvio (el de la plantilla `otp_code`).
DEFAULT_RESEND_CHANNEL = "sms"

#: Mensajes genericos estables (identicos exista o no el usuario).
INVALID_MESSAGE = "Codigo de activacion invalido"
EXPIRED_MESSAGE = "Codigo de activacion vencido, solicite uno nuevo"
RESEND_LIMIT_MESSAGE = "Limite de reenvios alcanzado, genere un codigo nuevo"


class ActivationInvalidError(ValueError):
    """Codigo invalido, sin OTP pendiente, usuario inexistente o ref malformada.

    Un solo tipo/mensaje para no filtrar existencia (-> `INVALID_OTP`).
    """


class ActivationExpiredError(ValueError):
    """Codigo vencido (-> `EXPIRED_OTP`)."""


class ActivationResendLimitError(ValueError):
    """Maximo de reenvios o espera minima (-> `RESEND_LIMIT`, 429)."""

    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ActivationRateLimitedError(RuntimeError):
    """Ventana de rate limit de reenvio excedida (-> 429 `RATE_LIMITED`)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_user_id(user_ref: object) -> uuid.UUID | None:
    """Interpreta `user_ref` como UUID (`None` si malformado, sin excepcion)."""
    if isinstance(user_ref, uuid.UUID):
        return user_ref
    try:
        return uuid.UUID(str(user_ref).strip())
    except (ValueError, AttributeError, TypeError):
        return None


# ------------------------------------------------------- Rate limit en memoria
_BUCKETS: dict[str, list[float]] = {}
_BUCKETS_LOCK = threading.Lock()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def resend_rate_limit_cfg() -> tuple[int, int]:
    """`(ventana_s, maximo)` desde env (defaults 60 s / 10 reenvios)."""
    return (
        _env_int("ACTIVATION_RESEND_RATE_LIMIT_WINDOW_SECONDS", 60),
        _env_int("ACTIVATION_RESEND_RATE_LIMIT_MAX_REQUESTS", 10),
    )


def build_activation_rate_key(user_ref_text: str) -> str:
    """Clave por usuario (hash: sin PII en el mapa)."""
    raw = (user_ref_text or "").strip() or "-"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def check_activation_rate_limit(key: str) -> None:
    """Ventana deslizante en memoria; excederla lanza `ActivationRateLimitedError`.

    NOTA produccion: con multirreplica este mapa local no se comparte;
    mover a Redis o a un middleware de rate limiting (misma clave).
    """
    window_s, maximum = resend_rate_limit_cfg()
    now = time.monotonic()
    with _BUCKETS_LOCK:
        hits = [t for t in _BUCKETS.get(key, []) if now - t < window_s]
        if len(hits) >= maximum:
            _BUCKETS[key] = hits
            logger.warning("activation resend rate_limited hits=%d", len(hits))
            raise ActivationRateLimitedError("limite de reenvios por minuto excedido")
        hits.append(now)
        _BUCKETS[key] = hits


def reset_activation_rate_limits() -> None:
    """Limpia las ventanas (uso en pruebas)."""
    with _BUCKETS_LOCK:
        _BUCKETS.clear()


# ------------------------------------------------------- Notificacion best-effort
def _notify_resend(
    session: Session,
    *,
    user_id: uuid.UUID,
    destination: str | None,
    phone: str | None,
    email: str | None,
    plain_code: str,
    channel: str | None,
) -> None:
    """Entrega el codigo via fachada `notifications.send` (best-effort).

    Import perezoso como E1-T03/E1-T08 (sin ciclos entre modulos). Un fallo
    (sin destinatario, canal/plantilla incompatibles, proveedor caido) se
    loguea sin PII ni codigo y NO revierte el reenvio: el codigo ya quedo
    persistido (hash) y el cliente puede pedir otro reenvio.
    """
    try:
        from app.modules.notifications.service import send as notifications_send

        resolved = (channel or DEFAULT_RESEND_CHANNEL).strip() or DEFAULT_RESEND_CHANNEL
        recipient = (destination or phone or email or "").strip()
        if not recipient:
            logger.warning("activation notify_skipped reason=%s", "sin_destinatario")
            return
        notifications_send(
            session,
            channel=resolved,
            recipient=recipient,
            template_code=OTP_TEMPLATE_CODE,
            data={
                "code": plain_code,
                "ttl_minutes": max(1, otp_service.OTP_TTL_SECONDS // 60),
            },
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning("activation notify_failed error=%s", type(exc).__name__)


# ------------------------------------------------------- Orquestacion
def activate_account(session: Session, *, user_ref: str, code: str) -> dict:
    """Valida el OTP y activa la cuenta (`flush`, sin `commit`).

    Exito: `users.status = ACTIVE` en la misma sesion del `validate_otp`
    (que ya encolo `user.activated`; no se duplica). Si ya estaba `ACTIVE`
    retorna idempotente sin exigir OTP. Cualquier otro estado no activable
    (`BLOCKED`/`CLOSED`) responde generico para no filtrar estado.
    """
    uid = _coerce_user_id(user_ref)
    user = identity_repo.get_user(session, uid) if uid is not None else None
    if user is None:
        # Rama ciega: mismo error/tiempo aparente que codigo invalido. Se
        # consulta el OTP pendiente (descartado) para igualar el patron de
        # acceso a BD antes de responder generico.
        if uid is not None:
            identity_repo.get_active_otp(session, uid, ACTIVATION_PURPOSE)
        raise ActivationInvalidError(INVALID_MESSAGE)
    if user.status == "ACTIVE":
        return {"user_id": str(user.id), "status": "ACTIVE"}
    if user.status != "PENDING_ACTIVATION":
        raise ActivationInvalidError(INVALID_MESSAGE)

    try:
        otp_service.validate_otp(session, user_id=user.id, purpose=ACTIVATION_PURPOSE, code=code)
    except otp_service.OtpExpiredError as exc:
        raise ActivationExpiredError(EXPIRED_MESSAGE) from exc
    except (otp_service.OtpError, ValueError) as exc:
        # `OtpNotFound/Invalid/AttemptsExceeded` + codigo malformado: generico.
        raise ActivationInvalidError(INVALID_MESSAGE) from exc

    user.status = "ACTIVE"
    session.flush()
    logger.info("activation ok")
    return {"user_id": str(user.id), "status": user.status}


def resend_activation_otp(
    session: Session,
    *,
    user_ref: str,
    channel: str | None = None,
    destination: str | None = None,
    wait_seconds: int | None = None,
) -> dict:
    """Emite un codigo nuevo y lo notifica (`flush`, sin `commit`).

    El rate limit se verifica ANTES de la existencia (misma respuesta para
    todos). Usuario inexistente/ref malformada/cuenta ya activa/sin OTP
    pendiente responden generico `INVALID_OTP`; vencido, `EXPIRED_OTP`;
    tope o espera minima, `RESEND_LIMIT` (429 con `retry_after_seconds` en
    detalles cuando aplica).
    """
    check_activation_rate_limit(build_activation_rate_key(str(user_ref)))
    uid = _coerce_user_id(user_ref)
    user = identity_repo.get_user(session, uid) if uid is not None else None
    if user is None:
        if uid is not None:
            identity_repo.get_active_otp(session, uid, ACTIVATION_PURPOSE)
        raise ActivationInvalidError(INVALID_MESSAGE)
    if user.status == "ACTIVE":
        raise ActivationInvalidError(INVALID_MESSAGE)

    try:
        row, plain = otp_service.resend_otp(
            session,
            user_id=user.id,
            purpose=ACTIVATION_PURPOSE,
            destination=destination,
            wait_seconds=wait_seconds,
        )
    except otp_service.OtpMaxResendsExceededError as exc:
        raise ActivationResendLimitError(RESEND_LIMIT_MESSAGE) from exc
    except otp_service.OtpResendTooSoonError as exc:
        raise ActivationResendLimitError(
            RESEND_LIMIT_MESSAGE, retry_after_seconds=exc.retry_after_seconds
        ) from exc
    except otp_service.OtpExpiredError as exc:
        raise ActivationExpiredError(EXPIRED_MESSAGE) from exc
    except (otp_service.OtpError, ValueError) as exc:
        raise ActivationInvalidError(INVALID_MESSAGE) from exc

    _notify_resend(
        session,
        user_id=user.id,
        destination=row.destination,
        phone=user.phone,
        email=user.email,
        plain_code=plain,
        channel=channel,
    )
    remaining = row.expires_at
    try:
        aware = remaining if remaining.tzinfo is not None else remaining.replace(tzinfo=UTC)
        expires_in = max(0, int((aware - _utcnow()).total_seconds()))
    except (AttributeError, TypeError):
        expires_in = otp_service.OTP_TTL_SECONDS
    logger.info("activation resent resend_count=%d", row.resend_count)
    return {
        "user_id": str(user.id),
        "resend_count": int(row.resend_count),
        "expires_in": expires_in,
    }


__all__ = [
    "ACTIVATION_PURPOSE",
    "DEFAULT_RESEND_CHANNEL",
    "EXPIRED_MESSAGE",
    "INVALID_MESSAGE",
    "OTP_TEMPLATE_CODE",
    "RESEND_LIMIT_MESSAGE",
    "ActivationExpiredError",
    "ActivationInvalidError",
    "ActivationRateLimitedError",
    "ActivationResendLimitError",
    "activate_account",
    "build_activation_rate_key",
    "check_activation_rate_limit",
    "resend_activation_otp",
    "resend_rate_limit_cfg",
    "reset_activation_rate_limits",
]
