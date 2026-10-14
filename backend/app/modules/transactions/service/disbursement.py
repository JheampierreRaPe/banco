"""Fachada aditiva del motor para el desembolso de prestamos (E3-T17, HU12).

Acredita el monto aprobado en la cuenta destino del titular con UN solo
asiento (`docs/04#7.5`)::

    Debe  5000-Cartera de prestamos  M   (activo: nace la deuda)
    Haber 2000-Cuenta-cliente        M   (sube el disponible)

Flujo atomico (misma sesion, solo `flush`, nunca `commit`):

1. Valida entrada pura (monto entero > 0 en centimos, moneda ISO-4217,
   UUIDs) y la cuenta destino por la fachada `accounts.service`
   (D-01: existe, `ACTIVE`, moneda coherente, del titular).
2. Resuelve idempotencia por (`idempotency_key`, titular): si ya existe
   la devuelve (mismo cuerpo) o lanza `CONFLICT` (cuerpo distinto).
3. Crea la `Transaction` `LOAN_DISBURSEMENT` (`INITIATED -> VALIDATED ->
   AUTHORIZED`; `source_account_id` = cuenta de cartera `5000`, REF de
   sistema; `target_account_id` = cuenta del cliente; `metadata` con el
   `loan_id` y la cuenta enmascarada).
4. Asegura la subcuenta destino (`2000-Cuenta`) via la fachada `ledger`,
   registra el asiento por `ledger.service.post_entry` (frontera: este
   modulo nunca escribe `journal_entries`/`postings` directamente),
   aplica el delta por el `BalancePort` y transiciona
   `AUTHORIZED -> POSTED -> SETTLED`.
5. **No** crea holds (acredita, no debita al cliente).

Fallo tecnico: marca `REJECTED`/`FAILED` best-effort segun el estado y
**siempre re-lanza** para que la sesion revierta (rollback total, sin
acreditacion parcial). Sin PII ni secretos en logs (la cuenta solo
viaja enmascarada `****dddd`).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.transactions import repository as tx_repository
from app.modules.transactions.domain.state_machine import (
    validate_amount_minor,
    validate_currency,
)
from app.modules.transactions.models import Transaction

logger = logging.getLogger(__name__)

#: Concepto contable del asiento de desembolso (`docs/04#7.5`).
LOAN_DISBURSEMENT_ENTRY_TYPE = "LOAN_DISBURSEMENT"

#: Tipo de la `Transaction` del desembolso.
LOAN_DISBURSEMENT_TX_TYPE = "LOAN_DISBURSEMENT"

#: Cuenta de cartera del activo (catalogo corregido E5-T23, `docs/18` G60).
PORTFOLIO_ACCOUNT_CODE = "5000"

#: Prefijo de enmascarado (regla de oro 9: solo ultimos 4 visibles).
MASK_PREFIX = "****"


class DisbursementError(ValueError):
    """Error base del desembolso del motor (mapeable a 4xx)."""

    code = "DISBURSEMENT_ERROR"


class DisbursementNotFoundError(DisbursementError, LookupError):
    """Cuenta destino inexistente o ajena (neutro, `NOT_FOUND`)."""

    code = "NOT_FOUND"


class DisbursementValidationError(DisbursementError):
    """Cuenta no `ACTIVE`, moneda incoherente o entrada invalida."""

    code = "VALIDATION_ERROR"


class DisbursementConflictError(DisbursementError):
    """Misma clave con cuerpo distinto (`CONFLICT`)."""

    code = "CONFLICT"


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise DisbursementValidationError(f"{field} debe ser UUID") from exc


def _coerce_optional_user(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise DisbursementValidationError("initiator_user_id debe ser UUID") from exc


def mask_account_number(account_number: str) -> str:
    """Enmascara la cuenta (`****dddd`); dominio puro, sin PII en logs."""
    if not isinstance(account_number, str) or not account_number:
        raise DisbursementValidationError("account_number debe ser texto no vacio")
    return f"{MASK_PREFIX}{account_number[-4:]}"


def _validate_target(
    session: Session,
    *,
    initiator: uuid.UUID,
    target: uuid.UUID,
    currency: str,
) -> dict:
    """Cuenta destino por la fachada `accounts` (D-01, solo lectura).

    Inexistente o de otro titular -> `NOT_FOUND` neutro (mismo cuerpo,
    anti-enumeracion); no `ACTIVE` o moneda incoherente ->
    `VALIDATION_ERROR`. El numero en claro nunca se loguea.
    """
    from app.modules.accounts.service import get_account as _get_account_info

    try:
        info = _get_account_info(session, target)
    except (ValueError, AttributeError, TypeError) as exc:
        raise DisbursementNotFoundError("cuenta destino inexistente") from exc
    if info is None or info["user_id"] != initiator:
        raise DisbursementNotFoundError("cuenta destino inexistente")
    if info["status"] != "ACTIVE":
        raise DisbursementValidationError(f"cuenta destino no disponible ({info['status']})")
    if info["currency"] != currency:
        raise DisbursementValidationError(
            "moneda incoherente sin FX: " f"operacion {currency}, destino {info['currency']}"
        )
    return info


def _check_replay_match(
    tx: Transaction, *, target: uuid.UUID, amount: int, loan_ref: uuid.UUID
) -> None:
    """Misma clave + cuerpo distinto -> `CONFLICT` (sin oraculo de datos)."""
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    if (
        tx.type != LOAN_DISBURSEMENT_TX_TYPE
        or int(tx.amount_minor) != int(amount)
        or tx.target_account_id != target
        or str(meta.get("loan_id") or "") != str(loan_ref)
    ):
        raise DisbursementConflictError("Idempotency-Key ya usada con un cuerpo distinto")


def _mark_technical_failure(session: Session, tx: Transaction | None) -> None:
    """Limpieza best-effort ante fallo tecnico (nunca lanza)."""
    try:
        if tx is None or tx.id is None:
            return
        fresh = tx_repository.get_transaction(session, tx.id)
        if fresh is None:
            return
        try:
            if fresh.status == "AUTHORIZED":
                tx_repository.transition_transaction(
                    session, fresh.id, "REJECTED", reason="fallo tecnico"
                )
            elif fresh.status == "POSTED":
                tx_repository.transition_transaction(
                    session, fresh.id, "FAILED", reason="fallo tecnico"
                )
        except Exception:  # noqa: BLE001, S110 - best-effort documentado
            pass
    except Exception:  # noqa: BLE001, S110 - best-effort documentado
        pass


def disburse_loan(
    session: Session,
    *,
    target_account_id: uuid.UUID | str,
    amount_minor: int,
    currency: str,
    loan_ref: uuid.UUID | str,
    initiator_user_id: uuid.UUID | str,
    idempotency_key: str | None,
    description: str | None = None,
    balance_port: Any | None = None,
) -> Transaction:
    """Desembolsa el prestamo acreditando la cuenta destino (E3-T17).

    Crea la `Transaction LOAN_DISBURSEMENT`, registra el asiento
    `Debe 5000 / Haber 2000` por la fachada de `ledger`, aplica el
    delta por el `BalancePort` y deja la operacion en `SETTLED`.
    Idempotente por (`idempotency_key`, titular). Solo `flush` (el
    llamante confirma o revierte toda la operacion de negocio).
    """
    validate_amount_minor(amount_minor)
    validate_currency(currency)
    target = _coerce_uuid(target_account_id, "target_account_id")
    loan_id = _coerce_uuid(loan_ref, "loan_ref")
    initiator = _coerce_uuid(initiator_user_id, "initiator_user_id")
    key = str(idempotency_key).strip() if isinstance(idempotency_key, str) else ""
    if not key:
        raise DisbursementValidationError("cabecera Idempotency-Key obligatoria")

    info = _validate_target(session, initiator=initiator, target=target, currency=currency)
    masked = mask_account_number(str(info["account_number"]))

    existing = tx_repository.get_by_idempotency_key(session, key, initiator_user_id=initiator)
    if existing is not None:
        _check_replay_match(existing, target=target, amount=amount_minor, loan_ref=loan_id)
        logger.info("loan disbursement replay status=%s", existing.status)
        return existing

    if balance_port is not None:
        port = balance_port
    else:  # Cableado productivo (opcion b, sin ciclos a nivel top).
        try:
            from app.composition import resolve_balance_port as _resolve_bp
        except ImportError:  # pragma: no cover - red de seguridad
            from app.modules.transactions.service import DEFAULT_BALANCE_PORT as _fallback

            port = _fallback
        else:
            from app.modules.transactions.service import DEFAULT_BALANCE_PORT as _fallback

            port = _resolve_bp(None, fallback=_fallback)

    tx: Transaction | None = None
    try:
        from app.modules.ledger import service as ledger_service
        from app.modules.ledger.repository import ensure_system_catalog, get_by_code

        ensure_system_catalog(session)
        portfolio = get_by_code(session, PORTFOLIO_ACCOUNT_CODE)
        if portfolio is None:  # pragma: no cover - defensivo
            raise RuntimeError(f"cuenta de cartera {PORTFOLIO_ACCOUNT_CODE} inexistente")

        tx = tx_repository.create_transaction(
            session,
            type=LOAN_DISBURSEMENT_TX_TYPE,
            amount_minor=amount_minor,
            currency=currency,
            status="INITIATED",
            idempotency_key=key,
            initiator_user_id=initiator,
            source_account_id=portfolio.id,
            target_account_id=target,
            fee_minor=0,
            metadata={"loan_id": str(loan_id), "account_masked": masked},
        )
        tx_repository.transition_transaction(session, tx.id, "VALIDATED", reason="entrada valida")
        tx_repository.transition_transaction(session, tx.id, "AUTHORIZED", reason="autorizada")

        dst_avail, _dst_hold = ledger_service.ensure_customer_accounts(session, target, currency)
        entry = ledger_service.post_entry(
            session,
            entry_type=LOAN_DISBURSEMENT_ENTRY_TYPE,
            postings=[
                {
                    "ledger_account_id": portfolio.id,
                    "direction": "DEBIT",
                    "amount_minor": amount_minor,
                    "currency": currency,
                    "account_ref": None,
                },
                {
                    "ledger_account_id": dst_avail.id,
                    "direction": "CREDIT",
                    "amount_minor": amount_minor,
                    "currency": currency,
                    "account_ref": target,
                },
            ],
            transaction_id=tx.id,
            description=description or f"loan disbursement {loan_id}",
        )
        port.apply_delta(session, target, amount_minor, currency)
        tx = tx_repository.transition_transaction(
            session, tx.id, "POSTED", reason="asiento de desembolso registrado"
        )
        tx = tx_repository.transition_transaction(
            session, tx.id, "SETTLED", reason="desembolso liquidado"
        )
        logger.info(
            "loan disbursement settled entry_type=%s status=%s",
            entry.entry_type,
            tx.status,
        )
        return tx
    except Exception:  # noqa: BLE001 - cleanup best-effort y re-lanza (rollback total)
        try:
            _mark_technical_failure(session, tx)
        finally:
            raise


def find_disbursement_tx(
    session: Session, *, initiator_user_id: uuid.UUID | str, loan_id: uuid.UUID | str
) -> Transaction | None:
    """Transaccion de desembolso de un prestamo (`None` si no existe).

    Solo lectura (idempotencia de negocio de `credits`: repetir sobre un
    expediente ya desembolsado devuelve lo existente sin mover dinero).
    """
    initiator = _coerce_optional_user(initiator_user_id)
    clean_loan = _coerce_uuid(loan_id, "loan_id")
    stmt = (
        sa.select(Transaction)
        .where(
            Transaction.type == LOAN_DISBURSEMENT_TX_TYPE,
            Transaction.initiator_user_id == initiator,
        )
        .order_by(Transaction.created_at, Transaction.id)
    )
    for row in session.scalars(stmt).all():
        meta = row.meta if isinstance(row.meta, dict) else {}
        if str(meta.get("loan_id") or "") == str(clean_loan):
            return row
    return None


__all__ = [
    "LOAN_DISBURSEMENT_ENTRY_TYPE",
    "LOAN_DISBURSEMENT_TX_TYPE",
    "MASK_PREFIX",
    "PORTFOLIO_ACCOUNT_CODE",
    "DisbursementConflictError",
    "DisbursementError",
    "DisbursementNotFoundError",
    "DisbursementValidationError",
    "disburse_loan",
    "find_disbursement_tx",
    "mask_account_number",
]
