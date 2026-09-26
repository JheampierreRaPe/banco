"""Caso de uso central del motor transaccional (E5-T03, HU17 CA-01/02/04; E2-T07, HU06).

Flujo atomico (misma sesion, solo `flush`, nunca `commit`):

Comun:
1. Valida entrada pura (monto entero > 0 en centimos, moneda ISO, cuentas
   distintas, fee entero >= 0). Sin `float`.
2. Crea `Transaction` (INITIATED) via repositorio E5-T02 (+ historial).
3. Avanza INITIATED -> VALIDATED -> AUTHORIZED (historial por transicion).
4. Bloquea saldos en orden estable de `account_id` (via `BalancePort`) y
   comprueba `available >= amount + fee`; si no, AUTHORIZED -> REJECTED
   (negocio, sin movimientos) y retorna.

Ruta con hold (terceros/interbancaria, `atomic=False`, camino E5-T03):
5. Retiene: asegura subcuentas `2000`/`2100` via fachada `ledger`,
   `create_hold` ACTIVE, `apply_delta` en el port, AUTHORIZED -> FUNDS_HELD,
   outbox `funds.held` (best-effort, ver E5-T05).
6. Asiento de hold `2000-S -> 2100-S` via `ledger.service.post_entry`,
   luego FUNDS_HELD -> POSTED.
7. Si `settle=True` (transferencia inmediata, `04#7.2`): asiento de
   liquidacion `2100-S -> 2000-D` (+ `2100-S -> 4100` si hay fee),
   `apply_delta` al destino, hold ACTIVE -> CAPTURED, POSTED -> SETTLED.
   Si `settle=False` (diferida, `04#7.3`): se detiene en POSTED con el
   hold ACTIVE.

Ruta atomica SIN hold (propia, `atomic=True`, `04#7.1`, E2-T07):
5'. Sin `holds` ni `FUNDS_HELD`: asegura `2000-S`/`2000-D` via fachada
   `ledger`, registra UN solo asiento `2000-S -> 2000-D` (sin comision),
   `apply_delta` en ambas puntas y transiciona AUTHORIZED -> POSTED ->
   SETTLED con outbox `transfer.settled`.

Fallo tecnico: se marcan holds RELEASED y se intenta pasar a FAILED
(best-effort en la misma sesion) y **siempre se re-lanza** la excepcion
original para que la sesion revierta (rollback total, sin residual).

Casos de uso E2-T07 (`transfer_own` / `transfer_third_party`): validan las
cuentas por la fachada `accounts.service.get_account` (D-01: existencia,
`ACTIVE`, moneda coherente, titular del origen) y delegan en
`execute_transfer` (atomica la propia, con hold la de tercero). La comision
de tercero se acredita a `4100-Ingresos por comisiones` (nunca `4000`).

Frontera (reglas de oro 1/3/4/8):
- Dinero entero en centimos; nunca `float`.
- Ledger solo por su fachada (`ensure_customer_accounts`, `post_entry`).
  Lectura puntual del codigo `4100` via `ledger.repository.get_by_code`
  (solo lectura; TODO: exponer catalogo por fachada).
- Cuentas solo por la fachada `accounts.service.get_account` (D-01 E2-T07);
  jamas se importan modelos/repositorios de `accounts`.
- La comision por defecto se lee best-effort de `config.parameters`
  (`transfer.third_party_fee_minor`, fallback `0`); sin regla cableada.
- Eventos solo por `outbox` con import perezoso (regla 8); si el modulo
  no existe se continua sin publicar (E5-T05 ya implementado en
  `app.core.outbox`).
- Saldos solo por el puerto `BalancePort` (adaptador real E2-T01 via
  `app.composition.resolve_balance_port`, con fallback a puerto por defecto).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.adapters.holder_lookup import (
    HolderLookupProvider,
    HolderLookupTimeoutError,
    HolderLookupUnavailableError,
    HolderNotFoundError,
    create_holder_lookup_provider,
    hash_cci,
    mask_holder_name,
    normalize_name,
    resolve_cache_ttl_seconds,
    validate_bank_code,
    validate_cci_format,
)
from app.core import events as domain_events
from app.modules.ledger import service as ledger_service
from app.modules.transactions import repository as tx_repository
from app.modules.transactions.domain.state_machine import (
    validate_amount_minor,
    validate_currency,
)
from app.modules.transactions.models import HoldStatus, Transaction

logger = logging.getLogger(__name__)

HOLD_ENTRY_TYPE = "TRANSFER_HOLD"
SETTLE_ENTRY_TYPE = "TRANSFER_SETTLE"
#: Asiento unico de la ruta atomica propia (`04#7.1`, E2-T07).
OWN_SETTLE_ENTRY_TYPE = "OWN_TRANSFER"
#: Cuenta canonica de comisiones (decision del dueno 2026-09-26, cierra G05).
FEE_ACCOUNT_CODE = "4100"
#: Parametro configurable de la comision por defecto de tercero (regla fuera
#: del codigo; `config.parameters`, modulo `transactions`; fallback `0`).
THIRD_PARTY_FEE_PARAM_KEY = "transfer.third_party_fee_minor"
THIRD_PARTY_FEE_DEFAULT_MINOR = 0


class BalanceView(Protocol):
    """Vista minima de saldo que retorna el port (proyeccion de accounts)."""

    available_minor: int
    currency: str


class BalancePort(Protocol):
    """Puerto minimo de saldos (adaptador real E2-T01 en `app.composition`)."""

    def lock_and_get(self, session: Session, account_id: uuid.UUID) -> BalanceView:
        """Bloquea (pesimista) y retorna el saldo de la cuenta."""
        ...  # pragma: no cover - protocolo

    def apply_delta(
        self, session: Session, account_id: uuid.UUID, delta_minor: int, currency: str
    ) -> None:
        """Aplica un delta entero (negativo debita, positivo acredita)."""
        ...  # pragma: no cover - protocolo


class _DefaultBalancePort:
    """Implementacion por defecto (fallback cuando no hay adaptador cableado)."""

    def lock_and_get(self, session: Session, account_id: uuid.UUID) -> BalanceView:
        raise NotImplementedError("E2-T01 pendiente: adaptador de saldos no provisto")

    def apply_delta(
        self, session: Session, account_id: uuid.UUID, delta_minor: int, currency: str
    ) -> None:
        raise NotImplementedError("E2-T01 pendiente: adaptador de saldos no provisto")


DEFAULT_BALANCE_PORT = _DefaultBalancePort()


class TransferError(ValueError):
    """Error base de los casos de uso de transferencia (E2-T07)."""


class AccountNotFoundError(TransferError):
    """Cuenta origen/destino inexistente (-> 404 NOT_FOUND)."""


class AccountNotAuthorizedError(TransferError):
    """Origen ajeno al titular (-> 403 NOT_AUTHORIZED)."""


class AccountBlockedError(TransferError):
    """Cuenta no `ACTIVE` (-> 403 NOT_AUTHORIZED en origen, `04#10`)."""


class CurrencyMismatchError(TransferError):
    """Moneda incoherente sin FX (-> 400 VALIDATION_ERROR, `04#10`)."""


def _read_int_parameter(session: Session, key: str, default: int) -> int:
    """Lee `config.parameters.<key>` best-effort (fallback a `default`).

    `config.parameters` es tabla transversal (duena: `shared`/`config`, no
    un modulo de negocio): SQL directo acotado a esa tabla dentro de un
    `SAVEPOINT` (mismo patron que `identity.otp_service.read_int_parameter`);
    un fallo (tabla ausente, valor invalido) no contamina la transaccion del
    llamante y rige `default`. Sin `float`; sin secretos/PII en logs.
    """
    try:
        with session.begin_nested():
            raw = session.execute(
                sa.text("SELECT value_json FROM config.parameters WHERE key = :key"),
                {"key": key},
            ).scalar()
    except Exception:  # noqa: BLE001 - sin acceso a parameters rige el default
        logger.debug("transfer parameters sin acceso (%s): default", key)
        return default
    if raw is None:
        return default
    if isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        candidate = int(raw)
    elif isinstance(raw, str):
        try:
            candidate = int(raw.strip())
        except (ValueError, AttributeError):
            return default
    else:
        return default
    return candidate if candidate >= 0 else default


def resolve_third_party_fee(session: Session, explicit_fee_minor: int | None = None) -> int:
    """Comision de tercero: explicita > `config.parameters` > `0`.

    `explicit_fee_minor=None` usa `transfer.third_party_fee_minor` (fallback
    `0`); un valor explicito debe ser entero `>= 0`. Sin regla cableada
    (regla de oro 6); nunca `float`.
    """
    if explicit_fee_minor is None:
        return _read_int_parameter(
            session, THIRD_PARTY_FEE_PARAM_KEY, THIRD_PARTY_FEE_DEFAULT_MINOR
        )
    if (
        isinstance(explicit_fee_minor, bool)
        or not isinstance(explicit_fee_minor, int)
        or explicit_fee_minor < 0
    ):
        raise ValueError(f"fee_minor debe ser entero >= 0, recibido: {explicit_fee_minor!r}")
    return explicit_fee_minor


def validate_transfer_parties(
    session: Session,
    *,
    initiator_user_id: uuid.UUID | str,
    source_account_id: uuid.UUID | str,
    target_account_id: uuid.UUID | str,
    currency: str,
    kind: str = "THIRD_PARTY_TRANSFER",
) -> dict[str, dict]:
    """Valida origen/destino por la fachada `accounts` (D-01, E2-T07).

    Solo lectura (sin `flush`/`commit`): ambas cuentas deben existir
    (`AccountNotFoundError`), estar `ACTIVE` (`AccountBlockedError`), la
    moneda de ambas debe coincidir con `currency` (`CurrencyMismatchError`)
    y el origen debe pertenecer al titular (`AccountNotAuthorizedError`).
    Para `OWN_TRANSFER` el destino tambien debe ser del titular.
    No registra numeros de cuenta en logs (regla de oro 7/9).
    Retorna `{"source": ..., "target": ...}` (vistas de la fachada).
    """
    from app.modules.accounts.service import get_account as _get_account_info

    try:
        initiator = (
            initiator_user_id
            if isinstance(initiator_user_id, uuid.UUID)
            else uuid.UUID(str(initiator_user_id))
        )
    except (ValueError, AttributeError, TypeError) as exc:
        raise AccountNotAuthorizedError("titular invalido") from exc
    source = _coerce_account(source_account_id, "source_account_id")
    target = _coerce_account(target_account_id, "target_account_id")
    if source == target:
        raise TransferError("source_account_id y target_account_id deben ser distintas")

    try:
        src_info = _get_account_info(session, source)
    except (ValueError, AttributeError, TypeError) as exc:
        raise AccountNotFoundError("cuenta origen inexistente") from exc
    if src_info is None:
        raise AccountNotFoundError("cuenta origen inexistente")
    try:
        dst_info = _get_account_info(session, target)
    except (ValueError, AttributeError, TypeError) as exc:
        raise AccountNotFoundError("cuenta destino inexistente") from exc
    if dst_info is None:
        raise AccountNotFoundError("cuenta destino inexistente")

    if src_info["user_id"] != initiator:
        raise AccountNotAuthorizedError("la cuenta origen no pertenece al titular")
    if kind == "OWN_TRANSFER" and dst_info["user_id"] != initiator:
        raise AccountNotAuthorizedError("la cuenta destino no pertenece al titular")
    if src_info["status"] != "ACTIVE":
        raise AccountBlockedError(f"cuenta origen no disponible ({src_info['status']})")
    if dst_info["status"] != "ACTIVE":
        raise AccountBlockedError(f"cuenta destino no disponible ({dst_info['status']})")
    if src_info["currency"] != currency or dst_info["currency"] != currency:
        raise CurrencyMismatchError(
            f"moneda incoherente sin FX: operacion {currency}, "
            f"origen {src_info['currency']}, destino {dst_info['currency']}"
        )
    return {"source": src_info, "target": dst_info}


def _publish_outbox(
    session: Session, *, event_type: str, tx: Transaction, payload: dict[str, Any]
) -> bool:
    """Publica en outbox con import perezoso; `False` si el modulo no existe."""
    try:
        from app.core.outbox import record as outbox_record
    except ImportError:
        # Sin outbox cableado se continua sin publicar (E5-T05 ya existe en `app.core.outbox`).
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


def _coerce_optional_user(value: uuid.UUID | str | None) -> uuid.UUID | None:
    """Normaliza el titular a UUID (los casos de uso aceptan texto)."""
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"initiator_user_id debe ser UUID, recibido: {value!r}") from exc


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
    """Asiento de liquidacion `2100-S -> 2000-D` (+ fee a `4100`)."""
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


def _own_settle_postings(
    src_avail_id: uuid.UUID,
    dst_avail_id: uuid.UUID,
    source_ref: uuid.UUID,
    target_ref: uuid.UUID,
    amount_minor: int,
    currency: str,
) -> list[dict[str, Any]]:
    """Asiento unico propio `2000-S -> 2000-D` (sin comision, `04#7.1`)."""
    return [
        {
            "ledger_account_id": src_avail_id,
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
    atomic: bool = False,
    balance_port: BalancePort | None = None,
) -> Transaction:
    """Ejecuta una transferencia atomica (hold + asiento + estados).

    - `atomic=False` (terceros/interbancaria, camino E5-T03): hold + asiento;
      retorna la `Transaction` en `SETTLED` (inmediata, `settle=True`) o en
      `POSTED` (diferida, `settle=False`), o en `REJECTED` si no hay fondos.
    - `atomic=True` (propia, `04#7.1`, E2-T07): UN solo asiento
      `2000-S -> 2000-D` sin hold ni `FUNDS_HELD`
      (`AUTHORIZED -> POSTED -> SETTLED`); exige `fee_minor == 0` y
      `settle=True`; retorna en `SETTLED` o en `REJECTED` si no hay fondos.
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
    if atomic:
        if not settle:
            raise ValueError("la ruta atomica propia (atomic=True) exige settle=True")
        if fee_minor != 0:
            raise ValueError("la transferencia propia no lleva comision (fee_minor debe ser 0)")
    if idempotency_key is not None:
        existing = tx_repository.get_by_idempotency_key(
            session,
            idempotency_key,
            initiator_user_id=_coerce_optional_user(initiator_user_id),
        )
        if existing is not None:
            return existing

    if balance_port is not None:
        port: BalancePort = balance_port
    else:  # Cableado productivo E2-T01 (opcion b): adaptador real sin ciclos.
        try:
            from app.composition import resolve_balance_port as _resolve_bp
        except ImportError:  # pragma: no cover - red de seguridad
            port = DEFAULT_BALANCE_PORT
        else:
            port = _resolve_bp(None, fallback=DEFAULT_BALANCE_PORT)
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

        # 5'. Ruta atomica propia (E2-T07, 04#7.1): sin hold ni FUNDS_HELD.
        if atomic:
            src_avail, _src_hold = ledger_service.ensure_customer_accounts(
                session, source, currency
            )
            dst_avail, _dst_hold = ledger_service.ensure_customer_accounts(
                session, target, currency
            )
            ledger_service.post_entry(
                session,
                entry_type=OWN_SETTLE_ENTRY_TYPE,
                postings=_own_settle_postings(
                    src_avail.id, dst_avail.id, source, target, amount_minor, currency
                ),
                transaction_id=tx.id,
                description=f"own settle {tx.id}",
            )
            port.apply_delta(session, source, -total_minor, currency)
            port.apply_delta(session, target, amount_minor, currency)
            tx_repository.transition_transaction(
                session, tx.id, "POSTED", reason="asiento propio registrado"
            )
            tx_repository.transition_transaction(
                session, tx.id, "SETTLED", reason="transferencia propia liquidada"
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
                    "fee_minor": 0,
                    "currency": currency,
                    "status": "SETTLED",
                },
            )
            return tx

        # 5. Retencion: subcuentas via fachada, hold ACTIVE, delta, estado.
        src_avail, src_hold = ledger_service.ensure_customer_accounts(session, source, currency)
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

                fee_account = get_by_code(session, FEE_ACCOUNT_CODE)
                if fee_account is None:  # pragma: no cover - defensivo
                    raise RuntimeError(f"cuenta de comision {FEE_ACCOUNT_CODE} inexistente")
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
    except Exception:  # noqa: BLE001 - cleanup best-effort y re-lanza (finally: raise)
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
                            except Exception:  # noqa: BLE001, S110 - best-effort cleanup
                                pass
                except Exception:  # noqa: BLE001, S110 - best-effort cleanup
                    pass
                try:
                    if tx.status in ("FUNDS_HELD", "POSTED"):
                        tx_repository.transition_transaction(
                            session, tx.id, "FAILED", reason="fallo tecnico"
                        )
                except Exception:  # noqa: BLE001, S110 - best-effort cleanup
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
                except Exception:  # noqa: BLE001, S110 - best-effort cleanup
                    pass
        finally:
            raise


def _emit_initiated(session: Session, tx: Transaction, *, tx_type: str, fee_minor: int) -> None:
    """Registra `transaction.initiated` en el outbox (best-effort, E2-T07)."""
    _publish_outbox(
        session,
        event_type=domain_events.TRANSACTION_INITIATED,
        tx=tx,
        payload={
            "transaction_id": str(tx.id),
            "type": tx_type,
            "source_account_id": str(tx.source_account_id),
            "target_account_id": str(tx.target_account_id),
            "amount_minor": int(tx.amount_minor),
            "fee_minor": int(fee_minor),
            "currency": tx.currency,
            "status": tx.status,
        },
    )


def transfer_own(
    session: Session,
    *,
    initiator_user_id: uuid.UUID | str,
    source_account_id: uuid.UUID | str,
    target_account_id: uuid.UUID | str,
    amount_minor: int,
    currency: str,
    idempotency_key: str | None = None,
    external_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
    balance_port: BalancePort | None = None,
) -> Transaction:
    """Caso de uso `OWN_TRANSFER` (E2-T07, HU06 CA-01, `04#7.1`).

    Valida las cuentas por fachada (ambas del titular, `ACTIVE`, moneda
    coherente) y ejecuta la ruta atomica SIN hold (`atomic=True`): un solo
    asiento `2000-S -> 2000-D`, sin comision. Saldo insuficiente ->
    `REJECTED` sin tocar saldos (`04#10`). Solo `flush` (el llamante decide
    la transaccion); eventos por `outbox` (`transaction.initiated` aqui,
    `transfer.settled` en el motor).
    """
    validate_amount_minor(amount_minor)
    validate_currency(currency)
    validate_transfer_parties(
        session,
        initiator_user_id=initiator_user_id,
        source_account_id=source_account_id,
        target_account_id=target_account_id,
        currency=currency,
        kind="OWN_TRANSFER",
    )
    tx = execute_transfer(
        session,
        source_account_id=source_account_id,
        target_account_id=target_account_id,
        amount_minor=amount_minor,
        currency=currency,
        fee_minor=0,
        tx_type="OWN_TRANSFER",
        idempotency_key=idempotency_key,
        initiator_user_id=_coerce_optional_user(initiator_user_id),
        external_ref=external_ref,
        metadata=metadata,
        settle=True,
        atomic=True,
        balance_port=balance_port,
    )
    _emit_initiated(session, tx, tx_type="OWN_TRANSFER", fee_minor=0)
    return tx


def transfer_third_party(
    session: Session,
    *,
    initiator_user_id: uuid.UUID | str,
    source_account_id: uuid.UUID | str,
    target_account_id: uuid.UUID | str,
    amount_minor: int,
    currency: str,
    fee_minor: int | None = None,
    idempotency_key: str | None = None,
    external_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
    settle: bool = True,
    balance_port: BalancePort | None = None,
) -> Transaction:
    """Caso de uso `THIRD_PARTY_TRANSFER` (E2-T07, HU06, `04#7.2`).

    Valida por fachada (origen del titular y `ACTIVE`, destino existente y
    `ACTIVE`, moneda coherente) y ejecuta el camino hold + liquidacion. La
    comision (`fee_minor` explicita o `transfer.third_party_fee_minor` de
    `config.parameters`, default `0`) se acredita a `4100`. Saldo
    insuficiente -> `REJECTED` sin tocar saldos. Solo `flush`; eventos por
    `outbox` (`transaction.initiated` aqui, `funds.held`/`transfer.settled`
    en el motor).
    """
    validate_amount_minor(amount_minor)
    validate_currency(currency)
    validate_transfer_parties(
        session,
        initiator_user_id=initiator_user_id,
        source_account_id=source_account_id,
        target_account_id=target_account_id,
        currency=currency,
        kind="THIRD_PARTY_TRANSFER",
    )
    resolved_fee = resolve_third_party_fee(session, fee_minor)
    tx = execute_transfer(
        session,
        source_account_id=source_account_id,
        target_account_id=target_account_id,
        amount_minor=amount_minor,
        currency=currency,
        fee_minor=resolved_fee,
        tx_type="THIRD_PARTY_TRANSFER",
        idempotency_key=idempotency_key,
        initiator_user_id=_coerce_optional_user(initiator_user_id),
        external_ref=external_ref,
        metadata=metadata,
        settle=settle,
        atomic=False,
        balance_port=balance_port,
    )
    _emit_initiated(session, tx, tx_type="THIRD_PARTY_TRANSFER", fee_minor=resolved_fee)
    return tx


#: Cache en memoria de validaciones de titular (E2-T08, opcional, sin ORM).
#: Clave = hash del CCI + titular normalizado + bank + huella del proveedor;
#: valor = (expira_en_monotonic, resultado). Solo guarda el booleano y el
#: titular enmascarado; nunca el CCI en claro ni el titular.
_HOLDER_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_HOLDER_CACHE_LOCK = threading.Lock()


def _holder_cache_ttl() -> float:
    """TTL del cache desde `Settings` (fuera del codigo, regla de oro 6)."""
    return resolve_cache_ttl_seconds()


def _holder_cache_key(
    *, cci_hash: str, holder_normalized: str, bank_code: str, provider: HolderLookupProvider
) -> str:
    fingerprint = f"{type(provider).__name__}:{getattr(provider, 'mode', '')}"
    return f"{cci_hash}|{holder_normalized}|{bank_code}|{fingerprint}"


def clear_holder_cache() -> None:
    """Vacia el cache de validaciones (principalmente para pruebas)."""
    with _HOLDER_CACHE_LOCK:
        _HOLDER_CACHE.clear()


def validate_holder(
    cci: object,
    holder_name: object = None,
    bank_code: object = None,
    provider: HolderLookupProvider | None = None,
) -> dict[str, Any]:
    """Fachada interna de validacion de titular/CCI (E2-T08, sin endpoint).

    La consumen `E2-T07`/`E2-T09` ANTES de mover dinero: valida el formato
    del CCI (20 digitos, `422` neutro sin llamar al proveedor), consulta el
    titular canonico por el adaptador y, si se indico `holder_name`, exige
    coincidencia normalizada (sin tildes, mayusculas). Retorna
    `{"valid": True, "holder_name_masked": "J*** P***"}`.

    Anti-enumeracion: "titular no coincide" y "CCI inexistente" lanzan el
    MISMO `HolderNotFoundError` neutro (404); el `timeout` se colapsa a
    `HolderLookupUnavailableError` (503) para no distinguir fallas de red.
    Ningun mensaje incluye el CCI ni el titular; los logs solo llevan el
    hash del CCI. Determinista: el mismo CCI produce el mismo resultado.
    """
    digits = validate_cci_format(cci)
    code = validate_bank_code(bank_code)
    wanted = normalize_name(holder_name) if holder_name is not None else ""
    if holder_name is not None and not wanted:
        raise HolderNotFoundError()
    active = provider if provider is not None else create_holder_lookup_provider()
    cci_hash = hash_cci(digits)
    logger.info("holder_validate start cci_hash=%s has_bank=%s", cci_hash, bool(code))

    cache_key = _holder_cache_key(
        cci_hash=cci_hash, holder_normalized=wanted, bank_code=code, provider=active
    )
    ttl = _holder_cache_ttl()
    if ttl > 0:
        with _HOLDER_CACHE_LOCK:
            cached = _HOLDER_CACHE.get(cache_key)
            if cached is not None and cached[0] > time.monotonic():
                logger.info("holder_validate cache_hit cci_hash=%s", cci_hash)
                return dict(cached[1])
            if cached is not None:
                _HOLDER_CACHE.pop(cache_key, None)

    try:
        record = active.lookup_holder(cci=digits, bank_code=code)
    except HolderLookupTimeoutError as exc:
        # Transporte indistinguible de "no disponible" (anti-enumeracion y
        # determinismo ante fallas: mismo error siempre).
        logger.warning("holder_validate timeout cci_hash=%s", cci_hash)
        raise HolderLookupUnavailableError() from exc
    canonical = normalize_name(record.holder_name)
    if not canonical:
        logger.info("holder_validate empty cci_hash=%s", cci_hash)
        raise HolderNotFoundError()
    if wanted and wanted != canonical:
        # Mismo error que CCI inexistente: sin oraculo de existencia.
        logger.info("holder_validate mismatch cci_hash=%s", cci_hash)
        raise HolderNotFoundError()
    result = {"valid": True, "holder_name_masked": mask_holder_name(record.holder_name)}
    logger.info("holder_validate ok cci_hash=%s", cci_hash)
    if ttl > 0:
        with _HOLDER_CACHE_LOCK:
            _HOLDER_CACHE[cache_key] = (time.monotonic() + ttl, dict(result))
    return result


__all__ = [
    "DEFAULT_BALANCE_PORT",
    "FEE_ACCOUNT_CODE",
    "HOLD_ENTRY_TYPE",
    "OWN_SETTLE_ENTRY_TYPE",
    "SETTLE_ENTRY_TYPE",
    "THIRD_PARTY_FEE_DEFAULT_MINOR",
    "THIRD_PARTY_FEE_PARAM_KEY",
    "AccountBlockedError",
    "AccountNotAuthorizedError",
    "AccountNotFoundError",
    "BalancePort",
    "BalanceView",
    "CurrencyMismatchError",
    "HolderLookupProvider",
    "HolderLookupTimeoutError",
    "HolderLookupUnavailableError",
    "HolderNotFoundError",
    "TransferError",
    "clear_holder_cache",
    "create_holder_lookup_provider",
    "execute_transfer",
    "resolve_third_party_fee",
    "transfer_own",
    "transfer_third_party",
    "validate_holder",
    "validate_transfer_parties",
]
