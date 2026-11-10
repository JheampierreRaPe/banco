"""Job de reconciliacion del cobro QR (R-1, HU15).

`run_qr_reconcile_once(session, *, limit=...)`: una pasada que finaliza
los `qr_payments` en `PENDING_AUTHORIZATION` cuya operacion del motor ya
esta liquidada (`Transaction` en `SETTLED`): ejecuta el helper
existente `wallet.service.qr_payment.finalize_qr_payment` (marca el
cargo `PAID` y emite **una** sola `qr_charge_paid`). Cierra el hueco de
`E4-T12` (`payments.py:263-266`): si el cliente autoriza y nunca
reintenta `POST /payments/qr` con la misma `Idempotency-Key`, el cobro
quedaba pendiente y el job de caducidad lo pasaba a `EXPIRED` sin
aviso, con el dinero ya movido.

Gestion de transaccion (patron `E3-T19`): el llamante pasa la sesion y
cada item confirma con su propio `commit`; el fallo de un item revierte
solo ese item y el lote continua. Idempotente y re-ejecutable: solo se
finaliza con la `Transaction` en `SETTLED` (otro estado -> `skipped`,
sin tocar nada); `finalize_qr_payment` ya es idempotente (`PAID` no
duplica evento ni aviso). No mueve dinero (ya se movio al autorizar:
solo `status` de `qr_payments`/`qr_charges`, evento y aviso, sin PII en
logs: solo conteos).

Frontera (regla de oro 4): el estado de la `Transaction` se lee por la
fachada `transactions.service.find_transaction_by_id` (patron M-1);
`wallet` no importa modelos ni repositorios de `transactions`.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.modules.wallet.repository import qr_payment as qr_payment_repo

logger = logging.getLogger(__name__)

#: Tope de pagos por pasada (acota el lote; el siguiente ciclo retoma).
DEFAULT_LIMIT = 100


def run_qr_reconcile_once(
    session: Session,
    *,
    limit: int = DEFAULT_LIMIT,
    notify_sender: Any | None = None,
) -> dict[str, Any]:
    """Una pasada de reconciliacion: finaliza los pendientes ya liquidados.

    Retorna `{"reconciled": [...], "skipped": [...]}` (ids en texto).
    `skipped` recoge los pendientes cuya operacion aun no esta `SETTLED`
    (pendiente/rechazada/inexistente: nada que finalizar), los que
    cambiaron de estado entre la seleccion y la finalizacion (carrera
    con el replay de `POST /payments/qr`: nada que hacer) y los que
    fallaron tecnicamente (el lote continua).
    """
    from app.modules.transactions.service import find_transaction_by_id
    from app.modules.wallet.service.qr_payment import finalize_qr_payment

    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError(f"limit debe ser entero > 0, recibido: {limit!r}")

    candidates = qr_payment_repo.list_pending_authorization(session, limit=limit)
    reconciled: list[str] = []
    skipped: list[str] = []
    for row in candidates:
        pid = str(row.id)
        if str(row.status) != "PENDING_AUTHORIZATION":
            skipped.append(pid)
            continue
        try:
            tx = find_transaction_by_id(session, row.transaction_id)
            if tx is None or str(getattr(tx, "status", "") or "") != "SETTLED":
                skipped.append(pid)
                continue
            finalize_qr_payment(
                session,
                qr_payment_id=row.id,
                transaction=tx,
                notify_sender=notify_sender,
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 - fallo tecnico: el lote continua
            logger.warning("qr-reconcile item fallo error=%s", type(exc).__name__)
            try:
                session.rollback()
            except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                pass
            skipped.append(pid)
            continue
        reconciled.append(pid)
    logger.info("qr-reconcile pasada reconciled=%s skipped=%s", len(reconciled), len(skipped))
    return {"reconciled": reconciled, "skipped": skipped}


__all__ = ["DEFAULT_LIMIT", "run_qr_reconcile_once"]
