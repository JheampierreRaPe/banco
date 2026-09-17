"""Login con nonce firmado por el dispositivo (`request_challenge` /
`login_with_device`; E1-T13, HU03 CA-01).

Flujo (D07: biometria local, sin liveness del servidor aqui):

1. `POST /auth/login/challenge {user_ref[, device_id]}` -> `nonce` de un
   solo uso con TTL corto (`domain/nonce.py`, `NONCE_TTL_SECONDS = 120`).
2. La app exige Face ID/huella del telefono y firma el `nonce` con la clave
   guardada en almacenamiento seguro.
3. `POST /auth/login/facial {nonce, device_id, signature}` -> verifica la
   firma contra `device_bindings.public_key`, abre `sessions` y devuelve
   JWT corto (`create_access_token`, reutilizado: no se inventan tokens) +
   refresh opaco (solo su hash SHA-256 se persiste).

DECISION CRIPTOGRAFICA (documentada, verificada en el `.venv`):

- El `.venv` trae `PyJWT 2.14.0` pero NO `cryptography`/`ecdsa`/`nacl`
  (verificado con `importlib.util.find_spec`): PyJWT sin `cryptography`
  no puede verificar ECDSA/EdDSA, asi que la ruta asimetrica no es usable
  hoy.
- Ruta preferida (produccion): `public_key` en PEM `Ed25519`/`EC` y firma
  en base64, verificada con `cryptography` cuando esta disponible
  (`verify_signature` la intenta primero, con import perezoso y sin romper
  si el paquete falta).
- Fallback implementado y probado: `public_key = "hmac:<hex>"` (secreto de
  16+ bytes) y `signature` = HMAC-SHA256 hex del `nonce`, comparado con
  `hmac.compare_digest`. Es HMAC estandar (sin cripto inventada): el
  secreto vive en el almacenamiento seguro del dispositivo igual que la
  clave privada. Cuando `cryptography` entre al `.venv`, los bindings
  nuevos usan PEM sin cambiar el contrato (el formato se autodetecta por
  prefijo/contenido).
- Firma invalida, binding inexistente/revocado, usuario inexistente o
  `user_ref` malformado responden IDENTICO (`LoginInvalidError`, mismo
  mensaje): no se filtra existencia ni estado. Nonce reutilizado ->
  generico; nonce vencido -> `LoginExpiredError` (mismo patron que
  `otp_service`: `EXPIRED_*` vs generico).

Registro de dispositivo ("si es confiable"): tras un login exitoso se
hace upsert del binding (toca `last_used_at` + meta `platform`/
`biometric_type`); el alta inicial del binding la hace el flujo de
enrolamiento/recuperacion (E1-T20) o `register_binding` directo: sin clave
previa no hay contra que verificar (por eso un dispositivo desconocido
responde generico, no "registrese aqui").

Eventos: al abrir sesion se enlista `auth.login_succeeded` via
`outbox.record` en la misma sesion (regla de oro 8, import perezoso como
E1-T03; best-effort documentado: si el outbox fallara se loguea y el login
igual se retorna, la sesion ya quedo persistida con `flush`).

Convencion: `flush` sin `commit`; quien llama decide la transaccion.
Sin `float`, sin secretos/PIN en claro (el PIN jamas pasa por aqui), sin
PII en logs (solo contadores/tipos). No toca `onboard_customer`,
`kyc_proxy`, `otp_service` ni `activation` (solo los reutiliza el JWT de
`core.security`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.modules.identity import repository as identity_repo
from app.modules.identity.domain import nonce as nonce_domain

logger = logging.getLogger(__name__)

#: Evento emitido al abrir sesion (regla de oro 8: solo via outbox).
LOGIN_SUCCEEDED_EVENT = "auth.login_succeeded"

#: Accion de auditoria para logins exitosos (reutiliza el evento).
AUDIT_LOGIN_SUCCEEDED = LOGIN_SUCCEEDED_EVENT

#: Accion de auditoria para intentos fallidos (E1-T17, HU03 CA-03).
AUDIT_FAILED_ATTEMPT = "auth.failed_attempt"

#: Tipo de agregado para el outbox.
USER_AGGREGATE_TYPE = "user"

#: Vigencia del refresh opaco en segundos (30 dias). Movible a
#: `config.parameters` (`auth.refresh_ttl_seconds`, misma clave y mismo
#: valor) cuando config la adopte (regla de oro 6). El JWT corto lo gobierna
#: `settings.access_token_minutes` (15 min, reutilizado sin cambios).
REFRESH_TTL_SECONDS = 30 * 24 * 3600

#: Mensaje generico estable (identico exista o no el usuario/dispositivo).
INVALID_MESSAGE = "Credenciales de dispositivo invalidas"
EXPIRED_MESSAGE = "Desafio vencido, solicite uno nuevo"


class LoginInvalidError(ValueError):
    """Firma invalida, nonce reutilizado/inexistente, binding ausente o
    usuario inexistente: una sola clase/mensaje para no filtrar (-> 400
    `INVALID_LOGIN`)."""


class LoginExpiredError(ValueError):
    """Nonce vencido (-> 400 `EXPIRED_NONCE`, mismo patron que OTP)."""


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


def _hmac_secret(public_key: str) -> bytes | None:
    """Extrae el secreto del formato `"hmac:<hex>"` (`None` si no aplica).

    Exige secreto de 16+ bytes (32+ hex): menos que eso se rechaza (la
    verificacion responde generico, nunca detalla el motivo).
    """
    if not isinstance(public_key, str) or not public_key.startswith("hmac:"):
        return None
    try:
        raw = bytes.fromhex(public_key[len("hmac:"):].strip())
    except (ValueError, AttributeError):
        return None
    return raw if len(raw) >= 16 else None


def _verify_asymmetric(nonce: str, signature: str, public_key: str) -> bool:
    """Verifica firma Ed25519/ECDSA con `cryptography` (`False` si no esta).

    `public_key`: PEM (`-----BEGIN ... PUBLIC KEY-----`); `signature`:
    base64 del mensaje `nonce` en UTF-8. Import perezoso: si `cryptography`
    no esta instalada retorna `False` sin excepcion (el llamante cae al
    siguiente formato / a generico). Cualquier fallo criptografico es
    `False`: jamas se detalla (sin filtrar).
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, padding
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except ImportError:
        return False
    try:
        key = load_pem_public_key(public_key.encode("utf-8"))
        raw_sig = base64.b64decode(signature, validate=True)
        message = nonce.encode("utf-8")
        key_type = type(key).__name__
        if key_type == "Ed25519PublicKey":
            key.verify(raw_sig, message)
        elif key_type in ("ECPublicKey", "_ECPublicKey"):
            key.verify(raw_sig, message, ec.ECDSA(hashes.SHA256()))
        elif key_type == "RSAPublicKey":
            key.verify(raw_sig, message, padding.PKCS1v15(), hashes.SHA256())
        else:  # Tipo de clave no soportado: generico.
            return False
    except Exception:  # noqa: BLE001 - todo fallo cripto es firma invalida
        try:
            _ = InvalidSignature
        except Exception:  # pragma: no cover - referencia al import
            pass
        return False
    return True


def verify_signature(nonce: str, signature: str, public_key: str) -> bool:
    """Verifica la firma del `nonce` contra la clave del binding.

    Orden: HMAC (`"hmac:<hex>"`, comparacion constante) y luego
    asimetrico (`cryptography` si disponible). Entradas malformadas o
    formatos desconocidos -> `False` (el servicio responde generico).
    """
    if (
        not isinstance(nonce, str)
        or not nonce
        or not isinstance(signature, str)
        or not signature.strip()
        or not isinstance(public_key, str)
        or not public_key.strip()
    ):
        return False
    sig = signature.strip()
    key = public_key.strip()

    secret = _hmac_secret(key)
    if secret is not None:
        try:
            expected = hmac.new(secret, nonce.encode("utf-8"), hashlib.sha256)
        except (TypeError, ValueError):
            return False
        if len(sig) != expected.digest_size * 2:
            return False
        try:
            candidate = bytes.fromhex(sig)
        except ValueError:
            return False
        return hmac.compare_digest(candidate, expected.digest())

    if "BEGIN" in key and "PUBLIC KEY" in key:
        return _verify_asymmetric(nonce, sig, key)
    return False


def _emit_login_succeeded(
    session: Session, *, user_id: uuid.UUID, session_id: uuid.UUID, device_id: str
) -> None:
    """Enlista `auth.login_succeeded` via outbox (best-effort, no bloquea)."""
    try:
        from app.core.outbox import record as outbox_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("device_login outbox no disponible: evento no enlistado")
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
        logger.warning("device_login evento no enlistado error=%s", type(exc).__name__)


def _audit_auth_event(
    session: Session,
    *,
    user_id: uuid.UUID | None,
    device_id: str | None,
    ip: str | None,
    moment: datetime,
    action: str,
    method: str,
    session_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> None:
    """Registra `auth.login_succeeded` / `auth.failed_attempt` via fachada.

    E1-T17 (best-effort como E1-T03): si la auditoria falla se loguea y el
    login igual se retorna; nunca `commit` (solo `flush` via la fachada).
    Sin PII: solo IDs (`user_id`, `session_id`, `device_id`, `ip`, `at`,
    `method`, `reason`); jamas PIN/firmas/nonces/tokens.
    """
    try:
        from app.modules.audit.service import record as audit_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("device_login audit no disponible")
        return
    try:
        metadata: dict = {"method": method, "at": moment.isoformat()}
        if session_id is not None:
            metadata["session_id"] = str(session_id)
        if reason is not None:
            metadata["reason"] = reason
        audit_record(
            session,
            actor=user_id,
            action=action,
            entity=USER_AGGREGATE_TYPE,
            entity_id=user_id,
            metadata=metadata,
            device_id=device_id,
            ip=ip,
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 - best-effort documentado E1-T17
        logger.warning("device_login audit no registrado error=%s", type(exc).__name__)


def request_challenge(
    session: Session,
    *,
    user_ref: str,
    device_id: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Emite el `nonce` a firmar (sin `commit`, sin writes en BD).

    Usuario inexistente o `user_ref` malformado responden generico
    (`LoginInvalidError`): el challenge no enumera usuarios.
    """
    uid = _coerce_user_id(user_ref)
    user = identity_repo.get_user(session, uid) if uid is not None else None
    if user is None:
        # Rama ciega: mismo error que cualquier fallo posterior.
        raise LoginInvalidError(INVALID_MESSAGE)
    try:
        nonce, expires_at = nonce_domain.issue_nonce(
            user.id, device_id=device_id, now=now
        )
    except ValueError as exc:
        raise LoginInvalidError(INVALID_MESSAGE) from exc
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()
    aware = _as_aware(expires_at)
    expires_in = max(0, int((aware - moment).total_seconds()))
    logger.info("login challenge emitido")
    return {"nonce": nonce, "expires_in": expires_in}


def login_with_device(
    session: Session,
    *,
    nonce: str,
    device_id: str,
    signature: str,
    user_ref: str | None = None,
    platform: str | None = None,
    biometric_type: str | None = None,
    device_info: dict | None = None,
    ip: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Verifica la firma del `nonce` y abre sesion (`flush`, sin `commit`).

    Exito: upsert del binding (`last_used_at` + meta), fila en `sessions`,
    JWT corto (reutiliza `create_access_token`) + refresh opaco (solo su
    hash se persiste). Todo fallo de verificacion responde generico
    (`LoginInvalidError`); solo el TTL agotado distingue (`LoginExpiredError`).
    """
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()

    try:
        entry = nonce_domain.consume_nonce(nonce, now=moment)
    except nonce_domain.NonceExpiredError as exc:
        _audit_auth_event(
            session,
            user_id=_coerce_user_id(user_ref) if user_ref is not None else None,
            device_id=device_id,
            ip=ip,
            moment=moment,
            action=AUDIT_FAILED_ATTEMPT,
            method="facial",
            reason="expired",
        )
        raise LoginExpiredError(EXPIRED_MESSAGE) from exc
    except (nonce_domain.NonceError, ValueError) as exc:
        # Inexistente o reutilizado (un solo uso): generico, sin filtrar.
        _audit_auth_event(
            session,
            user_id=_coerce_user_id(user_ref) if user_ref is not None else None,
            device_id=device_id,
            ip=ip,
            moment=moment,
            action=AUDIT_FAILED_ATTEMPT,
            method="facial",
            reason="invalid",
        )
        raise LoginInvalidError(INVALID_MESSAGE) from exc

    user = identity_repo.get_user(session, entry.user_id)
    if user is None:
        _audit_auth_event(
            session,
            user_id=entry.user_id,
            device_id=device_id,
            ip=ip,
            moment=moment,
            action=AUDIT_FAILED_ATTEMPT,
            method="facial",
            reason="invalid",
        )
        raise LoginInvalidError(INVALID_MESSAGE)
    if user_ref is not None:
        claimed = _coerce_user_id(user_ref)
        if claimed is None or claimed != user.id:
            _audit_auth_event(
                session,
                user_id=user.id,
                device_id=device_id,
                ip=ip,
                moment=moment,
                action=AUDIT_FAILED_ATTEMPT,
                method="facial",
                reason="invalid",
            )
            raise LoginInvalidError(INVALID_MESSAGE)

    try:
        binding = identity_repo.get_binding(session, user.id, device_id)
    except ValueError as exc:
        _audit_auth_event(
            session,
            user_id=user.id,
            device_id=device_id,
            ip=ip,
            moment=moment,
            action=AUDIT_FAILED_ATTEMPT,
            method="facial",
            reason="invalid",
        )
        raise LoginInvalidError(INVALID_MESSAGE) from exc
    if binding is None or binding.status != "ACTIVE":
        _audit_auth_event(
            session,
            user_id=user.id,
            device_id=device_id,
            ip=ip,
            moment=moment,
            action=AUDIT_FAILED_ATTEMPT,
            method="facial",
            reason="invalid",
        )
        raise LoginInvalidError(INVALID_MESSAGE)
    if not verify_signature(entry.nonce, signature, binding.public_key):
        _audit_auth_event(
            session,
            user_id=user.id,
            device_id=device_id,
            ip=ip,
            moment=moment,
            action=AUDIT_FAILED_ATTEMPT,
            method="facial",
            reason="invalid",
        )
        raise LoginInvalidError(INVALID_MESSAGE)

    identity_repo.touch_binding(session, binding, moment)
    if platform is not None and platform in ("android", "ios"):
        binding.platform = platform
        session.flush()
    if biometric_type is not None and biometric_type in ("FACE", "FINGERPRINT"):
        binding.biometric_type = biometric_type
        session.flush()

    refresh = secrets.token_urlsafe(32)
    row = identity_repo.create_session(
        session,
        user.id,
        identity_repo.hash_refresh_token(refresh),
        moment + timedelta(seconds=REFRESH_TTL_SECONDS),
        device_id=binding.device_id,
        device_info=device_info,
        ip=ip,
    )
    access_token = create_access_token(subject=str(user.id))
    _emit_login_succeeded(
        session, user_id=user.id, session_id=row.id, device_id=binding.device_id
    )
    _audit_auth_event(
        session,
        user_id=user.id,
        device_id=binding.device_id,
        ip=ip,
        moment=moment,
        action=AUDIT_LOGIN_SUCCEEDED,
        method="facial",
        session_id=row.id,
    )
    logger.info("login facial ok")
    try:
        aware_exp = _as_aware(row.expires_at)
        refresh_in = max(0, int((aware_exp - moment).total_seconds()))
    except (AttributeError, TypeError):
        refresh_in = REFRESH_TTL_SECONDS
    return {
        "access_token": access_token,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "session_id": str(row.id),
        "expires_in": refresh_in,
    }


__all__ = [
    "AUDIT_FAILED_ATTEMPT",
    "AUDIT_LOGIN_SUCCEEDED",
    "EXPIRED_MESSAGE",
    "INVALID_MESSAGE",
    "LOGIN_SUCCEEDED_EVENT",
    "REFRESH_TTL_SECONDS",
    "USER_AGGREGATE_TYPE",
    "LoginExpiredError",
    "LoginInvalidError",
    "login_with_device",
    "request_challenge",
    "verify_signature",
]
