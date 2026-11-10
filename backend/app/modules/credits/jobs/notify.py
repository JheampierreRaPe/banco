"""Envio push best-effort para los jobs de cuotas (E3-T19, HU12 CA-03/CA-04).

`credits` consume `notifications` solo por su fachada `send(...)`
(regla de oro 4: sin importar sus modelos ni tocar sus tablas). El
envio nunca bloquea el job: cualquier fallo se registra (sin PII) y
el item queda `failed` para el siguiente ciclo. El llamante decide el
`commit`/`rollback` (el helper solo hace `flush` via la fachada).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def send_push_best_effort(
    session: Session,
    *,
    owner: uuid.UUID | str,
    template_code: str,
    data: dict[str, Any],
    sender: Any | None = None,
) -> bool:
    """Encola un push al titular por la fachada `notifications.send`.

    `recipient` es el `user_id` en texto (patron E3-T08:
    `applications._notify_decision`); `data` solo lleva referencia del
    prestamo e instante (sin PII ni cifras). Retorna `True` si la fila
    quedo registrada; `False` ante cualquier fallo (el log lleva solo
    el codigo de plantilla, nunca el destinatario ni los datos).
    """
    try:
        from app.modules.notifications import service as notifications_service
        from app.modules.notifications.service import sender_for_channel

        sender_obj = sender if sender is not None else sender_for_channel("push")
        notifications_service.send(
            session,
            channel="push",
            recipient=str(owner),
            template_code=template_code,
            data=dict(data),
            user_id=owner,
            sender=sender_obj,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort: no bloquea el job
        logger.warning(
            "credit job notify_failed template=%s error=%s", template_code, type(exc).__name__
        )
        return False
    logger.info("credit job notify_queued template=%s", template_code)
    return True


__all__ = ["send_push_best_effort"]
