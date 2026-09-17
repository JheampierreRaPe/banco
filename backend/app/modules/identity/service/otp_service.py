"""Servicio de OTP (`generate_otp` / `validate_otp` / `resend_otp`; E1-T08, HU02).

Genera codigos aleatorios seguros de 6 digitos (`secrets`), guarda SOLO
`hash + salt` (`code_hash = "salt_hex$sha256(salt+codigo)"`, nunca el codigo
en claro ni en logs), con expiracion (TTL), un solo uso, contador de
intentos fallidos con bloqueo, y control de reenvio (nuevo codigo que
invalida el anterior, espera minima y maximo de reenvios).

Origen de los parametros (de donde salen TTL/reenvios):

- `otp.ttl_seconds` y `otp.max_resends` se leen de `config.parameters`
  (semilla `0001_init_schemas`: `600` y `3`, modulo `identity`) con lectura
  best-effort (`_read_int_parameter`): si la tabla no existe o el valor es
  invalido se usan los defaults de abajo. No hay fachada de config en el
  repo (verificado: ningun modulo lee `config.parameters` aun), asi que la
  lectura es SQL directo acotado a `config.parameters` (tabla transversal,
  no de otro modulo de negocio) dentro de un `SAVEPOINT`: un fallo jamas
  contamina la transaccion del llamante.
- Si no hay acceso a `config.parameters`, rigen las constantes
  `OTP_TTL_SECONDS = 600` y `OTP_MAX_RESENDS = 3`, documentadas como
  movibles a `parameters` (regla de oro 6: misma clave y mismo valor que
  la semilla para migrar sin sorpresas).
- La espera minima entre reenvios y el umbral de intentos no tienen clave
  en `config.parameters` (`03b` no las define): rigen las constantes
  `OTP_RESEND_WAIT_SECONDS` y el `max_attempts` por fila (`03b#4.5`
  default 3), ambas documentadas como candidatas a `otp.resend_wait_seconds`
  y `otp.max_attempts` cuando config las adopte.

Eventos: al validar OK con `purpose == "ACTIVATION"` se enlista
`user.activated` via `outbox.record` en la misma sesion (regla de oro 8,
mismo patron perezoso que E1-T03; best-effort documentado: si el modulo
`outbox` no estuviera disponible se registra en log y se retorna igual,
la validacion ya quedo persistida con `flush`).
Notificacion del codigo: `generate_otp`/`resend_otp` aceptan
`channel` + `template_code` opcionales y notifican via la fachada
`notifications.send` (import perezoso, mismo patron que E1-T03; best-effort:
un fallo de envio se loguea sin PII ni codigo y NO revierte el OTP).

Convencion: `flush` sin `commit`; quien llama decide la transaccion.
Sin `float`, sin secretos/PII en logs (los logs solo llevan `user_id`,
`purpose` y contadores). No toca `onboard_customer` ni `kyc_proxy`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.identity.models import OTP_PURPOSES, OtpCode
from app.modules.identity.repository import otp as otp_repo

logger = logging.getLogger(__name__)

#: Evento emitido al validar una activacion (regla de oro 8: solo via outbox).
USER_ACTIVATED_EVENT = "user.activated"

#: Tipo de agregado para el outbox.
USER_AGGREGATE_TYPE = "user"

#: Vigencia del OTP en segundos. Movible a `config.parameters`
#: (`otp.ttl_seconds`, semilla `600`): misma clave y mismo valor.
OTP_TTL_SECONDS = 600

#: Maximo de reenvios por ciclo. Movible a `config.parameters`
#: (`otp.max_resends`, semilla `3`): misma clave y mismo valor.
OTP_MAX_RESENDS = 3

#: Espera minima entre reenvios en segundos. Sin clave en
#: `config.parameters` (`03b` no la define): candidata a
#: `otp.resend_wait_seconds` cuando config la adopte.
OTP_RESEND_WAIT_SECONDS = 30

#: Tope de intentos fallidos por codigo (fallback). Manda el
#: `max_attempts` de la fila (`03b#4.5`, default 3); esta constante solo
#: rige la creacion de filas nuevas. Candidata a `otp.max_attempts`.
OTP_MAX_ATTEMPTS = 3

#: Digitos del codigo (6 digitos, `000000`-`999999` via `secrets`).
OTP_CODE_DIGITS = 6

_PARAM_TTL_KEY = "otp.ttl_seconds"
_PARAM_MAX_RESENDS_KEY = "otp.max_resends"


class OtpError(ValueError):
    """Error base de OTP (subclase de `ValueError` para mapear a 4xx en E1-T10)."""


class OtpNotFoundError(OtpError):
    """Sin OTP `PENDING` para (`user_id`, `purpose`): inexistente o ya usado."""


class OtpExpiredError(OtpError):
    """OTP vencido (se marca `EXPIRED` al detectarlo)."""


class OtpInvalidError(OtpError):
    """Codigo incorrecto (queda registrado el intento; informa restantes)."""

    def __init__(self, message: str, *, remaining_attempts: int) -> None:
        super().__init__(message)
        self.remaining_attempts = remaining_attempts


class OtpAttemptsExceededError(OtpError):
    """Bloqueo tras N intentos fallidos (umbral = `max_attempts` de la fila)."""


class OtpMaxResendsExceededError(OtpError):
    """Maximo de reenvios alcanzado (`otp.max_resends`)."""


class OtpResendTooSoonError(OtpError):
    """Reenvio dentro de la espera minima (informa segundos restantes)."""

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    """Normaliza a tz-aware UTC (SQLite devuelve naive; se asume UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _coerce_param_int(raw: object, default: int) -> int:
    if isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        candidate = int(raw)
    elif isinstance(raw, str):
        try:
            candidate = int(raw.strip())
        except (ValueError, AttributeError):
            return default
    else:
        return default
    return candidate if candidate > 0 else default


def _read_int_parameter(session: Session, key: str, default: int) -> int:
    """Lee `config.parameters.<key>` best-effort (fallback a `default`).

    `config.parameters` es tabla transversal (no de otro modulo de negocio)
    y ningun modulo del repo la lee aun (sin fachada): se usa SQL directo
    acotado a esa tabla dentro de un `SAVEPOINT`, asi un fallo (tabla
    ausente, valor invalido) no contamina la transaccion del llamante.
    """
    try:
        with session.begin_nested():
            raw = session.execute(
                sa.text("SELECT value_json FROM config.parameters WHERE key = :key"),
                {"key": key},
            ).scalar()
    except Exception:  # noqa: BLE001 - sin acceso a parameters rige la constante
        logger.debug("otp parameters sin acceso (%s): default", key)
        return default
    if raw is None:
        return default
    return _coerce_param_int(raw, default)


def _resolve_ttl_seconds(session: Session, override: int | None) -> int:
    if override is not None:
        if not isinstance(override, int) or isinstance(override, bool) or override <= 0:
            raise ValueError("ttl_seconds debe ser int > 0")
        return override
    return _read_int_parameter(session, _PARAM_TTL_KEY, OTP_TTL_SECONDS)


def _resolve_max_resends(session: Session, override: int | None) -> int:
    if override is not None:
        if not isinstance(override, int) or isinstance(override, bool) or override < 0:
            raise ValueError("max_resends debe ser int >= 0")
        return override
    raw_default = _read_int_parameter(session, _PARAM_MAX_RESENDS_KEY, OTP_MAX_RESENDS)
    return raw_default if raw_default >= 0 else OTP_MAX_RESENDS


def _new_plain_code() -> str:
    """Codigo aleatorio seguro de 6 digitos (`secrets`, con ceros a la izquierda)."""
    return f"{secrets.randbelow(10 ** OTP_CODE_DIGITS):0{OTP_CODE_DIGITS}d}"


def _hash_code(code: str) -> str:
    """Construye `"salt_hex$sha256_hex"` (97 caracteres, cabe en 128)."""
    salt = secrets.token_hex(16)
    digest = hashlib.sha256(f"{salt}{code}".encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def _verify_code(code: str, code_hash: str) -> bool:
    """Verifica el codigo contra `"salt_hex$sha256_hex"` (comparacion constante)."""
    try:
        salt, _, digest = code_hash.partition("$")
        expected = hashlib.sha256(f"{salt}{code}".encode("utf-8")).hexdigest()
    except (AttributeError, TypeError, ValueError):
        return False
    if not salt or len(digest) != 64:
        return False
    return hmac.compare_digest(expected, digest)


def _validate_new_code(code: object, *, field: str = "code") -> str:
    if not isinstance(code, str) or not code.strip():
        raise ValueError(f"{field} es obligatorio")
    text = code.strip()
    if len(text) != OTP_CODE_DIGITS or not text.isdigit():
        raise ValueError(f"{field} debe ser de {OTP_CODE_DIGITS} digitos")
    return text


def _notify_code(
    session: Session,
    *,
    user_id: uuid.UUID,
    channel: str | None,
    destination: str | None,
    template_code: str | None,
    purpose: str,
    sender=None,
) -> None:
    """Notifica via fachada `notifications.send` (best-effort, import perezoso).

    Solo actua si llegan `channel` + `destination` + `template_code`; un
    fallo de envio se loguea sin PII ni codigo y NO revierte el OTP.
    """
    if not channel or not destination or not template_code:
        return
    try:
        from app.modules.notifications.service import send as notifications_send

        notifications_send(
            session,
            channel=channel,
            recipient=destination,
            template_code=template_code,
            data={"purpose": purpose},
            user_id=user_id,
            sender=sender,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning(
            "otp notify_failed channel=%s template=%s error=%s",
            channel,
            template_code,
            type(exc).__name__,
        )


def _emit_user_activated(session: Session, *, user_id: uuid.UUID) -> None:
    """Enlista `user.activated` via outbox en la misma sesion (regla de oro 8).

    Import perezoso como E1-T03 (sin ciclos entre modulos). Best-effort
    documentado: si el modulo `outbox` no estuviera disponible se loguea y
    se continua (la validacion ya quedo persistida); cualquier otro fallo
    propaga para que el llamante revierta la transaccion completa.
    """
    try:
        from app.core.outbox import record as outbox_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("otp outbox no disponible: user.activated no enlistado")
        return
    outbox_record(
        session,
        aggregate_type=USER_AGGREGATE_TYPE,
        aggregate_id=user_id,
        event_type=USER_ACTIVATED_EVENT,
        payload={"user_id": str(user_id), "purpose": "ACTIVATION"},
    )
    session.flush()


def generate_otp(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    purpose: str,
    destination: str | None = None,
    ttl_seconds: int | None = None,
    max_attempts: int | None = None,
    now: datetime | None = None,
    channel: str | None = None,
    template_code: str | None = None,
    sender=None,
) -> tuple[OtpCode, str]:
    """Genera un OTP de un solo uso (`flush`, sin `commit`).

    Invalida (`EXPIRED`) cualquier `PENDING` previo del mismo
    (`user_id`, `purpose`): un ciclo nuevo supersede al anterior, asi solo
    hay un codigo valido a la vez. Retorna `(fila, codigo_en_claro)`; el
    codigo en claro solo existe en el retorno (para notificar/E1-T10) y
    jamas se persiste ni se loguea.
    """
    if purpose not in OTP_PURPOSES:
        raise ValueError(
            f"purpose debe ser uno de {OTP_PURPOSES}, recibido: {purpose!r}"
        )
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"user_id debe ser UUID, recibido: {user_id!r}") from exc
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()
    ttl = _resolve_ttl_seconds(session, ttl_seconds)
    if max_attempts is not None and (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or max_attempts < 1
    ):
        raise ValueError("max_attempts debe ser int >= 1")
    limit = max_attempts if max_attempts is not None else OTP_MAX_ATTEMPTS

    previous = otp_repo.get_active(session, uid, purpose)
    if previous is not None:
        otp_repo.mark_expired(session, previous)

    plain = _new_plain_code()
    row = otp_repo.create_otp(
        session,
        user_id=uid,
        purpose=purpose,
        code_hash=_hash_code(plain),
        expires_at=moment + timedelta(seconds=ttl),
        destination=destination,
        max_attempts=limit,
        resend_count=0,
    )
    logger.info("otp generated purpose=%s attempts_limit=%d", purpose, limit)
    _notify_code(
        session,
        user_id=uid,
        channel=channel,
        destination=row.destination,
        template_code=template_code,
        purpose=purpose,
        sender=sender,
    )
    return row, plain


def resend_otp(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    purpose: str,
    destination: str | None = None,
    max_resends: int | None = None,
    wait_seconds: int | None = None,
    now: datetime | None = None,
    channel: str | None = None,
    template_code: str | None = None,
    sender=None,
) -> tuple[OtpCode, str]:
    """Reenvia el OTP: codigo nuevo que invalida el anterior (`flush`, sin `commit`).

    Exige un `PENDING` vigente (sin el, usar `generate_otp`); respeta la
    espera minima desde su `created_at` (`OtpResendTooSoonError` con
    `retry_after_seconds`) y el maximo de reenvios del ciclo
    (`OtpMaxResendsExceededError`). El anterior pasa a `EXPIRED`: el codigo
    viejo jamas se reutiliza. Retorna `(fila_nueva, codigo_en_claro)`.
    """
    if purpose not in OTP_PURPOSES:
        raise ValueError(
            f"purpose debe ser uno de {OTP_PURPOSES}, recibido: {purpose!r}"
        )
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"user_id debe ser UUID, recibido: {user_id!r}") from exc
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()
    if wait_seconds is not None and (
        not isinstance(wait_seconds, int)
        or isinstance(wait_seconds, bool)
        or wait_seconds < 0
    ):
        raise ValueError("wait_seconds debe ser int >= 0")
    wait = OTP_RESEND_WAIT_SECONDS if wait_seconds is None else wait_seconds

    active = otp_repo.get_active(session, uid, purpose)
    if active is None:
        raise OtpNotFoundError("sin OTP pendiente: genere uno nuevo primero")
    if _as_aware(active.expires_at) <= moment:
        otp_repo.mark_expired(session, active)
        raise OtpExpiredError("el OTP pendiente vencio: genere uno nuevo")

    ceiling = _resolve_max_resends(session, max_resends)
    if int(active.resend_count) + 1 > ceiling:
        raise OtpMaxResendsExceededError(
            f"maximo de {ceiling} reenvios alcanzado para {purpose!r}"
        )
    elapsed = (moment - _as_aware(active.created_at)).total_seconds()
    if elapsed < wait:
        raise OtpResendTooSoonError(
            f"espere {int(wait - elapsed)} s antes de reenviar",
            retry_after_seconds=int(wait - elapsed),
        )

    ttl = _resolve_ttl_seconds(session, None)
    otp_repo.mark_expired(session, active)
    plain = _new_plain_code()
    row = otp_repo.create_otp(
        session,
        user_id=uid,
        purpose=purpose,
        code_hash=_hash_code(plain),
        expires_at=moment + timedelta(seconds=ttl),
        destination=destination if destination is not None else active.destination,
        max_attempts=int(active.max_attempts),
        resend_count=int(active.resend_count) + 1,
    )
    logger.info(
        "otp resent purpose=%s resend=%d/%d", purpose, row.resend_count, ceiling
    )
    _notify_code(
        session,
        user_id=uid,
        channel=channel,
        destination=row.destination,
        template_code=template_code,
        purpose=purpose,
        sender=sender,
    )
    return row, plain


def validate_otp(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    purpose: str,
    code: str,
    now: datetime | None = None,
) -> OtpCode:
    """Valida el OTP vigente (`flush`, sin `commit`; un solo uso).

    Exito: marca `USED` (+ `consumed_at`) y, solo si
    `purpose == "ACTIVATION"`, enlista `user.activated` via outbox en la
    misma sesion. Fallo por codigo: suma un intento; al agotar
    `max_attempts` (umbral documentado: `03b#4.5` default 3 por fila)
    bloquea (`EXPIRED`) y lanza `OtpAttemptsExceededError`; vencido lanza
    `OtpExpiredError`; reutilizado o inexistente, `OtpNotFoundError`
    (los `USED`/`EXPIRED` jamas se revalidan).
    """
    if purpose not in OTP_PURPOSES:
        raise ValueError(
            f"purpose debe ser uno de {OTP_PURPOSES}, recibido: {purpose!r}"
        )
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"user_id debe ser UUID, recibido: {user_id!r}") from exc
    plain = _validate_new_code(code)
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()

    active = otp_repo.get_active(session, uid, purpose)
    if active is None:
        raise OtpNotFoundError("OTP inexistente o ya utilizado")
    if _as_aware(active.expires_at) <= moment:
        otp_repo.mark_expired(session, active)
        raise OtpExpiredError("OTP vencido")
    if int(active.attempts) >= int(active.max_attempts):
        otp_repo.mark_expired(session, active)
        raise OtpAttemptsExceededError("OTP bloqueado por intentos agotados")

    if _verify_code(plain, active.code_hash):
        otp_repo.mark_used(session, active, moment)
        logger.info("otp validated purpose=%s", purpose)
        if purpose == "ACTIVATION":
            _emit_user_activated(session, user_id=uid)
        return active

    used = otp_repo.bump_attempts(session, active)
    remaining = int(active.max_attempts) - used
    if remaining <= 0:
        otp_repo.mark_expired(session, active)
        raise OtpAttemptsExceededError("OTP bloqueado por intentos agotados")
    logger.info("otp mismatch purpose=%s remaining=%d", purpose, remaining)
    raise OtpInvalidError("codigo incorrecto", remaining_attempts=remaining)


__all__ = [
    "OTP_CODE_DIGITS",
    "OTP_MAX_ATTEMPTS",
    "OTP_MAX_RESENDS",
    "OTP_RESEND_WAIT_SECONDS",
    "OTP_TTL_SECONDS",
    "USER_ACTIVATED_EVENT",
    "USER_AGGREGATE_TYPE",
    "OtpAttemptsExceededError",
    "OtpError",
    "OtpExpiredError",
    "OtpInvalidError",
    "OtpMaxResendsExceededError",
    "OtpNotFoundError",
    "OtpResendTooSoonError",
    "generate_otp",
    "resend_otp",
    "validate_otp",
]
