"""Desembolso transaccional del prestamo (E3-T17, HU12 CA-01/CA-02).

Con un expediente `APPROVED` y su juego contractual `SIGNED`, crea el
prestamo (`loans`) y su cronograma definitivo (`loan_schedules`) via la
fachada interna `service/loans.py` (E3-T18), puebla `contracts.loan_id`
(G54, dentro de `create_loan_with_schedule`) y ejecuta el desembolso
atomico a la cuenta elegida por la fachada del motor
`transactions.service.disbursement.disburse_loan` (asiento `docs/04#7.5`)
en la **misma transaccion**: todo o nada (una sola sesion, solo `flush`;
el `commit` es del llamante y ante fallo se revierte todo).

- Monto = `requested_amount_minor` del expediente (no se recalcula);
  moneda = la de la cuenta destino (fachada `accounts`, D-01).
- Cuenta destino: la del body o, si se omite, la principal del titular
  (la `ACTIVE` mas antigua; decision del dueno). Solo el titular; ajena
  o inexistente -> `NOT_FOUND` neutro; no `ACTIVE` -> `VALIDATION_ERROR`.
- Idempotencia doble: de negocio (un prestamo por expediente: repetir
  sobre un expediente ya desembolsado devuelve lo existente sin mover
  dinero) + del motor por `Idempotency-Key` obligatoria (patron
  `contract_signing`: misma clave + mismo cuerpo = replay; misma clave
  + cuerpo distinto = `409`; validaciones de titular/estado no consumen
  la clave).
- Emite `credit.disbursed` por `outbox` (payload sin PII) + auditoria
  best-effort (`credit.disbursed`).

Sin PII ni secretos en logs (la cuenta solo enmascarada `****dddd`);
dinero entero en centimos, sin `float`. `credits` no escribe tablas de
`transactions`/`ledger`/`accounts` (solo fachadas, regla de oro 4).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.credits import events as credit_events
from app.modules.credits.models import Loan
from app.modules.credits.repository import applications as apps_repo
from app.modules.credits.repository import contracts as contracts_repo
from app.modules.credits.repository.applications import (
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationValidationError,
    IdempotencyKeyConflictError,
)
from app.modules.credits.repository.contracts import (
    ContractConflictError,
)
from app.modules.credits.repository.loans import (
    LoanConflictError,
    LoanNotFoundError,
)

logger = logging.getLogger(__name__)

#: Endpoint logico para la idempotencia del `POST .../disburse` (clave `shared`, UQ por usuario).
IDEMPOTENCY_ENDPOINT = "/credits/applications/disburse"

#: Prefijo de la clave del motor (anclada al prestamo, no a la clave HTTP).
MOTOR_KEY_PREFIX = "loan-disbursement"


def _motor_key_for(loan_id: uuid.UUID) -> str:
    """Clave de idempotencia del motor para un prestamo (estable y unica).

    Anclar el dinero al `loan_id` (y no a la `Idempotency-Key` HTTP)
    hace que cualquier reintento —misma clave, otra clave o
    recuperacion tras un fallo— converja en la misma `Transaction`
    sin doble acreditacion.
    """
    return f"{MOTOR_KEY_PREFIX}:{loan_id}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_owner(owner_user_id: uuid.UUID | str) -> uuid.UUID:
    if isinstance(owner_user_id, uuid.UUID):
        return owner_user_id
    try:
        return uuid.UUID(str(owner_user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApplicationValidationError(
            f"user_id debe ser UUID, recibido: {owner_user_id!r}"
        ) from exc


def _coerce_application_id(application_id: object) -> uuid.UUID:
    if isinstance(application_id, uuid.UUID):
        return application_id
    try:
        return uuid.UUID(str(application_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApplicationNotFoundError("expediente inexistente") from exc


def _coerce_account_id(account_id: object) -> uuid.UUID | None:
    if account_id is None:
        return None
    if isinstance(account_id, uuid.UUID):
        return account_id
    try:
        return uuid.UUID(str(account_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApplicationValidationError(
            f"account_id debe ser UUID, recibido: {account_id!r}"
        ) from exc


def _resolve_account_by_id(session: Session, owner: uuid.UUID, account_id: uuid.UUID) -> dict:
    """Cuenta explicita por fachada `accounts` (D-01, solo lectura).

    Inexistente o de otro titular -> `NOT_FOUND` neutro (mismo cuerpo,
    anti-enumeracion); no `ACTIVE` -> `VALIDATION_ERROR`. El numero en
    claro nunca se loguea (regla de oro 7/9).
    """
    from app.modules.accounts.service import get_account as _get_account_info

    try:
        info = _get_account_info(session, account_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApplicationNotFoundError("cuenta destino inexistente") from exc
    if info is None or info["user_id"] != owner:
        raise ApplicationNotFoundError("cuenta destino inexistente")
    if info["status"] != "ACTIVE":
        raise ApplicationValidationError(f"cuenta destino no disponible ({info['status']})")
    return info


def _resolve_target_account(
    session: Session, owner: uuid.UUID, account_id: uuid.UUID | None
) -> dict:
    """Cuenta destino: la explicita o, si se omite, la principal del titular.

    La principal es la primera `ACTIVE` en orden de creacion (decision
    del dueno); sin cuentas `ACTIVE` -> `NOT_FOUND`.
    """
    from app.modules.accounts.service import list_active_by_user as _list_active

    if account_id is not None:
        return _resolve_account_by_id(session, owner, account_id)
    try:
        actives = _list_active(session, owner)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApplicationValidationError(f"titular invalido: {exc}") from exc
    if not actives:
        raise ApplicationNotFoundError("el titular no tiene cuenta activa para desembolsar")
    logger.info("loan disbursement cuenta principal por defecto activas=%s", len(actives))
    return actives[0]


def _require_approved_and_signed(session: Session, owner: uuid.UUID, app_id: uuid.UUID):
    """Expediente `APPROVED` del titular + juego `SIGNED` completo."""
    row = apps_repo.get_application(session, app_id)
    if row is None or row.user_id != owner:
        raise ApplicationNotFoundError("expediente inexistente")
    if row.status != "APPROVED":
        raise ApplicationConflictError("el expediente no esta aprobado para desembolsar")
    game = contracts_repo.list_by_application(session, row.id)
    if len(game) != 3 or any(item.status != "SIGNED" for item in game):
        raise ContractConflictError("el juego contractual no esta firmado")
    return row


def _get_loan_by_application(session: Session, app_id: uuid.UUID) -> Loan | None:
    """Prestamo del expediente (`None` si aun no se desembolso; mismo schema)."""
    stmt = sa.select(Loan).where(Loan.application_id == app_id)
    return session.scalars(stmt).one_or_none()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _view_from_loan_tx(loan: Loan, tx) -> dict[str, Any]:
    """Vista de respuesta del desembolso (sin PII; cuenta enmascarada)."""
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    return {
        "loan_id": str(loan.id),
        "application_id": str(loan.application_id),
        "transaction_id": str(tx.id),
        "status": str(tx.status),
        "disbursed_at": _iso(loan.disbursed_at),
        "account_masked": str(meta.get("account_masked") or ""),
        "amount_minor": int(loan.principal_minor),
        "currency": str(tx.currency),
    }


def _record_disbursed_event(session: Session, owner: uuid.UUID, loan: Loan, tx) -> None:
    """Registra `credit.disbursed` en el outbox (payload sin PII)."""
    from app.modules.shared.repository.outbox import insert_event

    insert_event(
        session,
        aggregate_type="credit_loan",
        aggregate_id=loan.id,
        event_type=credit_events.CREDIT_DISBURSED,
        payload={
            "loan_id": str(loan.id),
            "application_id": str(loan.application_id),
            "user_id": str(owner),
            "transaction_id": str(tx.id),
            "amount_minor": int(loan.principal_minor),
            "currency": str(tx.currency),
        },
    )


#: Plantillas de la notificacion dedicada del desembolso (E3-T24, HU12).
#: Push + email (dos codigos: el codigo es unico global y de un solo
#: canal, `docs/03b#13`). Es la UNICA notificacion del desembolso
#: (sustituye a la alerta generica de seguridad para `credit.disbursed`).
PUSH_DISBURSED_TEMPLATE = "credit_disbursed"
EMAIL_DISBURSED_TEMPLATE = "credit_disbursed_email"


def _amount_label(amount_minor: int) -> str:
    """Monto en formato `10000.00` para la notificacion (sin `float`).

    Solo digitos y punto a partir de centimos enteros; la moneda (`S/`)
    la aporta la plantilla (los creditos del MVP son en PEN, coherente
    con la cuenta destino que ya valido el caso de uso).
    """
    minor = int(amount_minor)
    magnitude = abs(minor)
    return f"{magnitude // 100}.{magnitude % 100:02d}"


def _notify_disbursed(session: Session, owner: uuid.UUID, view: dict[str, Any]) -> None:
    """Notificacion dedicada del desembolso (E3-T24, best-effort).

    Push + email por la fachada `notifications.send(...)` con las
    plantillas aditivas `credit_disbursed`/`credit_disbursed_email`
    (sin PII: referencia del prestamo + monto + cuenta enmascarada +
    instante). Se envia SOLO cuando el desembolso liquida de verdad
    (el llamante no la invoca en replay de negocio). Un fallo del
    proveedor (o sin correo resoluble) deja el desembolso intacto
    (`FAILED` u omitida). `flush` sin `commit` (decide el llamante).
    """
    try:
        from app.modules.notifications import service as notifications_service
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("credit disbursement notify_skipped reason=sin_fachada")
        return
    data = {
        "loan_ref": str(view.get("loan_id") or "")[:8],
        "amount_label": _amount_label(view.get("amount_minor") or 0),
        "account_masked": str(view.get("account_masked") or ""),
        "disbursed_at": str(view.get("disbursed_at") or ""),
    }
    try:
        notifications_service.send(
            session,
            channel="push",
            recipient=str(owner),
            template_code=PUSH_DISBURSED_TEMPLATE,
            data=data,
            user_id=owner,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort: no revierte el desembolso
        logger.warning(
            "credit disbursement notify_failed channel=push template=%s error=%s",
            PUSH_DISBURSED_TEMPLATE,
            type(exc).__name__,
        )
    try:
        from app.modules.identity.service import holder_contact as holder_contact_service

        recipient = holder_contact_service.get_holder_email(session, owner)
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning(
            "credit disbursement notify_skipped channel=email reason=%s", type(exc).__name__
        )
        return
    if not isinstance(recipient, str) or not recipient.strip():
        logger.warning("credit disbursement notify_skipped channel=email reason=sin_destinatario")
        return
    try:
        notifications_service.send(
            session,
            channel="email",
            recipient=recipient.strip(),
            template_code=EMAIL_DISBURSED_TEMPLATE,
            data=data,
            user_id=owner,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort: no revierte el desembolso
        logger.warning(
            "credit disbursement notify_failed channel=email template=%s error=%s",
            EMAIL_DISBURSED_TEMPLATE,
            type(exc).__name__,
        )
        return
    logger.info("credit disbursement notify_queued push+email")


def _expire_settled_view(session: Session, loan: Loan, tx) -> None:
    """Expira `loan`/`tx` tras el desembolso real (vista determinista).

    El motor carga colecciones `selectin` (`tx.history`) durante sus
    transiciones; si un bloque best-effort posterior captura una
    excepcion (p. ej. infra de notificaciones ausente), los tracebacks
    retenidos por el GC mantienen vivas esas instancias con la caché
    previa y las lecturas posteriores DE LA MISMA sesion ven estado
    rancio aunque el commit sea correcto (el dato persistido jamas se
    afecta). Expirar fuerza la recarga determinista en el proximo
    acceso; en produccion nadie los relee (costo cero, perezoso).
    """
    try:
        session.expire(tx)
    except Exception:  # noqa: BLE001, S110 - higiene best-effort
        pass
    try:
        session.expire(loan)
    except Exception:  # noqa: BLE001, S110 - higiene best-effort
        pass


def _audit_disbursement(session: Session, owner: uuid.UUID, loan: Loan, tx) -> None:
    """Auditoria best-effort del desembolso (sin PII ni cifras)."""
    try:
        from app.modules.audit.service import record as audit_record

        audit_record(
            session,
            actor=owner,
            action="credit.disbursed",
            entity="credit_loan",
            entity_id=loan.id,
            metadata={
                "application_id": str(loan.application_id),
                "transaction_id": str(tx.id),
                "status": str(tx.status),
            },
        )
    except Exception as exc:  # noqa: BLE001 - la auditoria no bloquea el desembolso
        logger.warning("credit disbursement audit best-effort omitida err=%s", type(exc).__name__)


def _product_rates(session: Session, product_id: object) -> tuple[Decimal, Decimal]:
    """Tasa anual (`tea` o su `tcea`) y desgravamen del producto (fachada propia)."""
    from app.modules.credits import service as catalog

    try:
        product = catalog.get_product(session, product_id=product_id)  # type: ignore[arg-type]
    except Exception as exc:
        raise ApplicationNotFoundError("producto del expediente inexistente") from exc
    annual = product.get("tea") if product.get("tea") is not None else product.get("tcea")
    return Decimal(str(annual)), Decimal(str(product.get("insurance_rate") or 0))


def _move_money(
    session: Session,
    owner: uuid.UUID,
    loan: Loan,
    target_account_id: uuid.UUID,
    amount_minor: int,
    currency: str,
) -> Any:
    """Acredita el prestamo por el motor (idempotente por clave de prestamo).

    Llama a la fachada `transactions.service.disbursement.disburse_loan`
    en la misma transaccion, emite `credit.disbursed` por `outbox` y
    audita best-effort. Si el dinero ya se movio (reintento), el motor
    devuelve la `Transaction` existente sin duplicar.
    """
    from app.modules.transactions.service import disbursement as motor

    tx = motor.disburse_loan(
        session,
        target_account_id=target_account_id,
        amount_minor=amount_minor,
        currency=currency,
        loan_ref=loan.id,
        initiator_user_id=owner,
        idempotency_key=_motor_key_for(loan.id),
    )
    _record_disbursed_event(session, owner, loan, tx)
    _audit_disbursement(session, owner, loan, tx)
    return tx


def _execute_disbursement(
    session: Session,
    row,
    owner: uuid.UUID,
    app_row,
    target: dict,
    amount_minor: int,
    currency: str,
) -> tuple[dict[str, Any], bool]:
    """Crea `loans`+cronograma, mueve el dinero y persiste el snapshot.

    Retorna `(vista, replayed_de_negocio)`: si el expediente ya tenia
    prestamo (reintento con otra clave o carrera por la UQ) devuelve lo
    existente sin mover dinero (`True`).
    """
    from app.modules.shared.repository.idempotency import store_response
    from app.modules.transactions.service import disbursement as motor

    existing_loan = _get_loan_by_application(session, app_row.id)
    if existing_loan is not None:
        from app.modules.credits.service.loans import link_contracts_to_loan

        tx = motor.find_disbursement_tx(session, initiator_user_id=owner, loan_id=existing_loan.id)
        if tx is None:
            # Residuo sin movimiento de dinero (solo alcanzable si el
            # fallo no reverso la transaccion externa): se completa hacia
            # adelante contra la cuenta del prestamo (revalidada por
            # fachada), sin duplicar (clave del motor anclada al
            # `loan_id`).
            loan_target = _resolve_account_by_id(session, owner, existing_loan.account_id)
            tx = _move_money(
                session,
                owner,
                existing_loan,
                loan_target["account_id"],
                int(existing_loan.principal_minor),
                str(loan_target["currency"]),
            )
            # El dinero se movio de verdad (completacion hacia adelante):
            # notificacion dedicada (E3-T24), igual que el camino feliz.
            _notify_disbursed(
                session,
                owner,
                _view_from_loan_tx(existing_loan, tx),
            )
            _expire_settled_view(session, existing_loan, tx)
        # Auto-reparacion del vinculo G54 (idempotente: solo rellena
        # filas aun sin vincular).
        link_contracts_to_loan(
            session, application_id=existing_loan.application_id, loan_id=existing_loan.id
        )
        view = _view_from_loan_tx(existing_loan, tx)
        store_response(session, row, response_snapshot=view, transaction_id=tx.id)
        logger.info("loan disbursement replay de negocio status=%s", tx.status)
        return view, True

    from app.modules.credits.domain.schedule import first_due_date_for
    from app.modules.credits.service.loans import create_loan_with_schedule

    moment = _utcnow()
    annual_rate, insurance_rate = _product_rates(session, app_row.product_id)
    try:
        loan, _schedules = create_loan_with_schedule(
            session,
            application_id=app_row.id,
            account_id=target["account_id"],
            principal_minor=amount_minor,
            annual_rate=annual_rate,
            term_months=int(app_row.term_months),
            insurance_rate=insurance_rate,
            first_due_date=first_due_date_for(moment.date()),
            disbursed_at=moment,
        )
    except LoanConflictError:
        # Carrera: otro worker desembolso primero (UQ por
        # `application_id`); el savepoint ya reverso lo parcial y la
        # transaccion externa sigue intacta: se devuelve lo existente
        # sin mover dinero de nuevo.
        logger.info("loan disbursement carrera UQ: se devuelve el prestamo existente")
        existing_loan = _get_loan_by_application(session, app_row.id)
        if existing_loan is None:  # pragma: no cover - defensa en profundidad
            raise
        tx = motor.find_disbursement_tx(session, initiator_user_id=owner, loan_id=existing_loan.id)
        if tx is None:  # pragma: no cover - defensa en profundidad
            raise
        view = _view_from_loan_tx(existing_loan, tx)
        store_response(session, row, response_snapshot=view, transaction_id=tx.id)
        return view, True

    tx = _move_money(session, owner, loan, target["account_id"], amount_minor, currency)
    view = _view_from_loan_tx(loan, tx)
    store_response(session, row, response_snapshot=view, transaction_id=tx.id)
    # Notificacion dedicada (E3-T24): solo en desembolso real, nunca en
    # replay de negocio (las ramas de replay retornan antes sin notificar).
    _notify_disbursed(session, owner, view)
    logger.info("loan disbursement ok status=%s", tx.status)
    _expire_settled_view(session, loan, tx)
    return view, False


def disburse_application_idempotent(
    session: Session,
    owner_user_id: uuid.UUID | str,
    application_id: object,
    *,
    account_id: object = None,
    idempotency_key: str | None,
) -> tuple[dict[str, Any], bool]:
    """Desembolsa con `Idempotency-Key` obligatoria (crea prestamo + dinero).

    Retorna `(data, replayed)`: `201/200` lo decide el endpoint. Las
    validaciones de titular/estado (`NOT_FOUND`/`CONFLICT`/
    `VALIDATION_ERROR`) **no consumen la clave** (el endpoint revierte
    y el llamado es reintentable); tras reclamarla, repetir el mismo
    cuerpo devuelve el snapshot y un cuerpo distinto da `409`. Sin
    `commit` (decide el llamante).
    """
    from app.modules.shared.repository.idempotency import (
        compute_request_hash,
        find_key,
        is_expired,
        renew_expired,
        resolve_ttl_seconds,
        try_claim,
    )

    owner = _coerce_owner(owner_user_id)
    key = str(idempotency_key).strip() if isinstance(idempotency_key, str) else ""
    if not key:
        raise ApplicationValidationError("cabecera Idempotency-Key obligatoria")
    if len(key) > 80:
        raise ApplicationValidationError("Idempotency-Key supera 80 caracteres")
    app_id = _coerce_application_id(application_id)
    wanted_account = _coerce_account_id(account_id)

    app_row = _require_approved_and_signed(session, owner, app_id)
    target = _resolve_target_account(session, owner, wanted_account)
    amount_minor = int(app_row.requested_amount_minor)
    currency = str(target["currency"])
    digest = compute_request_hash(
        {
            "application_id": str(app_row.id),
            "account_id": str(target["account_id"]),
            "amount_minor": amount_minor,
            "currency": currency,
        }
    )

    row, created = try_claim(
        session,
        key=key,
        user_id=owner,
        endpoint=IDEMPOTENCY_ENDPOINT,
        method="POST",
        request_hash=digest,
        ttl_seconds=resolve_ttl_seconds(),
    )
    if created:
        return _execute_disbursement(session, row, owner, app_row, target, amount_minor, currency)
    stored = find_key(session, key=key, user_id=owner)
    if stored is None:  # pragma: no cover - carrera extrema
        raise ApplicationConflictError("clave de idempotencia en conflicto")
    if is_expired(stored):
        renew_expired(
            session, stored, request_hash=digest, endpoint=IDEMPOTENCY_ENDPOINT, method="POST"
        )
        return _execute_disbursement(
            session, stored, owner, app_row, target, amount_minor, currency
        )
    if stored.request_hash != digest:
        raise IdempotencyKeyConflictError("Idempotency-Key ya usada con un cuerpo distinto")
    snapshot = stored.response_snapshot
    if not isinstance(snapshot, dict) or not snapshot:
        raise ApplicationConflictError("operacion en curso para esta Idempotency-Key")
    logger.info("loan disbursement replay status=%s", snapshot.get("status"))
    return dict(snapshot), True


def _loan_to_view(loan: Loan) -> dict[str, Any]:
    """Vista minima del prestamo para el listado (sin PII)."""
    return {
        "id": str(loan.id),
        "application_id": str(loan.application_id),
        "account_id": str(loan.account_id),
        "principal_minor": int(loan.principal_minor),
        "outstanding_principal_minor": int(loan.outstanding_principal_minor),
        "tcea": str(loan.tcea),
        "term_months": int(loan.term_months),
        "status": str(loan.status),
        "disbursed_at": _iso(loan.disbursed_at),
        "auto_debit_enabled": bool(loan.auto_debit_enabled),
        "created_at": _iso(loan.created_at),
    }


def list_own_loans(session: Session, owner_user_id: uuid.UUID | str) -> list[dict[str, Any]]:
    """Prestamos del titular (`created_at DESC`; solo lectura)."""
    from app.modules.credits.service.loans import list_loans_by_user

    owner = _coerce_owner(owner_user_id)
    return [_loan_to_view(loan) for loan in list_loans_by_user(session, owner)]


def get_own_schedule(
    session: Session, owner_user_id: uuid.UUID | str, loan_id: object
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Prestamo + cronograma definitivo del titular (`404` neutro si ajeno)."""
    from app.modules.credits.service.loans import get_loan, list_schedule

    owner = _coerce_owner(owner_user_id)
    try:
        clean_loan = uuid.UUID(str(loan_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise LoanNotFoundError("prestamo inexistente") from exc
    loan = get_loan(session, clean_loan)
    if loan is None:
        raise LoanNotFoundError("prestamo inexistente")
    app_row = apps_repo.get_application(session, loan.application_id)
    if app_row is None or app_row.user_id != owner:
        raise LoanNotFoundError("prestamo inexistente")

    def _row_to_view(item) -> dict[str, Any]:
        paid = item.paid_at.isoformat() if item.paid_at is not None else None
        return {
            "installment_no": int(item.installment_no),
            "due_date": item.due_date.isoformat(),
            "principal_minor": int(item.principal_minor),
            "interest_minor": int(item.interest_minor),
            "insurance_minor": int(item.insurance_minor),
            "total_minor": int(item.total_minor),
            "status": str(item.status),
            "paid_at": paid,
        }

    return _loan_to_view(loan), [_row_to_view(item) for item in list_schedule(session, loan.id)]


__all__ = [
    "IDEMPOTENCY_ENDPOINT",
    "disburse_application_idempotent",
    "get_own_schedule",
    "list_own_loans",
]
