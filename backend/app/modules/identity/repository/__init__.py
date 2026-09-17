"""Repositorio de identidad (`users`, `credentials`, `kyc_verifications`,
`otp_codes`; E1-T03+E1-T04+E1-T08, HU01/HU02).

Capa de datos sin endpoints: solo SQLAlchemy sobre el schema propio
(`identity`). Sin FK entre schemas y sin acceso a tablas de otros modulos:
`user_id`/`account refs` son UUID logicos fuera de aqui (regla de oro 4).

Convencion: las funciones hacen `flush` y no `commit`; quien llama decide
la transaccion (permite rollback total en `onboard_customer` y atomicidad
con `outbox`). No publica eventos aqui (regla de oro 8: `kyc.completed`
solo via `outbox.record` en la capa de servicio, misma sesion).
Sin `float`, sin secretos/PII en logs, sin guardar frames biometricos.
"""

from __future__ import annotations

import uuid
from datetime import date

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.identity.models import (
    BIOMETRIC_TYPES,
    DEVICE_BINDING_STATUSES,
    DEVICE_PLATFORMS,
    DOC_TYPES,
    KYC_STATUSES,
    OTP_PURPOSES,
    OTP_STATUSES,
    USER_STATUSES,
    Credential,
    DeviceBinding,
    OtpCode,
    User,
    UserSession,
)
from app.modules.identity.repository.bindings import (
    create_session,
    get_binding,
    get_session_by_refresh_hash,
    hash_refresh_token,
    register_binding,
    revoke_session,
    revoke_user_sessions,
    touch_binding,
)
from app.modules.identity.repository.kyc_verifications import (
    MAX_FAILURE_REASON,
    PROVIDER_DEFAULT,
    get_verification,
    list_by_user,
    save_verification,
)
from app.modules.identity.repository.otp import (
    bump_attempts,
    create_otp,
    get_active as get_active_otp,
    mark_expired as mark_otp_expired,
    mark_used as mark_otp_used,
)


class DuplicateDocumentError(ValueError):
    """`doc_number_hash` duplicado: el documento ya tiene un usuario."""


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def _require_text(value: str, field: str, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} es obligatorio")
    text = value.strip()
    if len(text) > max_len:
        raise ValueError(f"{field} supera {max_len} caracteres")
    return text


def create_user(
    session: Session,
    *,
    doc_type: str,
    doc_number_hash: str,
    first_name: str,
    last_name: str,
    doc_number_masked: str | None = None,
    birth_date: date | None = None,
    email: str | None = None,
    phone: str | None = None,
    status: str = "PENDING_ACTIVATION",
    kyc_status: str = "VERIFIED",
    risk_profile: str = "STANDARD",
) -> User:
    """Crea el usuario (`flush`, sin `commit`).

    Valida antes de insertar (nada a medias): `doc_type` del enum,
    `doc_number_hash` obligatorio y unico (duplicado lanza
    `DuplicateDocumentError`), nombres obligatorios, `status`/`kyc_status`
    de sus enums. El UQ de BD (`uq_users_doc_number_hash`) queda como
    respaldo ante carreras.
    """
    if doc_type not in DOC_TYPES:
        raise ValueError(f"doc_type debe ser uno de {DOC_TYPES}, recibido: {doc_type!r}")
    doc_hash = _require_text(doc_number_hash, "doc_number_hash", 128)
    first = _require_text(first_name, "first_name", 100)
    last = _require_text(last_name, "last_name", 100)
    if status not in USER_STATUSES:
        raise ValueError(f"status debe ser uno de {USER_STATUSES}, recibido: {status!r}")
    if kyc_status not in KYC_STATUSES:
        raise ValueError(f"kyc_status debe ser uno de {KYC_STATUSES}, recibido: {kyc_status!r}")
    if doc_number_masked is not None and len(doc_number_masked) > 20:
        raise ValueError("doc_number_masked supera 20 caracteres")
    if email is not None and len(email) > 320:
        raise ValueError("email supera 320 caracteres")
    if phone is not None and len(phone) > 20:
        raise ValueError("phone supera 20 caracteres")
    if birth_date is not None and not isinstance(birth_date, date):
        raise TypeError(f"birth_date debe ser date, recibido: {birth_date!r}")
    if (
        session.scalar(sa.select(User.id).where(User.doc_number_hash == doc_hash))
        is not None
    ):
        raise DuplicateDocumentError("documento ya registrado")
    if email is not None and session.scalar(sa.select(User.id).where(User.email == email)) is not None:
        raise ValueError("email ya registrado")

    user = User(
        doc_type=doc_type,
        doc_number_hash=doc_hash,
        doc_number_masked=doc_number_masked,
        first_name=first,
        last_name=last,
        birth_date=birth_date,
        email=email,
        phone=phone,
        status=status,
        kyc_status=kyc_status,
        risk_profile=risk_profile,
    )
    session.add(user)
    session.flush()
    return user


def get_user(session: Session, user_id: uuid.UUID | str) -> User | None:
    """Lee un usuario por id (`None` si no existe)."""
    return session.get(User, _coerce_uuid(user_id, "user_id"))


def get_by_doc_hash(session: Session, doc_number_hash: str) -> User | None:
    """Lee un usuario por `doc_number_hash` (`None` si no existe)."""
    stmt = sa.select(User).where(User.doc_number_hash == doc_number_hash)
    return session.scalars(stmt).first()


def get_by_email(session: Session, email: str) -> User | None:
    """Lee un usuario por `email` (`None` si no existe)."""
    stmt = sa.select(User).where(User.email == email)
    return session.scalars(stmt).first()


def create_credential(
    session: Session,
    user_id: uuid.UUID | str,
    *,
    password_hash: str | None = None,
    pin_hash: str | None = None,
) -> Credential:
    """Crea la credencial 1:1 del usuario (`flush`, sin `commit`).

    Guarda solo hashes, nunca secretos en claro. Al menos una llamada
    posterior (E1-T14) garantiza credencial operativa si ambos son `None`.
    """
    uid = _coerce_uuid(user_id, "user_id")
    if session.get(Credential, uid) is not None:
        raise ValueError("el usuario ya tiene credencial")
    credential = Credential(
        user_id=uid,
        password_hash=password_hash,
        pin_hash=pin_hash,
    )
    session.add(credential)
    session.flush()
    return credential


def get_credential(session: Session, user_id: uuid.UUID | str) -> Credential | None:
    """Lee la credencial de un usuario (`None` si no existe)."""
    return session.get(Credential, _coerce_uuid(user_id, "user_id"))


__all__ = [
    "BIOMETRIC_TYPES",
    "DEVICE_BINDING_STATUSES",
    "DEVICE_PLATFORMS",
    "DOC_TYPES",
    "KYC_STATUSES",
    "MAX_FAILURE_REASON",
    "OTP_PURPOSES",
    "OTP_STATUSES",
    "PROVIDER_DEFAULT",
    "USER_STATUSES",
    "Credential",
    "DeviceBinding",
    "DuplicateDocumentError",
    "OtpCode",
    "User",
    "UserSession",
    "bump_attempts",
    "create_credential",
    "create_otp",
    "create_session",
    "create_user",
    "get_active_otp",
    "get_binding",
    "get_by_doc_hash",
    "get_by_email",
    "get_credential",
    "get_session_by_refresh_hash",
    "get_user",
    "get_verification",
    "hash_refresh_token",
    "list_by_user",
    "mark_otp_expired",
    "mark_otp_used",
    "register_binding",
    "revoke_session",
    "revoke_user_sessions",
    "save_verification",
    "touch_binding",
]
