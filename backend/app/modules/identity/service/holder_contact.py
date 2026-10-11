"""Correo del titular para notificaciones post-firma (E3-T14, HU11 CA-04).

Fachada de **solo lectura** (D-03, regla de oro 4): resuelve el `email`
de `identity.users` para que `credits` envie la copia certificada del
contrato firmado por la fachada `notifications.send(channel="email")`
sin importar modelos ni repositorios de `identity`.

Solo lectura: sin `flush`/`commit`, sin eventos, sin `Idempotency-Key`.
Sin PII en logs (unico log: si se resolvio o no, sin el valor).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.modules.identity import repository as identity_repo

logger = logging.getLogger(__name__)


def get_holder_email(session: Session, user_id: uuid.UUID | str | None) -> str | None:
    """Email del titular (`None` si no existe o no tiene correo).

    `user_id` malformado o usuario inexistente -> `None` (el llamante
    omite el envio best-effort sin filtrar existencia). El valor en
    claro es dato de entrega (igual que el `recipient` de
    `notifications.send`): se persiste en la fila de notificacion para
    el reintento, pero jamas se incluye en logs.
    """
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError):
        return None
    try:
        user = identity_repo.get_user(session, uid)
    except Exception:  # noqa: BLE001 - lectura best-effort: sin correo no hay envio
        logger.warning("holder_contact lookup no disponible")
        return None
    if user is None:
        return None
    mail = str(getattr(user, "email", "") or "").strip()
    if not mail:
        logger.warning("holder_contact sin correo resoluble")
        return None
    logger.info("holder_contact correo resuelto")
    return mail


__all__ = ["get_holder_email"]
