"""Reverso/compensacion idempotente de transferencias (E2-T25, HU06/HU17/HU18).

Expone `reverse_transaction`: compensa una operacion en estado reversible con
**asientos inversos** creados exclusivamente via la fachada
`ledger.service.reverse_entry` (nunca edita ni borra el original: los
`postings` quedan intactos; el original solo cambia su marca
`POSTED -> REVERSED`, mecanismo append-only del propio `ledger`, E5-T10).

Reglas (supuesto G17 del brief, `docs/18` §9 pendiente de registro):

- Reversibles: `POSTED`, `SETTLED`, `CONCILIATED` -> `REVERSED` (terminal).
- No reversibles: `INITIATED`/`VALIDATED`/`PENDING_AUTHORIZATION`/
  `AUTHORIZED`/`FUNDS_HELD` (usar cancelacion/liberacion de hold, no reverso)
  y los terminales `REJECTED`/`FAILED` -> `ReversalStateError` (el router lo
  traduce a `409 CONFLICT`, sin crear asientos).
- Una transaccion ya en `REVERSED` no se re-reversa: se devuelve el resultado
  almacenado en `meta["reversal"]` (idempotente, sin asientos nuevos).
- `INTERBANK_TRANSFER` delega en `interbank.reverse_interbank` (saga E2-T09,
  con su propio manejo de holds/transito); el resto usa la ruta generica.
- Solo el titular (`initiator_user_id`); el router valida antes de reclamar
  la idempotencia (un 403/404/409 de validacion no consume la clave).

Frontera (reglas de oro 1/3/4/8):

- Dinero entero en centimos; nunca `float`.
- Ledger solo por su fachada (`get_entry`, `reverse_entry`); jamas se
  importan modelos/repositorios de `ledger` ni de `accounts`.
- Los ids de los asientos originales se leen de `transactions.meta`
  (`ledger_entries`, registrados al crear por el propio motor en este mismo
  modulo): sin esta traza el reverso se rechaza (`409`, sin adivinar).
- Saldos solo por el puerto `BalancePort` (mismo cableado que E5-T03/E2-T07).
- Evento `transfer.reversed` solo por `outbox` (nombre fijado aqui;
  `docs/18` §9 pendiente). El `ledger.entry.posted` de cada asiento inverso
  lo emite `ledger` (E2-T23).
- Sin PII ni secretos en logs (solo UUIDs de transaccion/estado).

Concurrencia: exclusion por transaccion (`RLock` en proceso + `SELECT ...
FOR UPDATE` best-effort en BD) con reclamo CAS (`expected_version`, patron
de E2-T09): el perdedor relee fresco y retorna el resultado idempotente en
lugar de duplicar la compensacion. Todo hace `flush`, nunca `commit`.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.modules.ledger import service as ledger_service
from app.modules.transactions import repository as tx_repository
from app.modules.transactions.domain.state_machine import (
    ConcurrencyError,
    InvalidTransitionError,
    UnauthorizedActorError,
)
from app.modules.transactions.models import HoldStatus, Transaction

logger = logging.getLogger(__name__)

#: Estados que admiten reverso (supuesto G17 del brief E2-T25).
REVERSIBLE_STATUSES = frozenset({"POSTED", "SETTLED", "CONCILIATED"})

#: Evento de dominio del reverso (via `outbox`; `docs/18` §9 pendiente).
TRANSFER_REVERSED_EVENT = "transfer.reversed"

#: Traza de asientos originales en `transactions.meta` (la escribe el motor
#: al crear: `[{"id": <uuid>, "kind": "hold"|"settle"|"own"}]` en orden de
#: creacion). Sin ella no hay reverso (no se adivinan asientos).
LEDGER_ENTRIES_META_KEY = "ledger_entries"

#: Resultado del reverso en `transactions.meta` (idempotencia de servicio).
REVERSAL_META_KEY = "reversal"

#: Motivo por defecto (trazabilidad en `transaction_status_history`).
DEFAULT_REASON = "reverso autorizado"

#: Actores autorizados a reversar (los que admite la maquina `-> REVERSED`).
REVERSAL_ACTORS = frozenset({"SYSTEM", "ANALYST"})


class ReversalError(ValueError):
    """Error base del reverso (E2-T25)."""


class TransactionNotFoundError(ReversalError):
    """Transaccion inexistente (-> 404 NOT_FOUND, neutro)."""


class NotAuthorizedError(ReversalError):
    """Transaccion de otro titular (-> 403 NOT_AUTHORIZED, generico)."""


class ReversalStateError(ReversalError):
    """Estado no reversible o sin asientos para reverso (-> 409 CONFLICT)."""


class ReversalIntegrityError(ReversalError):
    """Asiento original inconsistente (-> 500, sin reintentar a ciegas)."""


#: Exclusion por transaccion (mismo patron que la saga E2-T09: `RLock` en
#: proceso porque SQLite ignora `FOR UPDATE`; `RLock` por reentrancia con
#: la delegacion interbancaria).
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
    """Exclusion mutua por transaccion (en proceso)."""
    with _per_tx_lock(tx_id):
        yield


def _is_lock_contention(exc: BaseException) -> bool:
    """`True` si es contienda de escritores SQLite (reintentable)."""
    text = str(exc).lower()
    return "database is locked" in text or "database table is locked" in text


def _backoff(attempt: int) -> None:
    time.sleep(min(0.05 * (attempt + 1), 0.3))


def _locked_get(session: Session, tx_id: uuid.UUID) -> Transaction | None:
    """Relee la fila bajo lock (`SELECT ... FOR UPDATE`, best-effort).

    `populate_existing` fuerza a recargar aunque la sesion conserve el
    snapshot obsoleto en su identity map. En SQLite `FOR UPDATE` es no-op
    (el RLock en proceso + CAS cubre); en Postgres bloquea. Nunca lanza.
    """
    try:
        stmt = (
            sa.select(Transaction)
            .where(Transaction.id == tx_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        row = session.scalars(stmt).first()
        if row is not None:
            return row
    except Exception:  # noqa: BLE001 - fallback al get simple
        logger.debug("reverse locked_get sin FOR UPDATE, fallback al get")
    try:
        current = session.get(Transaction, tx_id)
        if current is not None:
            try:
                session.expire(current)
                return session.get(Transaction, tx_id)
            except Exception:  # noqa: BLE001 - best-effort
                return current
        return None
    except Exception:  # noqa: BLE001 - defensivo
        return tx_repository.get_transaction(session, tx_id)


def _coerce_tx_id(value: uuid.UUID | str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise TransactionNotFoundError("transaccion inexistente") from exc


def normalize_reason(reason: str | None) -> str | None:
    """Normaliza el motivo opcional (`None` si vacio; max 280 lo valida el esquema)."""
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise TypeError(f"reason debe ser texto, recibido: {type(reason).__name__}")
    clean = reason.strip()
    return clean or None


def record_ledger_entry(
    session: Session, tx: Transaction, entry_id: uuid.UUID | str, kind: str
) -> None:
    """Registra un asiento original en `meta["ledger_entries"]` (al crear).

    La llama el motor justo despues de `ledger.post_entry`, ANTES de
    transicionar (el CAS de `transition_transaction` expira el objeto:
    llamar siempre antes). `flush` sin `commit`; preserva el resto de
    `meta`. Sin PII (solo ids tecnicos).
    """
    meta = dict(tx.meta) if isinstance(tx.meta, dict) else {}
    entries = list(meta.get(LEDGER_ENTRIES_META_KEY) or [])
    entries.append({"id": str(entry_id), "kind": str(kind)})
    meta[LEDGER_ENTRIES_META_KEY] = entries
    tx.meta = meta
    session.add(tx)
    session.flush()


def _recorded_entry_ids(tx: Transaction) -> list[str]:
    """Ids de asientos originales en orden de creacion (`[]` si no hay traza)."""
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    raw = meta.get(LEDGER_ENTRIES_META_KEY) or []
    ids: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
    return ids


def _stored_reversal(tx: Transaction) -> dict[str, Any] | None:
    """Resultado guardado en `meta["reversal"]` (`None` si aun no hay)."""
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    block = meta.get(REVERSAL_META_KEY)
    return dict(block) if isinstance(block, dict) else None


def _save_reversal_result(
    session: Session,
    tx: Transaction,
    *,
    reversal_entry_ids: list[str],
    reverses_entry_ids: list[str],
    reason: str | None,
    reversal_map: dict[str, str] | None = None,
) -> None:
    """Guarda el resultado idempotente en `meta["reversal"]` (`flush`)."""
    meta = dict(tx.meta) if isinstance(tx.meta, dict) else {}
    meta[REVERSAL_META_KEY] = {
        "reversal_entry_ids": [str(v) for v in reversal_entry_ids],
        "reverses_entry_ids": [str(v) for v in reverses_entry_ids],
        "reason": reason,
        "reversal_map": {str(k): str(v) for k, v in (reversal_map or {}).items()},
    }
    tx.meta = meta
    session.add(tx)
    session.flush()


def _publish_reversed(
    session: Session,
    tx: Transaction,
    *,
    reversal_entry_ids: list[str],
    reverses_entry_ids: list[str],
) -> bool:
    """Enlista `transfer.reversed` en el outbox (best-effort, sin PII)."""
    try:
        from app.core.outbox import record as outbox_record
    except ImportError:
        return False
    outbox_record(
        session,
        aggregate_type="transaction",
        aggregate_id=tx.id,
        event_type=TRANSFER_REVERSED_EVENT,
        payload={
            "transaction_id": str(tx.id),
            "status": tx.status,
            "reversal_entry_ids": [str(v) for v in reversal_entry_ids],
            "reverses_entry_ids": [str(v) for v in reverses_entry_ids],
        },
    )
    return True


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


def _reverse_interbank_result(
    session: Session,
    tx: Transaction,
    *,
    reason: str | None,
    actor: str,
    balance_port=None,
) -> dict[str, Any]:
    """Delega el reverso interbancario en la saga (E2-T09, ya idempotente)."""
    from app.modules.transactions.service.interbank import reverse_interbank

    settled = reverse_interbank(
        session,
        tx.id,
        reason=reason or DEFAULT_REASON,
        actor=actor,
        balance_port=balance_port,
    )
    fresh = tx_repository.get_transaction(session, settled.id)
    if fresh is None:  # pragma: no cover - defensivo
        raise TransactionNotFoundError("transaccion inexistente")
    block = fresh.meta.get("interbank") if isinstance(fresh.meta, dict) else {}
    entries = dict(block.get("entries") or {}) if isinstance(block, dict) else {}
    reverses = [str(entries[k]) for k in ("send_entry", "settle_entry") if entries.get(k)]
    stored = _stored_reversal(fresh) or {}
    reversals = [str(v) for v in stored.get("reversal_entry_ids", [])]
    if entries.get("return_entry") and str(entries["return_entry"]) not in reversals:
        reversals = [*reversals, str(entries["return_entry"])]
    return {
        "transaction": fresh,
        "reversal_entry_ids": reversals,
        "reverses_entry_ids": reverses,
    }


def reverse_transaction(
    session: Session,
    tx_id: uuid.UUID | str,
    *,
    reason: str | None = None,
    actor: str = "SYSTEM",
    balance_port=None,
    initiator_user_id: uuid.UUID | str | None = None,
) -> dict[str, Any]:
    """Revierte una transferencia con asientos inversos (E2-T25).

    Retorna `{"transaction", "reversal_entry_ids", "reverses_entry_ids"}`
    con la transaccion en `REVERSED`. Idempotente: si ya esta `REVERSED`
    devuelve el resultado guardado sin crear asientos; ante carrera, el
    perdedor del CAS retorna el resultado del ganador. Solo `flush`.
    """
    wanted = _coerce_tx_id(tx_id)
    clean_reason = normalize_reason(reason)
    if actor not in REVERSAL_ACTORS:
        raise UnauthorizedActorError(f"actor no autorizado para reverso: {actor!r}")
    initiator: uuid.UUID | None = None
    if initiator_user_id is not None:
        if isinstance(initiator_user_id, uuid.UUID):
            initiator = initiator_user_id
        else:
            try:
                initiator = uuid.UUID(str(initiator_user_id))
            except (ValueError, AttributeError, TypeError) as exc:
                raise NotAuthorizedError("la transaccion no pertenece al titular") from exc

    probe = tx_repository.get_transaction(session, wanted)
    if probe is not None and probe.type == "INTERBANK_TRANSFER":
        if (
            initiator is not None
            and probe.initiator_user_id is not None
            and probe.initiator_user_id != initiator
        ):
            raise NotAuthorizedError("la transaccion no pertenece al titular")
        if probe.status != "REVERSED" and probe.status not in REVERSIBLE_STATUSES:
            raise ReversalStateError(
                f"solo se reversa POSTED/SETTLED/CONCILIATED, " f"estado actual: {probe.status!r}"
            )
        try:
            return _reverse_interbank_result(
                session,
                probe,
                reason=clean_reason,
                actor=actor,
                balance_port=balance_port,
            )
        except InvalidTransitionError as exc:
            raise ReversalStateError(str(exc)) from exc

    with _tx_exclusive(wanted):
        for attempt in range(5):
            try:
                with session.begin_nested():
                    fresh = _locked_get(session, wanted)
                    if fresh is None:
                        raise TransactionNotFoundError("transaccion inexistente")
                    if (
                        initiator is not None
                        and fresh.initiator_user_id is not None
                        and fresh.initiator_user_id != initiator
                    ):
                        raise NotAuthorizedError("la transaccion no pertenece al titular")
                    if fresh.status == "REVERSED":
                        stored = _stored_reversal(fresh) or {}
                        return {
                            "transaction": tx_repository.get_transaction(session, wanted),
                            "reversal_entry_ids": [
                                str(v) for v in stored.get("reversal_entry_ids", [])
                            ],
                            "reverses_entry_ids": [
                                str(v) for v in stored.get("reverses_entry_ids", [])
                            ],
                        }
                    if fresh.status not in REVERSIBLE_STATUSES:
                        raise ReversalStateError(
                            f"solo se reversa POSTED/SETTLED/CONCILIATED, "
                            f"estado actual: {fresh.status!r}"
                        )
                    from_status = fresh.status
                    originals = _recorded_entry_ids(fresh)
                    if not originals:
                        raise ReversalStateError("la operacion no registra asientos para reverso")
                    try:
                        tx_repository.transition_transaction(
                            session,
                            fresh.id,
                            "REVERSED",
                            actor=actor,
                            reason=clean_reason or DEFAULT_REASON,
                            expected_version=int(fresh.version),
                        )
                    except (InvalidTransitionError, ConcurrencyError, KeyError):
                        refreshed = tx_repository.get_transaction(session, wanted)
                        if refreshed is not None and refreshed.status == "REVERSED":
                            stored = _stored_reversal(refreshed) or {}
                            return {
                                "transaction": refreshed,
                                "reversal_entry_ids": [
                                    str(v) for v in stored.get("reversal_entry_ids", [])
                                ],
                                "reverses_entry_ids": [
                                    str(v) for v in stored.get("reverses_entry_ids", [])
                                ],
                            }
                        raise
                    done = _locked_get(session, wanted)
                    if done is None:  # pragma: no cover - defensivo
                        raise TransactionNotFoundError("transaccion inexistente")

                    # Asientos inversos del mas nuevo al mas viejo (solo via
                    # fachada; los `postings` originales nunca se tocan).
                    previous = _stored_reversal(done) or {}
                    known = {
                        str(k): str(v) for k, v in (previous.get("reversal_map") or {}).items()
                    }
                    reversal_map: dict[str, str] = dict(known)
                    for original_id in reversed(originals):
                        if str(original_id) in reversal_map:
                            continue
                        current = ledger_service.get_entry(session, original_id)
                        if current is None:
                            raise ReversalIntegrityError(
                                "asiento original inexistente para reverso"
                            )
                        if current.status == "REVERSED":
                            continue
                        if current.status != "POSTED":
                            raise ReversalIntegrityError(
                                "asiento original no reversible para reverso"
                            )
                        comp = ledger_service.reverse_entry(
                            session,
                            original_id,
                            transaction_id=done.id,
                            description=f"reversal {done.id}",
                        )
                        reversal_map[str(original_id)] = str(comp.id)

                    # Holds: los `ACTIVE` se liberan; los `CAPTURED` quedan
                    # (terminales, nunca se reabren).
                    for hold in tx_repository.list_holds_by_transaction(session, done.id):
                        if hold.status == HoldStatus.ACTIVE.value:
                            tx_repository.update_hold_status(
                                session, hold.id, HoldStatus.RELEASED.value
                            )

                    # Proyeccion de saldos: se devuelve lo debitado al origen
                    # y, si llego a liquidarse, se retira lo acreditado al
                    # destino (enteros en centimos, sin `float`).
                    if done.source_account_id is None:
                        raise ReversalIntegrityError("reverso sin cuenta origen para compensar")
                    port = _resolve_port(balance_port)
                    total_minor = int(done.amount_minor) + int(done.fee_minor or 0)
                    port.apply_delta(session, done.source_account_id, total_minor, done.currency)
                    if (
                        from_status in ("SETTLED", "CONCILIATED")
                        and done.target_account_id is not None
                    ):
                        port.apply_delta(
                            session,
                            done.target_account_id,
                            -int(done.amount_minor),
                            done.currency,
                        )

                    reverses = list(originals)
                    reversals = [reversal_map[o] for o in reverses if o in reversal_map]
                    _save_reversal_result(
                        session,
                        done,
                        reversal_entry_ids=reversals,
                        reverses_entry_ids=reverses,
                        reason=clean_reason,
                        reversal_map=reversal_map,
                    )
                    final = tx_repository.get_transaction(session, wanted)
                    if final is None:  # pragma: no cover - defensivo
                        raise TransactionNotFoundError("transaccion inexistente")
                    _publish_reversed(
                        session,
                        final,
                        reversal_entry_ids=reversals,
                        reverses_entry_ids=reverses,
                    )
                    logger.info(
                        "reverse transaction_id=%s from=%s reversals=%s",
                        final.id,
                        from_status,
                        len(reversals),
                    )
                    return {
                        "transaction": final,
                        "reversal_entry_ids": reversals,
                        "reverses_entry_ids": reverses,
                    }
            except ReversalError:
                raise
            except ConcurrencyError:
                _backoff(attempt)
                continue
            except OperationalError as exc:
                if not _is_lock_contention(exc):
                    raise
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001, S110 - best-effort
                    pass
                _backoff(attempt)
                continue
        refreshed = tx_repository.get_transaction(session, wanted)
        if refreshed is not None and refreshed.status == "REVERSED":
            stored = _stored_reversal(refreshed) or {}
            return {
                "transaction": refreshed,
                "reversal_entry_ids": [str(v) for v in stored.get("reversal_entry_ids", [])],
                "reverses_entry_ids": [str(v) for v in stored.get("reverses_entry_ids", [])],
            }
        raise ConcurrencyError(  # pragma: no cover - solo si agota reintentos
            f"reverso en contienda para {wanted} (posible doble procesamiento)"
        )


__all__ = [
    "DEFAULT_REASON",
    "LEDGER_ENTRIES_META_KEY",
    "REVERSAL_ACTORS",
    "REVERSAL_META_KEY",
    "REVERSIBLE_STATUSES",
    "TRANSFER_REVERSED_EVENT",
    "NotAuthorizedError",
    "ReversalError",
    "ReversalIntegrityError",
    "ReversalStateError",
    "TransactionNotFoundError",
    "normalize_reason",
    "record_ledger_entry",
    "reverse_transaction",
]
