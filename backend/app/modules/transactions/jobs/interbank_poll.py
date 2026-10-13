"""Job de polling/timeout de transferencias interbancarias (E2-T09, HU06).

Recorre las `INTERBANK_TRANSFER` en `FUNDS_HELD`/`POSTED` y avanza cada
una un paso (`service.interbank.poll_interbank`): reintento de envio,
consulta de estado, liquidacion o compensacion al expirar.

Frontera (reglas de oro 1/2/3/4/8):
- Sin SQL directo sobre `transactions`: seleccion via `sa.select` sobre el
  modelo propio (mismo modulo) y cambios de estado via el servicio
  (que valida con la maquina E5-T01 + optimistic locking).
- Ledger solo por su fachada (dentro del servicio); este job nunca toca
  postings ni crea asientos a mano.
- Dinero entero en centimos; nunca `float`.
- Eventos solo por `outbox` (dentro del servicio).

Gestion de transaccion (decision documentada):
- Solo `flush`, nunca `commit` (convencion E5-T02/E5-T03; el llamante
  decide `commit`/`rollback`). Cada pendiente se procesa en `SAVEPOINT`
  (`session.begin_nested()`): si una falla, se revierte solo ella y el
  lote continua (el polling es best-effort y el reintento retoma).
- Idempotente: reprocesar no duplica transiciones (optimistic locking),
  asientos (transicion reclamada primero) ni eventos.

Nota: los holds interbancarios se crean SIN `expires_at` a proposito: el
timeout lo posee esta saga (`meta.interbank.expires_at`) y el job
generico `release_expired_holds` (E5-T06) los ignora, asi no interfiere
con el transito `2200` (ese job solo libera `2100 -> 2000`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.transactions.models import Transaction
from app.modules.transactions.service.interbank import (
    INTERBANK_TX_TYPE,
    poll_interbank,
)

logger = logging.getLogger(__name__)

#: Estados pendientes que el polling debe avanzar.
PENDING_STATES = ("FUNDS_HELD", "POSTED")

DEFAULT_LIMIT = 100


def _utcnow() -> datetime:
    return datetime.now(UTC)


def list_pending_interbank(session: Session, *, limit: int = DEFAULT_LIMIT) -> list[Transaction]:
    """Pendientes de la saga (`INTERBANK_TRANSFER` en `FUNDS_HELD`/`POSTED`)."""
    stmt = (
        sa.select(Transaction)
        .where(
            Transaction.type == INTERBANK_TX_TYPE,
            Transaction.status.in_(PENDING_STATES),
        )
        .order_by(Transaction.created_at.asc(), Transaction.id.asc())
        .limit(limit)
    )
    return list(session.scalars(stmt).all())


def poll_interbank_once(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    gateway=None,
    timeout_seconds: float | None = None,
    max_attempts: int | None = None,
    balance_port=None,
) -> dict[str, Any]:
    """Una pasada de polling: avanza cada pendiente un paso (solo `flush`).

    Retorna `{"processed": [...ids], "failed": [{"id", "error"}]}` (ids en
    texto). Los fallos por pendiente se aislan en `SAVEPOINT` y no abortan
    el lote; el reintento posterior retoma (idempotencia).
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError(f"limit debe ser entero > 0, recibido: {limit!r}")
    moment = now or _utcnow()
    pending = list_pending_interbank(session, limit=limit)
    processed: list[str] = []
    failed: list[dict[str, str]] = []
    for tx in pending:
        tx_id = tx.id
        try:
            with session.begin_nested():
                result = poll_interbank(
                    session,
                    tx_id,
                    gateway=gateway,
                    now=moment,
                    timeout_seconds=timeout_seconds,
                    max_attempts=max_attempts,
                    balance_port=balance_port,
                )
        except Exception as exc:  # noqa: BLE001 - best-effort por pendiente
            logger.warning("interbank poll fallo tx=%s err=%s", tx_id, type(exc).__name__)
            failed.append({"id": str(tx_id), "error": type(exc).__name__})
            continue
        processed.append(str(result.id if result is not None else tx_id))
    if processed or failed:
        session.flush()
    return {"processed": processed, "failed": failed}


__all__ = ["DEFAULT_LIMIT", "PENDING_STATES", "list_pending_interbank", "poll_interbank_once"]
