"""Perfil autenticado minimo de solo lectura (`get_profile`; E1-T44, HU01/HU02).

Resuelve `identity_repo.get_user(session, user_id)` con el `user_id` del JWT
(`sub`) y devuelve **solo** `{"first_name", "last_name", "business_name"}`:
para RUC de persona juridica los nombres vienen `""` y `business_name` trae
la razon social (E1-T36); para el resto `business_name` es `None`.
Usuario inexistente -> `ProfileUserNotFoundError` (404 neutro).

Solo lectura: sin `flush`/`commit`, sin eventos, sin `Idempotency-Key`, sin
auditoria (lectura propia). Sin PII/secretos en logs (unico log: resultado
ok, sin valores). Sin `float`, sin datos biometricos.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.schemas.profile import ProfileData

logger = logging.getLogger(__name__)

#: Mensaje neutro (no revela datos ni existencia ajena, sin eco de valores).
NOT_FOUND_MESSAGE = "Recurso no encontrado"


class ProfileUserNotFoundError(ValueError):
    """Usuario inexistente (-> 404 `NOT_FOUND` neutro)."""


def to_profile(user: User) -> ProfileData:
    """Proyecta el titular a `ProfileData` (pura, sin BD).

    Solo copia nombre/razon social; jamas `email`/`phone`/`doc_*`/`status`.
    """
    return ProfileData(
        first_name=user.first_name,
        last_name=user.last_name,
        business_name=user.business_name,
    )


def get_profile(session: Session, user_id: uuid.UUID) -> dict:
    """Lee el nombre del titular (`solo lectura`, sin `flush`/`commit`).

    Resuelve `identity_repo.get_user(session, user_id)`; ausente ->
    `ProfileUserNotFoundError` (404 neutro). Retorna el dict listo para el
    `data` del `ProfileResponse` (05#4).
    """
    user = identity_repo.get_user(session, user_id)
    if user is None:
        raise ProfileUserNotFoundError(NOT_FOUND_MESSAGE)
    logger.info("profile ok")
    return to_profile(user).model_dump()


__all__ = [
    "NOT_FOUND_MESSAGE",
    "ProfileUserNotFoundError",
    "get_profile",
    "to_profile",
]
