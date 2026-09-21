"""Persistencia de recuperaciones de acceso (`identity.access_recovery`; E1-T31, HU04).

Capa de datos sin endpoints: solo SQLAlchemy sobre el schema propio
(`identity`). Sin FK entre schemas y sin acceso a tablas de otros modulos
(`user_id` referencia `identity.users`, mismo schema, regla de oro 4).

Convencion (igual que el resto del repositorio identity): `flush` sin
`commit`; quien llama decide la transaccion. Sin `float`, sin secretos/PII
en logs: `verification_result` solo guarda el resultado (`{"result", "at"}`,
jamas email, OTP ni hashes); el `device_id` va en su columna.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.modules.identity.models import ACCESS_RECOVERY_METHODS, AccessRecovery


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def record_access_recovery(
    session: Session,
    user_id: uuid.UUID | str,
    *,
    method: str,
    verification_result: dict | None = None,
    device_id: str | None = None,
    new_credential_set: bool = False,
    notified_channels: list | None = None,
) -> AccessRecovery:
    """Inserta la fila de `access_recovery` (`flush`, sin `commit`).

    Valida antes de insertar: `method` del enum `03b#4.8`
    (`DEVICE_BIOMETRIC`/`OTP`), `verification_result` dict cuando viene,
    `device_id` acotado, `notified_channels` lista cuando viene.
    """
    uid = _coerce_uuid(user_id, "user_id")
    if method not in ACCESS_RECOVERY_METHODS:
        raise ValueError(f"method debe ser uno de {ACCESS_RECOVERY_METHODS}, recibido: {method!r}")
    if verification_result is not None and not isinstance(verification_result, dict):
        raise TypeError(f"verification_result debe ser dict, recibido: {verification_result!r}")
    device = None
    if device_id is not None:
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("device_id debe ser texto no vacio")
        device = device_id.strip()
        if len(device) > 128:
            raise ValueError("device_id supera 128 caracteres")
    channels = None
    if notified_channels is not None:
        if not isinstance(notified_channels, list):
            raise TypeError(f"notified_channels debe ser lista, recibido: {notified_channels!r}")
        channels = list(notified_channels)

    row = AccessRecovery(
        user_id=uid,
        method=method,
        verification_result=dict(verification_result) if verification_result is not None else None,
        device_id=device,
        new_credential_set=bool(new_credential_set),
        notified_channels=channels,
    )
    session.add(row)
    session.flush()
    return row


__all__ = ["record_access_recovery"]
