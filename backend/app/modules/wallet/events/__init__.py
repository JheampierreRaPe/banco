"""Eventos de `wallet` (E4-T01, HU13; pagos HU16 en E4-T17).

Se emiten por `outbox` en la misma transaccion del caso de uso (`flush`,
sin `commit`; regla de oro 8). Payloads sin PII: solo IDs y tipos.
"""

from app.core.events import (
    QR_CHARGE_CREATED,
    QR_PAYMENT_CONFIRMED,
    SERVICE_PAID,
    TOPUP_COMPLETED,
    WALLET_LINK_CREATED,
)

__all__ = [
    "QR_CHARGE_CREATED",
    "QR_PAYMENT_CONFIRMED",
    "SERVICE_PAID",
    "TOPUP_COMPLETED",
    "WALLET_LINK_CREATED",
]
