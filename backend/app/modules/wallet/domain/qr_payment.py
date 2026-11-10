"""Dominio puro del cobro QR (E4-T11, HU15 CA-01/CA-04).

Verificacion de integridad del `qr_content` (sobre canonico `BO-QR1`,
adaptador `E4-T03`) + caducidad/estado del cargo, ANTES de operar
(`docs/04#7.4`: vencido/corrupto/manipulado se rechaza sin retencion,
sin descontar fondos).

Puro y determinista: sin BD, sin red, sin reloj (el `now` se inyecta).
Dinero entero en centimos; nunca `float` (regla de oro 3). Sin PII ni
secretos en logs (regla 7): aqui no se loguea el secreto, el contenido
firmable ni referencias en claro (solo longitudes/tipos/decisiones).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class QrPaymentError(ValueError):
    """Error base del cobro QR (mapeable a 4xx en `E4-T12`)."""

    code = "QR_PAYMENT_ERROR"


class InvalidQrError(QrPaymentError):
    """QR corrupto, manipulado o ajeno (-> `VALIDATION_ERROR`, 422).

    Neutro y sin oraculo: decodificacion imposible, cargo inexistente,
    `payload_hash`/firma/envelope que no coinciden o estado terminal
    distinto de `PAID`/`EXPIRED` colapsan aqui con el mismo mensaje.
    """

    code = "VALIDATION_ERROR"


class QrExpiredError(QrPaymentError):
    """QR vencido (`EXPIRED` o `ACTIVE` con `expires_at <= now`, 422).

    Se rechaza antes de operar: sin retencion, sin asiento, sin
    descuento (`docs/04#7.4`).
    """

    code = "VALIDATION_ERROR"


class QrAlreadyPaidError(QrPaymentError):
    """El cargo ya fue cobrado (-> `CONFLICT`, 409, un solo asiento)."""

    code = "CONFLICT"


class QrMerchantUnknownError(QrPaymentError):
    """El comercio no tiene cuenta de abono `ACTIVE` (422, sin oraculo)."""

    code = "VALIDATION_ERROR"


class QrPaymentConflictError(QrPaymentError):
    """Misma `Idempotency-Key` con distinto cuerpo (-> `CONFLICT`, 409)."""

    code = "CONFLICT"


class QrPaymentNotFoundError(QrPaymentError, LookupError):
    """El pago no existe o es de otro titular (neutro, `NOT_FOUND`)."""

    code = "NOT_FOUND"


def decode_qr_content(qr_content: object) -> Any:
    """Decodifica el sobre `BO-QR1` via el adaptador (`E4-T03`).

    Puro (sin red): cualquier manipulacion (base64/JSON roto, version
    distinta, campos ausentes) -> `InvalidQrError` neutro (CA-04).
    """
    from app.adapters import wallet_interop as interop

    if not isinstance(qr_content, str) or not qr_content.strip():
        raise InvalidQrError("QR con formato invalido")
    try:
        envelope = interop.decode_payload(qr_content.strip())
    except interop.WalletInteropError as exc:
        logger.debug("qr decode invalido code=%s", exc.code)
        raise InvalidQrError("QR con formato invalido") from None
    except (TypeError, ValueError) as exc:
        logger.debug("qr decode invalido err=%s", type(exc).__name__)
        raise InvalidQrError("QR con formato invalido") from None
    return envelope


def is_personal_charge(charge: Any) -> bool:
    """Indica si el cargo es un QR personal (destino `PERSONAL`, E4-T21).

    Acepta la fila ORM o un dict con las columnas de `qr_charges`
    (por compatibilidad tambien vale `merchant_id` nulo con
    `owner_user_id` presente).
    """
    if isinstance(charge, dict):
        payee = charge.get("payee_type")
        if payee is not None:
            return str(payee) == "PERSONAL"
        return charge.get("merchant_id") is None and charge.get("owner_user_id") is not None
    payee = getattr(charge, "payee_type", None)
    if payee is not None:
        return str(payee) == "PERSONAL"
    return getattr(charge, "merchant_id", None) is None and bool(
        getattr(charge, "owner_user_id", None)
    )


def expected_payload_for_charge(charge: Any) -> dict[str, Any]:
    """Reconstruye el payload canonico esperado desde la fila del cargo.

    Determinista: mismo cargo -> mismo payload -> misma firma. Acepta
    la fila ORM o un dict con las columnas de `qr_charges`. Un QR
    personal (E4-T21) reconstruye el payload sin monto (`amount_minor
    = None`, viaja como `0` en el sobre) con `owner_user_id`.
    """
    from app.modules.wallet.domain import qr as qr_domain

    if isinstance(charge, dict):
        charge_id = charge.get("id", charge.get("charge_id"))
        merchant_id = charge.get("merchant_id")
        owner_user_id = charge.get("owner_user_id")
        amount_minor = charge.get("amount_minor")
        currency = charge.get("currency")
        concept = charge.get("concept")
        mode = charge.get("mode", "DYNAMIC")
        expires_at = charge.get("expires_at")
    else:
        charge_id = getattr(charge, "id", None)
        merchant_id = getattr(charge, "merchant_id", None)
        owner_user_id = getattr(charge, "owner_user_id", None)
        amount_minor = getattr(charge, "amount_minor", None)
        currency = getattr(charge, "currency", None)
        concept = getattr(charge, "concept", None)
        mode = getattr(charge, "mode", "DYNAMIC")
        expires_at = getattr(charge, "expires_at", None)
    try:
        if is_personal_charge(charge):
            return qr_domain.build_payload(
                charge_id=charge_id,
                owner_user_id=owner_user_id,
                amount_minor=None,
                currency=currency,
                concept=concept,
                mode="STATIC",
                expires_at=None,
            )
        return qr_domain.build_payload(
            charge_id=charge_id,
            merchant_id=merchant_id,
            amount_minor=amount_minor,
            currency=currency,
            concept=concept,
            mode=mode,
            expires_at=expires_at,
        )
    except (TypeError, ValueError):
        raise InvalidQrError("QR con formato invalido") from None


def verify_charge_integrity(
    charge: Any, envelope: Any, *, secret: str | None = None
) -> dict[str, Any]:
    """Verifica firma/hash/envelope del cargo contra el sobre escaneado.

    Comprueba (fail-closed, todo neutro -> `InvalidQrError`):
    1. `envelope.ref` es el `charge_id` del cargo (QR ajeno -> rechazo);
    2. `payload_hash` coincide con el SHA-256 del payload canonico;
    3. la firma HMAC-SHA256 es valida (`verify_charge_signature`);
    4. el sobre transporta lo firmado (`amount_minor`, `currency`,
       `exp` y `sig` iguales a los del cargo).

    Retorna el payload canonico verificado. La firma alterada o el
    monto/moneda manipulados se rechazan sin tocar dinero (CA-04).
    """
    from app.modules.wallet.domain import qr as qr_domain

    payload = expected_payload_for_charge(charge)
    charge_id = str(payload.get("charge_id") or "")
    if str(getattr(envelope, "ref", "") or "") != charge_id:
        logger.debug("qr integrity ref distinto")
        raise InvalidQrError("QR con formato invalido")
    stored_hash = (
        charge.get("payload_hash")
        if isinstance(charge, dict)
        else getattr(charge, "payload_hash", None)
    )
    stored_signature = (
        charge.get("signature") if isinstance(charge, dict) else getattr(charge, "signature", None)
    )
    if qr_domain.payload_hash_for(payload) != stored_hash:
        logger.debug("qr integrity hash distinto")
        raise InvalidQrError("QR con formato invalido")
    if not qr_domain.verify_signature(payload, stored_signature, secret=secret):
        logger.debug("qr integrity firma invalida")
        raise InvalidQrError("QR con formato invalido")
    if (
        int(getattr(envelope, "amount_minor", -1)) != int(payload.get("amount_minor") or 0)
        or str(getattr(envelope, "currency", "") or "").upper()
        != str(payload.get("currency") or "").upper()
        or int(getattr(envelope, "expires_at", -1)) != int(payload.get("exp") or 0)
        or str(getattr(envelope, "sig", "") or "") != str(stored_signature or "")
    ):
        logger.debug("qr integrity envelope distinto")
        raise InvalidQrError("QR con formato invalido")
    logger.debug("qr integrity ok bytes=%d", len(qr_domain.canonical_bytes(payload)))
    return payload


def ensure_charge_payable(charge: Any, now: datetime | None = None) -> None:
    """Exige cargo cobrable: `ACTIVE` y no vencido (fail-closed).

    - `PAID` -> `QrAlreadyPaidError` (409, un solo asiento);
    - `EXPIRED` o `ACTIVE` con `expires_at <= now` -> `QrExpiredError`
      (422, sin retencion);
    - `CANCELLED`/otro estado/`None` -> `InvalidQrError` (422).
    """
    from datetime import UTC

    from app.modules.wallet.domain import qr as qr_domain

    if charge is None:
        raise InvalidQrError("QR con formato invalido")
    status = charge.get("status") if isinstance(charge, dict) else getattr(charge, "status", None)
    if str(status) == "PAID":
        raise QrAlreadyPaidError("el QR ya fue cobrado")
    if str(status) == "EXPIRED":
        raise QrExpiredError("el QR expiro")
    if str(status) != "ACTIVE":
        raise InvalidQrError("QR con formato invalido")
    moment = now if isinstance(now, datetime) else datetime.now(UTC)
    if qr_domain.is_expired(charge, moment):
        raise QrExpiredError("el QR expiro")


__all__ = [
    "InvalidQrError",
    "QrAlreadyPaidError",
    "QrExpiredError",
    "QrMerchantUnknownError",
    "QrPaymentConflictError",
    "QrPaymentError",
    "QrPaymentNotFoundError",
    "decode_qr_content",
    "ensure_charge_payable",
    "expected_payload_for_charge",
    "is_personal_charge",
    "verify_charge_integrity",
]
