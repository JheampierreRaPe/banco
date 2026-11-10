"""Consentimiento biometrico post-login (`set_biometric_consent`; E1-T39, HU02/HU03).

Fija `identity.credentials.biometric_enabled` del usuario autenticado
(`{enabled: true|false}`). Reutiliza la columna existente: sin migracion ni
cambios de esquema. Idempotente: fijar el mismo valor no produce error.

Convencion: `flush` sin `commit`; quien llama decide la transaccion (el
endpoint confirma en exito y revierte ante error). Auditoria
`auth.biometric_consent` best-effort via fachada `audit` (import perezoso,
como `pin_setup._audit_pin_setup`): sin PII, solo `user_id`/`enabled`/`at`.
Sin `float`, sin secretos/PII en logs. No toca `device_login`, `pin_setup`,
`hashing` ni los bindings del dispositivo.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.modules.identity import repository as identity_repo

logger = logging.getLogger(__name__)

#: Accion de auditoria (best-effort via fachada `audit`, regla de oro 8).
AUDIT_BIOMETRIC_CONSENT = "auth.biometric_consent"

#: Tipo de agregado para la auditoria (el mismo que `pin_setup`/`pin_login`).
USER_AGGREGATE_TYPE = "user"

#: Mensaje neutro (no revela datos: mismo cuerpo exista o no la credencial
#: salvo el codigo 404, sin eco de valores).
NOT_FOUND_MESSAGE = "Recurso no encontrado"


class BiometricConsentUserNotFoundError(ValueError):
    """Usuario/credencial inexistente (-> 404 `NOT_FOUND` neutro)."""


def _audit_consent(session: Session, *, user_id: uuid.UUID, enabled: bool) -> None:
    """Registra `auth.biometric_consent` via fachada (best-effort).

    Nunca `commit` (solo `flush` via la fachada). Sin PII: solo el `user_id`,
    el flag booleano `enabled` y el momento `at`; jamas tokens, PIN ni
    `device_public_key`.
    """
    try:
        from app.modules.audit.service import record as audit_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("biometric_consent audit no disponible")
        return
    try:
        audit_record(
            session,
            actor=user_id,
            action=AUDIT_BIOMETRIC_CONSENT,
            entity=USER_AGGREGATE_TYPE,
            entity_id=user_id,
            metadata={
                "enabled": bool(enabled),
                "at": datetime.now(UTC).isoformat(),
            },
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 - best-effort documentado E1-T39
        logger.warning("biometric_consent audit no registrado error=%s", type(exc).__name__)


def set_biometric_consent(
    session: Session,
    *,
    user_id: uuid.UUID,
    enabled: bool,
) -> dict:
    """Fija `credential.biometric_enabled` (`flush`, sin `commit`).

    Resuelve `identity_repo.get_credential(session, user_id)`; credencial
    ausente -> `BiometricConsentUserNotFoundError` (404 neutro). Fija
    `credential.biometric_enabled = enabled is True` (idempotente), `flush`,
    audita best-effort y retorna `{"biometric_enabled": <bool>}`.
    """
    credential = identity_repo.get_credential(session, user_id)
    if credential is None:
        raise BiometricConsentUserNotFoundError(NOT_FOUND_MESSAGE)
    flag = enabled is True
    credential.biometric_enabled = flag
    session.flush()
    _audit_consent(session, user_id=credential.user_id, enabled=flag)
    logger.info("biometric_consent ok")
    return {"biometric_enabled": flag}


__all__ = [
    "AUDIT_BIOMETRIC_CONSENT",
    "NOT_FOUND_MESSAGE",
    "USER_AGGREGATE_TYPE",
    "BiometricConsentUserNotFoundError",
    "set_biometric_consent",
]
