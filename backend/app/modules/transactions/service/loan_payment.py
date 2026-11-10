"""Fachada aditiva del motor para el pago de cuota de prestamo (E3-T20, HU12).

Debita la cuenta del titular por el total de la cuota con UN solo
asiento (`docs/04#7.6`)::

    Debe  2000-Cuenta-cliente     total
    Haber 5000-Cartera            principal   (solo si > 0)
    Haber 4000-Ingresos intereses interes     (solo si > 0)
    Haber 4200-Ingresos seguro    seguro      (solo si > 0)

Flujo atomico (misma sesion, solo `flush`, nunca `commit`):

1. Valida entrada pura (montos enteros en centimos, `principal +
   interest + insurance == amount`, moneda ISO-4217, UUIDs) y la
   cuenta origen por la fachada `accounts.service` (D-01: existe,
   `ACTIVE`, moneda coherente, del titular).
2. Resuelve idempotencia por (`idempotency_key`, titular): si ya
   existe la devuelve (mismo cuerpo) o lanza `CONFLICT` (cuerpo
   distinto).
3. Crea la `Transaction` `LOAN_INSTALLMENT` (`INITIATED -> VALIDATED
   -> AUTHORIZED`; `source_account_id` = cuenta del cliente;
   `metadata` con `loan_id`/`schedule_id` y la cuenta enmascarada).
4. Bloquea el saldo (`BalancePort.lock_and_get`) y comprueba fondos:
   sin fondos -> `REJECTED` + `failure_code=INSUFFICIENT_FUNDS`
   (`docs/04#10`), sin tocar saldos ni crear asiento.
5. Con fondos: asegura la subcuenta `2000` origen y el catalogo
   (`5000`/`4000`/`4200`, E5-T23 `docs/18` G60) via la fachada
   `ledger`, registra el asiento por `ledger.service.post_entry`
   (frontera: este modulo nunca escribe `journal_entries`/`postings`
   directamente), aplica el debito por el `BalancePort` y
   transiciona `AUTHORIZED -> POSTED -> SETTLED`.
6. **No** usa holds (debito inmediato, como la ruta atomica propia
   `04#7.1`).

Fallo tecnico: marca `REJECTED`/`FAILED` best-effort segun el estado y
**siempre re-lanza** para que la sesion revierta (rollback total, sin
debito parcial). Sin PII ni secretos en logs (la cuenta solo viaja
enmascarada `****dddd`).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.modules.transactions import repository as tx_repository
from app.modules.transactions.domain.state_machine import (
    validate_amount_minor,
    validate_currency,
)
from app.modules.transactions.models import Transaction

logger = logging.getLogger(__name__)

#: Concepto contable del asiento de pago de cuota (`docs/04#7.6`).
LOAN_INSTALLMENT_ENTRY_TYPE = "LOAN_INSTALLMENT"

#: Tipo de la `Transaction` del pago de cuota.
LOAN_INSTALLMENT_TX_TYPE = "LOAN_INSTALLMENT"

#: Catalogo del asiento `7.6` (E5-T23, `docs/18` G60).
PORTFOLIO_ACCOUNT_CODE = "5000"
INTEREST_ACCOUNT_CODE = "4000"
INSURANCE_ACCOUNT_CODE = "4200"

#: Codigo de rechazo por fondos insuficientes (`docs/04#10`).
INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"

#: Prefijo de enmascarado (regla de oro 9: solo ultimos 4 visibles).
MASK_PREFIX = "****"


class LoanPaymentError(ValueError):
    """Error base del pago de cuota del motor (mapeable a 4xx)."""

    code = "LOAN_PAYMENT_ERROR"


class LoanPaymentNotFoundError(LoanPaymentError, LookupError):
    """Cuenta origen inexistente o ajena (neutro, `NOT_FOUND`)."""

    code = "NOT_FOUND"


class LoanPaymentValidationError(LoanPaymentError):
    """Cuenta no `ACTIVE`, moneda incoherente o entrada invalida."""

    code = "VALIDATION_ERROR"


class LoanPaymentConflictError(LoanPaymentError):
    """Misma clave con cuerpo distinto (`CONFLICT`)."""

    code = "CONFLICT"


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise LoanPaymentValidationError(f"{field} debe ser UUID") from exc


def _coerce_component(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LoanPaymentValidationError(f"{name} debe ser entero de centimos")
    if value < 0:
        raise LoanPaymentValidationError(f"{name} debe ser >= 0")
    return value


def mask_account_number(account_number: str) -> str:
    """Enmascara la cuenta (`****dddd`); dominio puro, sin PII en logs."""
    if not isinstance(account_number, str) or not account_number:
        raise LoanPaymentValidationError("account_number debe ser texto no vacio")
    return f"{MASK_PREFIX}{account_number[-4:]}"


def _validate_source(
    session: Session,
    *,
    initiator: uuid.UUID,
    source: uuid.UUID,
    currency: str,
) -> dict:
    """Cuenta origen por la fachada `accounts` (D-01, solo lectura).

    Inexistente o de otro titular -> `NOT_FOUND` neutro (mismo cuerpo,
    anti-enumeracion); no `ACTIVE` o moneda incoherente ->
    `VALIDATION_ERROR`. El numero en claro nunca se loguea.
    """
    from app.modules.accounts.service import get_account as _get_account_info

    try:
        info = _get_account_info(session, source)
    except (ValueError, AttributeError, TypeError) as exc:
        raise LoanPaymentNotFoundError("cuenta origen inexistente") from exc
    if info is None or info["user_id"] != initiator:
        raise LoanPaymentNotFoundError("cuenta origen inexistente")
    if info["status"] != "ACTIVE":
        raise LoanPaymentValidationError(f"cuenta origen no disponible ({info['status']})")
    if info["currency"] != currency:
        raise LoanPaymentValidationError(
            "moneda incoherente sin FX: " f"operacion {currency}, origen {info['currency']}"
        )
    return info


def _check_replay_match(
    tx: Transaction,
    *,
    source: uuid.UUID,
    amount: int,
    principal: int,
    interest: int,
    insurance: int,
    loan_ref: uuid.UUID,
    schedule_ref: uuid.UUID | None,
) -> None:
    """Misma clave + cuerpo distinto -> `CONFLICT` (sin oraculo de datos)."""
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    if (
        tx.type != LOAN_INSTALLMENT_TX_TYPE
        or int(tx.amount_minor) != int(amount)
        or tx.source_account_id != source
        or str(meta.get("loan_id") or "") != str(loan_ref)
        or str(meta.get("schedule_id") or "") != str(schedule_ref or "")
        or int(meta.get("principal_minor") or 0) != int(principal)
        or int(meta.get("interest_minor") or 0) != int(interest)
        or int(meta.get("insurance_minor") or 0) != int(insurance)
    ):
        raise LoanPaymentConflictError("Idempotency-Key ya usada con un cuerpo distinto")


def _mark_insufficient(tx: Transaction) -> None:
    """Fija `failure_reason` + `meta.failure_code` (`flush`, sin `commit`)."""
    meta = dict(tx.meta) if isinstance(tx.meta, dict) else {}
    meta["failure_code"] = INSUFFICIENT_FUNDS
    tx.meta = meta
    tx.failure_reason = INSUFFICIENT_FUNDS


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


def pay_loan_installment(
    session: Session,
    *,
    source_account_id: uuid.UUID | str,
    amount_minor: int,
    currency: str,
    principal_minor: int,
    interest_minor: int,
    insurance_minor: int,
    loan_ref: uuid.UUID | str,
    initiator_user_id: uuid.UUID | str,
    idempotency_key: str | None,
    schedule_ref: uuid.UUID | str | None = None,
    description: str | None = None,
    balance_port: Any | None = None,
) -> Transaction:
    """Cobra la cuota debitando la cuenta del titular (E3-T20).

    Crea la `Transaction LOAN_INSTALLMENT`, registra el asiento
    `Debe 2000 / Haber 5000 + Haber 4000 + Haber 4200` (las patas con
    monto > 0) por la fachada de `ledger`, debita por el
    `BalancePort` y deja la operacion en `SETTLED`. Sin fondos ->
    `REJECTED` + `INSUFFICIENT_FUNDS` sin tocar saldos ni crear
    asiento (`docs/04#10`). Idempotente por (`idempotency_key`,
    titular). Solo `flush` (el llamante confirma o revierte toda la
    operacion de negocio).
    """
    validate_amount_minor(amount_minor)
    validate_currency(currency)
    principal = _coerce_component("principal_minor", principal_minor)
    interest = _coerce_component("interest_minor", interest_minor)
    insurance = _coerce_component("insurance_minor", insurance_minor)
    if principal + interest + insurance != int(amount_minor):
        raise LoanPaymentValidationError(
            "el desglose no cuadra: principal+interest+insurance != amount_minor"
        )
    source = _coerce_uuid(source_account_id, "source_account_id")
    loan_id = _coerce_uuid(loan_ref, "loan_ref")
    schedule_id: uuid.UUID | None = None
    if schedule_ref is not None:
        schedule_id = _coerce_uuid(schedule_ref, "schedule_ref")
    initiator = _coerce_uuid(initiator_user_id, "initiator_user_id")
    key = str(idempotency_key).strip() if isinstance(idempotency_key, str) else ""
    if not key:
        raise LoanPaymentValidationError("cabecera Idempotency-Key obligatoria")

    info = _validate_source(session, initiator=initiator, source=source, currency=currency)
    masked = mask_account_number(str(info["account_number"]))

    existing = tx_repository.get_by_idempotency_key(session, key, initiator_user_id=initiator)
    if existing is not None:
        _check_replay_match(
            existing,
            source=source,
            amount=amount_minor,
            principal=principal,
            interest=interest,
            insurance=insurance,
            loan_ref=loan_id,
            schedule_ref=schedule_id,
        )
        logger.info("loan installment replay status=%s", existing.status)
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
        interest_account = get_by_code(session, INTEREST_ACCOUNT_CODE)
        insurance_account = get_by_code(session, INSURANCE_ACCOUNT_CODE)
        if (
            portfolio is None or interest_account is None or insurance_account is None
        ):  # pragma: no cover - defensivo
            raise RuntimeError("catalogo de cartera/intereses/seguro inexistente")

        tx = tx_repository.create_transaction(
            session,
            type=LOAN_INSTALLMENT_TX_TYPE,
            amount_minor=amount_minor,
            currency=currency,
            status="INITIATED",
            idempotency_key=key,
            initiator_user_id=initiator,
            source_account_id=source,
            target_account_id=None,
            fee_minor=0,
            metadata={
                "loan_id": str(loan_id),
                "schedule_id": str(schedule_id) if schedule_id is not None else "",
                "principal_minor": principal,
                "interest_minor": interest,
                "insurance_minor": insurance,
                "account_masked": masked,
            },
        )
        tx_repository.transition_transaction(session, tx.id, "VALIDATED", reason="entrada valida")
        tx_repository.transition_transaction(session, tx.id, "AUTHORIZED", reason="autorizada")

        locked = port.lock_and_get(session, source)
        if locked.currency != currency or int(locked.available_minor) < int(amount_minor):
            _mark_insufficient(tx)
            session.flush()
            tx = tx_repository.transition_transaction(
                session, tx.id, "REJECTED", reason="fondos insuficientes"
            )
            logger.info("loan installment rejected failure_code=%s", INSUFFICIENT_FUNDS)
            return tx

        src_avail, _src_hold = ledger_service.ensure_customer_accounts(session, source, currency)
        postings: list[dict[str, Any]] = [
            {
                "ledger_account_id": src_avail.id,
                "direction": "DEBIT",
                "amount_minor": amount_minor,
                "currency": currency,
                "account_ref": source,
            }
        ]
        if principal > 0:
            postings.append(
                {
                    "ledger_account_id": portfolio.id,
                    "direction": "CREDIT",
                    "amount_minor": principal,
                    "currency": currency,
                    "account_ref": None,
                }
            )
        if interest > 0:
            postings.append(
                {
                    "ledger_account_id": interest_account.id,
                    "direction": "CREDIT",
                    "amount_minor": interest,
                    "currency": currency,
                    "account_ref": None,
                }
            )
        if insurance > 0:
            postings.append(
                {
                    "ledger_account_id": insurance_account.id,
                    "direction": "CREDIT",
                    "amount_minor": insurance,
                    "currency": currency,
                    "account_ref": None,
                }
            )
        entry = ledger_service.post_entry(
            session,
            entry_type=LOAN_INSTALLMENT_ENTRY_TYPE,
            postings=postings,
            transaction_id=tx.id,
            description=description or f"loan installment {schedule_id or loan_id}",
        )
        port.apply_delta(session, source, -int(amount_minor), currency)
        tx = tx_repository.transition_transaction(
            session, tx.id, "POSTED", reason="asiento de cuota registrado"
        )
        tx = tx_repository.transition_transaction(
            session, tx.id, "SETTLED", reason="cuota liquidada"
        )
        logger.info(
            "loan installment settled entry_type=%s status=%s",
            entry.entry_type,
            tx.status,
        )
        return tx
    except Exception:  # noqa: BLE001 - cleanup best-effort y re-lanza (rollback total)
        try:
            _mark_technical_failure(session, tx)
        finally:
            raise


__all__ = [
    "INSUFFICIENT_FUNDS",
    "INSURANCE_ACCOUNT_CODE",
    "INTEREST_ACCOUNT_CODE",
    "LOAN_INSTALLMENT_ENTRY_TYPE",
    "LOAN_INSTALLMENT_TX_TYPE",
    "MASK_PREFIX",
    "PORTFOLIO_ACCOUNT_CODE",
    "LoanPaymentConflictError",
    "LoanPaymentError",
    "LoanPaymentNotFoundError",
    "LoanPaymentValidationError",
    "mask_account_number",
    "pay_loan_installment",
]
