"""Persistencia de OTP (`identity.otp_codes`; E1-T08, HU02 CA-01/CA-02/CA-03).

Capa de datos sin endpoints: solo SQLAlchemy sobre el schema propio
(`identity`). Sin FK entre schemas y sin acceso a tablas de otros modulos
(`user_id` referencia `identity.users`, mismo schema, regla de oro 4).

Convencion (igual que el resto del repositorio identity): `flush` sin
`commit`; quien llama decide la transaccion. Este repositorio NO calcula
hashes ni emite eventos (eso vive en `service/otp_service.py`): recibe el
`code_hash` ya construido (`"salt_hex$sha256_hex"`) y solo persiste y
transiciona estados (`PENDING` -> `USED`/`EXPIRED`). Sin `float`, sin
secretos/PII en logs, sin guardar el codigo en claro en ninguna columna.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.identity.models import OTP_PURPOSES, OTP_STATUSES, OtpCode


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def create_otp(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    purpose: str,
    code_hash: str,
    expires_at: datetime,
    destination: str | None = None,
    max_attempts: int = 3,
    resend_count: int = 0,
) -> OtpCode:
    """Inserta un OTP `PENDING` (`flush`, sin `commit`).

    Valida antes de insertar: `purpose` del enum `otp_purpose`,
    `code_hash` con formato `"salt_hex$sha256_hex"` (jamas el codigo en
    claro), `expires_at` obligatorio, `max_attempts >= 1`,
    `resend_count >= 0`.
    """
    uid = _coerce_uuid(user_id, "user_id")
    if purpose not in OTP_PURPOSES:
        raise ValueError(f"purpose debe ser uno de {OTP_PURPOSES}, recibido: {purpose!r}")
    if not isinstance(code_hash, str) or "$" not in code_hash:
        raise ValueError("code_hash debe tener formato 'salt_hex$sha256_hex'")
    salt, _, digest = code_hash.partition("$")
    if len(code_hash) > 128 or not salt.strip() or len(digest) != 64:
        raise ValueError("code_hash invalido (salt + sha256 de 64 hex)")
    if not isinstance(expires_at, datetime):
        raise TypeError(f"expires_at debe ser datetime, recibido: {expires_at!r}")
    if destination is not None:
        if not isinstance(destination, str) or not destination.strip():
            raise ValueError("destination debe ser texto no vacio")
        if len(destination.strip()) > 255:
            raise ValueError("destination supera 255 caracteres")
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool):
        raise TypeError("max_attempts debe ser int")
    if max_attempts < 1:
        raise ValueError("max_attempts debe ser >= 1")
    if not isinstance(resend_count, int) or isinstance(resend_count, bool):
        raise TypeError("resend_count debe ser int")
    if resend_count < 0:
        raise ValueError("resend_count debe ser >= 0")

    row = OtpCode(
        user_id=uid,
        purpose=purpose,
        destination=destination.strip() if destination is not None else None,
        code_hash=code_hash,
        expires_at=expires_at,
        attempts=0,
        max_attempts=max_attempts,
        resend_count=resend_count,
        status="PENDING",
    )
    session.add(row)
    session.flush()
    return row


def get_active(session: Session, user_id: uuid.UUID | str, purpose: str) -> OtpCode | None:
    """OTP `PENDING` mas reciente de (`user_id`, `purpose`).

    Como cada emision invalida el anterior, hay como maximo un `PENDING`
    por ciclo; si no hay ninguno, retorna `None` (nada que validar o
    reenviar: el llamante decide entre `generate_otp` o error).
    """
    uid = _coerce_uuid(user_id, "user_id")
    if purpose not in OTP_PURPOSES:
        raise ValueError(f"purpose debe ser uno de {OTP_PURPOSES}, recibido: {purpose!r}")
    stmt = (
        sa.select(OtpCode)
        .where(
            OtpCode.user_id == uid,
            OtpCode.purpose == purpose,
            OtpCode.status == "PENDING",
        )
        .order_by(OtpCode.created_at.desc())
    )
    return session.scalars(stmt).first()


def mark_used(session: Session, row: OtpCode, consumed_at: datetime) -> OtpCode:
    """Marca un OTP como `USED` (un solo uso; `flush`, sin `commit`)."""
    if row.status != "PENDING":
        raise ValueError(f"solo un OTP PENDING puede consumirse, estado: {row.status!r}")
    if not isinstance(consumed_at, datetime):
        raise TypeError(f"consumed_at debe ser datetime, recibido: {consumed_at!r}")
    row.status = "USED"
    row.consumed_at = consumed_at
    session.flush()
    return row


def mark_expired(session: Session, row: OtpCode) -> OtpCode:
    """Marca un OTP `PENDING` como `EXPIRED` (`flush`, sin `commit`).

    Se usa al expirar por tiempo, al agotar intentos y al invalidar el
    anterior en un reenvio (el codigo viejo jamas se reutiliza).
    """
    if row.status != "PENDING":
        raise ValueError(f"solo un OTP PENDING puede expirar, estado: {row.status!r}")
    row.status = "EXPIRED"
    session.flush()
    return row


def bump_attempts(session: Session, row: OtpCode) -> int:
    """Suma un intento fallido y retorna el contador (`flush`, sin `commit`)."""
    if row.status != "PENDING":
        raise ValueError(f"solo un OTP PENDING acumula intentos, estado: {row.status!r}")
    row.attempts = int(row.attempts) + 1
    session.flush()
    return row.attempts


__all__ = [
    "OTP_PURPOSES",
    "OTP_STATUSES",
    "bump_attempts",
    "create_otp",
    "get_active",
    "mark_expired",
    "mark_used",
]
