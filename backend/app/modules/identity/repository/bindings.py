"""Persistencia de dispositivos y sesiones (`identity.device_bindings`,
`identity.sessions`; E1-T13, HU03 CA-01).

Capa de datos sin endpoints: solo SQLAlchemy sobre el schema propio
(`identity`). Sin FK entre schemas y sin acceso a tablas de otros modulos
(`user_id` referencia `identity.users`, mismo schema, regla de oro 4).

Convencion (igual que el resto del repositorio identity): `flush` sin
`commit`; quien llama decide la transaccion. Este repositorio NO verifica
firmas ni emite tokens (eso vive en `domain/nonce.py` y
`service/device_login.py`): persiste el `public_key` tal cual y
transiciona sesiones. Sin `float`, sin secretos/PII en logs, sin guardar
frames biometricos (solo la clave publica del dispositivo).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.identity.models import (
    BIOMETRIC_TYPES,
    DEVICE_BINDING_STATUSES,
    DEVICE_PLATFORMS,
    DeviceBinding,
    UserSession,
)


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def _require_device_id(device_id: object) -> str:
    if not isinstance(device_id, str) or not device_id.strip():
        raise ValueError("device_id es obligatorio")
    text = device_id.strip()
    if len(text) > 128:
        raise ValueError("device_id supera 128 caracteres")
    return text


def register_binding(
    session: Session,
    user_id: uuid.UUID | str,
    device_id: str,
    public_key: str,
    *,
    platform: str | None = None,
    biometric_type: str | None = None,
) -> DeviceBinding:
    """Registra o actualiza el dispositivo confiable (upsert, `flush`).

    Si ya existe (`user_id`, `device_id`) actualiza `public_key`/meta y lo
    deja `ACTIVE`; si no, lo crea. Valida antes de insertar: `public_key`
    obligatoria (texto, sin tope salvo vacio), `platform` en
    (`android`, `ios`) y `biometric_type` en (`FACE`, `FINGERPRINT`) cuando
    vienen. Nunca guarda secretos/PIN en claro aqui: solo la clave publica
    (o el secreto HMAC documentado en `device_login`, nunca un PIN).
    """
    uid = _coerce_uuid(user_id, "user_id")
    device = _require_device_id(device_id)
    if not isinstance(public_key, str) or not public_key.strip():
        raise ValueError("public_key es obligatoria")
    key = public_key.strip()
    if platform is not None and platform not in DEVICE_PLATFORMS:
        raise ValueError(f"platform debe ser una de {DEVICE_PLATFORMS}, recibido: {platform!r}")
    if biometric_type is not None and biometric_type not in BIOMETRIC_TYPES:
        raise ValueError(
            f"biometric_type debe ser una de {BIOMETRIC_TYPES}, " f"recibido: {biometric_type!r}"
        )

    stmt = sa.select(DeviceBinding).where(
        DeviceBinding.user_id == uid,
        DeviceBinding.device_id == device,
    )
    row = session.scalars(stmt).first()
    if row is None:
        row = DeviceBinding(
            user_id=uid,
            device_id=device,
            public_key=key,
            platform=platform,
            biometric_type=biometric_type,
            status="ACTIVE",
        )
        session.add(row)
    else:
        row.public_key = key
        row.status = "ACTIVE"
        if platform is not None:
            row.platform = platform
        if biometric_type is not None:
            row.biometric_type = biometric_type
    session.flush()
    return row


def get_binding(session: Session, user_id: uuid.UUID | str, device_id: str) -> DeviceBinding | None:
    """Lee el binding de (`user_id`, `device_id`) (`None` si no existe).

    Retorna la fila en cualquier estado: el llamante decide (un `REVOKED`
    responde generico sin filtrar, igual que inexistente).
    """
    uid = _coerce_uuid(user_id, "user_id")
    device = _require_device_id(device_id)
    stmt = sa.select(DeviceBinding).where(
        DeviceBinding.user_id == uid,
        DeviceBinding.device_id == device,
    )
    return session.scalars(stmt).first()


def touch_binding(session: Session, row: DeviceBinding, used_at: datetime) -> DeviceBinding:
    """Actualiza `last_used_at` tras un login exitoso (`flush`)."""
    if not isinstance(used_at, datetime):
        raise TypeError(f"used_at debe ser datetime, recibido: {used_at!r}")
    row.last_used_at = used_at
    session.flush()
    return row


def hash_refresh_token(refresh_token: str) -> str:
    """SHA-256 hex del refresh opaco (64 caracteres, cabe en 128).

    Solo el hash se persiste (`refresh_token_hash` UQ); el token en claro
    solo viaja una vez al cliente.
    """
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ValueError("refresh_token es obligatorio")
    return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()


def create_session(
    session: Session,
    user_id: uuid.UUID | str,
    refresh_token_hash: str,
    expires_at: datetime,
    *,
    device_id: str | None = None,
    device_info: dict | None = None,
    ip: str | None = None,
) -> UserSession:
    """Crea la sesion con el hash del refresh (`flush`, sin `commit`).

    Valida antes de insertar: `refresh_token_hash` obligatorio (<=128),
    `expires_at` datetime obligatorio, `device_id`/`ip` acotados,
    `device_info` dict JSON-serializable cuando viene.
    """
    uid = _coerce_uuid(user_id, "user_id")
    if (
        not isinstance(refresh_token_hash, str)
        or not refresh_token_hash.strip()
        or len(refresh_token_hash) > 128
    ):
        raise ValueError("refresh_token_hash invalido (texto de 1..128)")
    if not isinstance(expires_at, datetime):
        raise TypeError(f"expires_at debe ser datetime, recibido: {expires_at!r}")
    device = _require_device_id(device_id) if device_id is not None else None
    if ip is not None and (not isinstance(ip, str) or not ip.strip() or len(ip) > 45):
        raise ValueError("ip debe ser texto de 1..45 caracteres")
    if device_info is not None and not isinstance(device_info, dict):
        raise TypeError(f"device_info debe ser dict, recibido: {device_info!r}")

    row = UserSession(
        user_id=uid,
        refresh_token_hash=refresh_token_hash.strip(),
        expires_at=expires_at,
        device_id=device,
        device_info=dict(device_info) if device_info is not None else None,
        ip=ip.strip() if ip is not None else None,
    )
    session.add(row)
    session.flush()
    return row


def get_session_by_refresh_hash(session: Session, refresh_token_hash: str) -> UserSession | None:
    """Lee una sesion por su `refresh_token_hash` (`None` si no existe)."""
    if not isinstance(refresh_token_hash, str) or not refresh_token_hash.strip():
        raise ValueError("refresh_token_hash es obligatorio")
    stmt = sa.select(UserSession).where(
        UserSession.refresh_token_hash == refresh_token_hash.strip()
    )
    return session.scalars(stmt).first()


def revoke_session(session: Session, row: UserSession, revoked_at: datetime) -> UserSession:
    """Marca la sesion como revocada (`flush`, sin `commit`)."""
    if not isinstance(revoked_at, datetime):
        raise TypeError(f"revoked_at debe ser datetime, recibido: {revoked_at!r}")
    row.revoked_at = revoked_at
    session.flush()
    return row


def revoke_user_sessions(session: Session, user_id: uuid.UUID | str, revoked_at: datetime) -> int:
    """Revoca TODAS las sesiones activas del usuario (`flush`, sin `commit`).

    Respuesta ante reuso de un refresh revocado (E1-T15, posible robo del
    refresh): mata la cadena completa, incluido el sucesor legitimo (el
    usuario reingresa). Retorna cuantas sesiones revoco.
    """
    uid = _coerce_uuid(user_id, "user_id")
    if not isinstance(revoked_at, datetime):
        raise TypeError(f"revoked_at debe ser datetime, recibido: {revoked_at!r}")
    stmt = sa.select(UserSession).where(
        UserSession.user_id == uid,
        UserSession.revoked_at.is_(None),
    )
    rows = list(session.scalars(stmt).all())
    for row in rows:
        row.revoked_at = revoked_at
    session.flush()
    return len(rows)


__all__ = [
    "BIOMETRIC_TYPES",
    "DEVICE_BINDING_STATUSES",
    "DEVICE_PLATFORMS",
    "DeviceBinding",
    "UserSession",
    "create_session",
    "get_binding",
    "get_session_by_refresh_hash",
    "hash_refresh_token",
    "register_binding",
    "revoke_session",
    "revoke_user_sessions",
    "touch_binding",
]
