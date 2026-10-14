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
from app.modules.transactions.service import biometric as biometric_service
from app.modules.transactions.service.reversal import record_ledger_entry as _record_ledger_entry

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


def find_by_idempotency_key(
    session: Session,
    key: str,
    *,
    initiator_user_id: uuid.UUID | str | None = None,
) -> Transaction | None:
    """Fachada de lectura por `Idempotency-Key` acotada al titular (frontera).

    Envuelve `transactions.repository.get_by_idempotency_key` para que
    otros modulos (p. ej. `wallet.service.express`) resuelvan el replay
    sin importar el repositorio ajeno (regla de oro 4). Solo lectura
    (sin `flush`/`commit`); `None` si no existe.
    """
    return tx_repository.get_by_idempotency_key(
        session,
        key,
        initiator_user_id=_coerce_optional_user(initiator_user_id),
    )


def find_transaction_by_id(
    session: Session,
    transaction_id: uuid.UUID | str,
) -> Transaction | None:
    """Fachada de lectura por id de `Transaction` (frontera, R-1).

    Envuelve `transactions.repository.get_transaction` para que otros
    modulos (p. ej. el job `wallet/jobs/qr_reconcile.py`) lean el estado
    de la operacion del motor sin importar el repositorio ajeno (regla
    de oro 4, patron M-1). Solo lectura (sin `flush`/`commit`); `None`
    si no existe o el id es malformado.
    """
    try:
        wanted = (
            transaction_id
            if isinstance(transaction_id, uuid.UUID)
            else uuid.UUID(str(transaction_id))
        )
    except (ValueError, AttributeError, TypeError):
        return None
    return tx_repository.get_transaction(session, wanted)


def _resolve_beneficiary_id(
    explicit: uuid.UUID | str | None, metadata: dict[str, Any] | None
) -> uuid.UUID | str | None:
    """Beneficiario destino para la resolucion server-side (E2-T30, HU06/HU07).

    Precede el parametro explicito; como alternativa, `metadata` puede
    traer `"beneficiary_id"` (asi `POST /transfers/third-party` lo usa sin
    cambiar su contrato). Sin dato -> `None` (se exige `target_account_id`).
    No valida aqui: la fachada lo resuelve (inexistente/ajeno/baja ->
    `AccountNotFoundError` neutro, sin oraculo).
    """
    if explicit is not None:
        return explicit
    if isinstance(metadata, dict):
        candidate = metadata.get("beneficiary_id")
        if candidate is not None:
            return candidate
    return None


#: Mensaje neutro cuando el destino no resuelve (cuenta inexistente,
#: beneficiario inexistente/ajeno/dado de baja o numero no resoluble):
#: identico en todos los casos (anti-enumeracion, E2-T30).
NEUTRAL_TARGET_NOT_FOUND_MESSAGE = "cuenta destino inexistente"


def resolve_third_party_target_id(
    session: Session,
    *,
    initiator_user_id: uuid.UUID | str,
    target_account_id: uuid.UUID | str | None = None,
    beneficiary_id: uuid.UUID | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Resuelve el destino de `transfer_third_party` (E2-T30, solo lectura).

    Precedencia (sin tocar BD salvo lecturas):

    1. Si `target_account_id` viene y `accounts.service.get_account` lo
       resuelve -> se usa tal cual (comportamiento E2-T07).
    2. Si `target_account_id` falta **o no resuelve** y hay beneficiario
       (`beneficiary_id` explicito o `metadata["beneficiary_id"]`) ->
       `accounts.service.beneficiaries.get_for_operation` ->
       `account_or_cci` -> `accounts.service.get_account_by_number` ->
       el `account_id` real de la cuenta.
    3. Sin ninguno de los dos -> `ValueError` ("destino requerido",
       el router lo traduce a `400 VALIDATION_ERROR`).

    Solo resuelven cuentas CuyCash de 14 digitos: un `account_or_cci` de
    20 (CCI interbancario) u otro formato, o un numero sin cuenta, es
    `AccountNotFoundError` neutro (sin derivar a interbancaria). Un
    beneficiario inexistente, ajeno o dado de baja colapsa al MISMO
    `AccountNotFoundError` neutro (sin oraculo de existencia ni de
    titularidad; nunca `403` por el beneficiario). El numero en claro
    jamas se registra en logs ni se expone (regla de oro 7/9).
    """
    from app.modules.accounts.service import beneficiaries as _beneficiaries_service
    from app.modules.accounts.service import get_account as _get_account_info
    from app.modules.accounts.service import get_account_by_number as _get_by_number

    try:
        initiator = (
            initiator_user_id
            if isinstance(initiator_user_id, uuid.UUID)
            else uuid.UUID(str(initiator_user_id))
        )
    except (ValueError, AttributeError, TypeError) as exc:
        raise AccountNotAuthorizedError("titular invalido") from exc

    if target_account_id is not None:
        wanted = _coerce_account(target_account_id, "target_account_id")
        try:
            info = _get_account_info(session, wanted)
        except (ValueError, AttributeError, TypeError):
            info = None
        if info is not None:
            return wanted

    beneficiary_ref = _resolve_beneficiary_id(beneficiary_id, metadata)
    if beneficiary_ref is None:
        if target_account_id is not None:
            raise AccountNotFoundError(NEUTRAL_TARGET_NOT_FOUND_MESSAGE)
        raise ValueError("destino requerido: target_account_id o beneficiary_id")

    try:
        record = _beneficiaries_service.get_for_operation(session, initiator, beneficiary_ref)
    except (
        _beneficiaries_service.BeneficiaryNotFoundError,
        _beneficiaries_service.BeneficiaryForbiddenError,
        ValueError,
        AttributeError,
        TypeError,
    ):
        raise AccountNotFoundError(NEUTRAL_TARGET_NOT_FOUND_MESSAGE) from None
    number = record.get("account_or_cci")
    if not isinstance(number, str) or len(number) != 14 or not number.isdigit():
        raise AccountNotFoundError(NEUTRAL_TARGET_NOT_FOUND_MESSAGE)
    found = _get_by_number(session, number)
    if found is None:
        raise AccountNotFoundError(NEUTRAL_TARGET_NOT_FOUND_MESSAGE)
    return found["account_id"]


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


def _cleanup_technical_failure(session: Session, tx: Transaction | None) -> None:
    """Limpieza best-effort ante fallo tecnico (holds + `FAILED`, sin ocultar).

    Libera holds `ACTIVE` y marca `FAILED` si ya se habia retenido; nunca
    lanza (el llamante re-lanza la excepcion original para revertir).
    """
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
    except Exception:  # noqa: BLE001, S110 - best-effort cleanup
        pass


def _retain_hold_phase(
    session: Session,
    tx: Transaction,
    *,
    source: uuid.UUID,
    total_minor: int,
    currency: str,
    port: BalancePort,
):
    """Retencion con hold: subcuentas + fila + delta + asiento + `funds.held`.

    Sin transiciones de estado (el llamante avanza a `FUNDS_HELD`/`POSTED`
    o a `PENDING_AUTHORIZATION` segun la ruta). Solo `flush`.
    """
    src_avail, src_hold = ledger_service.ensure_customer_accounts(session, source, currency)
    hold = tx_repository.create_hold(
        session,
        transaction_id=tx.id,
        account_id=source,
        amount_minor=total_minor,
        currency=currency,
    )
    port.apply_delta(session, source, -total_minor, currency)
    hold_entry = ledger_service.post_entry(
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
    # Traza para el reverso (E2-T25): ids de asientos en `meta`, antes de
    # transicionar (el CAS expira el objeto: registrar siempre antes).
    _record_ledger_entry(session, tx, hold_entry.id, "hold")
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
    return hold, src_hold.id


def _settle_held_phase(
    session: Session,
    tx: Transaction,
    *,
    hold,
    src_hold_id: uuid.UUID,
    source: uuid.UUID,
    target: uuid.UUID,
    amount_minor: int,
    fee_minor: int,
    currency: str,
    port: BalancePort,
    settle: bool,
    device_id: str | None = None,
) -> Transaction:
    """Liquidacion de un hold ya en `POSTED` (`2100-S -> 2000-D` + fee a `4100`).

    Con `settle=False` (diferida, `04#7.3`) se detiene en `POSTED` con el
    hold `ACTIVE`. Solo `flush`.

    A1/HU22: el `transfer.settled` lleva `user_id` (titular) y
    `device_id` (si el llamante lo conoce) para que el consumidor de
    alertas dispare push + correo sin importar otros modulos.
    """
    if not settle:
        return tx
    dst_avail, _dst_hold = ledger_service.ensure_customer_accounts(session, target, currency)
    fee_account_id: uuid.UUID | None = None
    if fee_minor > 0:
        # Solo lectura del catalogo semilla (creado por
        # ensure_customer_accounts); TODO: exponer por fachada.
        from app.modules.ledger.repository import get_by_code

        fee_account = get_by_code(session, FEE_ACCOUNT_CODE)
        if fee_account is None:  # pragma: no cover - defensivo
            raise RuntimeError(f"cuenta de comision {FEE_ACCOUNT_CODE} inexistente")
        fee_account_id = fee_account.id
    settle_entry = ledger_service.post_entry(
        session,
        entry_type=SETTLE_ENTRY_TYPE,
        postings=_settle_postings(
            src_hold_id,
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
    # Traza para el reverso (E2-T25), antes de transicionar.
    _record_ledger_entry(session, tx, settle_entry.id, "settle")
    port.apply_delta(session, target, amount_minor, currency)
    tx_repository.update_hold_status(session, hold.id, HoldStatus.CAPTURED.value)
    tx = tx_repository.transition_transaction(
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
            "user_id": str(tx.initiator_user_id) if tx.initiator_user_id is not None else None,
            "device_id": device_id,
        },
    )
    return tx


def _pend_for_biometry(
    session: Session,
    tx: Transaction,
    *,
    source: uuid.UUID,
    target: uuid.UUID,
    amount_minor: int,
    currency: str,
    fee_minor: int,
    total_minor: int,
    atomic: bool,
    settle: bool,
    port: BalancePort,
    device_id: str | None,
    request_metadata: dict[str, Any] | None,
) -> Transaction:
    """Retiene en `PENDING_AUTHORIZATION` con reto biometrico (E2-T17, HU08).

    Comprueba fondos (sin fondos -> `REJECTED`, sin movimientos); en rutas
    con hold crea la retencion real (fila + delta + asiento + `funds.held`)
    y en la ruta atomica propia no retiene nada (preserva `04#7.1`: nada
    se mueve hasta `AUTHORIZED`). Emite el `nonce` a firmar y avanza
    `VALIDATED -> PENDING_AUTHORIZATION`. Solo `flush`.
    """
    ordered = sorted({source, target}, key=str)
    locked = {aid: port.lock_and_get(session, aid) for aid in ordered}
    src_balance = locked[source]
    if src_balance.currency != currency or src_balance.available_minor < total_minor:
        tx_repository.transition_transaction(
            session, tx.id, "REJECTED", reason="fondos insuficientes"
        )
        return tx_repository.get_transaction(session, tx.id)
    if not atomic:
        _retain_hold_phase(
            session, tx, source=source, total_minor=total_minor, currency=currency, port=port
        )
    hint = device_id
    if hint is None and isinstance(request_metadata, dict):
        candidate = request_metadata.get("device_id")
        if isinstance(candidate, str) and candidate.strip():
            hint = candidate
    block, _nonce, _expires = biometric_service.new_challenge_block(hint)
    block["resume"] = {"settle": bool(settle), "atomic": bool(atomic)}
    biometric_service.save_block(session, tx, block)
    tx = tx_repository.transition_transaction(
        session, tx.id, "PENDING_AUTHORIZATION", reason=biometric_service.REQUIRED_REASON
    )
    return tx


def continue_from_authorized(
    session: Session,
    tx: Transaction,
    *,
    source: uuid.UUID | str,
    target: uuid.UUID | str,
    amount_minor: int,
    currency: str,
    fee_minor: int,
    total_minor: int,
    tx_type: str,
    settle: bool = True,
    atomic: bool = False,
    port: BalancePort | None = None,
    balance_port: BalancePort | None = None,
    reuse_hold: bool = False,
    device_id: str | None = None,
) -> Transaction:
    """Continua una operacion `AUTHORIZED` (motor o `authorize` biometrico).

    - `reuse_hold=False` (ruta inmediata): comprueba fondos y retiene
      (hold + asiento) o liquida atomico, como E5-T03/E2-T07.
    - `reuse_hold=True` (tras biometria superada en ruta con hold):
      exige el hold `ACTIVE` creado en `PENDING_AUTHORIZATION` y lo
      reutiliza sin duplicar fila, delta ni asiento (`E2-T17`); si falta,
      falla cerrado (`BiometricStateError`, sin mover dinero).
    Fallo tecnico: libera holds + `FAILED` best-effort y re-lanza (el
    llamante revierte). Solo `flush`.
    """
    source = _coerce_account(source, "source_account_id")
    target = _coerce_account(target, "target_account_id")
    active_port: BalancePort = port if port is not None else balance_port  # type: ignore[assignment]
    if active_port is None:
        try:
            from app.composition import resolve_balance_port as _resolve_bp
        except ImportError:  # pragma: no cover - red de seguridad
            active_port = DEFAULT_BALANCE_PORT
        else:
            active_port = _resolve_bp(None, fallback=DEFAULT_BALANCE_PORT)
    fresh = tx_repository.get_transaction(session, tx.id)
    if fresh is None:  # pragma: no cover - defensivo
        raise KeyError(f"transaccion no encontrada: {tx.id}")
    tx = fresh
    try:
        if reuse_hold:
            hold = None
            for candidate in tx_repository.list_holds_by_transaction(session, tx.id):
                if (
                    candidate.status == HoldStatus.ACTIVE.value
                    and int(candidate.amount_minor) == int(total_minor)
                    and candidate.currency == currency
                ):
                    hold = candidate
                    break
            if hold is None:
                raise biometric_service.BiometricStateError("la operacion no conserva la retencion")
            tx = tx_repository.transition_transaction(
                session, tx.id, "FUNDS_HELD", reason="fondos retenidos"
            )
            tx = tx_repository.transition_transaction(
                session, tx.id, "POSTED", reason="asiento de hold registrado"
            )
            resume = (biometric_service.get_block(tx) or {}).get("resume") or {}
            resume_settle = resume.get("settle", settle)
            tx = _settle_held_phase(
                session,
                tx,
                hold=hold,
                src_hold_id=hold_src_hold_id(session, source, currency),
                source=source,
                target=target,
                amount_minor=amount_minor,
                fee_minor=fee_minor,
                currency=currency,
                port=active_port,
                settle=settle if resume_settle is None else bool(resume_settle),
                device_id=device_id,
            )
            return tx

        # 4. Bloqueo en orden estable + comprobacion de fondos.
        ordered = sorted({source, target}, key=str)
        locked = {aid: active_port.lock_and_get(session, aid) for aid in ordered}
        src_balance = locked[source]
        if src_balance.currency != currency or src_balance.available_minor < total_minor:
            tx = tx_repository.transition_transaction(
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
            own_entry = ledger_service.post_entry(
                session,
                entry_type=OWN_SETTLE_ENTRY_TYPE,
                postings=_own_settle_postings(
                    src_avail.id, dst_avail.id, source, target, amount_minor, currency
                ),
                transaction_id=tx.id,
                description=f"own settle {tx.id}",
            )
            # Traza para el reverso (E2-T25), antes de transicionar.
            _record_ledger_entry(session, tx, own_entry.id, "own")
            active_port.apply_delta(session, source, -total_minor, currency)
            active_port.apply_delta(session, target, amount_minor, currency)
            tx = tx_repository.transition_transaction(
                session, tx.id, "POSTED", reason="asiento propio registrado"
            )
            tx = tx_repository.transition_transaction(
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
                    "user_id": (
                        str(tx.initiator_user_id) if tx.initiator_user_id is not None else None
                    ),
                    "device_id": device_id,
                },
            )
            return tx

        # 5. Retencion + 6. asiento de hold + 7. liquidacion (E5-T03).
        hold, src_hold_id = _retain_hold_phase(
            session,
            tx,
            source=source,
            total_minor=total_minor,
            currency=currency,
            port=active_port,
        )
        tx = tx_repository.transition_transaction(
            session, tx.id, "FUNDS_HELD", reason="fondos retenidos"
        )
        tx = tx_repository.transition_transaction(
            session, tx.id, "POSTED", reason="asiento de hold registrado"
        )
        tx = _settle_held_phase(
            session,
            tx,
            hold=hold,
            src_hold_id=src_hold_id,
            source=source,
            target=target,
            amount_minor=amount_minor,
            fee_minor=fee_minor,
            currency=currency,
            port=active_port,
            settle=settle,
            device_id=device_id,
        )
        return tx
    except Exception:  # noqa: BLE001 - cleanup best-effort y re-lanza (finally: raise)
        # Fallo tecnico: libera holds + FAILED best-effort y re-lanza
        # (no se oculta el rollback: el llamador revierte la sesion).
        try:
            _cleanup_technical_failure(session, tx)
        finally:
            raise


def hold_src_hold_id(session: Session, source: uuid.UUID, currency: str) -> uuid.UUID:
    """Subcuenta `2100` del origen (para liquidar un hold reutilizado)."""
    _, src_hold = ledger_service.ensure_customer_accounts(session, source, currency)
    return src_hold.id


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
    risk_profile: str | None = None,
    device_id: str | None = None,
    threshold_reader=None,
    daily_limit_reader=None,
) -> Transaction:
    """Ejecuta una transferencia atomica (hold + asiento + estados).

    - `atomic=False` (terceros/interbancaria, camino E5-T03): hold + asiento;
      retorna la `Transaction` en `SETTLED` (inmediata, `settle=True`) o en
      `POSTED` (diferida, `settle=False`), o en `REJECTED` si no hay fondos.
    - `atomic=True` (propia, `04#7.1`, E2-T07): UN solo asiento
      `2000-S -> 2000-D` sin hold ni `FUNDS_HELD`
      (`AUTHORIZED -> POSTED -> SETTLED`); exige `fee_minor == 0` y
      `settle=True`; retorna en `SETTLED` o en `REJECTED` si no hay fondos.
    - Gate biometrico (E2-T17, HU08; umbral por operacion E2-T34): si el
      monto de la operacion (`amount_minor + fee_minor`) supera el
      umbral (`transfer.biometric_threshold_minor`), retorna en
      `PENDING_AUTHORIZATION` con hold (rutas con hold) y `nonce` a firmar;
      la continuacion llega por `biometric.authorize_transaction`
      (E2-T19). Si supera el limite diario del perfil, retorna `REJECTED`
      (`DAILY_LIMIT_EXCEEDED`) sin hold. `risk_profile` lo aporta el
      llamante (por defecto rige el generico/`STANDARD`); `device_id`
      anticipa el dispositivo del reto (si falta se adopta al autorizar).
      E2-T33 (retiro del tope pre-aprobado): el `beneficiary_id` (campo o
      `metadata`) ya no altera la decision; toda operacion sobre el umbral
      exige autorizacion y el evento `transaction.initiated` no lleva datos
      de exencion.
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

    total_minor = amount_minor + fee_minor

    if balance_port is not None:
        port: BalancePort = balance_port
    else:  # Cableado productivo E2-T01 (opcion b): adaptador real sin ciclos.
        try:
            from app.composition import resolve_balance_port as _resolve_bp
        except ImportError:  # pragma: no cover - red de seguridad
            port = DEFAULT_BALANCE_PORT
        else:
            port = _resolve_bp(None, fallback=DEFAULT_BALANCE_PORT)
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

        # 3b. Gate biometrico + limite diario por perfil (E2-T17, HU08).
        gate = biometric_service.evaluate_gate(
            session,
            user_id=_coerce_optional_user(initiator_user_id),
            amount_minor=amount_minor,
            fee_minor=fee_minor,
            exclude_tx_id=tx.id,
            risk_profile=risk_profile,
            threshold_reader=threshold_reader,
            daily_limit_reader=daily_limit_reader,
        )
        if gate.limit_exceeded:
            biometric_service.mark_rejected(session, tx, biometric_service.DAILY_LIMIT_EXCEEDED)
            tx = tx_repository.transition_transaction(
                session, tx.id, "REJECTED", reason=biometric_service.DAILY_LIMIT_REASON
            )
            return tx
        if gate.requires_biometry:
            return _pend_for_biometry(
                session,
                tx,
                source=source,
                target=target,
                amount_minor=amount_minor,
                currency=currency,
                fee_minor=fee_minor,
                total_minor=total_minor,
                atomic=atomic,
                settle=settle,
                port=port,
                device_id=device_id,
                request_metadata=metadata,
            )
        tx_repository.transition_transaction(session, tx.id, "AUTHORIZED", reason="autorizada")
        return continue_from_authorized(
            session,
            tx,
            source=source,
            target=target,
            amount_minor=amount_minor,
            currency=currency,
            fee_minor=fee_minor,
            total_minor=total_minor,
            tx_type=tx_type,
            settle=settle,
            atomic=atomic,
            port=port,
            device_id=device_id,
        )
    except Exception:  # noqa: BLE001 - cleanup best-effort y re-lanza (finally: raise)
        # Fallo tecnico en el prefijo (creacion, gate o retencion en
        # PENDING): libera holds + FAILED best-effort y re-lanza (el
        # llamante revierte la sesion; rollback total, sin residual).
        try:
            _cleanup_technical_failure(session, tx)
        finally:
            raise


def _emit_initiated(session: Session, tx: Transaction, *, tx_type: str, fee_minor: int) -> None:
    """Registra `transaction.initiated` en el outbox (best-effort, E2-T07).

    Payload minimo (solo UUIDs e importes, sin PII): E2-T33 retira los
    datos de exencion por tope (`exemption_reason`/`beneficiary_id`).
    """
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
    risk_profile: str | None = None,
    device_id: str | None = None,
    threshold_reader=None,
    daily_limit_reader=None,
) -> Transaction:
    """Caso de uso `OWN_TRANSFER` (E2-T07, HU06 CA-01, `04#7.1`).

    Valida las cuentas por fachada (ambas del titular, `ACTIVE`, moneda
    coherente) y ejecuta la ruta atomica SIN hold (`atomic=True`): un solo
    asiento `2000-S -> 2000-D`, sin comision. Saldo insuficiente ->
    `REJECTED` sin tocar saldos (`04#10`). Gate biometrico (E2-T17, HU08):
    sobre el umbral queda en `PENDING_AUTHORIZATION` sin retener (la ruta
    atomica no crea hold) hasta `authorize_transaction` (E2-T19); sobre el
    limite diario del perfil queda `REJECTED` (`DAILY_LIMIT_EXCEEDED`).
    Solo `flush` (el llamante decide la transaccion); eventos por `outbox`
    (`transaction.initiated` aqui, `transfer.settled` en el motor).
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
        risk_profile=risk_profile,
        device_id=device_id,
        threshold_reader=threshold_reader,
        daily_limit_reader=daily_limit_reader,
    )
    _emit_initiated(session, tx, tx_type="OWN_TRANSFER", fee_minor=0)
    return tx


def transfer_third_party(
    session: Session,
    *,
    initiator_user_id: uuid.UUID | str,
    source_account_id: uuid.UUID | str,
    target_account_id: uuid.UUID | str | None = None,
    amount_minor: int,
    currency: str,
    fee_minor: int | None = None,
    idempotency_key: str | None = None,
    external_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
    settle: bool = True,
    balance_port: BalancePort | None = None,
    risk_profile: str | None = None,
    device_id: str | None = None,
    threshold_reader=None,
    daily_limit_reader=None,
    beneficiary_id: uuid.UUID | str | None = None,
) -> Transaction:
    """Caso de uso `THIRD_PARTY_TRANSFER` (E2-T07, HU06, `04#7.2`).

    El destino se resuelve server-side con `resolve_third_party_target_id`
    (E2-T30): `target_account_id` de una cuenta real, o `beneficiary_id`
    (campo o `metadata["beneficiary_id"]`) resuelto a la cuenta real del
    beneficiario. La transaccion persiste el `target_account_id` real y
    conserva `beneficiary_id` en `metadata` (trazabilidad del destino
    E2-T30; E2-T33: ya no exime del gate biometrico).

    Valida por fachada (origen del titular y `ACTIVE`, destino existente y
    `ACTIVE`, moneda coherente) y ejecuta el camino hold + liquidacion. La
    comision (`fee_minor` explicita o `transfer.third_party_fee_minor` de
    `config.parameters`, default `0`) se acredita a `4100`. Saldo
    insuficiente -> `REJECTED` sin tocar saldos. Gate biometrico (E2-T17,
    HU08): sobre el umbral queda en `PENDING_AUTHORIZATION` con hold hasta
    `authorize_transaction` (E2-T19); sobre el limite diario del perfil
    queda `REJECTED` (`DAILY_LIMIT_EXCEEDED`) sin hold. Solo `flush`;
    eventos por `outbox` (`transaction.initiated` aqui,
    `funds.held`/`transfer.settled` en el motor).
    """
    validate_amount_minor(amount_minor)
    validate_currency(currency)
    resolved_target = resolve_third_party_target_id(
        session,
        initiator_user_id=initiator_user_id,
        target_account_id=target_account_id,
        beneficiary_id=beneficiary_id,
        metadata=metadata,
    )
    validate_transfer_parties(
        session,
        initiator_user_id=initiator_user_id,
        source_account_id=source_account_id,
        target_account_id=resolved_target,
        currency=currency,
        kind="THIRD_PARTY_TRANSFER",
    )
    resolved_fee = resolve_third_party_fee(session, fee_minor)
    beneficiary_ref = _resolve_beneficiary_id(beneficiary_id, metadata)
    effective_metadata = metadata
    if beneficiary_ref is not None:
        effective_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        if "beneficiary_id" not in effective_metadata:
            effective_metadata["beneficiary_id"] = str(beneficiary_ref)
    tx = execute_transfer(
        session,
        source_account_id=source_account_id,
        target_account_id=resolved_target,
        amount_minor=amount_minor,
        currency=currency,
        fee_minor=resolved_fee,
        tx_type="THIRD_PARTY_TRANSFER",
        idempotency_key=idempotency_key,
        initiator_user_id=_coerce_optional_user(initiator_user_id),
        external_ref=external_ref,
        metadata=effective_metadata,
        settle=settle,
        atomic=False,
        balance_port=balance_port,
        risk_profile=risk_profile,
        device_id=device_id,
        threshold_reader=threshold_reader,
        daily_limit_reader=daily_limit_reader,
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
    "INSUFFICIENT_FUNDS",
    "INSURANCE_ACCOUNT_CODE",
    "INTEREST_ACCOUNT_CODE",
    "LOAN_DISBURSEMENT_ENTRY_TYPE",
    "LOAN_DISBURSEMENT_TX_TYPE",
    "LOAN_INSTALLMENT_ENTRY_TYPE",
    "LOAN_INSTALLMENT_TX_TYPE",
    "OWN_SETTLE_ENTRY_TYPE",
    "PORTFOLIO_ACCOUNT_CODE",
    "SETTLE_ENTRY_TYPE",
    "THIRD_PARTY_FEE_DEFAULT_MINOR",
    "THIRD_PARTY_FEE_PARAM_KEY",
    "AccountBlockedError",
    "AccountNotAuthorizedError",
    "AccountNotFoundError",
    "BalancePort",
    "BalanceView",
    "CurrencyMismatchError",
    "DisbursementConflictError",
    "DisbursementError",
    "DisbursementNotFoundError",
    "DisbursementValidationError",
    "HolderLookupProvider",
    "HolderLookupTimeoutError",
    "HolderLookupUnavailableError",
    "HolderNotFoundError",
    "LoanPaymentConflictError",
    "LoanPaymentError",
    "LoanPaymentNotFoundError",
    "LoanPaymentValidationError",
    "TransferError",
    "biometric",
    "clear_holder_cache",
    "continue_from_authorized",
    "create_holder_lookup_provider",
    "disburse_loan",
    "execute_transfer",
    "find_by_idempotency_key",
    "find_disbursement_tx",
    "find_transaction_by_id",
    "hold_src_hold_id",
    "mask_account_number",
    "pay_loan_installment",
    "resolve_third_party_fee",
    "resolve_third_party_target_id",
    "transfer_interbank",
    "transfer_own",
    "transfer_third_party",
    "validate_holder",
    "validate_interbank_parties",
    "validate_transfer_parties",
]


def __getattr__(name: str):
    """Re-export perezoso de la saga interbancaria (E2-T09), del desembolso (E3-T17)
    y del pago de cuota (E3-T20).

    `service/interbank.py`, `service/disbursement.py` y
    `service/loan_payment.py` no se importan a nivel top para evitar el
    ciclo (referencian este paquete de forma perezosa dentro de sus
    funciones); se resuelven bajo demanda y se cachean en el modulo.
    """
    if name in ("transfer_interbank", "validate_interbank_parties"):
        from app.modules.transactions.service import interbank as _interbank

        value = getattr(_interbank, name)
        globals()[name] = value
        return value
    if name in (
        "LOAN_DISBURSEMENT_ENTRY_TYPE",
        "LOAN_DISBURSEMENT_TX_TYPE",
        "PORTFOLIO_ACCOUNT_CODE",
        "DisbursementConflictError",
        "DisbursementError",
        "DisbursementNotFoundError",
        "DisbursementValidationError",
        "disburse_loan",
        "find_disbursement_tx",
        "mask_account_number",
    ):
        from app.modules.transactions.service import disbursement as _disbursement

        value = getattr(_disbursement, name)
        globals()[name] = value
        return value
    if name in (
        "LOAN_INSTALLMENT_ENTRY_TYPE",
        "LOAN_INSTALLMENT_TX_TYPE",
        "INSUFFICIENT_FUNDS",
        "INTEREST_ACCOUNT_CODE",
        "INSURANCE_ACCOUNT_CODE",
        "LoanPaymentConflictError",
        "LoanPaymentError",
        "LoanPaymentNotFoundError",
        "LoanPaymentValidationError",
        "pay_loan_installment",
    ):
        from app.modules.transactions.service import loan_payment as _loan_payment

        value = getattr(_loan_payment, name)
        globals()[name] = value
        return value
    raise AttributeError(f"modulo {__name__!r} sin atributo {name!r}")
