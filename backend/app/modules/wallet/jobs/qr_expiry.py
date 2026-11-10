"""Job de caducidad del QR (E4-T07, HU14 CA-02).

`run_qr_expiry_once(session, *, now=None, limit=...)`: una pasada que
marca `ACTIVE` -> `EXPIRED` los `qr_charges` con `expires_at <= now`
(borde exacto: `expires_at == now` ya expira, `docs/03b#93` +
`domain.qr.is_expired`). No mueve dinero (solo `status` de
`qr_charges`, sin PII en logs: solo conteos); no escribe tablas
ajenas; no emite evento (basta el cambio de `status`, decision 1 del
brief).

Gestion de transaccion (patron `E3-T19`): el llamante pasa la sesion
y cada item confirma con su propio `commit`; el fallo de un item
revierte solo ese item y el lote continua. Idempotente y
re-ejecutable: `EXPIRED`/`PAID`/`CANCELLED` nunca se reprocesan (el
selector solo ve `ACTIVE` vencidos) y repetir la pasada no duplica
efectos.

El `now` es inyectable (tests deterministas; en produccion el
`runner` de `app.composition` pasa el reloj real).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.modules.wallet.repository import qr as qr_repo

logger = logging.getLogger(__name__)

#: Tope de cargos por pasada (acota el lote; el siguiente ciclo retoma).
DEFAULT_LIMIT = 100


def _utcnow() -> datetime:
    return datetime.now(UTC)


def run_qr_expiry_once(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Una pasada de caducidad: expira los `ACTIVE` vencidos.

    Retorna `{"expired": [...], "skipped": [...], "as_of": "<iso>"}`
    (ids en texto). `skipped` recoge los candidatos que cambiaron de
    estado entre la seleccion y la marca (carrera con el pago `HU15`:
    nada que expirar). `STATIC` (`expires_at NULL`) nunca vence.
    """
    moment = now if isinstance(now, datetime) else _utcnow()
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError(f"limit debe ser entero > 0, recibido: {limit!r}")

    candidates = qr_repo.list_expired_charges(session, moment, limit=limit)
    expired: list[str] = []
    skipped: list[str] = []
    for row in candidates:
        cid = str(row.id)
        if str(row.status) != "ACTIVE":
            skipped.append(cid)
            continue
        try:
            qr_repo.mark_expired(session, row)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - fallo tecnico: el lote continua
            logger.warning("qr-expiry item fallo error=%s", type(exc).__name__)
            try:
                session.rollback()
            except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                pass
            skipped.append(cid)
            continue
        expired.append(cid)
    logger.info("qr-expiry pasada expired=%s skipped=%s", len(expired), len(skipped))
    as_of = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return {"expired": expired, "skipped": skipped, "as_of": as_of.isoformat()}


__all__ = ["DEFAULT_LIMIT", "run_qr_expiry_once"]
