"""Servicio de generacion de QR firmado (E4-T06, HU14 CA-01/CA-03).

`create_qr_charge(...)` genera el payload canonico firmado, calcula
`expires_at` desde `wallet.qr_ttl_seconds` (default `300` s, via la
unica fachada `shared/config`, regla de oro 6), persiste el cargo
`ACTIVE` y emite `qr.charge.created` por `outbox` (regla de oro 8).

**No mueve dinero** (la generacion no contabiliza; el pago es HU15).
Sin endpoint publico (el router es `E4-T08`). Sin PII/secretos en logs
(regla 7): solo `id`s, `mode`, `replayed`-like y decisiones; el
secreto HMAC y el RUC jamas se loguean ni viajan en respuestas.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.modules.shared.config import parameters as config_params
from app.modules.wallet import repository as wallet_repo
from app.modules.wallet.domain import qr as qr_domain
from app.modules.wallet.events import QR_CHARGE_CREATED
from app.modules.wallet.repository import qr as qr_repo

logger = logging.getLogger(__name__)

#: Accion de auditoria best-effort (sin evento nuevo: basta el outbox).
AUDIT_ACTION = "qr.charge.created"


class QrError(ValueError):
    """Error base del QR firmado (mapeable a 4xx en `E4-T08`)."""

    code = "QR_ERROR"


class QrMerchantNotFoundError(QrError, LookupError):
    """El comercio no existe o no esta `ACTIVE` (neutro, `NOT_FOUND`)."""

    code = "NOT_FOUND"


class QrValidationError(QrError):
    """Monto/moneda/modo/concepto invalidos."""

    code = "VALIDATION_ERROR"


class QrNotFoundError(QrError, LookupError):
    """El cargo no existe o es de otro comercio (neutro, `NOT_FOUND`)."""

    code = "NOT_FOUND"


class QrNotAuthorizedError(QrError, PermissionError):
    """El titular no tiene cuenta de comercio `ACTIVE` (`NOT_AUTHORIZED`)."""

    code = "NOT_AUTHORIZED"


class QrIdempotencyConflictError(QrError):
    """Misma `Idempotency-Key` con distinto cuerpo (-> 409)."""

    code = "IDEMPOTENCY_CONFLICT"


#: Endpoint logico para la idempotencia del alta de QR (clave `shared`,
#: UQ por usuario; patron `E2-T13`, opcional al no mover dinero).
QR_IDEMPOTENCY_ENDPOINT = "/merchants/qr"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise QrValidationError(f"{field} debe ser UUID") from exc


def get_qr_ttl_seconds(session: Session) -> int:
    """Vigencia del QR en segundos (best-effort con fallback `300`).

    Lee `wallet.qr_ttl_seconds` via la unica fachada `shared/config`
    (regla de oro 6); nunca lanza.
    """
    return config_params.get_int_parameter(
        session,
        config_params.WALLET_QR_TTL_SECONDS_KEY,
        config_params.DEFAULT_WALLET_QR_TTL_SECONDS,
        1,
    )


def _resolve_merchant(session: Session, merchant_id: uuid.UUID) -> Any:
    row = qr_repo.get_merchant(session, merchant_id)
    if row is None or str(row.status) != "ACTIVE":
        raise QrMerchantNotFoundError("comercio inexistente")
    return row


def _audit_charge(session: Session, row: Any) -> None:
    """Auditoria best-effort de la generacion (sin PII ni cifras)."""
    try:
        from app.modules.audit.service import record as audit_record

        audit_record(
            session,
            actor=row.merchant_id,
            action=AUDIT_ACTION,
            entity="qr_charge",
            entity_id=row.id,
            metadata={
                "charge_id": str(row.id),
                "merchant_id": str(row.merchant_id),
                "mode": str(row.mode),
                "status": str(row.status),
            },
        )
    except Exception as exc:  # noqa: BLE001 - la auditoria no bloquea la generacion
        logger.warning("qr audit best-effort omitida err=%s", type(exc).__name__)


def _record_charge_created_event(session: Session, row: Any) -> None:
    """Registra `qr.charge.created` en el outbox (payload sin PII).

    Comercio (E4-T06): `merchant_id` + `amount_minor` presentes.
    Personal (E4-T21): `merchant_id`/`amount_minor` en `None` y
    `payee_user_id` con el titular destino.
    """
    from app.modules.shared.repository.outbox import insert_event

    expires_at = row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    merchant_id = getattr(row, "merchant_id", None)
    amount = getattr(row, "amount_minor", None)
    insert_event(
        session,
        aggregate_type="qr_charge",
        aggregate_id=row.id,
        event_type=QR_CHARGE_CREATED,
        payload={
            "charge_id": str(row.id),
            "merchant_id": str(merchant_id) if merchant_id is not None else None,
            "payee_user_id": (
                str(getattr(row, "owner_user_id", None))
                if getattr(row, "owner_user_id", None) is not None
                else None
            ),
            "amount_minor": int(amount) if amount is not None else None,
            "currency": str(row.currency),
            "mode": str(row.mode),
            "expires_at": expires_at.isoformat() if expires_at is not None else None,
        },
    )


def to_charge_view(
    row: Any,
    *,
    payload: dict[str, Any] | None = None,
    qr_content: str | None = None,
) -> dict[str, Any]:
    """Vista del cargo (el secreto jamas sale; la firma si: es verificable).

    `qr_content` es el sobre canonico `BO-QR1` listo para render/escaneo
    (`docs/05#6.5`); se incluye cuando el llamante lo aporta (creacion y
    replay lo calculan con `domain.qr.to_interop_token`).
    """
    expires_at = row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    merchant_id = getattr(row, "merchant_id", None)
    amount = getattr(row, "amount_minor", None)
    view: dict[str, Any] = {
        "charge_id": str(row.id),
        "merchant_id": str(merchant_id) if merchant_id is not None else None,
        "amount_minor": int(amount) if amount is not None else None,
        "currency": str(row.currency),
        "concept": row.concept,
        "mode": str(row.mode),
        "status": str(row.status),
        "payload_hash": str(row.payload_hash),
        "signature": str(row.signature),
        "expires_at": expires_at,
        "created_at": row.created_at,
        "paid_at": row.paid_at,
        "payee_type": str(getattr(row, "payee_type", "MERCHANT")),
        "owner_user_id": (
            str(getattr(row, "owner_user_id", None))
            if getattr(row, "owner_user_id", None) is not None
            else None
        ),
        "payee_account_id": (
            str(getattr(row, "payee_account_id", None))
            if getattr(row, "payee_account_id", None) is not None
            else None
        ),
        "reusable": bool(getattr(row, "reusable", False)),
    }
    if payload is not None:
        view["payload"] = dict(payload)
    if qr_content is not None:
        view["qr_content"] = str(qr_content)
    return view


def create_qr_charge(
    session: Session,
    *,
    merchant_id: uuid.UUID | str,
    amount_minor: int,
    currency: str,
    concept: str | None = None,
    mode: str = "DYNAMIC",
    now: datetime | None = None,
    ttl_seconds: int | None = None,
    secret: str | None = None,
) -> dict[str, Any]:
    """Genera un QR de cobro firmado y lo persiste `ACTIVE`.

    - Valida comercio (`ACTIVE`, si no `QrMerchantNotFoundError`
      neutro), monto (`> 0`, entero), moneda (ISO-4217) y modo.
    - `expires_at = now + ttl` en `DYNAMIC`; `None` en `STATIC` (sin
      caducidad estricta en el MVP).
    - Firma HMAC-SHA256 con secreto de servidor (encapsulado en
      `domain.qr`, migrable a `Ed25519`); persiste `payload_hash` +
      `signature` y emite `qr.charge.created` por `outbox`.
    - La vista incluye `qr_content` (`str`): el sobre canonico `BO-QR1`
      (`domain.qr.to_interop_token`, adaptador `E4-T03`) listo para
      render/escaneo (`docs/05#6.5`).

    Solo `flush` (el llamante confirma). Generar es `< 1 s` y no mueve
    dinero.
    """
    mid = _coerce_uuid(merchant_id, "merchant_id")
    merchant = _resolve_merchant(session, mid)
    try:
        clean_mode = qr_domain._require_mode(mode)
        clean_currency = qr_domain._require_currency(currency)
        clean_concept = qr_domain._require_concept(concept)
        clean_amount = qr_domain._require_minor("amount_minor", amount_minor)
    except (TypeError, ValueError) as exc:
        raise QrValidationError(str(exc)) from exc

    moment = now if isinstance(now, datetime) else _utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    if ttl_seconds is None:
        ttl = get_qr_ttl_seconds(session)
    else:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise QrValidationError("ttl_seconds debe ser entero")
        if ttl_seconds < 1:
            raise QrValidationError("ttl_seconds debe ser >= 1")
        ttl = ttl_seconds

    from datetime import timedelta

    expires_at = moment + timedelta(seconds=ttl) if clean_mode == "DYNAMIC" else None

    charge_id = uuid.uuid4()
    payload = qr_domain.build_payload(
        charge_id=charge_id,
        merchant_id=merchant.id,
        amount_minor=clean_amount,
        currency=clean_currency,
        concept=clean_concept,
        mode=clean_mode,
        expires_at=expires_at,
    )
    payload_hash = qr_domain.payload_hash_for(payload)
    signature = qr_domain.sign_payload(payload, secret=secret)

    row = qr_repo.create_qr_charge(
        session,
        id=charge_id,
        merchant_id=merchant.id,
        amount_minor=clean_amount,
        currency=clean_currency,
        concept=clean_concept,
        mode=clean_mode,
        payload_hash=payload_hash,
        signature=signature,
        expires_at=expires_at,
    )
    _record_charge_created_event(session, row)
    _audit_charge(session, row)
    logger.info("qr charge creado mode=%s charge_id=%s", clean_mode, row.id)
    _ = wallet_repo  # frontera: alta futura de billetera-comercio aqui (G69-b)
    qr_content = qr_domain.to_interop_token(payload, signature)
    return to_charge_view(row, payload=payload, qr_content=qr_content)


def verify_charge_signature(
    payload: dict[str, Any], signature: object, *, secret: str | None = None
) -> bool:
    """Verifica la firma de un payload (puro; lo usa HU15 al pagar)."""
    return qr_domain.verify_signature(payload, signature, secret=secret)


def is_charge_expired(charge: Any, now: datetime | None = None) -> bool:
    """Indica si el cargo expiro (`expires_at <= now`, borde exacto).

    Doble defensa on-read (`E4-T07`): un vencido es invalido aunque el
    job aun no lo haya marcado `EXPIRED`. Acepta la fila ORM o un
    dict con `expires_at` (`STATIC` con `NULL` nunca expira). La usan
    el endpoint de estado (`E4-T08`) y el pago (`HU15`) antes de
    operar. Sin PII (solo el `now` inyectable del llamante).
    """
    moment = now if isinstance(now, datetime) else _utcnow()
    return qr_domain.is_expired(charge, moment)


def is_charge_valid(charge: Any, now: datetime | None = None) -> bool:
    """Indica si el cargo es utilizable: `ACTIVE` y no expirado.

    Puerta on-read reutilizable (`E4-T08`/`HU15`): un `PAID`/
    `EXPIRED`/`CANCELLED` o un `ACTIVE` con `expires_at <= now` es
    invaluido sin tocar dinero (`docs/04#7.4`: el pago vencido se
    rechaza antes, sin retencion). `None` o estado desconocido ->
    `False` (fail-closed).
    """
    if charge is None:
        return False
    status = charge.get("status") if isinstance(charge, dict) else getattr(charge, "status", None)
    if str(status) != "ACTIVE":
        return False
    return not is_charge_expired(charge, now)


def require_merchant_for_user(session: Session, user_id: uuid.UUID | str) -> Any:
    """Cuenta de comercio `ACTIVE` del titular o `QrNotAuthorizedError`.

    El rol de comercio es la fila propia en
    `wallet.merchant_accounts` (`E4-T08`, decision 3 del brief): sin
    fila o con estado distinto de `ACTIVE` el titular no opera
    `/merchants/qr*` (-> `403 NOT_AUTHORIZED`, sin oraculo sobre
    comercios ajenos).
    """
    try:
        mid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise QrNotAuthorizedError("sin rol de comercio") from exc
    row = qr_repo.get_merchant_by_user(session, mid)
    if row is None or str(row.status) != "ACTIVE":
        raise QrNotAuthorizedError("sin rol de comercio")
    return row


def _canonical_qr_body(
    *, amount_minor: object, currency: object, concept: object, mode: object
) -> dict:
    """Cuerpo canonico del QR para el hash de idempotencia (sin PII)."""
    try:
        clean_mode = qr_domain._require_mode("DYNAMIC" if mode is None else mode)
        clean_currency = qr_domain._require_currency("PEN" if currency is None else currency)
        clean_concept = qr_domain._require_concept(concept)
        clean_amount = qr_domain._require_minor("amount_minor", amount_minor)
    except (TypeError, ValueError) as exc:
        raise QrValidationError(str(exc)) from exc
    body: dict[str, Any] = {
        "amount_minor": clean_amount,
        "currency": clean_currency,
        "mode": clean_mode,
    }
    if clean_concept is not None:
        body["concept"] = clean_concept
    return body


def _replay_charge_view(session: Session, merchant: Any, stored) -> dict[str, Any]:
    """Reconstruye la vista de creacion re-leyendo la fila (sin PII).

    El `payload` canonico es determinista: se reconstruye desde la
    fila con `domain.qr.build_payload` (mismo input, misma firma).
    """
    snapshot = stored.response_snapshot
    if not isinstance(snapshot, dict):
        raise QrIdempotencyConflictError("operacion en curso para esta Idempotency-Key")
    charge_id = snapshot.get("charge_id")
    if charge_id is None:
        raise QrIdempotencyConflictError("operacion en curso para esta Idempotency-Key")
    row = qr_repo.get_qr_charge(session, charge_id)
    if row is None or row.merchant_id != merchant.id:
        raise QrIdempotencyConflictError("cargo asociado a la clave ya no esta disponible")
    payload = qr_domain.build_payload(
        charge_id=row.id,
        merchant_id=row.merchant_id,
        amount_minor=int(row.amount_minor),
        currency=str(row.currency),
        concept=row.concept,
        mode=str(row.mode),
        expires_at=row.expires_at,
    )
    qr_content = qr_domain.to_interop_token(payload, str(row.signature))
    return to_charge_view(row, payload=payload, qr_content=qr_content)


def create_qr_charge_idempotent(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    body: dict,
    idempotency_key: str | None,
) -> tuple[dict[str, Any], bool]:
    """Genera el QR del comercio con `Idempotency-Key` opcional.

    Sin clave: genera directo (`replayed=False`). Con clave: misma
    clave + mismo cuerpo -> replay (`replayed=True`, un solo cargo);
    misma clave + distinto cuerpo -> `QrIdempotencyConflictError`
    (409). No mueve dinero (la generacion no contabiliza; HU15 paga).
    Solo `flush` (el endpoint confirma).
    """
    if not isinstance(body, dict):
        raise QrValidationError("cuerpo con formato invalido")
    merchant = require_merchant_for_user(session, user_id)
    canonical = _canonical_qr_body(
        amount_minor=body.get("amount_minor"),
        currency=body.get("currency", "PEN"),
        concept=body.get("concept"),
        mode=body.get("mode", "DYNAMIC"),
    )
    if idempotency_key is None:
        return (
            create_qr_charge(
                session,
                merchant_id=merchant.id,
                amount_minor=canonical["amount_minor"],
                currency=canonical["currency"],
                concept=canonical.get("concept"),
                mode=canonical["mode"],
            ),
            False,
        )
    from app.modules.shared.repository.idempotency import (
        compute_request_hash,
        find_key,
        is_expired,
        renew_expired,
        resolve_ttl_seconds,
        store_response,
        try_claim,
    )

    key = str(idempotency_key).strip()
    if not key:
        return (
            create_qr_charge(
                session,
                merchant_id=merchant.id,
                amount_minor=canonical["amount_minor"],
                currency=canonical["currency"],
                concept=canonical.get("concept"),
                mode=canonical["mode"],
            ),
            False,
        )
    if len(key) > 80:
        raise QrValidationError("Idempotency-Key supera 80 caracteres")
    digest = compute_request_hash({"merchant_id": str(merchant.id), **canonical})
    row, created = try_claim(
        session,
        key=key,
        user_id=merchant.user_id,
        endpoint=QR_IDEMPOTENCY_ENDPOINT,
        method="POST",
        request_hash=digest,
        ttl_seconds=resolve_ttl_seconds(),
    )
    if created:
        view = create_qr_charge(
            session,
            merchant_id=merchant.id,
            amount_minor=canonical["amount_minor"],
            currency=canonical["currency"],
            concept=canonical.get("concept"),
            mode=canonical["mode"],
        )
        store_response(
            session,
            row,
            response_snapshot={"charge_id": view["charge_id"]},
            transaction_id=view["charge_id"],
        )
        return view, False
    stored = find_key(session, key=key, user_id=merchant.user_id)
    if stored is None:  # pragma: no cover - carrera extrema
        raise QrIdempotencyConflictError("clave de idempotencia en conflicto")
    if is_expired(stored):
        renew_expired(
            session,
            stored,
            request_hash=digest,
            endpoint=QR_IDEMPOTENCY_ENDPOINT,
            method="POST",
        )
        view = create_qr_charge(
            session,
            merchant_id=merchant.id,
            amount_minor=canonical["amount_minor"],
            currency=canonical["currency"],
            concept=canonical.get("concept"),
            mode=canonical["mode"],
        )
        store_response(
            session,
            stored,
            response_snapshot={"charge_id": view["charge_id"]},
            transaction_id=view["charge_id"],
        )
        return view, False
    if stored.request_hash != digest:
        raise QrIdempotencyConflictError("Idempotency-Key ya usada con un cuerpo distinto")
    return _replay_charge_view(session, merchant, stored), True


def get_charge_status(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    charge_id: uuid.UUID | str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Estado del cargo del comercio dueno con caducidad on-read.

    Solo el comercio dueno: cargo inexistente, ajeno o con id
    malformado -> `QrNotFoundError` neutro (`404`, sin montos, sin
    oraculo). Sin rol de comercio -> `QrNotAuthorizedError` (`403`).
    El `status` efectivo recalcula la caducidad (`E4-T07`): un
    `ACTIVE` con `expires_at <= now` se reporta `EXPIRED` aunque el
    job aun no lo haya marcado (solo lectura, sin mutar la fila).
    """
    merchant = require_merchant_for_user(session, user_id)
    row = qr_repo.get_qr_charge(session, charge_id)
    if row is None or row.merchant_id != merchant.id:
        raise QrNotFoundError("cargo inexistente")
    status = str(row.status)
    if status == "ACTIVE" and is_charge_expired(row, now):
        status = "EXPIRED"
    payload = qr_domain.build_payload(
        charge_id=row.id,
        merchant_id=row.merchant_id,
        amount_minor=int(row.amount_minor),
        currency=str(row.currency),
        concept=row.concept,
        mode=str(row.mode),
        expires_at=row.expires_at,
    )
    return {
        "charge_id": str(row.id),
        "qr_content": qr_domain.to_interop_token(payload, str(row.signature)),
        "status": status,
        "amount_minor": int(row.amount_minor),
        "currency": str(row.currency),
        "expires_at": row.expires_at,
        "paid_at": row.paid_at,
    }


#: Endpoint logico para la idempotencia del alta del QR personal
#: (clave `shared`, UQ por usuario; no mueve dinero).
QR_PERSONAL_IDEMPOTENCY_ENDPOINT = "/wallet/qr"


def resolve_holder_settlement_account(
    session: Session, owner_user_id: uuid.UUID | str, currency: str
) -> uuid.UUID:
    """Cuenta de abono del titular por la fachada `accounts` (E4-T21).

    Primera cuenta `ACTIVE` del titular con la moneda pedida, en orden
    de creacion (determinista). Sin cuenta compatible ->
    `QrMerchantNotFoundError` neutro (mismo codigo que el comercio sin
    abono; el numero en claro jamas se loguea). Solo lectura (sin
    `flush`/`commit`). La consume el QR personal y, por delegacion,
    `resolve_merchant_settlement_account` de `HU15`.
    """
    from app.modules.accounts.service import list_active_by_user

    try:
        owner = (
            owner_user_id if isinstance(owner_user_id, uuid.UUID) else uuid.UUID(str(owner_user_id))
        )
    except (ValueError, AttributeError, TypeError) as exc:
        raise QrMerchantNotFoundError("titular sin cuenta de abono") from exc
    wanted = str(currency or "").strip().upper()
    try:
        candidates = list_active_by_user(session, owner)
    except (ValueError, AttributeError, TypeError) as exc:
        raise QrMerchantNotFoundError("titular sin cuenta de abono") from exc
    for view in candidates:
        if str(view.get("currency") or "").upper() == wanted:
            account_id = view.get("account_id")
            if isinstance(account_id, uuid.UUID):
                return account_id
            try:
                return uuid.UUID(str(account_id))
            except (ValueError, AttributeError, TypeError):
                continue
    raise QrMerchantNotFoundError("titular sin cuenta de abono")


def create_personal_qr_charge(
    session: Session,
    *,
    owner_user_id: uuid.UUID | str,
    currency: str = "PEN",
    concept: str | None = None,
    now: datetime | None = None,
    secret: str | None = None,
) -> dict[str, Any]:
    """Genera el QR personal estatico del titular y lo persiste `ACTIVE`.

    Cualquier titular autenticado con al menos una cuenta `ACTIVE`
    (sin rol comercial): QR `STATIC` sin monto (`amount_minor = NULL`;
    el pagador lo digita) y sin caducidad (`expires_at = NULL`;
    reutilizable). El abono se fija a la cuenta `ACTIVE` del titular
    con la moneda pedida (fachada `accounts`; sin cuenta compatible
    -> `QrMerchantNotFoundError` neutro antes de persistir).

    La vista incluye `qr_content` (`str`): el sobre canonico `BO-QR1`
    con `amount_minor = 0` ("lo define el pagador"). Solo `flush` (el
    llamante confirma). **No mueve dinero**.
    """
    owner = _coerce_uuid(owner_user_id, "owner_user_id")
    try:
        clean_currency = qr_domain._require_currency("PEN" if currency is None else currency)
        clean_concept = qr_domain._require_concept(concept)
    except (TypeError, ValueError) as exc:
        raise QrValidationError(str(exc)) from exc
    try:
        target_account_id = resolve_holder_settlement_account(session, owner, clean_currency)
    except QrMerchantNotFoundError as exc:
        raise QrValidationError("titular sin cuenta de abono") from exc

    charge_id = uuid.uuid4()
    payload = qr_domain.build_payload(
        charge_id=charge_id,
        owner_user_id=owner,
        amount_minor=None,
        currency=clean_currency,
        concept=clean_concept,
        mode="STATIC",
        expires_at=None,
    )
    payload_hash = qr_domain.payload_hash_for(payload)
    signature = qr_domain.sign_payload(payload, secret=secret)

    row = qr_repo.create_qr_charge(
        session,
        id=charge_id,
        merchant_id=None,
        owner_user_id=owner,
        payee_account_id=target_account_id,
        payee_type="PERSONAL",
        reusable=True,
        amount_minor=None,
        currency=clean_currency,
        concept=clean_concept,
        mode="STATIC",
        payload_hash=payload_hash,
        signature=signature,
        expires_at=None,
    )
    _record_charge_created_event(session, row)
    _audit_personal_charge(session, row)
    logger.info("qr personal creado charge_id=%s", row.id)
    qr_content = qr_domain.to_interop_token(payload, signature)
    return to_charge_view(row, payload=payload, qr_content=qr_content)


def _audit_personal_charge(session: Session, row: Any) -> None:
    """Auditoria best-effort del QR personal (sin PII ni cifras)."""
    try:
        from app.modules.audit.service import record as audit_record

        audit_record(
            session,
            actor=row.owner_user_id,
            action=AUDIT_ACTION,
            entity="qr_charge",
            entity_id=row.id,
            metadata={
                "charge_id": str(row.id),
                "payee_type": "PERSONAL",
                "mode": str(row.mode),
                "status": str(row.status),
            },
        )
    except Exception as exc:  # noqa: BLE001 - la auditoria no bloquea la generacion
        logger.warning("qr personal audit best-effort omitida err=%s", type(exc).__name__)


def _canonical_personal_body(*, currency: object, concept: object) -> dict:
    """Cuerpo canonico del QR personal para el hash de idempotencia (sin PII)."""
    try:
        clean_currency = qr_domain._require_currency("PEN" if currency is None else currency)
        clean_concept = qr_domain._require_concept(concept)
    except (TypeError, ValueError) as exc:
        raise QrValidationError(str(exc)) from exc
    body: dict[str, Any] = {"currency": clean_currency, "payee_type": "PERSONAL"}
    if clean_concept is not None:
        body["concept"] = clean_concept
    return body


def _replay_personal_charge_view(session: Session, owner: uuid.UUID, stored) -> dict[str, Any]:
    """Reconstruye la vista personal re-leyendo la fila (sin PII)."""
    snapshot = stored.response_snapshot
    if not isinstance(snapshot, dict):
        raise QrIdempotencyConflictError("operacion en curso para esta Idempotency-Key")
    charge_id = snapshot.get("charge_id")
    if charge_id is None:
        raise QrIdempotencyConflictError("operacion en curso para esta Idempotency-Key")
    row = qr_repo.get_qr_charge(session, charge_id)
    if row is None or row.owner_user_id != owner:
        raise QrIdempotencyConflictError("cargo asociado a la clave ya no esta disponible")
    payload = qr_domain.build_payload(
        charge_id=row.id,
        owner_user_id=row.owner_user_id,
        amount_minor=None,
        currency=str(row.currency),
        concept=row.concept,
        mode="STATIC",
        expires_at=None,
    )
    qr_content = qr_domain.to_interop_token(payload, str(row.signature))
    return to_charge_view(row, payload=payload, qr_content=qr_content)


def create_personal_qr_charge_idempotent(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    body: dict,
    idempotency_key: str | None,
) -> tuple[dict[str, Any], bool]:
    """Genera el QR personal con `Idempotency-Key` opcional.

    Sin clave: genera directo (`replayed=False`). Con clave: misma
    clave + mismo cuerpo -> replay (`replayed=True`, un solo cargo);
    misma clave + distinto cuerpo -> `QrIdempotencyConflictError`
    (409). No mueve dinero. Solo `flush` (el endpoint confirma).
    """
    if not isinstance(body, dict):
        raise QrValidationError("cuerpo con formato invalido")
    owner = _coerce_uuid(user_id, "user_id")
    canonical = _canonical_personal_body(
        currency=body.get("currency", "PEN"),
        concept=body.get("concept"),
    )
    if idempotency_key is None or not str(idempotency_key).strip():
        return (
            create_personal_qr_charge(
                session,
                owner_user_id=owner,
                currency=canonical["currency"],
                concept=canonical.get("concept"),
            ),
            False,
        )
    from app.modules.shared.repository.idempotency import (
        compute_request_hash,
        find_key,
        is_expired,
        renew_expired,
        resolve_ttl_seconds,
        store_response,
        try_claim,
    )

    key = str(idempotency_key).strip()
    if len(key) > 80:
        raise QrValidationError("Idempotency-Key supera 80 caracteres")
    digest = compute_request_hash({"owner_user_id": str(owner), **canonical})
    row, created = try_claim(
        session,
        key=key,
        user_id=owner,
        endpoint=QR_PERSONAL_IDEMPOTENCY_ENDPOINT,
        method="POST",
        request_hash=digest,
        ttl_seconds=resolve_ttl_seconds(),
    )

    def _generate() -> dict[str, Any]:
        return create_personal_qr_charge(
            session,
            owner_user_id=owner,
            currency=canonical["currency"],
            concept=canonical.get("concept"),
        )

    if created:
        view = _generate()
        store_response(
            session,
            row,
            response_snapshot={"charge_id": view["charge_id"]},
            transaction_id=view["charge_id"],
        )
        return view, False
    stored = find_key(session, key=key, user_id=owner)
    if stored is None:  # pragma: no cover - carrera extrema
        raise QrIdempotencyConflictError("clave de idempotencia en conflicto")
    if is_expired(stored):
        renew_expired(
            session,
            stored,
            request_hash=digest,
            endpoint=QR_PERSONAL_IDEMPOTENCY_ENDPOINT,
            method="POST",
        )
        view = _generate()
        store_response(
            session,
            stored,
            response_snapshot={"charge_id": view["charge_id"]},
            transaction_id=view["charge_id"],
        )
        return view, False
    if stored.request_hash != digest:
        raise QrIdempotencyConflictError("Idempotency-Key ya usada con un cuerpo distinto")
    return _replay_personal_charge_view(session, owner, stored), True


def get_personal_charge_status(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    charge_id: uuid.UUID | str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Estado del QR personal del titular dueno (E4-T21).

    Solo el titular dueno de un cargo `PERSONAL`: inexistente, ajeno,
    de comercio o con id malformado -> `QrNotFoundError` neutro
    (`404`, sin montos, sin oraculo). El personal no caduca
    (`expires_at = NULL`) ni se agota (reutilizable): el estado es el
    de la fila.
    """
    _ = now  # sin caducidad: se acepta por simetria con el comercio
    owner = _coerce_uuid(user_id, "user_id")
    row = qr_repo.get_qr_charge(session, charge_id)
    if (
        row is None
        or str(getattr(row, "payee_type", "MERCHANT")) != "PERSONAL"
        or row.owner_user_id != owner
    ):
        raise QrNotFoundError("cargo inexistente")
    payload = qr_domain.build_payload(
        charge_id=row.id,
        owner_user_id=row.owner_user_id,
        amount_minor=None,
        currency=str(row.currency),
        concept=row.concept,
        mode="STATIC",
        expires_at=None,
    )
    return {
        "charge_id": str(row.id),
        "qr_content": qr_domain.to_interop_token(payload, str(row.signature)),
        "status": str(row.status),
        "amount_minor": None,
        "currency": str(row.currency),
        "expires_at": None,
        "paid_at": row.paid_at,
        "payee_type": "PERSONAL",
        "reusable": True,
    }


def notify_merchant_charge_paid(
    session: Session,
    *,
    merchant_user_id: uuid.UUID | str,
    charge_id: uuid.UUID | str,
    sender: Any | None = None,
) -> Any:
    """Avisa al comercio el abono del QR por `notifications.send`.

    Ruta del aviso de abono (HU14 CA-04, `docs/18` G72): plantilla
    `qr_charge_paid` (push, sin PII ni cifras del pagador: solo la
    referencia del cargo y el instante; el detalle vive en el panel
    del comercio). El disparador real es el pago de `HU15` (evento
    `qr.payment.confirmed`); aqui queda definida la plantilla + la
    ruta, probada con pago simulado en `E4-T10` (decision 2 del
    brief). Solo `flush` via la fachada (el llamante confirma).
    """
    from app.modules.notifications import service as notifications_service
    from app.modules.notifications.service import sender_for_channel

    moment = _utcnow()
    ref = str(charge_id)
    sender_obj = sender if sender is not None else sender_for_channel("push")
    return notifications_service.send(
        session,
        channel="push",
        recipient=str(merchant_user_id),
        template_code="qr_charge_paid",
        data={"charge_ref": ref[:8], "at": moment.isoformat()},
        user_id=merchant_user_id,
        sender=sender_obj,
    )


def notify_personal_charge_paid(
    session: Session,
    *,
    owner_user_id: uuid.UUID | str,
    charge_id: uuid.UUID | str,
    sender: Any | None = None,
) -> Any:
    """Avisa al titular el abono de su QR personal (E4-T21, HU14 CA-04).

    Reutiliza la plantilla `qr_charge_paid` (push, sin PII ni cifras:
    solo la referencia del cargo y el instante) con destinatario = el
    titular personal (decision del dueno). El disparador es el pago
    (`qr.payment.confirmed`); best-effort via la fachada
    `notifications.send` (el llamante confirma).
    """
    from app.modules.notifications import service as notifications_service
    from app.modules.notifications.service import sender_for_channel

    moment = _utcnow()
    ref = str(charge_id)
    sender_obj = sender if sender is not None else sender_for_channel("push")
    return notifications_service.send(
        session,
        channel="push",
        recipient=str(owner_user_id),
        template_code="qr_charge_paid",
        data={"charge_ref": ref[:8], "at": moment.isoformat()},
        user_id=owner_user_id,
        sender=sender_obj,
    )


__all__ = [
    "AUDIT_ACTION",
    "QR_IDEMPOTENCY_ENDPOINT",
    "QR_PERSONAL_IDEMPOTENCY_ENDPOINT",
    "QrError",
    "QrIdempotencyConflictError",
    "QrMerchantNotFoundError",
    "QrNotAuthorizedError",
    "QrNotFoundError",
    "QrValidationError",
    "create_personal_qr_charge",
    "create_personal_qr_charge_idempotent",
    "create_qr_charge",
    "create_qr_charge_idempotent",
    "get_charge_status",
    "get_personal_charge_status",
    "get_qr_ttl_seconds",
    "is_charge_expired",
    "is_charge_valid",
    "notify_merchant_charge_paid",
    "notify_personal_charge_paid",
    "require_merchant_for_user",
    "resolve_holder_settlement_account",
    "to_charge_view",
    "verify_charge_signature",
]
