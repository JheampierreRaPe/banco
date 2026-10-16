"""Servicio de cobro QR (E4-T11, HU15 CA-01/CA-04).

`pay_qr_charge(...)` decodifica el `qr_content` (sobre canonico `BO-QR1`,
adaptador `E4-T03`), verifica firma/hash + caducidad/estado del cargo y,
si es valido, mueve el dinero **solo** por el servicio interno
`wallet.service.express.pay_express` (`E4-T02`, camino hold + liquidacion
`docs/04#7.4` sin comision: `Debe 2000-Pagador / Haber 2100-Pagador` +
`Debe 2100-Pagador / Haber 2000-Comercio`), persiste `qr_payments`,
marca el cargo `PAID` y emite `qr.payment.confirmed` por `outbox` +
aviso `qr_charge_paid` al comercio (< 5 s).

Todo o nada, idempotente (`Idempotency-Key` obligatoria: misma clave +
mismo cuerpo = replay; cuerpo distinto = `CONFLICT`; un cargo no se
cobra dos veces: guarda Python con bloqueo de fila + indice unico
parcial `uq_qr_payments_charge_active` en BD, migracion `0041`; los
`REJECTED` no bloquean el reintento). Un QR vencido/corrupto/
manipulado se rechaza **antes de operar**: sin retencion, sin asiento,
sin descuento. `preview_qr_charge(...)` previsualiza comercio/monto
(read-only, sin mover dinero) para el consentimiento (HU15 CA-01).

Frontera (regla de oro 4): `wallet` no importa modelos ni repositorios
de `transactions`/`ledger`/`accounts`; solo fachadas (`transactions.
service`, `accounts.service`), `shared` (transversal: `outbox`,
idempotencia, parametros) y `notifications.send`. La cuenta de abono
del comercio se resuelve por la fachada `accounts` (decision 1a del
brief: cuenta `ACTIVE` del titular comercio con la moneda del cargo;
sin columna nueva en `merchant_accounts`).

Al autorizar una pendiente (`E2-T19`/`E2-T31`), `E4-T12` pasa
`daily_limit_reader=wallet_daily_limit_reader` (re-chequeo `E2-T36`
con el limite de billetera, `docs/18` G75) y luego invoca
`finalize_qr_payment(...)` para marcar `PAID` + notificar.

Sin PII/secretos en logs ni en eventos (regla 7): solo `id`s, estados
y decisiones; la cuenta solo enmascarada en `metadata` del motor.
Convencion: `flush` sin `commit`; el llamante confirma.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.events import QR_PAYMENT_CONFIRMED
from app.modules.wallet.domain import qr_payment as qr_payment_domain
from app.modules.wallet.domain.qr_payment import (
    InvalidQrError,
    QrAlreadyPaidError,
    QrExpiredError,
    QrMerchantUnknownError,
    QrPaymentConflictError,
    QrPaymentError,
    QrPaymentNotFoundError,
)
from app.modules.wallet.repository import qr as qr_repo
from app.modules.wallet.repository import qr_payment as qr_payment_repo
from app.modules.wallet.service.express import (
    pay_express,
    wallet_daily_limit_reader,
)

logger = logging.getLogger(__name__)

#: Accion de auditoria best-effort (sin evento nuevo: basta el outbox).
AUDIT_ACTION = "qr.payment.confirmed"

#: Endpoint logico para la idempotencia del cobro (clave `shared`,
#: UQ por (`key`, titular); patron `E2-T13`/`E4-T08`).
QR_PAYMENT_IDEMPOTENCY_ENDPOINT = "/payments/qr"

#: Nombre del indice unico parcial de la guarda de doble cobro
#: (migracion `0041_wallet_qr_payment_guard`): solo pagos con cobro
#: real/pendiente (`status <> 'REJECTED'`).
CHARGE_GUARD_INDEX = "uq_qr_payments_charge_active"


def _is_charge_guard_violation(exc: IntegrityError) -> bool:
    """Indica si la `IntegrityError` es la guarda de doble cobro (portable).

    - Postgres nombra el indice (`... violates unique constraint
      "uq_qr_payments_charge_active"`).
    - SQLite solo dice `UNIQUE constraint failed:
      qr_payments.charge_id` (sin el nombre).
    Otras integridades (FK `fk_qr_payments_*`, `CHECK`, UQ de
    `transaction_id`) no matchean: se relanzan intactas.
    """
    msg = str(getattr(exc, "orig", exc) or exc)
    if CHARGE_GUARD_INDEX in msg:
        return True
    upper = msg.upper()
    return (
        "CHARGE_ID" in upper
        and "UNIQUE" in upper
        and "FOREIGN" not in upper
        and "CHECK" not in upper
    )


def _persist_payment_or_conflict(session: Session, **fields: Any) -> Any:
    """Persiste el `qr_payment` mapeando la carrera a `CONFLICT` (ALTO-2).

    Si dos pagos concurrentes del mismo cargo superan la lectura de la
    guarda, la BD decide via `uq_qr_payments_charge_active`: el perdedor
    recibe `IntegrityError` en el `flush` y aqui colapsa a
    `QrAlreadyPaidError` (409, un solo cobro). Otra integridad (FK,
    `CHECK`) se relanza intacta. Sin PII en logs (solo la decision).
    """
    try:
        return qr_payment_repo.create_qr_payment(session, **fields)
    except IntegrityError as exc:
        if not _is_charge_guard_violation(exc):
            raise
        logger.info("qr payment carrera por cargo resuelta a CONFLICT")
        raise QrAlreadyPaidError("el QR ya fue cobrado") from exc


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidQrError("QR con formato invalido") from exc


class _SimplePayee:
    """Destino minimo para el aviso (`user_id`; E4-T21, titular personal)."""

    def __init__(self, user_id: Any) -> None:
        self.user_id = user_id


def _require_key(idempotency_key: object) -> str:
    key = str(idempotency_key).strip() if isinstance(idempotency_key, str) else ""
    if not key:
        raise InvalidQrError("cabecera Idempotency-Key obligatoria")
    if len(key) > 80:
        raise InvalidQrError("Idempotency-Key supera 80 caracteres")
    return key


def resolve_merchant_settlement_account(
    session: Session, merchant: Any, currency: str
) -> uuid.UUID:
    """Cuenta de abono del comercio por la fachada `accounts` (decision 1a).

    Primera cuenta `ACTIVE` del titular comercio (`merchant.user_id`)
    con la moneda del cargo, en orden de creacion (determinista). Sin
    cuenta compatible -> `QrMerchantUnknownError` neutro (sin oraculo
    sobre cuentas ajenas; el numero en claro jamas se loguea).
    Solo lectura (sin `flush`/`commit`). Delega en el resolutor
    generico de `service/qr.py` (E4-T21).
    """
    from app.modules.wallet.service.qr import QrMerchantNotFoundError as _NotFound
    from app.modules.wallet.service.qr import resolve_holder_settlement_account

    try:
        return resolve_holder_settlement_account(session, merchant.user_id, currency)
    except _NotFound as exc:
        raise QrMerchantUnknownError("comercio sin cuenta de abono") from exc


def resolve_personal_settlement_account(session: Session, charge: Any, currency: str) -> uuid.UUID:
    """Cuenta de abono del QR personal (E4-T21, HU14 CA-03).

    Respeta el destino fijado al generar (`payee_account_id`) cuando
    sigue `ACTIVE` y en la moneda del cobro; si no, resuelve la
    primera cuenta `ACTIVE` del titular con esa moneda por la fachada
    `accounts` (sin columna nueva en `accounts`). Sin cuenta
    compatible -> `QrMerchantUnknownError` neutro (mismo codigo que
    el comercio sin abono) antes de operar. Solo lectura.
    """
    from app.modules.accounts.service import get_account as accounts_get_account
    from app.modules.wallet.service.qr import QrMerchantNotFoundError as _NotFound
    from app.modules.wallet.service.qr import resolve_holder_settlement_account

    wanted = str(currency or "").strip().upper()
    preferred = getattr(charge, "payee_account_id", None)
    if preferred is not None:
        try:
            view = accounts_get_account(session, preferred)
        except (ValueError, AttributeError, TypeError):
            view = None
        if (
            isinstance(view, dict)
            and view.get("user_id") == getattr(charge, "owner_user_id", None)
            and str(view.get("status")) == "ACTIVE"
            and str(view.get("currency") or "").upper() == wanted
        ):
            account_id = view.get("account_id")
            try:
                return (
                    account_id if isinstance(account_id, uuid.UUID) else uuid.UUID(str(account_id))
                )
            except (ValueError, AttributeError, TypeError):
                pass
    try:
        return resolve_holder_settlement_account(
            session, getattr(charge, "owner_user_id", None), currency
        )
    except _NotFound as exc:
        raise QrMerchantUnknownError("titular sin cuenta de abono") from exc


def _record_payment_confirmed_event(session: Session, payment: Any, charge: Any) -> None:
    """Registra `qr.payment.confirmed` en el outbox (payload sin PII).

    A1/HU22: incluye `user_id` (pagador) para que el consumidor de
    alertas dispare push + correo sin importar otros modulos.
    """
    from app.modules.shared.repository.outbox import insert_event

    merchant_id = getattr(payment, "merchant_id", None)
    payee_user_id = getattr(payment, "payee_user_id", None)
    insert_event(
        session,
        aggregate_type="qr_payment",
        aggregate_id=payment.id,
        event_type=QR_PAYMENT_CONFIRMED,
        payload={
            "qr_payment_id": str(payment.id),
            "charge_id": str(charge.id),
            "merchant_id": str(merchant_id) if merchant_id is not None else None,
            "payee_user_id": str(payee_user_id) if payee_user_id is not None else None,
            "amount_minor": int(payment.amount_minor),
            "currency": str(payment.currency),
            "status": str(payment.status),
            "user_id": str(payment.payer_user_id),
        },
    )


def _audit_payment(session: Session, payment: Any, charge: Any) -> None:
    """Auditoria best-effort del cobro (sin PII ni cifras)."""
    try:
        from app.modules.audit.service import record as audit_record

        merchant_id = getattr(payment, "merchant_id", None)
        payee_user_id = getattr(payment, "payee_user_id", None)
        audit_record(
            session,
            actor=payment.payer_user_id,
            action=AUDIT_ACTION,
            entity="qr_payment",
            entity_id=payment.id,
            metadata={
                "qr_payment_id": str(payment.id),
                "charge_id": str(charge.id),
                "merchant_id": str(merchant_id) if merchant_id is not None else None,
                "payee_user_id": str(payee_user_id) if payee_user_id is not None else None,
                "status": str(payment.status),
            },
        )
    except Exception as exc:  # noqa: BLE001 - la auditoria no bloquea el cobro
        logger.warning("qr payment audit best-effort omitida err=%s", type(exc).__name__)


def _notify_payee(session: Session, charge: Any, payee: Any, *, sender: Any | None = None) -> None:
    """Aviso de abono al destino, best-effort (no revierte el dinero).

    Comercio: `notify_merchant_charge_paid`; titular personal (E4-T21):
    `notify_personal_charge_paid`. Ambos reutilizan la plantilla
    `qr_charge_paid` (push, sin PII ni cifras del pagador). Un fallo
    del proveedor no anula el pago ya liquidado (`docs/04#12`
    post-commit asincrono).
    """
    try:
        if qr_payment_domain.is_personal_charge(charge):
            from app.modules.wallet.service.qr import notify_personal_charge_paid

            notify_personal_charge_paid(
                session,
                owner_user_id=payee.user_id,
                charge_id=charge.id,
                sender=sender,
            )
            return
        from app.modules.wallet.service.qr import notify_merchant_charge_paid

        notify_merchant_charge_paid(
            session,
            merchant_user_id=payee.user_id,
            charge_id=charge.id,
            sender=sender,
        )
    except Exception as exc:  # noqa: BLE001 - el aviso no bloquea el cobro
        logger.warning("qr payment aviso best-effort omitido err=%s", type(exc).__name__)


def _settle_payment(
    session: Session,
    *,
    payment: Any,
    charge: Any,
    payee: Any,
    now: datetime,
    sender: Any | None,
) -> Any:
    """Marca `PAID` + evento + aviso (solo al liquidar `SETTLED`).

    Un cargo reutilizable (QR personal, E4-T21) queda `ACTIVE` para
    admitir mas cobros: solo el pago se marca `PAID` (el cargo no se
    agota). Un cargo de un solo uso se marca `PAID` como siempre.
    """
    if not bool(getattr(charge, "reusable", False)):
        qr_repo.mark_paid(session, charge, paid_at=now)
    qr_payment_repo.mark_paid(session, payment)
    _record_payment_confirmed_event(session, payment, charge)
    _audit_payment(session, payment, charge)
    _notify_payee(session, charge, payee, sender=sender)
    logger.info("qr payment liquidado status=PAID")
    return payment


def _require_payer_amount(value: object) -> int:
    """Monto digitado por el pagador: entero de centimos `> 0` (nunca `float`)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidQrError("el QR no trae monto: el pagador debe indicarlo")
    if value <= 0:
        raise InvalidQrError("el QR no trae monto: el pagador debe indicarlo")
    return value


def pay_qr_charge(
    session: Session,
    *,
    payer_user_id: uuid.UUID | str,
    qr_content: str,
    link_id: uuid.UUID | str,
    idempotency_key: str,
    amount_minor: int | None = None,
    secret: str | None = None,
    now: datetime | None = None,
    notify_sender: Any | None = None,
    fee_minor: int | None = None,
    risk_profile: str | None = None,
    device_id: str | None = None,
    balance_port: Any | None = None,
) -> tuple[Any, Any]:
    """Cobra un QR vigente por el motor (`docs/04#7.4`).

    1. Decodifica el `qr_content` con el adaptador `E4-T03`
       (`decode_payload`); imposible -> `InvalidQrError` (422).
    2. Verifica firma (`verify_charge_signature`) + `payload_hash` +
       envelope + estado `ACTIVE`/no vencido (`is_charge_valid`):
       vencido -> `QrExpiredError`, ya cobrado (un solo uso) ->
       `QrAlreadyPaidError`, resto -> `InvalidQrError`. Todo **antes
       de operar** (sin retencion, sin asiento).
    3. Resuelve el monto efectivo: el del cargo (comercio) o el
       digitado por el pagador (`amount_minor`, E4-T21) cuando el QR
       no trae monto; sin monto -> `InvalidQrError` (422). Resuelve
       la cuenta de abono por la fachada `accounts`: comercio (cuenta
       `ACTIVE` del titular comercio) o titular personal (cuenta
       fijada al generar o primera `ACTIVE` con la moneda); sin ella
       -> `QrMerchantUnknownError`.
    4. Mueve el dinero **solo** por `pay_express` (`E4-T02`) con la
       misma `Idempotency-Key` (el gate del motor aplica base/diario;
       `CARD`/ajeno y conflictos los informa el express).
    5. Persiste `qr_payments`: `SETTLED` -> `PAID` (+ cargo `PAID`/
       `paid_at` salvo QR reutilizable, evento
       `qr.payment.confirmed`, aviso `qr_charge_paid` al destino);
       `PENDING_AUTHORIZATION` -> pago pendiente (sin marcar `PAID`
       aun; `E4-T12` autoriza y finaliza con `finalize_qr_payment`
       pasando `daily_limit_reader=wallet_daily_limit_reader`, G75);
       `REJECTED` -> pago `REJECTED` sin tocar el cargo.
    6. Idempotencia por (`Idempotency-Key`, titular): misma clave +
       mismo cuerpo = replay (un solo pago); cuerpo distinto =
       `QrPaymentConflictError` (409). Un QR reutilizable admite
       varios cobros (uno por clave); un QR de un solo uso conserva
       la guarda (`409` al segundo cobro).

    Retorna `(QrPayment, Transaction)`. Solo `flush` (el llamante
    confirma o revierte toda la operacion: todo o nada).
    """
    from app.modules.shared.repository.idempotency import (
        compute_request_hash,
        find_key,
        is_expired,
        renew_expired,
        resolve_ttl_seconds,
        store_response,
        try_claim,
    )
    from app.modules.transactions.service import find_by_idempotency_key

    payer = _coerce_uuid(payer_user_id, "payer_user_id")
    link_ref = _coerce_uuid(link_id, "link_id")
    key = _require_key(idempotency_key)
    moment = now if isinstance(now, datetime) else _utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    # 1-2. Decodificar + verificar integridad (hash/firma/envelope),
    # antes de operar (sin retencion, `docs/04#7.4`). La guarda de
    # cobrabilidad (estado/caducidad) va DESPUES de la idempotencia:
    # el replay con la misma clave debe devolver el pago existente
    # aunque el cargo ya este `PAID`.
    envelope = qr_payment_domain.decode_qr_content(qr_content)
    charge = qr_repo.get_qr_charge(session, envelope.ref)
    if charge is None:
        raise InvalidQrError("QR con formato invalido")
    qr_payment_domain.verify_charge_integrity(charge, envelope, secret=secret)

    # Monto efectivo: el del cargo (comercio) o el digitado por el
    # pagador cuando el QR no trae monto (personal, E4-T21). Sin monto
    # -> 422 antes de operar (nunca se confia en el cliente: el sobre
    # trae `0` y la fila `NULL`).
    personal = qr_payment_domain.is_personal_charge(charge)
    if personal:
        if getattr(charge, "amount_minor", None) is not None:
            raise InvalidQrError("QR con formato invalido")
        effective_amount = _require_payer_amount(amount_minor)
    else:
        effective_amount = int(charge.amount_minor)

    # 6. Idempotencia del cobro (clave `shared`, UQ por titular). El
    # monto efectivo entra al digest: misma clave + distinto monto =
    # `CONFLICT` (el replay exige el cuerpo exacto).
    digest = compute_request_hash(
        {
            "payer_user_id": str(payer),
            "qr_content": str(qr_content).strip(),
            "link_id": str(link_ref),
            "amount_minor": effective_amount,
        }
    )
    row, created = try_claim(
        session,
        key=key,
        user_id=payer,
        endpoint=QR_PAYMENT_IDEMPOTENCY_ENDPOINT,
        method="POST",
        request_hash=digest,
        ttl_seconds=resolve_ttl_seconds(),
    )
    if not created:
        stored = find_key(session, key=key, user_id=payer)
        if stored is None:  # pragma: no cover - carrera extrema
            raise QrPaymentConflictError("clave de idempotencia en conflicto")
        if is_expired(stored):
            renew_expired(
                session,
                stored,
                request_hash=digest,
                endpoint=QR_PAYMENT_IDEMPOTENCY_ENDPOINT,
                method="POST",
            )
            row = stored
            created = True
        elif stored.request_hash != digest:
            raise QrPaymentConflictError("Idempotency-Key ya usada con un cuerpo distinto")
        elif not isinstance(stored.response_snapshot, dict):
            raise QrPaymentConflictError("operacion en curso para esta Idempotency-Key")
        else:
            snapshot = stored.response_snapshot
            payment = qr_payment_repo.get_qr_payment(session, snapshot.get("qr_payment_id"))
            motor_tx = find_by_idempotency_key(session, key, initiator_user_id=payer)
            if payment is None or motor_tx is None:
                raise QrPaymentConflictError("pago asociado a la clave ya no esta disponible")
            logger.info("qr payment replay status=%s", payment.status)
            return payment, motor_tx

    # Cobrabilidad: estado/caducidad + doble cobro, solo en el camino
    # nuevo (el replay ya retorno arriba). Todo antes de operar: sin
    # retencion, sin asiento (`docs/04#7.4`). La fila del cargo se lee
    # con bloqueo (`FOR UPDATE` en Postgres) para serializar la ventana
    # verificar -> operar; el backstop atomico es el indice unico
    # parcial `uq_qr_payments_charge_active` (ALTO-2, migracion `0041`).
    # La guarda solo cuenta cobros reales/pendientes: un `REJECTED`
    # (sin mover dinero) admite reintento con nueva clave (MEDIO-2).
    locked = qr_payment_repo.lock_charge(session, charge.id)
    payable = locked if locked is not None else charge
    qr_payment_domain.ensure_charge_payable(payable, moment)
    reusable = bool(getattr(charge, "reusable", False))
    if not reusable and qr_payment_repo.get_active_by_charge(session, charge.id) is not None:
        raise QrAlreadyPaidError("el QR ya fue cobrado")

    # 3. Cuenta de abono por la fachada `accounts`: comercio (decision
    # 1a) o titular personal (destino fijado al generar, E4-T21).
    if personal:
        payee_user_id = getattr(charge, "owner_user_id", None)
        target_account_id = resolve_personal_settlement_account(
            session, charge, str(charge.currency)
        )
        merchant_id: uuid.UUID | None = None
        payee: Any = _SimplePayee(payee_user_id)
    else:
        merchant = qr_repo.get_merchant(session, charge.merchant_id)
        if merchant is None or str(merchant.status) != "ACTIVE":
            raise QrMerchantUnknownError("comercio sin cuenta de abono")
        target_account_id = resolve_merchant_settlement_account(
            session, merchant, str(charge.currency)
        )
        merchant_id = merchant.id
        payee = merchant
        payee_user_id = None

    # 4. Dinero solo por el motor via `pay_express` (misma clave: el
    # motor resuelve su propio replay sin duplicar asientos).
    tx = pay_express(
        session,
        initiator_user_id=payer,
        link_id=link_ref,
        target_account_id=target_account_id,
        amount_minor=effective_amount,
        currency=str(charge.currency),
        idempotency_key=key,
        fee_minor=fee_minor,
        device_id=device_id,
        risk_profile=risk_profile,
        balance_port=balance_port,
    )

    # 5. Persistir el cobro segun el estado del motor. La insercion
    # decide la carrera (ALTO-2): si otro pago del cargo gano primero,
    # el indice unico parcial aborta con `CONFLICT` (el llamante
    # revierte toda la operacion, incluido el `tx` del motor: todo o
    # nada, un solo asiento). En un QR reutilizable el indice no
    # aplica (`reusable = true`, migracion `0047`): varios cobros
    # coexisten.
    status = str(tx.status)
    if status == "SETTLED":
        payment = _persist_payment_or_conflict(
            session,
            transaction_id=tx.id,
            charge_id=charge.id,
            payer_user_id=payer,
            merchant_id=merchant_id,
            payee_user_id=payee_user_id,
            reusable=reusable,
            amount_minor=effective_amount,
            currency=str(charge.currency),
            status="PAID",
        )
        _settle_payment(
            session,
            payment=payment,
            charge=charge,
            payee=payee,
            now=moment,
            sender=notify_sender,
        )
    elif status == "PENDING_AUTHORIZATION":
        payment = _persist_payment_or_conflict(
            session,
            transaction_id=tx.id,
            charge_id=charge.id,
            payer_user_id=payer,
            merchant_id=merchant_id,
            payee_user_id=payee_user_id,
            reusable=reusable,
            amount_minor=effective_amount,
            currency=str(charge.currency),
            status="PENDING_AUTHORIZATION",
        )
        _audit_payment(session, payment, charge)
        logger.info("qr payment pendiente de autorizacion")
    else:
        # `REJECTED` fuera del indice parcial: varios rechazos del mismo
        # cargo coexisten (cada reintento fallido deja su traza) sin
        # bloquear el reintento legitimo (MEDIO-2); insercion directa.
        payment = qr_payment_repo.create_qr_payment(
            session,
            transaction_id=tx.id,
            charge_id=charge.id,
            payer_user_id=payer,
            merchant_id=merchant_id,
            payee_user_id=payee_user_id,
            reusable=reusable,
            amount_minor=effective_amount,
            currency=str(charge.currency),
            status="REJECTED",
        )
        _audit_payment(session, payment, charge)
        logger.info("qr payment rechazado status=%s", status)
    store_response(
        session,
        row,
        response_snapshot={
            "qr_payment_id": str(payment.id),
            "transaction_id": str(tx.id),
            "charge_id": str(charge.id),
            "status": str(payment.status),
        },
        transaction_id=tx.id,
    )
    return payment, tx


def preview_qr_charge(
    session: Session,
    *,
    qr_content: str,
    secret: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Previsualiza un QR para el consentimiento informado (MEDIO-3, HU15 CA-01).

    Decodifica el `qr_content` (adaptador `E4-T03`), verifica
    firma/hash/envelope + caducidad/estado y devuelve `{charge_id,
    merchant {merchant_id, business_name}, amount_minor, currency,
    expires_at, status}` para mostrar **comercio y monto antes de
    confirmar**. Estrictamente read-only: **no mueve dinero**, no crea
    pagos ni eventos (sin `flush`/`commit`; sin `Idempotency-Key`).

    Un QR personal (E4-T21) devuelve `amount_minor: None` (el pagador
    digita el monto al confirmar) y `payee` con el titular destino en
    lugar de `merchant`.

    Errores neutros (mismo mapeo que el cobro): QR
    corrupto/manipulado/ajeno o destino sin cuenta de abono ->
    `InvalidQrError`/`QrMerchantUnknownError` (422); vencido ->
    `QrExpiredError` (422); ya cobrado (un solo uso) ->
    `QrAlreadyPaidError` (409).
    La respuesta nunca expone `qr_content` crudo, firma, secreto ni
    RUC (solo el nombre publico del comercio, como `POST /payments/qr`).
    """
    moment = now if isinstance(now, datetime) else _utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    envelope = qr_payment_domain.decode_qr_content(qr_content)
    charge = qr_repo.get_qr_charge(session, envelope.ref)
    if charge is None:
        raise InvalidQrError("QR con formato invalido")
    qr_payment_domain.verify_charge_integrity(charge, envelope, secret=secret)
    qr_payment_domain.ensure_charge_payable(charge, moment)
    if qr_payment_domain.is_personal_charge(charge):
        resolve_personal_settlement_account(session, charge, str(charge.currency))
        logger.debug("qr preview personal ok")
        return {
            "charge_id": str(charge.id),
            "merchant": None,
            "payee": {"owner_user_id": str(getattr(charge, "owner_user_id", None))},
            "amount_minor": None,
            "currency": str(charge.currency),
            "expires_at": None,
            "status": str(charge.status),
        }
    merchant = qr_repo.get_merchant(session, charge.merchant_id)
    if merchant is None or str(merchant.status) != "ACTIVE":
        raise QrMerchantUnknownError("comercio sin cuenta de abono")
    expires_at = charge.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    logger.debug("qr preview ok")
    return {
        "charge_id": str(charge.id),
        "merchant": {
            "merchant_id": str(merchant.id),
            "business_name": str(merchant.business_name),
        },
        "payee": None,
        "amount_minor": int(charge.amount_minor),
        "currency": str(charge.currency),
        "expires_at": expires_at,
        "status": str(charge.status),
    }


def finalize_qr_payment(
    session: Session,
    *,
    qr_payment_id: uuid.UUID | str,
    transaction: Any,
    notify_sender: Any | None = None,
    now: datetime | None = None,
) -> Any:
    """Finaliza un pago diferido tras la autorizacion (`E4-T12`).

    La invoca el flujo de `E4-T12` despues de autorizar la operacion
    del motor (`authorize_transaction` / `authorize_transaction_with_
    pin` con `daily_limit_reader=wallet_daily_limit_reader`, G75):
    exige la `Transaction` ya en `SETTLED`, marca `qr_payments` y el
    `qr_charge` `PAID` (+ `paid_at`), emite `qr.payment.confirmed` por
    `outbox` y avisa al comercio (`qr_charge_paid`), una sola vez.

    Idempotente: un pago ya `PAID` se retorna sin duplicar evento ni
    aviso. La `Transaction` llega como objeto del llamante (el motor
    la cargo): `wallet` no importa repositorios ajenos (regla 4).
    Solo `flush` (el llamante confirma).
    """
    try:
        pid = (
            qr_payment_id if isinstance(qr_payment_id, uuid.UUID) else uuid.UUID(str(qr_payment_id))
        )
    except (ValueError, AttributeError, TypeError) as exc:
        raise QrPaymentNotFoundError("pago inexistente") from exc
    payment = qr_payment_repo.get_qr_payment(session, pid)
    if payment is None:
        raise QrPaymentNotFoundError("pago inexistente")
    if str(payment.status) == "PAID":
        logger.info("qr payment finalize replay status=PAID")
        return payment
    if str(payment.status) != "PENDING_AUTHORIZATION":
        raise InvalidQrError("el pago no espera autorizacion")
    tx_status = str(getattr(transaction, "status", "") or "")
    tx_id = str(getattr(transaction, "id", "") or "")
    if tx_status != "SETTLED":
        raise InvalidQrError("la operacion aun no esta liquidada")
    if tx_id != str(payment.transaction_id):
        raise InvalidQrError("QR con formato invalido")
    charge = qr_repo.get_qr_charge(session, payment.charge_id)
    if charge is None:  # pragma: no cover - defensivo
        raise InvalidQrError("QR con formato invalido")
    if str(charge.status) == "PAID":
        qr_payment_repo.mark_paid(session, payment)
        return payment
    if qr_payment_domain.is_personal_charge(charge):
        payee: Any = _SimplePayee(getattr(charge, "owner_user_id", None))
        resolve_personal_settlement_account(session, charge, str(charge.currency))
    else:
        merchant = qr_repo.get_merchant(session, charge.merchant_id)
        if merchant is None or str(merchant.status) != "ACTIVE":
            raise QrMerchantUnknownError("comercio sin cuenta de abono")
        payee = merchant
    moment = now if isinstance(now, datetime) else _utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return _settle_payment(
        session,
        payment=payment,
        charge=charge,
        payee=payee,
        now=moment,
        sender=notify_sender,
    )


__all__ = [
    "AUDIT_ACTION",
    "QR_PAYMENT_IDEMPOTENCY_ENDPOINT",
    "InvalidQrError",
    "QrAlreadyPaidError",
    "QrExpiredError",
    "QrMerchantUnknownError",
    "QrPaymentConflictError",
    "QrPaymentError",
    "QrPaymentNotFoundError",
    "finalize_qr_payment",
    "pay_qr_charge",
    "preview_qr_charge",
    "resolve_merchant_settlement_account",
    "resolve_personal_settlement_account",
    "wallet_daily_limit_reader",
]
