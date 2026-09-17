"""Caso de uso central del motor transaccional (E5-T03, HU17 CA-01/02/04).

Flujo atomico (misma sesion, solo `flush`, nunca `commit`):
1. Valida entrada pura (monto entero > 0 en centimos, moneda ISO, cuentas
   distintas, fee entero >= 0). Sin `float`.
2. Crea `Transaction` (INITIATED) via repositorio E5-T02 (+ historial).
3. Avanza INITIATED -> VALIDATED -> AUTHORIZED (historial por transicion).
4. Bloquea saldos en orden estable de `account_id` (via `BalancePort`) y
   comprueba `available >= amount + fee`; si no, AUTHORIZED -> REJECTED
   (negocio, sin movimientos) y retorna.
5. Retiene: asegura subcuentas `2000`/`2100` via fachada `ledger`,
   `create_hold` ACTIVE, `apply_delta` en el port, AUTHORIZED -> FUNDS_HELD,
   outbox `funds.held` (best-effort, ver E5-T05).
6. Asiento de hold `2000-S -> 2100-S` via `ledger.service.post_entry`,
   luego FUNDS_HELD -> POSTED.
7. Si `settle=True` (transferencia inmediata, `04#7.1/#7.2`): asiento de
   liquidacion `2100-S -> 2000-D` (+ `2100-S -> 4000` si hay fee),
   `apply_delta` al destino, hold ACTIVE -> CAPTURED, POSTED -> SETTLED.
   Si `settle=False` (diferida, `04#7.3`): se detiene en POSTED con el
   hold ACTIVE.

Fallo tecnico: se marcan holds RELEASED y se intenta pasar a FAILED
(best-effort en la misma sesion) y **siempre se re-lanza** la excepcion
original para que la sesion revierta (rollback total, sin residual).

Frontera (reglas de oro 1/3/4/8):
- Dinero entero en centimos; nunca `float`.
- Ledger solo por su fachada (`ensure_customer_accounts`, `post_entry`).
  Lectura puntual del codigo `4000` via `ledger.repository.get_by_code`
  (solo lectura; TODO: exponer catalogo por fachada).
- Eventos solo por `outbox` con import perezoso (regla 8); si el modulo
  no existe (E5-T05 en curso) se continua sin publicar.
- Saldos solo por el puerto `BalancePort` (E2-T01 pendiente).

TODO(E5-T05): cuando exista `app.core.outbox`, los `_publish_outbox`
ya lo usan sin cambios (misma firma).
TODO(E2-T01): proveer el adaptador real de `BalancePort` sobre
`accounts.account_balances` con `SELECT ... FOR UPDATE`.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.core import events as domain_events
from app.modules.ledger import service as ledger_service
from app.modules.transactions import repository as tx_repository
from app.modules.transactions.domain.state_machine import (
    validate_amount_minor,
    validate_currency,
)
from app.modules.transactions.models import HoldStatus, Transaction

HOLD_ENTRY_TYPE = "TRANSFER_HOLD"
SETTLE_ENTRY_TYPE = "TRANSFER_SETTLE"


class BalanceView(Protocol):
    """Vista minima de saldo que retorna el port (proyeccion de accounts)."""

    available_minor: int
    currency: str


class BalancePort(Protocol):
    """Puerto minimo de saldos (adaptador real: E2-T01 pendiente)."""

    def lock_and_get(self, session: Session, account_id: uuid.UUID) -> BalanceView:
        """Bloquea (pesimista) y retorna el saldo de la cuenta."""
        ...  # pragma: no cover - protocolo

    def apply_delta(
        self, session: Session, account_id: uuid.UUID, delta_minor: int, currency: str
    ) -> None:
        """Aplica un delta entero (negativo debita, positivo acredita)."""
        ...  # pragma: no cover - protocolo


class _DefaultBalancePort:
    """Implementacion por defecto: siempre pendiente (E2-T01)."""

    def lock_and_get(self, session: Session, account_id: uuid.UUID) -> BalanceView:
        raise NotImplementedError("E2-T01 pendiente: adaptador de saldos no provisto")

    def apply_delta(
        self, session: Session, account_id: uuid.UUID, delta_minor: int, currency: str
    ) -> None:
        raise NotImplementedError("E2-T01 pendiente: adaptador de saldos no provisto")


DEFAULT_BALANCE_PORT = _DefaultBalancePort()


def _publish_outbox(
    session: Session, *, event_type: str, tx: Transaction, payload: dict[str, Any]
) -> bool:
    """Publica en outbox con import perezoso; `False` si E5-T05 pendiente."""
    try:
        from app.core.outbox import record as outbox_record
    except ImportError:
        # TODO(E5-T05): modulo outbox aun en curso; se continua sin publicar.
        return False
    outbox_record(
        session,
        aggregate_type="transaction",
        aggregate_id=tx.id,
        event_type=event_type,
        payload=payload,
    )
    return True


def _coerce_account(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def _settle_postings(
    src_hold_id: uuid.UUID,
    dst_avail_id: uuid.UUID,
    fee_account_id: uuid.UUID | None,
    source_ref: uuid.UUID,
    target_ref: uuid.UUID,
    amount_minor: int,
    fee_minor: int,
    currency: str,
) -> list[dict[str, Any]]:
    """Asiento de liquidacion `2100-S -> 2000-D` (+ fee a `4000`)."""
    postings: list[dict[str, Any]] = [
        {
            "ledger_account_id": src_hold_id,
            "direction": "DEBIT",
            "amount_minor": amount_minor,
            "currency": currency,
            "account_ref": source_ref,
        },
        {
            "ledger_account_id": dst_avail_id,
            "direction": "CREDIT",
            "amount_minor": amount_minor,
            "currency": currency,
            "account_ref": target_ref,
        },
    ]
    if fee_minor > 0:
        assert fee_account_id is not None
        postings += [
            {
                "ledger_account_id": src_hold_id,
                "direction": "DEBIT",
                "amount_minor": fee_minor,
                "currency": currency,
                "account_ref": source_ref,
            },
            {
                "ledger_account_id": fee_account_id,
                "direction": "CREDIT",
                "amount_minor": fee_minor,
                "currency": currency,
                "account_ref": source_ref,
            },
        ]
    return postings


def execute_transfer(
    session: Session,
    *,
    source_account_id: uuid.UUID | str,
    target_account_id: uuid.UUID | str,
    amount_minor: int,
    currency: str,
    fee_minor: int = 0,
    tx_type: str = "OWN_TRANSFER",
    idempotency_key: str | None = None,
    initiator_user_id: uuid.UUID | None = None,
    external_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
    settle: bool = True,
    balance_port: BalancePort | None = None,
) -> Transaction:
    """Ejecuta una transferencia atomica (hold + asiento + estados).

    Retorna la `Transaction` en `SETTLED` (inmediata) o `POSTED`
    (diferida, `settle=False`), o en `REJECTED` si no hay fondos.
    Ante fallo tecnico re-lanza tras marcar FAILED/RELEASED best-effort
    (el llamador revierte: rollback total).
    """
    # 1-2. Validacion pura (antes de tocar BD) + idempotencia por clave.
    validate_amount_minor(amount_minor)
    validate_currency(currency)
    if isinstance(fee_minor, bool) or not isinstance(fee_minor, int) or fee_minor < 0:
        raise ValueError(f"fee_minor debe ser entero >= 0, recibido: {fee_minor!r}")
    source = _coerce_account(source_account_id, "source_account_id")
    target = _coerce_account(target_account_id, "target_account_id")
    if source == target:
        raise ValueError("source_account_id y target_account_id deben ser distintas")
    if idempotency_key is not None:
        existing = tx_repository.get_by_idempotency_key(session, idempotency_key)
        if existing is not None:
            return existing

    port: BalancePort = balance_port if balance_port is not None else DEFAULT_BALANCE_PORT
    total_minor = amount_minor + fee_minor
    tx: Transaction | None = None
    try:
        # 3. Crea + valida + autoriza (historial por transicion).
        tx = tx_repository.create_transaction(
            session,
            type=tx_type,
            amount_minor=amount_minor,
            currency=currency,
            status="INITIATED",
            idempotency_key=idempotency_key,
            initiator_user_id=initiator_user_id,
            source_account_id=source,
            target_account_id=target,
            external_ref=external_ref,
            fee_minor=fee_minor,
            metadata=metadata,
        )
        tx_repository.transition_transaction(session, tx.id, "VALIDATED", reason="entrada valida")
        tx_repository.transition_transaction(session, tx.id, "AUTHORIZED", reason="autorizada")

        # 4. Bloqueo en orden estable + comprobacion de fondos.
        ordered = sorted({source, target}, key=str)
        locked = {aid: port.lock_and_get(session, aid) for aid in ordered}
        src_balance = locked[source]
        if src_balance.currency != currency or src_balance.available_minor < total_minor:
            tx_repository.transition_transaction(
                session, tx.id, "REJECTED", reason="fondos insuficientes"
            )
            return tx

        # 5. Retencion: subcuentas via fachada, hold ACTIVE, delta, estado.
        src_avail, src_hold = ledger_service.ensure_customer_accounts(
            session, source, currency
        )
        hold = tx_repository.create_hold(
            session,
            transaction_id=tx.id,
            account_id=source,
            amount_minor=total_minor,
            currency=currency,
        )
        port.apply_delta(session, source, -total_minor, currency)
        tx_repository.transition_transaction(
            session, tx.id, "FUNDS_HELD", reason="fondos retenidos"
        )
        _publish_outbox(
            session,
            event_type=domain_events.FUNDS_HELD,
            tx=tx,
            payload={
                "transaction_id": str(tx.id),
                "account_id": str(source),
                "amount_minor": total_minor,
                "currency": currency,
                "status": "FUNDS_HELD",
            },
        )

        # 6. Asiento de hold 2000-S -> 2100-S (total invariante, 04#2.1).
        ledger_service.post_entry(
            session,
            entry_type=HOLD_ENTRY_TYPE,
            postings=[
                {
                    "ledger_account_id": src_avail.id,
                    "direction": "DEBIT",
                    "amount_minor": total_minor,
                    "currency": currency,
                    "account_ref": source,
                },
                {
                    "ledger_account_id": src_hold.id,
                    "direction": "CREDIT",
                    "amount_minor": total_minor,
                    "currency": currency,
                    "account_ref": source,
                },
            ],
            transaction_id=tx.id,
            description=f"hold {tx.id}",
        )
        tx_repository.transition_transaction(
            session, tx.id, "POSTED", reason="asiento de hold registrado"
        )

        # 7. Liquidacion inmediata (o stop en POSTED si diferida).
        if settle:
            dst_avail, _dst_hold = ledger_service.ensure_customer_accounts(
                session, target, currency
            )
            fee_account_id: uuid.UUID | None = None
            if fee_minor > 0:
                # Solo lectura del catalogo semilla (creado por
                # ensure_customer_accounts); TODO: exponer por fachada.
                from app.modules.ledger.repository import get_by_code

                fee_account = get_by_code(session, "4000")
                if fee_account is None:  # pragma: no cover - defensivo
                    raise RuntimeError("cuenta de comision 4000 inexistente")
                fee_account_id = fee_account.id
            ledger_service.post_entry(
                session,
                entry_type=SETTLE_ENTRY_TYPE,
                postings=_settle_postings(
                    src_hold.id,
                    dst_avail.id,
                    fee_account_id,
                    source,
                    target,
                    amount_minor,
                    fee_minor,
                    currency,
                ),
                transaction_id=tx.id,
                description=f"settle {tx.id}",
            )
            port.apply_delta(session, target, amount_minor, currency)
            tx_repository.update_hold_status(session, hold.id, HoldStatus.CAPTURED.value)
            tx_repository.transition_transaction(
                session, tx.id, "SETTLED", reason="transferencia liquidada"
            )
            _publish_outbox(
                session,
                event_type=domain_events.TRANSFER_SETTLED,
                tx=tx,
                payload={
                    "transaction_id": str(tx.id),
                    "source_account_id": str(source),
                    "target_account_id": str(target),
                    "amount_minor": amount_minor,
                    "fee_minor": fee_minor,
                    "currency": currency,
                    "status": "SETTLED",
                },
            )
        return tx
    except Exception:
        # Fallo tecnico: libera holds + FAILED best-effort y re-lanza
        # (no se oculta el rollback: el llamador revierte la sesion).
        try:
            if tx is not None and tx.id is not None:
                try:
                    for active in tx_repository.list_holds_by_transaction(session, tx.id):
                        if active.status == HoldStatus.ACTIVE.value:
                            try:
                                tx_repository.update_hold_status(
                                    session, active.id, HoldStatus.RELEASED.value
                                )
                            except Exception:
                                pass
                except Exception:
                    pass
                try:
                    if tx.status in ("FUNDS_HELD", "POSTED"):
                        tx_repository.transition_transaction(
                            session, tx.id, "FAILED", reason="fallo tecnico"
                        )
                except Exception:
                    pass
                try:
                    _publish_outbox(
                        session,
                        event_type=domain_events.FUNDS_RELEASED,
                        tx=tx,
                        payload={
                            "transaction_id": str(tx.id),
                            "status": "FAILED",
                        },
                    )
                except Exception:
                    pass
        finally:
            raise


__all__ = [
    "BalancePort",
    "BalanceView",
    "DEFAULT_BALANCE_PORT",
    "HOLD_ENTRY_TYPE",
    "SETTLE_ENTRY_TYPE",
    "execute_transfer",
]
