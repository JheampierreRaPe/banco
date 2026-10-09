"""Saga de transferencia interbancaria saliente (E2-T09, HU06 CA-02).

Orquestador sobre el motor (`transactions`) y el `ledger` por fachada:

- Validacion previa del titular/CCI via la fachada `validate_holder`
  (E2-T08) ANTES de mover dinero; errores mapeados a codigos neutros
  (sin filtrar existencia de cuentas ni PII en logs).
- Asiento 7.3 en cuatro pasos: retencion `2000-A -> 2100-A` (monto+fee),
  al confirmar envio `2100-A -> 2200-Transito` (monto), al confirmar
  recepcion `2200-Transito -> 1000-Banco` (monto) + comision `2100-A ->
  4100`. Si el tercero no confirma en plazo: compensacion y
  `FAILED`/`REVERSED`. La comision se carga SOLO al liquidar (nunca en
  fallo/timeout: la operacion fallida no cobra) y el reverso la anula via
  `reverse_entry`; sigue el precedente de E2-T07 (`2100-S -> 4100` en el
  settle) en lugar del `2000-A` literal de `04#7.3` paso 2 (ver reporte).
- Maquina: `FUNDS_HELD -> POSTED -> SETTLED`; timeout mantiene
  `FUNDS_HELD`, reintenta y al expirar libera + compensa (`FAILED`);
  `REVERSED` usa asiento inverso (`ledger.reverse_entry`), nunca borra.
- Todo hold queda terminal (nunca huerfano); `debitos=creditos` siempre.

Frontera (reglas de oro 1/3/4/8):
- Dinero entero en centimos; nunca `float`.
- Ledger solo por su fachada (`ensure_customer_accounts`, `post_entry`,
  `reverse_entry`, `find_hold_release`, `get_entry`); lectura puntual del
  catalogo (`2200`, `1000`, `4100`) via `ledger.repository.get_by_code`
  (solo lectura, mismo patron que E2-T07 con `4100`).
- Cuentas solo por la fachada `accounts.service.get_account` (D-01);
  jamas se importan modelos/repositorios de `accounts` ni de `ledger`.
- Parametros fuera del codigo: `config.parameters` best-effort
  (`transfer.interbank_fee_minor`, `interbank.timeout_seconds`,
  `interbank.max_attempts`) con fallback a entorno y a defaults.
- Eventos solo por `outbox` con import perezoso (regla 8).
- Saldos solo por el puerto `BalancePort` (mismo cableado que E5-T03).
- El CCI/titular nunca se persiste en claro: solo `cci_hash` y el
  titular enmascarado van a `meta`/logs (reglas 7/9).

Concurrencia (de fondo): cada paso serializa por transaccion todas las
escrituras de `meta`, transiciones y asientos de esa tx (`_tx_exclusive` +
`SELECT ... FOR UPDATE` + `UPDATE ... WHERE version` con merge y reintento;
la pasarela corre fuera del lock). `_set_meta` es CAS por `version` (nunca
last-writer-wins): el `bump` de un timeout relee fresco y conserva el
`send_entry` del ganador, asi `send_entry`/`settle_entry` se postean una
sola vez. Se mantiene el CAS de `transition_transaction` (defensa en
profundidad) y el patron reclamar-primero en todas las ramas. Todo es
`flush` sin `commit` (atomico por sesion: o todo el paso persiste o nada).
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.adapters.holder_lookup import (
    HolderLookupProvider,
    hash_cci,
    validate_bank_code,
    validate_cci_format,
)
from app.adapters.interbank_gateway import (
    GatewayTimeoutError,
    GatewayUnavailableError,
    InterbankGateway,
    SendResult,
    create_interbank_gateway,
    hash_reference,
)
from app.core import events as domain_events
from app.modules.ledger import service as ledger_service
from app.modules.transactions import repository as tx_repository
from app.modules.transactions.domain.state_machine import (
    ConcurrencyError,
    InvalidTransitionError,
    validate_amount_minor,
    validate_currency,
)
from app.modules.transactions.models import HoldStatus, Transaction
from app.modules.transactions.service import biometric as biometric_service

logger = logging.getLogger(__name__)

#: Exclusion por transaccion de la saga (de fondo, no parche).
#:
#: Cada paso (`transfer_interbank` post-pasarela, `poll_interbank`,
#: `_settle_confirmed`, `_compensate`, `reverse_interbank`) serializa por
#: `transactions.id` todas las escrituras de `meta`, transiciones y asientos
#: de esa tx: el perdedor relee fresco y no asienta doble. Dos niveles:
#:
#: - En proceso: `RLock` por tx (SQLite ignora `FOR UPDATE`; sin este nivel
#:   dos hilos/engines reescribirian `meta` desde snapshots obsoletos).
#:   `RLock` (no `Lock`) porque `_compensate`/`_settle_confirmed` se llaman
#:   ya bajo exclusion desde `poll_interbank`/`transfer_interbank`.
#: - En BD (Postgres/multiproceso): `SELECT ... FOR UPDATE` sobre la fila
#:   (`_locked_get`, `populate_existing` para no devolver el identity map
#:   obsoleto) + `UPDATE ... WHERE version` (`_set_meta` CAS). La pasarela
#:   (I/O de red) corre SIEMPRE fuera de la exclusion: solo el tramo
#:   releer->reclamar->asentar->guardar meta va bajo lock, asi la `Barrier`
#:   de los tests nunca se interbloquea y el `commit` ajeno se detecta por
#:   `rowcount` + reintento con merge (nunca last-writer-wins).
_TX_LOCKS_GUARD = threading.Lock()
_TX_LOCKS: dict[str, threading.RLock] = {}


def _per_tx_lock(tx_id: uuid.UUID | str) -> threading.RLock:
    key = str(tx_id)
    with _TX_LOCKS_GUARD:
        lock = _TX_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _TX_LOCKS[key] = lock
        return lock


@contextmanager
def _tx_exclusive(tx_id: uuid.UUID | str):
    """Exclusion mutua por transaccion (en proceso; ver modulo)."""
    with _per_tx_lock(tx_id):
        yield


def _is_lock_contention(exc: BaseException) -> bool:
    """`True` si es contienda de escritores SQLite (reintentable).

    En Postgres `FOR UPDATE` bloquea; en SQLite el segundo escritor recibe
    `OperationalError: database is locked` aunque haya `busy_timeout`
    (artefacto del harness con archivos/WAL + `NullPool`). Se trata como
    CAS perdedor: rollback al `SAVEPOINT` + reintento sobre fresco.
    """
    text = str(exc).lower()
    return "database is locked" in text or "database table is locked" in text


def _backoff(attempt: int) -> None:
    time.sleep(min(0.05 * (attempt + 1), 0.3))


def _locked_get(session: Session, tx_id: uuid.UUID | str) -> Transaction | None:
    """Relee la fila bajo lock (`SELECT ... FOR UPDATE`, best-effort).

    `populate_existing` fuerza a recargar desde BD aunque la sesion tenga
    el snapshot obsoleto en su identity map. En SQLite `FOR UPDATE` es
    no-op (el RLock en proceso + CAS cubre); en Postgres bloquea hasta el
    `commit` ajeno. Nunca `None` salvo fila inexistente.
    """
    try:
        wanted = tx_id if isinstance(tx_id, uuid.UUID) else uuid.UUID(str(tx_id))
    except (ValueError, AttributeError, TypeError):
        return None
    try:
        stmt = (
            sa.select(Transaction)
            .where(Transaction.id == wanted)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        row = session.scalars(stmt).first()
        if row is not None:
            return row
    except Exception:  # noqa: BLE001 - fallback al get simple
        logger.debug("interbank locked_get sin FOR UPDATE, fallback al get")
    try:
        current = session.get(Transaction, wanted)
        if current is not None:
            try:
                session.expire(current)
                return session.get(Transaction, wanted)
            except Exception:  # noqa: BLE001 - best-effort
                return current
        return None
    except Exception:  # noqa: BLE001 - defensivo
        return tx_repository.get_transaction(session, wanted)


#: Tipo de transaccion de la saga (vive en `transactions.type`).
INTERBANK_TX_TYPE = "INTERBANK_TRANSFER"

#: Asientos de la saga (`04#7.3`; `entry_type` libre <= 30, ver ledger E5-T10).
INTERBANK_HOLD_ENTRY_TYPE = "INTERBANK_HOLD"
INTERBANK_SEND_ENTRY_TYPE = "INTERBANK_SEND"
INTERBANK_SETTLE_ENTRY_TYPE = "INTERBANK_SETTLE"
#: Devolucion al disponible tras reverso post-liquidacion (`2100-A -> 2000-A`).
INTERBANK_RETURN_ENTRY_TYPE = "INTERBANK_RETURN"

#: Cuenta canonica de comisiones (misma que E2-T07, cierra G05).
FEE_ACCOUNT_CODE = "4100"
#: Cuenta de transito interbancario (`04#8`: toda operacion pendiente pasa
#: por `2200-Transito` hasta confirmar/compensar).
TRANSIT_ACCOUNT_CODE = "2200"
#: Caja/bancos: destino final al confirmar recepcion (`04#7.3` paso 4).
BANK_ACCOUNT_CODE = "1000"

#: Parametros configurables (duenos `shared`/`config`; fuera del codigo).
INTERBANK_FEE_PARAM_KEY = "transfer.interbank_fee_minor"
INTERBANK_TIMEOUT_PARAM_KEY = "interbank.timeout_seconds"
INTERBANK_MAX_ATTEMPTS_PARAM_KEY = "interbank.max_attempts"
INTERBANK_FEE_DEFAULT_MINOR = 0
INTERBANK_TIMEOUT_DEFAULT_SECONDS = 300
INTERBANK_MAX_ATTEMPTS_DEFAULT = 5

#: Codigos de fallo visibles en `tracking`/`failure_code`.
FAILURE_INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
FAILURE_TRANSFER_TIMEOUT = "TRANSFER_TIMEOUT"
FAILURE_GATEWAY_REJECTED = "INTERBANK_REJECTED"


class InterbankError(ValueError):
    """Error base de la saga (mapeado a `AppError` en el router)."""


class ExternalRefConflictError(InterbankError):
    """`external_ref` ya usada por otra operacion (-> 409 CONFLICT)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _read_int_parameter(session: Session, key: str, default: int) -> int:
    """Lee `config.parameters.<key>` best-effort (fallback a `default`).

    Mismo patron que `service._read_int_parameter` (SAVEPOINT acotado a la
    tabla transversal duena de `shared`/`config`); un fallo no contamina la
    transaccion del llamante. Sin secretos/PII en logs.
    """
    try:
        with session.begin_nested():
            raw = session.execute(
                sa.text("SELECT value_json FROM config.parameters WHERE key = :key"),
                {"key": key},
            ).scalar()
    except Exception:  # noqa: BLE001 - sin acceso a parameters rige el default
        logger.debug("interbank parameters sin acceso (%s): default", key)
        return default
    if raw is None or isinstance(raw, bool):
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


def _read_int_env(name: str, default: int) -> int:
    try:
        candidate = int(str(os.getenv(name, "")).strip())
    except (ValueError, AttributeError, TypeError):
        return default
    return candidate if candidate >= 0 else default


def resolve_interbank_fee(session: Session, explicit_fee_minor: int | None = None) -> int:
    """Comision interbancaria: explicita > `config.parameters` > `0`.

    Nunca `float`; se acredita a `4100` (`04#7.3` paso 2).
    """
    if explicit_fee_minor is None:
        return _read_int_parameter(session, INTERBANK_FEE_PARAM_KEY, INTERBANK_FEE_DEFAULT_MINOR)
    if (
        isinstance(explicit_fee_minor, bool)
        or not isinstance(explicit_fee_minor, int)
        or explicit_fee_minor < 0
    ):
        raise ValueError(f"fee_minor debe ser entero >= 0, recibido: {explicit_fee_minor!r}")
    return explicit_fee_minor


def resolve_timeout_seconds(session: Session | None, explicit: float | None = None) -> int:
    """Plazo de la saga: explicito > `config.parameters` > entorno > default."""
    if explicit is not None:
        candidate = int(explicit)
        if candidate <= 0:
            raise ValueError(f"timeout_seconds debe ser > 0, recibido: {explicit!r}")
        return candidate
    if session is not None:
        from_config = _read_int_parameter(session, INTERBANK_TIMEOUT_PARAM_KEY, -1)
        if from_config >= 0:
            configured = from_config if from_config > 0 else INTERBANK_TIMEOUT_DEFAULT_SECONDS
            return configured
    return _read_int_env("INTERBANK_TIMEOUT_SECONDS", INTERBANK_TIMEOUT_DEFAULT_SECONDS)


def resolve_max_attempts(session: Session | None, explicit: int | None = None) -> int:
    """Reintentos de polling: explicito > `config.parameters` > entorno > default."""
    if explicit is not None:
        if isinstance(explicit, bool) or not isinstance(explicit, int) or explicit <= 0:
            raise ValueError(f"max_attempts debe ser entero > 0, recibido: {explicit!r}")
        return explicit
    if session is not None:
        from_config = _read_int_parameter(session, INTERBANK_MAX_ATTEMPTS_PARAM_KEY, -1)
        if from_config > 0:
            return from_config
    return _read_int_env("INTERBANK_MAX_ATTEMPTS", INTERBANK_MAX_ATTEMPTS_DEFAULT)


def resolve_gateway(explicit: InterbankGateway | None = None) -> InterbankGateway:
    """Pasarela efectiva: explicita (tests) > fabrica por entorno."""
    if explicit is not None:
        return explicit
    return create_interbank_gateway()


def _publish_outbox(
    session: Session, *, event_type: str, tx: Transaction, payload: dict[str, Any]
) -> bool:
    """Publica en outbox con import perezoso; `False` si no disponible."""
    try:
        from app.core.outbox import record as outbox_record
    except ImportError:
        return False
    outbox_record(
        session,
        aggregate_type="transaction",
        aggregate_id=tx.id,
        event_type=event_type,
        payload=payload,
    )
    return True


def _get_meta(tx: Transaction) -> dict[str, Any]:
    return dict(tx.meta or {})


def _set_meta(session: Session, tx: Transaction, meta: dict[str, Any]) -> None:
    """Guarda `meta` con compare-and-swap por `version` (nunca last-writer-wins).

    `UPDATE ... WHERE id=:id AND version=:esperada` + `version+1`; el
    perdedor recibe `ConcurrencyError` y debe releer fresco + fusionar
    (merge) en lugar de pisar (p. ej. el `bump` de un timeout no borra el
    `send_entry` del ganador). Expira el objeto para que el llamante
    relea fresco en el siguiente acceso.
    """
    current = int(tx.version)
    result = session.execute(
        sa.update(Transaction)
        .where(Transaction.id == tx.id, Transaction.version == current)
        .values(meta=dict(meta), version=current + 1, updated_at=_utcnow())
    )
    if (result.rowcount or 0) != 1:
        try:
            session.expire(tx)
        except Exception:  # noqa: BLE001, S110 - best-effort
            pass
        raise ConcurrencyError(f"meta obsoleta para {tx.id} (posible doble procesamiento)")
    try:
        session.expire(tx)
    except Exception:  # noqa: BLE001, S110 - best-effort
        pass


def _interbank_meta(tx: Transaction) -> dict[str, Any]:
    meta = _get_meta(tx)
    block = meta.get("interbank")
    return dict(block) if isinstance(block, dict) else {}


def _save_interbank_meta(session: Session, tx: Transaction, block: dict[str, Any]) -> None:
    meta = _get_meta(tx)
    meta["interbank"] = dict(block)
    _set_meta(session, tx, meta)


def _set_failure(session: Session, tx: Transaction, code: str) -> None:
    meta = _get_meta(tx)
    meta["failure_code"] = code
    _set_meta(session, tx, meta)


def _save_block_merging(
    session: Session, tx_id: uuid.UUID | str, apply, *, retries: int = 8
) -> Transaction:
    """Lee-modifica-escribe `meta.interbank` bajo exclusion con merge.

    `apply(block)` muta el bloque fresco (idempotente: `setdefault` /
    conservar `entries`/`gateway_ref`); el guardado es CAS por `version`.
    Ante `ConcurrencyError` (commit ajeno entre la lectura y el `flush`) o
    contienda SQLite (`database is locked`) se reintenta sobre fresco: el
    perdedor nunca pisa lo del ganador. Retorna la tx fresca.
    """
    last_exc: Exception | None = None
    with _tx_exclusive(tx_id):
        for attempt in range(max(1, int(retries))):
            try:
                with session.begin_nested():
                    fresh = _locked_get(session, tx_id)
                    if fresh is None:  # pragma: no cover - defensivo
                        raise KeyError("transaccion interbancaria inexistente")
                    block = _interbank_meta(fresh)
                    apply(block)
                    _save_interbank_meta(session, fresh, block)
            except ConcurrencyError as exc:
                last_exc = exc
                _backoff(attempt)
                continue
            except OperationalError as exc:
                if not _is_lock_contention(exc):
                    raise
                last_exc = exc
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001, S110 - best-effort
                    pass
                # Espera al commit ajeno (el RLock ya serializa en proceso;
                # aqui el que bloquea es el lock de archivo SQLite).
                _backoff(attempt)
                continue
            refreshed = _locked_get(session, tx_id)
            # `_locked_get` abre su propio SELECT; si la sesion quedo en
            # estado fallido por la contienda, el `rollback` de arriba la
            # deja utilizable. Si aun asi falla, reintenta.
            return refreshed if refreshed is not None else _locked_get(session, tx_id)
    raise last_exc  # pragma: no cover - solo si agota reintentos


def _bump_attempts_merged(session: Session, tx_id: uuid.UUID | str) -> dict[str, Any]:
    """Incrementa `attempts` sobre meta fresca bajo exclusion (con merge).

    Preserva `entries`/`gateway_ref`/`expires_at` del ganador: el timeout
    perdedor suma su intento sin borrar el `send_entry` ajeno. Retorna el
    bloque fresco ya guardado.
    """

    def _apply(block: dict[str, Any]) -> None:
        block["attempts"] = int(block.get("attempts", 0)) + 1
        block["last_poll_at"] = _utcnow().isoformat()

    fresh = _save_block_merging(session, tx_id, _apply)
    return _interbank_meta(fresh)


def failure_code_of(tx: Transaction) -> str | None:
    """Codigo de fallo visible (`None` si la operacion no fallo)."""
    meta = tx.meta or {}
    if isinstance(meta, dict) and meta.get("failure_code"):
        return str(meta["failure_code"])
    if tx.status == "REJECTED":
        return FAILURE_INSUFFICIENT_FUNDS
    return None


def tracking_of(tx: Transaction) -> dict[str, Any]:
    """Seguimiento para `GET /transactions/{id}` (sin PII: solo hash/mascara)."""
    block = _interbank_meta(tx)
    return {
        "poll_url": f"/api/v1/transactions/{tx.id}",
        "status": tx.status,
        "external_ref": tx.external_ref,
        "gateway_ref": block.get("gateway_ref"),
        "attempts": int(block.get("attempts", 0)),
        "expires_at": block.get("expires_at"),
        "holder_name_masked": block.get("holder_name_masked"),
    }


def _find_by_external_ref(
    session: Session, external_ref: str, initiator: uuid.UUID | None
) -> Transaction | None:
    stmt = sa.select(Transaction).where(Transaction.external_ref == external_ref)
    if initiator is None:
        stmt = stmt.where(Transaction.initiator_user_id.is_(None))
    else:
        stmt = stmt.where(Transaction.initiator_user_id == initiator)
    return session.scalars(stmt).first()


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def _coerce_optional_user(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"initiator_user_id debe ser UUID, recibido: {value!r}") from exc


def _resolve_port(explicit=None):  # tipo laxo: evita import en tests de dominio
    if explicit is not None:
        return explicit
    try:
        from app.composition import resolve_balance_port as _resolve_bp
    except ImportError:  # pragma: no cover - red de seguridad
        from app.modules.transactions.service import DEFAULT_BALANCE_PORT

        return DEFAULT_BALANCE_PORT
    from app.modules.transactions.service import DEFAULT_BALANCE_PORT

    return _resolve_bp(None, fallback=DEFAULT_BALANCE_PORT)


def _catalog_account_id(session: Session, code: str):
    """Lee una cuenta del catalogo semilla (solo lectura, como E2-T07)."""
    from app.modules.ledger.repository import get_by_code

    account = get_by_code(session, code)
    if account is None:  # pragma: no cover - defensivo
        raise RuntimeError(f"cuenta de catalogo {code} inexistente")
    return account.id


def validate_interbank_parties(
    session: Session,
    *,
    initiator_user_id: uuid.UUID | str,
    source_account_id: uuid.UUID | str,
    currency: str,
    cci: object,
    holder_name: object = None,
    bank_code: object = None,
    holder_provider: HolderLookupProvider | None = None,
) -> dict[str, Any]:
    """Validaciones previas SIN mover dinero (el router las corre ANTES de
    reclamar la idempotencia: un 404/422/403 no consume la clave).

    1. Titular/CCI via la fachada `validate_holder` (E2-T08): propaga sus
       errores tipados (el router los mapea a codigos neutros).
    2. Cuenta origen por la fachada `accounts` (D-01): existe, `ACTIVE`,
       moneda coherente y del titular (mismos errores que E2-T07).
    Sin `flush`/`commit`; sin PII en logs (solo hash del CCI).
    """
    from app.modules.transactions.service import (
        AccountBlockedError,
        AccountNotAuthorizedError,
        AccountNotFoundError,
        CurrencyMismatchError,
        validate_holder,
    )

    try:
        initiator = (
            initiator_user_id
            if isinstance(initiator_user_id, uuid.UUID)
            else uuid.UUID(str(initiator_user_id))
        )
    except (ValueError, AttributeError, TypeError) as exc:
        raise AccountNotAuthorizedError("titular invalido") from exc
    source = _coerce_uuid(source_account_id, "source_account_id")

    holder_result = validate_holder(cci, holder_name, bank_code, provider=holder_provider)

    from app.modules.accounts.service import get_account as _get_account_info

    try:
        src_info = _get_account_info(session, source)
    except (ValueError, AttributeError, TypeError) as exc:
        raise AccountNotFoundError("cuenta origen inexistente") from exc
    if src_info is None:
        raise AccountNotFoundError("cuenta origen inexistente")
    if src_info["user_id"] != initiator:
        raise AccountNotAuthorizedError("la cuenta origen no pertenece al titular")
    if src_info["status"] != "ACTIVE":
        raise AccountBlockedError(f"cuenta origen no disponible ({src_info['status']})")
    if src_info["currency"] != currency:
        raise CurrencyMismatchError(
            f"moneda incoherente sin FX: operacion {currency}, origen {src_info['currency']}"
        )
    return {"source": src_info, "holder": holder_result}


def _post_hold_entries(
    session: Session,
    tx: Transaction,
    *,
    src_avail_id,
    src_hold_id,
    total_minor: int,
    currency: str,
    source_ref: uuid.UUID,
) -> dict[str, str]:
    """Asiento 7.3 paso 1: `INTERBANK_HOLD` (`2000-A -> 2100-A`, monto+fee).

    El hold cubre el total para reservar la comision; esta se carga recien
    al liquidar (`_post_settle_entry`). Retorna ids para `meta`."""
    hold_entry = ledger_service.post_entry(
        session,
        entry_type=INTERBANK_HOLD_ENTRY_TYPE,
        postings=[
            {
                "ledger_account_id": src_avail_id,
                "direction": "DEBIT",
                "amount_minor": total_minor,
                "currency": currency,
                "account_ref": source_ref,
            },
            {
                "ledger_account_id": src_hold_id,
                "direction": "CREDIT",
                "amount_minor": total_minor,
                "currency": currency,
                "account_ref": source_ref,
            },
        ],
        transaction_id=tx.id,
        description=f"interbank hold {tx.id}",
    )
    return {"hold_entry": str(hold_entry.id)}


def _hold_account_id(session: Session, source_ref: uuid.UUID, currency: str):
    """Subcuenta `2100-<origen>` (retenido) via la fachada ledger."""
    _, retained = ledger_service.ensure_customer_accounts(session, source_ref, currency)
    return retained.id


def _post_send_entry(
    session: Session,
    tx: Transaction,
    *,
    amount_minor: int,
    currency: str,
    source_ref: uuid.UUID,
) -> str:
    """Asiento 7.3 paso 3: `INTERBANK_SEND` (`2100-A -> 2200-Transito`)."""
    entry = ledger_service.post_entry(
        session,
        entry_type=INTERBANK_SEND_ENTRY_TYPE,
        postings=[
            {
                "ledger_account_id": _hold_account_id(session, source_ref, currency),
                "direction": "DEBIT",
                "amount_minor": amount_minor,
                "currency": currency,
                "account_ref": source_ref,
            },
            {
                "ledger_account_id": _catalog_account_id(session, TRANSIT_ACCOUNT_CODE),
                "direction": "CREDIT",
                "amount_minor": amount_minor,
                "currency": currency,
                "account_ref": source_ref,
            },
        ],
        transaction_id=tx.id,
        description=f"interbank send {tx.id}",
    )
    return str(entry.id)


def _post_settle_entry(
    session: Session,
    tx: Transaction,
    *,
    amount_minor: int,
    fee_minor: int,
    currency: str,
    source_ref: uuid.UUID,
) -> str:
    """Asiento 7.3 paso 4: `INTERBANK_SETTLE` (`2200-Transito -> 1000-Banco`).

    `2200` cuadra a cero: lo que entro en el paso 3 sale aqui. La comision
    (`2100-A -> 4100`, precedente E2-T07) se carga aqui y solo aqui: una
    operacion fallida/timeout nunca cobra, y el reverso la anula junto al
    resto via `reverse_entry`.
    """
    from app.modules.transactions.service import FEE_ACCOUNT_CODE as _FEE_CODE

    postings: list[dict] = [
        {
            "ledger_account_id": _catalog_account_id(session, TRANSIT_ACCOUNT_CODE),
            "direction": "DEBIT",
            "amount_minor": amount_minor,
            "currency": currency,
            "account_ref": source_ref,
        },
        {
            "ledger_account_id": _catalog_account_id(session, BANK_ACCOUNT_CODE),
            "direction": "CREDIT",
            "amount_minor": amount_minor,
            "currency": currency,
            "account_ref": source_ref,
        },
    ]
    if fee_minor > 0:
        postings += [
            {
                "ledger_account_id": _hold_account_id(session, source_ref, currency),
                "direction": "DEBIT",
                "amount_minor": fee_minor,
                "currency": currency,
                "account_ref": source_ref,
            },
            {
                "ledger_account_id": _catalog_account_id(session, _FEE_CODE),
                "direction": "CREDIT",
                "amount_minor": fee_minor,
                "currency": currency,
                "account_ref": source_ref,
            },
        ]
    entry = ledger_service.post_entry(
        session,
        entry_type=INTERBANK_SETTLE_ENTRY_TYPE,
        postings=postings,
        transaction_id=tx.id,
        description=f"interbank settle {tx.id}",
    )
    return str(entry.id)


def _reverse_posted_entries(session: Session, tx: Transaction, block: dict[str, Any]) -> None:
    """Invierte los asientos de transito ya publicados (mas nuevo primero).

    Idempotente: si el asiento ya esta `REVERSED` se omite (original
    intacto, regla de oro 2); cualquier otro fallo se propaga (rollback).
    """
    entries = dict(block.get("entries") or {})
    for key in ("settle_entry", "send_entry"):
        raw_id = entries.get(key)
        if not raw_id:
            continue
        try:
            ledger_service.reverse_entry(
                session,
                raw_id,
                transaction_id=tx.id,
                description=f"interbank reversal {tx.id} ({key})",
            )
        except ValueError:
            current = ledger_service.get_entry(session, raw_id)
            if current is not None and current.status == "REVERSED":
                continue
            raise


def _release_hold_best_effort(session: Session, tx: Transaction, *, balance_port=None) -> None:
    """Devuelve lo retenido (`2100-A -> 2000-A`, `HOLD_RELEASE`, `04#7.3`).

    Solo si el hold sigue `ACTIVE`; idempotente via `find_hold_release`
    (no duplica el compensatorio) y via la transicion terminal del hold.
    Restaura ademas el `available` por el puerto de saldos: a diferencia
    de E5-T03 (que revierte la sesion ante fallo), esta compensacion hace
    `commit`, asi que sin el delta positivo el cliente perderia fondos.
    """
    port = _resolve_port(balance_port)
    holds = tx_repository.list_holds_by_transaction(session, tx.id)
    for hold in holds:
        if hold.status != HoldStatus.ACTIVE.value:
            continue
        try:
            tx_repository.update_hold_status(session, hold.id, HoldStatus.RELEASED.value)
        except (KeyError, ValueError):
            pass
        port.apply_delta(session, hold.account_id, int(hold.amount_minor), hold.currency)
        if (
            ledger_service.find_hold_release(session, transaction_id=tx.id, hold_id=hold.id)
            is not None
        ):
            continue
        available, retained = ledger_service.ensure_customer_accounts(
            session, hold.account_id, hold.currency
        )
        ledger_service.post_entry(
            session,
            entry_type="HOLD_RELEASE",
            transaction_id=tx.id,
            description=f"hold release {hold.id} (tx {tx.id})",
            postings=[
                {
                    "ledger_account_id": retained.id,
                    "direction": "DEBIT",
                    "amount_minor": hold.amount_minor,
                    "currency": hold.currency,
                    "account_ref": hold.account_id,
                },
                {
                    "ledger_account_id": available.id,
                    "direction": "CREDIT",
                    "amount_minor": hold.amount_minor,
                    "currency": hold.currency,
                    "account_ref": hold.account_id,
                },
            ],
        )
    _return_captured_funds(session, tx, balance_port=port)


def _return_captured_funds(session: Session, tx: Transaction, *, balance_port=None) -> None:
    """Devuelve al disponible lo ya liquidado ante reverso post-`SETTLED`.

    Si el hold quedo `CAPTURED`, el dinero salio a la red (`2200 -> 1000`)
    y revertir el transito lo deja en `2100-A`: este `INTERBANK_RETURN`
    (`2100-A -> 2000-A`) + delta positivo lo devuelve al cliente.
    Idempotente via `meta.entries["return_entry"]` (el hold sigue
    `CAPTURED`, terminal: nunca se reabre).
    """
    block = _interbank_meta(tx)
    entries = dict(block.get("entries") or {})
    if entries.get("return_entry"):
        return
    captured = [
        h
        for h in tx_repository.list_holds_by_transaction(session, tx.id)
        if h.status == HoldStatus.CAPTURED.value
    ]
    if not captured:
        return
    port = _resolve_port(balance_port)
    for hold in captured:
        available, retained = ledger_service.ensure_customer_accounts(
            session, hold.account_id, hold.currency
        )
        entry = ledger_service.post_entry(
            session,
            entry_type=INTERBANK_RETURN_ENTRY_TYPE,
            transaction_id=tx.id,
            description=f"interbank return {hold.id} (tx {tx.id})",
            postings=[
                {
                    "ledger_account_id": retained.id,
                    "direction": "DEBIT",
                    "amount_minor": hold.amount_minor,
                    "currency": hold.currency,
                    "account_ref": hold.account_id,
                },
                {
                    "ledger_account_id": available.id,
                    "direction": "CREDIT",
                    "amount_minor": hold.amount_minor,
                    "currency": hold.currency,
                    "account_ref": hold.account_id,
                },
            ],
        )
        entries["return_entry"] = str(entry.id)
        port.apply_delta(session, hold.account_id, int(hold.amount_minor), hold.currency)
    block["entries"] = entries
    _save_interbank_meta(session, tx, block)


def _claim_transition(session: Session, tx: Transaction, target: str, *, reason: str) -> bool:
    """Reclama el avance con compare-and-swap atomico; `True` solo si ESTE
    llamador gano el CAS. `False` = otro proceso gano o la transicion ya no
    aplica (replay: sin doble asiento, sin doble historial)."""
    try:
        tx_repository.transition_transaction(
            session,
            tx.id,
            target,
            reason=reason,
            expected_version=int(tx.version),
        )
    except (InvalidTransitionError, ConcurrencyError, KeyError):
        try:
            session.expire(tx)
        except Exception:  # noqa: BLE001, S110 - best-effort: el get siguiente recarga
            pass
        logger.debug("interbank CAS perdedor target=%s", target)
        return False
    return True


def _merge_gateway_ref(session: Session, tx: Transaction, gateway_ref: str | None) -> Transaction:
    """Registra `gateway_ref` sobre meta fresca (best-effort, sin PII).

    Fusion con CAS + merge bajo exclusion: si ya existe no escribe; ante
    `ConcurrencyError` reintenta sobre fresco (nunca pisa `entries`).
    """
    if not gateway_ref:
        return tx

    def _apply(block: dict[str, Any]) -> None:
        block.setdefault("gateway_ref", gateway_ref)

    try:
        merged = _save_block_merging(session, tx.id, _apply)
    except (ConcurrencyError, OperationalError, KeyError):
        return tx_repository.get_transaction(session, tx.id)
    current = _locked_get(session, tx.id)
    return current if current is not None else merged


def _settle_confirmed(session: Session, tx: Transaction) -> Transaction:
    """Liquida una confirmacion (`FUNDS_HELD` o `POSTED` -> `SETTLED`).

    Exclusion por tx: relee fresco bajo `_tx_exclusive` + `FOR UPDATE`,
    reclama la transicion (CAS ya existente, el perdedor es replay) y solo
    entonces postea/verifica `send_entry`/`settle_entry` dentro del lock.
    Cada intento corre en `SAVEPOINT`: si el CAS de meta pierde (commit
    ajeno), se revierte el asiento del intento y se reintenta sobre fresco,
    asi el perdedor jamas duplica asientos. Atomico por sesion (`flush`).
    """
    tx_id = tx.id
    with _tx_exclusive(tx_id):
        for _ in range(5):
            try:
                with session.begin_nested():
                    fresh = _locked_get(session, tx_id)
                    if fresh is None:  # pragma: no cover - defensivo
                        raise KeyError("transaccion interbancaria inexistente")
                    if fresh.status == "SETTLED":
                        return tx_repository.get_transaction(session, tx_id)
                    amount_minor = int(fresh.amount_minor)
                    currency = fresh.currency
                    source_ref = fresh.source_account_id

                    if fresh.status == "FUNDS_HELD":
                        if not _claim_transition(
                            session, fresh, "POSTED", reason="envio confirmado"
                        ):
                            return tx_repository.get_transaction(session, tx_id)
                        fresh = _locked_get(session, tx_id)
                        if fresh is None:  # pragma: no cover - defensivo
                            raise KeyError("transaccion interbancaria inexistente")
                        if fresh.status == "SETTLED":
                            return tx_repository.get_transaction(session, tx_id)
                    block = _interbank_meta(fresh)
                    entries = dict(block.get("entries") or {})
                    if "send_entry" not in entries:
                        entries["send_entry"] = _post_send_entry(
                            session,
                            fresh,
                            amount_minor=amount_minor,
                            currency=currency,
                            source_ref=source_ref,
                        )
                        block["entries"] = entries
                        _save_interbank_meta(session, fresh, block)
                        fresh = _locked_get(session, tx_id)

                    if not _claim_transition(
                        session, fresh, "SETTLED", reason="recepcion confirmada"
                    ):
                        return tx_repository.get_transaction(session, tx_id)
                    fresh = _locked_get(session, tx_id)
                    if fresh is None:  # pragma: no cover - defensivo
                        raise KeyError("transaccion interbancaria inexistente")
                    block = _interbank_meta(fresh)
                    entries = dict(block.get("entries") or {})
                    if "settle_entry" not in entries:
                        entries["settle_entry"] = _post_settle_entry(
                            session,
                            fresh,
                            amount_minor=amount_minor,
                            fee_minor=int(fresh.fee_minor or 0),
                            currency=currency,
                            source_ref=source_ref,
                        )
                        block["entries"] = entries
                        _save_interbank_meta(session, fresh, block)
                        fresh = _locked_get(session, tx_id)

                    for hold in tx_repository.list_holds_by_transaction(session, tx_id):
                        if hold.status == HoldStatus.ACTIVE.value:
                            try:
                                tx_repository.update_hold_status(
                                    session, hold.id, HoldStatus.CAPTURED.value
                                )
                            except (KeyError, ValueError):
                                pass
                    _publish_outbox(
                        session,
                        event_type=domain_events.TRANSFER_SETTLED,
                        tx=fresh,
                        payload={
                            "transaction_id": str(fresh.id),
                            "source_account_id": str(fresh.source_account_id),
                            "amount_minor": amount_minor,
                            "fee_minor": int(fresh.fee_minor or 0),
                            "currency": currency,
                            "external_ref": fresh.external_ref,
                            "status": "SETTLED",
                        },
                    )
                    logger.info(
                        "interbank settled ref_hash=%s", hash_reference(fresh.external_ref or "")
                    )
                    return tx_repository.get_transaction(session, tx_id)
            except ConcurrencyError:
                _backoff(0)
                continue
            except OperationalError as _op_exc:
                if not _is_lock_contention(_op_exc):
                    raise
                _backoff(1)
                continue
        return tx_repository.get_transaction(session, tx_id)


def _compensate(
    session: Session,
    tx: Transaction,
    *,
    target: str,
    reason: str,
    failure_code: str | None,
    balance_port=None,
) -> Transaction:
    """Compensacion sin perdida bajo exclusion por tx.

    Relee fresco con `FOR UPDATE`, reclama el estado final con CAS (el
    perdedor es replay) y solo entonces invierte transito / libera hold /
    guarda `failure_code`. Cada intento corre en `SAVEPOINT` con reintento
    ante `ConcurrencyError` (commit ajeno): nunca pisa `meta.entries`.
    """
    tx_id = tx.id
    with _tx_exclusive(tx_id):
        for _ in range(5):
            try:
                with session.begin_nested():
                    fresh = _locked_get(session, tx_id)
                    if fresh is None:  # pragma: no cover - defensivo
                        raise KeyError("transaccion interbancaria inexistente")
                    if fresh.status == target:
                        return tx_repository.get_transaction(session, tx_id)
                    if not _claim_transition(session, fresh, target, reason=reason):
                        return tx_repository.get_transaction(session, tx_id)
                    fresh = _locked_get(session, tx_id)
                    if fresh is None:  # pragma: no cover - defensivo
                        raise KeyError("transaccion interbancaria inexistente")
                    _reverse_posted_entries(session, fresh, _interbank_meta(fresh))
                    # Relee tras el reverso (el `reverse_entry` no toca
                    # `transactions`, pero el objeto puede haber expirado).
                    fresh = _locked_get(session, tx_id)
                    if fresh is None:  # pragma: no cover - defensivo
                        raise KeyError("transaccion interbancaria inexistente")
                    _release_hold_best_effort(session, fresh, balance_port=balance_port)
                    if failure_code:
                        latest = _locked_get(session, tx_id)
                        if latest is None:  # pragma: no cover - defensivo
                            raise KeyError("transaccion interbancaria inexistente")
                        # `_release_hold_best_effort` pudo guardar meta
                        # (`return_entry`): relee para no pisarla.
                        try:
                            _set_failure(session, latest, failure_code)
                        except ConcurrencyError:
                            # El hold-release del mismo intento avanzo la
                            # version: fusiona el codigo sobre fresco.
                            newest = _locked_get(session, tx_id)
                            if newest is None:  # pragma: no cover
                                raise KeyError("transaccion interbancaria inexistente")
                            meta = _get_meta(newest)
                            meta["failure_code"] = failure_code
                            _set_meta(session, newest, meta)
                            latest = _locked_get(session, tx_id)
                        except OperationalError as _op_inner:
                            if not _is_lock_contention(_op_inner):
                                raise
                            raise ConcurrencyError(str(_op_inner))
                    done = _locked_get(session, tx_id)
                    if done is None:  # pragma: no cover - defensivo
                        raise KeyError("transaccion interbancaria inexistente")
                    _publish_outbox(
                        session,
                        event_type=domain_events.FUNDS_RELEASED,
                        tx=done,
                        payload={
                            "transaction_id": str(done.id),
                            "external_ref": done.external_ref,
                            "status": target,
                            "failure_code": failure_code,
                        },
                    )
                    logger.info(
                        "interbank compensated ref_hash=%s status=%s",
                        hash_reference(done.external_ref or ""),
                        target,
                    )
                    return tx_repository.get_transaction(session, tx_id)
            except ConcurrencyError:
                _backoff(0)
                continue
            except OperationalError as _op_exc:
                if not _is_lock_contention(_op_exc):
                    raise
                _backoff(1)
                continue
        return tx_repository.get_transaction(session, tx_id)


def _expired(block: dict[str, Any], now: datetime) -> bool:
    raw = block.get("expires_at")
    if not raw:
        return False
    try:
        expires = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return moment >= expires


def _bump_attempts(session: Session, tx: Transaction, block: dict[str, Any]) -> dict[str, Any]:
    """Suma un intento sobre meta FRESCA bajo exclusion (nunca pisa `entries`).

    `tx`/`block` heredados pueden ser snapshots obsoletos (dos pollers): se
    ignoran y se relee bajo `_tx_exclusive` + CAS con merge, preservando
    `entries`/`gateway_ref`/`expires_at` del ganador. Retorna el bloque
    fresco ya guardado.
    """
    return _bump_attempts_merged(session, tx.id)


def _send_after_hold(
    session: Session,
    tx: Transaction,
    *,
    amount_minor: int,
    currency: str,
    source: uuid.UUID,
    balance_port=None,
    gw: InterbankGateway,
) -> Transaction:
    """Envia por la pasarela y encamina el resultado (E2-T09, `04#7.3`).

    Tramo extraido de `transfer_interbank` para reutilizarlo en la
    continuacion tras biometria (`resume_after_authorization`, E2-T17):
    `CONFIRMED` liquida, `ACCEPTED` reclama `POSTED` (solo el ganador
    postea `send_entry`), timeout/caida mantiene `FUNDS_HELD`
    (reintentable) y rechazo compensa a `FAILED`.     Solo `flush`.
    """
    snap_block = _interbank_meta(tx)
    try:
        result: SendResult = gw.send_transfer(
            reference=tx.external_ref,
            amount_minor=amount_minor,
            currency=currency,
            bank_code=str(snap_block.get("bank_code") or ""),
            cci_hash=str(snap_block.get("cci_hash") or ""),
        )
    except (GatewayTimeoutError, GatewayUnavailableError):
        logger.warning("interbank send pending ref_hash=%s", hash_reference(tx.external_ref or ""))
        return tx_repository.get_transaction(session, tx.id)

    # Bajo exclusion: fusiona `gateway_ref` sobre fresco con CAS + merge
    # (nunca pisa `entries`; variante menor `gateway_ref` antes del claim).
    def _apply_gw(block: dict[str, Any]) -> None:
        if result.gateway_ref and not block.get("gateway_ref"):
            block["gateway_ref"] = result.gateway_ref

    try:
        _save_block_merging(session, tx.id, _apply_gw)
    except (ConcurrencyError, OperationalError, KeyError):
        pass

    if result.status == "REJECTED":
        return _compensate(
            session,
            tx,
            target="FAILED",
            reason="contraparte rechazo el envio",
            failure_code=FAILURE_GATEWAY_REJECTED,
            balance_port=balance_port,
        )
    if result.status == "CONFIRMED":
        return _settle_confirmed(session, tx)
    # ACCEPTED: reclama POSTED primero y luego publica el envio bajo
    # exclusion (el perdedor ante carrera retorna sin asentar; SAVEPOINT +
    # reintento ante CAS perdedor).
    with _tx_exclusive(tx.id):
        for _ in range(5):
            try:
                with session.begin_nested():
                    fresh = _locked_get(session, tx.id)
                    if fresh is None:  # pragma: no cover - defensivo
                        break
                    if fresh.status != "FUNDS_HELD":
                        break
                    if not _claim_transition(session, fresh, "POSTED", reason="envio aceptado"):
                        break
                    posted = _locked_get(session, tx.id)
                    if posted is None:  # pragma: no cover - defensivo
                        break
                    entries = dict(_interbank_meta(posted).get("entries") or {})
                    if "send_entry" not in entries:
                        entries["send_entry"] = _post_send_entry(
                            session,
                            posted,
                            amount_minor=amount_minor,
                            currency=currency,
                            source_ref=source,
                        )
                        block = _interbank_meta(posted)
                        block["entries"] = entries
                        _save_interbank_meta(session, posted, block)
                break
            except ConcurrencyError:
                _backoff(0)
                continue
            except OperationalError as _op_exc:
                if not _is_lock_contention(_op_exc):
                    raise
                _backoff(1)
                continue
    return tx_repository.get_transaction(session, tx.id)


def _pend_interbank_for_biometry(
    session: Session,
    tx: Transaction,
    *,
    source: uuid.UUID,
    amount_minor: int,
    currency: str,
    fee_minor: int,
    total_minor: int,
    digits: str,
    bank_code: str,
    holder_masked: str,
    attempts_limit: int,
    ttl_seconds: int,
    port,
    device_id: str | None,
    request_metadata: dict[str, Any] | None,
) -> Transaction:
    """Retiene una interbancaria en `PENDING_AUTHORIZATION` (E2-T17, HU08).

    Comprueba fondos (sin fondos -> `REJECTED`, sin movimientos); retiene
    el total (hold `ACTIVE` SIN `expires_at` + delta + asiento `2000-A ->
    2100-A` + `funds.held`), deja `external_ref` y el bloque `meta`
    (`cci_hash`/mascara, jamas el CCI en claro) listos para el envio,
    emite el `nonce` a firmar y avanza `VALIDATED -> PENDING_AUTHORIZATION`
    (sin enviar por la pasarela). Solo `flush`.
    """
    ordered = sorted({source}, key=str)
    locked = {aid: port.lock_and_get(session, aid) for aid in ordered}
    src_balance = locked[source]
    if src_balance.currency != currency or src_balance.available_minor < total_minor:
        tx = tx_repository.transition_transaction(
            session, tx.id, "REJECTED", reason="fondos insuficientes"
        )
        tx = tx_repository.get_transaction(session, tx.id)
        _publish_outbox(
            session,
            event_type=domain_events.TRANSACTION_INITIATED,
            tx=tx,
            payload={
                "transaction_id": str(tx.id),
                "type": INTERBANK_TX_TYPE,
                "source_account_id": str(source),
                "amount_minor": amount_minor,
                "fee_minor": fee_minor,
                "currency": currency,
                "status": tx.status,
            },
        )
        return tx

    src_avail, _src_hold = ledger_service.ensure_customer_accounts(session, source, currency)
    tx_repository.create_hold(
        session,
        transaction_id=tx.id,
        account_id=source,
        amount_minor=total_minor,
        currency=currency,
        expires_at=None,  # el timeout lo posee esta saga (meta.expires_at)
    )
    port.apply_delta(session, source, -total_minor, currency)
    entries = _post_hold_entries(
        session,
        tx,
        src_avail_id=src_avail.id,
        src_hold_id=_src_hold.id,
        total_minor=total_minor,
        currency=currency,
        source_ref=source,
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

    tx = tx_repository.get_transaction(session, tx.id)
    if tx.external_ref is None:
        tx.external_ref = f"IB-{tx.id.hex[:12].upper()}"
        session.add(tx)
        session.flush()
    block: dict[str, Any] = {
        "cci_hash": hash_cci(digits),
        "bank_code": bank_code,
        "holder_name_masked": holder_masked,
        "gateway_ref": "",
        "attempts": 0,
        "max_attempts": attempts_limit,
        "expires_at": (_utcnow().timestamp() + ttl_seconds),
        "entries": entries,
    }
    block["expires_at"] = datetime.fromtimestamp(block["expires_at"], UTC).isoformat()
    _save_interbank_meta(session, tx, block)

    hint = device_id
    if hint is None and isinstance(request_metadata, dict):
        candidate = request_metadata.get("device_id")
        if isinstance(candidate, str) and candidate.strip():
            hint = candidate
    challenge, _nonce, _expires = biometric_service.new_challenge_block(hint)
    biometric_service.save_block(session, tx, challenge)
    tx = tx_repository.transition_transaction(
        session, tx.id, "PENDING_AUTHORIZATION", reason=biometric_service.REQUIRED_REASON
    )
    _publish_outbox(
        session,
        event_type=domain_events.TRANSACTION_INITIATED,
        tx=tx,
        payload={
            "transaction_id": str(tx.id),
            "type": INTERBANK_TX_TYPE,
            "source_account_id": str(source),
            "amount_minor": amount_minor,
            "fee_minor": fee_minor,
            "currency": currency,
            "status": tx.status,
        },
    )
    return tx_repository.get_transaction(session, tx.id)


def resume_after_authorization(
    session: Session,
    tx_id: uuid.UUID | str,
    *,
    balance_port=None,
    gateway: InterbankGateway | None = None,
    daily_limit_reader=None,
) -> Transaction:
    """Reanuda una interbancaria tras biometria superada (E2-T17, HU08).

    La llama `biometric.authorize_transaction` con la operacion ya en
    `AUTHORIZED`: exige el hold `ACTIVE` creado en `PENDING_AUTHORIZATION`
    (sin duplicarlo), re-chequea el limite diario con el acumulado a ese
    instante (E2-T36: si `acumulado + total > limite` la operacion queda
    `REJECTED` con `DAILY_LIMIT_EXCEEDED`, hold liberado con el
    `HOLD_RELEASE` existente y sin enviar por la pasarela) y, si cabe,
    avanza a `FUNDS_HELD` y ejecuta el envio por la pasarela
    (`_send_after_hold`: confirma, acepta o compensa, sin reenviar de
    mas). Solo `flush`.
    """
    try:
        wanted = tx_id if isinstance(tx_id, uuid.UUID) else uuid.UUID(str(tx_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise biometric_service.BiometricStateError(
            "la operacion no espera autorizacion biometrica"
        ) from exc
    tx = tx_repository.get_transaction(session, wanted)
    if tx is None or tx.type != INTERBANK_TX_TYPE or tx.status != "AUTHORIZED":
        raise biometric_service.BiometricStateError(
            "la operacion no espera autorizacion biometrica"
        )
    total = int(tx.amount_minor) + int(tx.fee_minor or 0)
    held = False
    for candidate in tx_repository.list_holds_by_transaction(session, tx.id):
        if (
            candidate.status == HoldStatus.ACTIVE.value
            and int(candidate.amount_minor) == total
            and candidate.currency == tx.currency
        ):
            held = True
            break
    if not held:
        raise biometric_service.BiometricStateError("la operacion no conserva la retencion")
    block = _interbank_meta(tx)
    if not isinstance((block.get("entries") or {}).get("hold_entry"), str):
        raise biometric_service.BiometricStateError("la operacion no conserva la retencion")
    rejected = biometric_service.reject_if_daily_limit_exceeded(
        session, tx, balance_port=balance_port, daily_limit_reader=daily_limit_reader
    )
    if rejected is not None:
        return rejected
    tx = tx_repository.transition_transaction(
        session, tx.id, "FUNDS_HELD", reason="fondos retenidos"
    )
    tx = tx_repository.get_transaction(session, tx.id)
    gw = resolve_gateway(gateway)
    return _send_after_hold(
        session,
        tx,
        amount_minor=int(tx.amount_minor),
        currency=tx.currency,
        source=tx.source_account_id,
        balance_port=balance_port,
        gw=gw,
    )


def transfer_interbank(
    session: Session,
    *,
    initiator_user_id: uuid.UUID | str,
    source_account_id: uuid.UUID | str,
    amount_minor: int,
    currency: str,
    cci: str,
    holder_name: object = None,
    bank_code: object = None,
    fee_minor: int | None = None,
    idempotency_key: str | None = None,
    external_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
    gateway: InterbankGateway | None = None,
    holder_provider: HolderLookupProvider | None = None,
    timeout_seconds: float | None = None,
    max_attempts: int | None = None,
    balance_port=None,
    risk_profile: str | None = None,
    device_id: str | None = None,
    threshold_reader=None,
    daily_limit_reader=None,
) -> Transaction:
    """Caso de uso `INTERBANK_TRANSFER` (E2-T09, HU06, `04#7.3`).

    1. Valida entrada pura + titular/CCI (fachada E2-T08) + cuenta origen
       (fachada `accounts`): todo ANTES de mover dinero.
    2. Crea `INITIATED -> VALIDATED -> AUTHORIZED`; sin fondos ->
       `REJECTED` (negocio, sin movimientos). Gate biometrico (E2-T17,
       HU08): sobre el limite diario del perfil -> `REJECTED`
        (`DAILY_LIMIT_EXCEEDED`, sin hold; el acumulado incluye
        `PENDING_AUTHORIZATION` y el limite se re-chequea al autorizar,
        E2-T36); sobre el umbral de biometria
        (monto de la operacion, E2-T34) ->
       `PENDING_AUTHORIZATION` con hold hasta `authorize_transaction`
       (E2-T19; la continuacion reanuda el envio sin duplicar).
    3. Retiene el total (`2000-A -> 2100-A`, hold `ACTIVE` SIN `expires_at`:
       el timeout lo posee esta saga via `meta`, no el job generico de
       vencimiento) y avanza a `FUNDS_HELD`. La comision se carga recien
       al liquidar (el fallo nunca cobra).
    4. Envia por la pasarela: `CONFIRMED` -> liquida (`SETTLED`);
       `ACCEPTED` -> `POSTED` (pendiente, sigue por polling);
       timeout/caida -> se mantiene `FUNDS_HELD` (reintentable);
       rechazo -> compensa a `FAILED`.
    Solo `flush` (el llamante decide la transaccion); eventos por `outbox`.
    """
    validate_amount_minor(amount_minor)
    validate_currency(currency)
    digits = validate_cci_format(cci)
    code = validate_bank_code(bank_code)
    initiator = _coerce_optional_user(initiator_user_id)
    source = _coerce_uuid(source_account_id, "source_account_id")
    resolved_fee = resolve_interbank_fee(session, fee_minor)
    ttl_seconds = resolve_timeout_seconds(session, timeout_seconds)
    attempts_limit = resolve_max_attempts(session, max_attempts)
    if external_ref is not None and (
        not isinstance(external_ref, str) or not external_ref.strip() or len(external_ref) > 80
    ):
        raise ValueError("external_ref debe ser texto de 1-80 caracteres")
    wanted_ref = external_ref.strip() if isinstance(external_ref, str) else None

    if idempotency_key is not None:
        existing = tx_repository.get_by_idempotency_key(
            session, idempotency_key, initiator_user_id=initiator
        )
        if existing is not None:
            return existing

    parties = validate_interbank_parties(
        session,
        initiator_user_id=initiator_user_id,
        source_account_id=source,
        currency=currency,
        cci=digits,
        holder_name=holder_name,
        bank_code=code,
        holder_provider=holder_provider,
    )
    masked = str((parties["holder"] or {}).get("holder_name_masked") or "")

    if wanted_ref is not None:
        clash = _find_by_external_ref(session, wanted_ref, initiator)
        if clash is not None:
            raise ExternalRefConflictError(f"external_ref ya usada por la operacion {clash.id}")

    port = _resolve_port(balance_port)
    total_minor = amount_minor + resolved_fee
    gw = resolve_gateway(gateway)

    tx = tx_repository.create_transaction(
        session,
        type=INTERBANK_TX_TYPE,
        amount_minor=amount_minor,
        currency=currency,
        status="INITIATED",
        idempotency_key=idempotency_key,
        initiator_user_id=initiator,
        source_account_id=source,
        target_account_id=None,
        external_ref=wanted_ref,
        fee_minor=resolved_fee,
        metadata=metadata,
    )
    tx_repository.transition_transaction(session, tx.id, "VALIDATED", reason="entrada valida")

    # Gate biometrico + limite diario por perfil (E2-T17, HU08 CA-01/CA-03).
    gate = biometric_service.evaluate_gate(
        session,
        user_id=initiator,
        amount_minor=amount_minor,
        fee_minor=resolved_fee,
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
        _set_failure(session, tx, biometric_service.DAILY_LIMIT_EXCEEDED)
        tx = tx_repository.get_transaction(session, tx.id)
        _publish_outbox(
            session,
            event_type=domain_events.TRANSACTION_INITIATED,
            tx=tx,
            payload={
                "transaction_id": str(tx.id),
                "type": INTERBANK_TX_TYPE,
                "source_account_id": str(source),
                "amount_minor": amount_minor,
                "fee_minor": resolved_fee,
                "currency": currency,
                "status": tx.status,
            },
        )
        return tx
    if gate.requires_biometry:
        return _pend_interbank_for_biometry(
            session,
            tx,
            source=source,
            amount_minor=amount_minor,
            currency=currency,
            fee_minor=resolved_fee,
            total_minor=total_minor,
            digits=digits,
            bank_code=code,
            holder_masked=masked,
            attempts_limit=attempts_limit,
            ttl_seconds=ttl_seconds,
            port=port,
            device_id=device_id,
            request_metadata=metadata,
        )
    tx_repository.transition_transaction(session, tx.id, "AUTHORIZED", reason="autorizada")
    _publish_outbox(
        session,
        event_type=domain_events.TRANSACTION_INITIATED,
        tx=tx,
        payload={
            "transaction_id": str(tx.id),
            "type": INTERBANK_TX_TYPE,
            "source_account_id": str(source),
            "amount_minor": amount_minor,
            "fee_minor": resolved_fee,
            "currency": currency,
            "status": tx.status,
        },
    )

    ordered = sorted({source}, key=str)
    locked = {aid: port.lock_and_get(session, aid) for aid in ordered}
    src_balance = locked[source]
    if src_balance.currency != currency or src_balance.available_minor < total_minor:
        tx_repository.transition_transaction(
            session, tx.id, "REJECTED", reason="fondos insuficientes"
        )
        return tx_repository.get_transaction(session, tx.id)

    src_avail, _src_hold = ledger_service.ensure_customer_accounts(session, source, currency)
    _hold = tx_repository.create_hold(
        session,
        transaction_id=tx.id,
        account_id=source,
        amount_minor=total_minor,
        currency=currency,
        expires_at=None,  # el timeout lo posee esta saga (meta.expires_at)
    )
    port.apply_delta(session, source, -total_minor, currency)
    tx_repository.transition_transaction(session, tx.id, "FUNDS_HELD", reason="fondos retenidos")
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
    entry_ids = _post_hold_entries(
        session,
        tx,
        src_avail_id=src_avail.id,
        src_hold_id=_src_hold.id,
        total_minor=total_minor,
        currency=currency,
        source_ref=source,
    )

    tx = tx_repository.get_transaction(session, tx.id)
    if tx.external_ref is None:
        tx.external_ref = f"IB-{tx.id.hex[:12].upper()}"
        session.add(tx)
        session.flush()
    block: dict[str, Any] = {
        "cci_hash": hash_cci(digits),
        "bank_code": code,
        "holder_name_masked": masked,
        "gateway_ref": "",
        "attempts": 0,
        "max_attempts": attempts_limit,
        "expires_at": (_utcnow().timestamp() + ttl_seconds),
        "entries": entry_ids,
    }
    block["expires_at"] = datetime.fromtimestamp(block["expires_at"], UTC).isoformat()
    _save_interbank_meta(session, tx, block)

    return _send_after_hold(
        session,
        tx,
        amount_minor=amount_minor,
        currency=currency,
        source=source,
        balance_port=balance_port,
        gw=gw,
    )


def poll_interbank(
    session: Session,
    tx_id: uuid.UUID | str,
    *,
    gateway: InterbankGateway | None = None,
    now: datetime | None = None,
    timeout_seconds: float | None = None,
    max_attempts: int | None = None,
    balance_port=None,
) -> Transaction | None:
    """Avanza una transferencia interbancaria pendiente (un paso, idempotente).

    - `FUNDS_HELD`: reintenta el envio (misma `reference`: idempotente en la
      pasarela); timeout/caida suma intento y mantiene el hold.
    - `POSTED`: consulta estado; `CONFIRMED` liquida; `PENDING`/timeout
      suma intento; `FAILED`/`REJECTED` compensa a `FAILED`;
      `REVERSED` compensa a `REVERSED` (asiento inverso).
    - Expirado (`expires_at`) o intentos agotados -> `FAILED`
      (`TRANSFER_TIMEOUT`) con liberacion + compensacion.
    - Terminal (`SETTLED`/`FAILED`/`REVERSED`/`REJECTED`) -> sin cambios.
    Solo `flush`; retorna la `Transaction` (`None` si no existe).
    """
    try:
        wanted = tx_id if isinstance(tx_id, uuid.UUID) else uuid.UUID(str(tx_id))
    except (ValueError, AttributeError, TypeError):
        return None
    tx = tx_repository.get_transaction(session, wanted)
    if tx is None or tx.type != INTERBANK_TX_TYPE:
        return None
    if tx.status in ("SETTLED", "FAILED", "REVERSED", "REJECTED"):
        return tx
    if tx.status not in ("FUNDS_HELD", "POSTED"):
        return tx
    moment = now or _utcnow()
    gw = resolve_gateway(gateway)
    # Snapshot solo para parametros de la pasarela (I/O fuera del lock).
    snapshot = tx_repository.get_transaction(session, wanted)
    snap_block = _interbank_meta(snapshot) if snapshot is not None else {}
    limit = resolve_max_attempts(session, max_attempts)
    # Variante menor `expires_at` antes de reclamar: se inicializa bajo
    # exclusion con merge (nunca desde el snapshot obsoleto).
    if timeout_seconds is not None and not snap_block.get("expires_at"):
        try:
            ttl = int(timeout_seconds)
        except (ValueError, TypeError):
            ttl = 0
        if ttl > 0:

            def _apply_ttl(block: dict[str, Any]) -> None:
                if not block.get("expires_at"):
                    base = moment
                    block["expires_at"] = datetime.fromtimestamp(
                        base.timestamp() + ttl, UTC
                    ).isoformat()

            try:
                _save_block_merging(session, wanted, _apply_ttl)
            except (ConcurrencyError, OperationalError, KeyError):
                pass
            snapshot = _locked_get(session, wanted)
            snap_block = _interbank_meta(snapshot) if snapshot is not None else {}

    if _expired(snap_block, moment):
        return _compensate(
            session,
            snapshot if snapshot is not None else tx,
            target="FAILED",
            reason="timeout interbancario",
            failure_code=FAILURE_TRANSFER_TIMEOUT,
            balance_port=balance_port,
        )

    reference = (
        snapshot.external_ref if snapshot is not None else None
    ) or f"IB-{wanted.hex[:12].upper()}"
    snap_status = snapshot.status if snapshot is not None else tx.status
    snap_amount = int(snapshot.amount_minor) if snapshot is not None else int(tx.amount_minor)
    snap_currency = snapshot.currency if snapshot is not None else tx.currency
    snap_bank = str(snap_block.get("bank_code") or "")
    snap_cci = str(snap_block.get("cci_hash") or "")
    try:
        if snap_status == "FUNDS_HELD":
            result = gw.send_transfer(
                reference=reference,
                amount_minor=snap_amount,
                currency=snap_currency,
                bank_code=snap_bank,
                cci_hash=snap_cci,
            )
            if result.status == "REJECTED":
                failed = _compensate(
                    session,
                    tx,
                    target="FAILED",
                    reason="contraparte rechazo el envio",
                    failure_code=FAILURE_GATEWAY_REJECTED,
                    balance_port=balance_port,
                )
                return _merge_gateway_ref(session, failed, result.gateway_ref)
            if result.status == "CONFIRMED":
                settled = _settle_confirmed(session, tx)
                return _merge_gateway_ref(session, settled, result.gateway_ref)
            # ACCEPTED bajo exclusion: relee fresco, reclama POSTED (CAS) y
            # solo el ganador postea `send_entry` (SAVEPOINT + reintento).
            with _tx_exclusive(wanted):
                for _ in range(5):
                    try:
                        with session.begin_nested():
                            fresh = _locked_get(session, wanted)
                            if fresh is None:  # pragma: no cover - defensivo
                                break
                            if fresh.status != "FUNDS_HELD":
                                break
                            if not _claim_transition(
                                session, fresh, "POSTED", reason="envio aceptado"
                            ):
                                break
                            posted = _locked_get(session, wanted)
                            if posted is None:  # pragma: no cover - defensivo
                                break
                            pblock = _interbank_meta(posted)
                            dirty = False
                            if result.gateway_ref and not pblock.get("gateway_ref"):
                                pblock["gateway_ref"] = result.gateway_ref
                                dirty = True
                            pentries = dict(pblock.get("entries") or {})
                            if "send_entry" not in pentries:
                                pentries["send_entry"] = _post_send_entry(
                                    session,
                                    posted,
                                    amount_minor=int(posted.amount_minor),
                                    currency=posted.currency,
                                    source_ref=posted.source_account_id,
                                )
                                pblock["entries"] = pentries
                                dirty = True
                            if dirty:
                                _save_interbank_meta(session, posted, pblock)
                        break
                    except ConcurrencyError:
                        continue
            return tx_repository.get_transaction(session, wanted)

        status = gw.get_status(reference=reference)
        if status == "CONFIRMED":
            return _settle_confirmed(session, tx)
        if status in ("FAILED", "REJECTED"):
            return _compensate(
                session,
                tx,
                target="FAILED",
                reason=f"pasarela informo {status}",
                failure_code=(
                    FAILURE_GATEWAY_REJECTED if status == "REJECTED" else FAILURE_TRANSFER_TIMEOUT
                ),
                balance_port=balance_port,
            )
        if status == "REVERSED":
            return _compensate(
                session,
                tx,
                target="REVERSED",
                reason="contraparte reverso la operacion",
                failure_code=None,
                balance_port=balance_port,
            )
        # PENDING: bump con merge sobre fresco (nunca pisa `send_entry`).
        merged = _bump_attempts_merged(session, wanted)
        if int(merged.get("attempts", 0)) >= int(merged.get("max_attempts", limit)):
            return _compensate(
                session,
                tx,
                target="FAILED",
                reason="reintentos agotados",
                failure_code=FAILURE_TRANSFER_TIMEOUT,
                balance_port=balance_port,
            )
        return tx_repository.get_transaction(session, wanted)
    except (GatewayTimeoutError, GatewayUnavailableError):
        # Timeout/caida: bump con merge sobre fresco bajo exclusion; el
        # perdedor conserva `send_entry`/`gateway_ref` del ganador y
        # retorna la fila fresca (POSTED si el otro ya avanzo).
        merged = _bump_attempts_merged(session, wanted)
        if _expired(merged, moment) or int(merged.get("attempts", 0)) >= int(
            merged.get("max_attempts", limit)
        ):
            return _compensate(
                session,
                tx,
                target="FAILED",
                reason="timeout interbancario",
                failure_code=FAILURE_TRANSFER_TIMEOUT,
                balance_port=balance_port,
            )
        logger.warning(
            "interbank poll pending ref_hash=%s attempts=%s",
            hash_reference(reference),
            merged.get("attempts"),
        )
        return tx_repository.get_transaction(session, wanted)


def reverse_interbank(
    session: Session,
    tx_id: uuid.UUID | str,
    *,
    reason: str = "reverso autorizado",
    actor: str = "SYSTEM",
    balance_port=None,
) -> Transaction:
    """Reverso autorizado `POSTED`/`SETTLED`/`CONCILIATED` -> `REVERSED`
    (E5-T06): asiento inverso via `ledger.reverse_entry` + liberacion del
    hold si sigue `ACTIVE`; el original queda intacto. Idempotente.

    Exclusion por tx (`FOR UPDATE` + RLock) con `SAVEPOINT` + reintento:
    relee fresco, reclama con CAS y solo entonces invierte/libera.
    """
    try:
        wanted = tx_id if isinstance(tx_id, uuid.UUID) else uuid.UUID(str(tx_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise KeyError(f"transaccion no encontrada: {tx_id}") from exc
    if actor not in ("SYSTEM", "ANALYST"):
        from app.modules.transactions.domain.state_machine import UnauthorizedActorError

        raise UnauthorizedActorError(f"actor no autorizado para reverso: {actor!r}")
    with _tx_exclusive(wanted):
        for _ in range(5):
            try:
                with session.begin_nested():
                    fresh = _locked_get(session, wanted)
                    if fresh is None or fresh.type != INTERBANK_TX_TYPE:
                        raise KeyError(f"transaccion interbancaria no encontrada: {tx_id}")
                    if fresh.status == "REVERSED":
                        return tx_repository.get_transaction(session, wanted)
                    if fresh.status not in ("POSTED", "SETTLED", "CONCILIATED"):
                        raise InvalidTransitionError(
                            f"solo se reversa POSTED/SETTLED/CONCILIATED, estado actual: {fresh.status!r}"
                        )
                    try:
                        tx_repository.transition_transaction(
                            session,
                            fresh.id,
                            "REVERSED",
                            actor=actor,
                            reason=reason,
                            expected_version=int(fresh.version),
                        )
                    except (InvalidTransitionError, ConcurrencyError, KeyError):
                        refreshed = tx_repository.get_transaction(session, wanted)
                        if refreshed is not None and refreshed.status == "REVERSED":
                            return refreshed
                        raise
                    done = _locked_get(session, wanted)
                    if done is None:  # pragma: no cover - defensivo
                        raise KeyError(f"transaccion interbancaria no encontrada: {tx_id}")
                    _reverse_posted_entries(session, done, _interbank_meta(done))
                    settled = _locked_get(session, wanted)
                    if settled is None:  # pragma: no cover - defensivo
                        raise KeyError(f"transaccion interbancaria no encontrada: {tx_id}")
                    _release_hold_best_effort(session, settled, balance_port=balance_port)
                    final = _locked_get(session, wanted)
                    if final is None:  # pragma: no cover - defensivo
                        raise KeyError(f"transaccion interbancaria no encontrada: {tx_id}")
                    _publish_outbox(
                        session,
                        event_type=domain_events.FUNDS_RELEASED,
                        tx=final,
                        payload={
                            "transaction_id": str(final.id),
                            "external_ref": final.external_ref,
                            "status": "REVERSED",
                        },
                    )
                    return tx_repository.get_transaction(session, wanted)
            except ConcurrencyError:
                _backoff(0)
                continue
            except OperationalError as _op_exc:
                if not _is_lock_contention(_op_exc):
                    raise
                _backoff(1)
                continue
        return tx_repository.get_transaction(session, wanted)


__all__ = [
    "BANK_ACCOUNT_CODE",
    "FAILURE_GATEWAY_REJECTED",
    "FAILURE_INSUFFICIENT_FUNDS",
    "FAILURE_TRANSFER_TIMEOUT",
    "FEE_ACCOUNT_CODE",
    "INTERBANK_FEE_DEFAULT_MINOR",
    "INTERBANK_FEE_PARAM_KEY",
    "INTERBANK_HOLD_ENTRY_TYPE",
    "INTERBANK_MAX_ATTEMPTS_DEFAULT",
    "INTERBANK_MAX_ATTEMPTS_PARAM_KEY",
    "INTERBANK_RETURN_ENTRY_TYPE",
    "INTERBANK_SEND_ENTRY_TYPE",
    "INTERBANK_SETTLE_ENTRY_TYPE",
    "INTERBANK_TIMEOUT_DEFAULT_SECONDS",
    "INTERBANK_TIMEOUT_PARAM_KEY",
    "INTERBANK_TX_TYPE",
    "TRANSIT_ACCOUNT_CODE",
    "ExternalRefConflictError",
    "InterbankError",
    "failure_code_of",
    "poll_interbank",
    "resolve_gateway",
    "resolve_interbank_fee",
    "resolve_max_attempts",
    "resolve_timeout_seconds",
    "resume_after_authorization",
    "reverse_interbank",
    "tracking_of",
    "transfer_interbank",
    "validate_interbank_parties",
]
