"""Casos de uso de billetera (E4-T01, HU13 CA-01).

- `get_wallet_view`: billetera + vinculos enmascarados del titular
  (solo lectura; si aun no tiene billetera, vista vacia `NONE`) +
  rol de comercio (`is_merchant`/`merchant_status`, E4-T23).
- `link_source_idempotent`: vincula una cuenta (validada por la
  fachada de `accounts`) o una tarjeta (tokenizada server-side,
  crudo descartado), con `Idempotency-Key` opcional (patron E2-T13).
- `create_qr_charge` (E4-T06, HU14): genera el QR firmado del
  comercio (payload canonico + HMAC + `expires_at` desde
  `wallet.qr_ttl_seconds`), persiste el cargo `ACTIVE` y emite
  `qr.charge.created` por `outbox`. No mueve dinero (el pago es
  HU15); sin endpoint publico (el router es `E4-T08`).

Frontera (regla de oro 4): `wallet` no importa modelos ni repositorios
de `accounts`; solo la fachada `accounts.service.get_account`
(import perezoso, sin ciclo). El `account_number` en claro de la
fachada es de uso interno (solo se deriva `last4` y se descarta;
jamas en logs ni respuestas, reglas 7/9).

Sin PII/secretos en logs: aqui no se loguea PAN, `token_ref` completo
ni numeros (solo `id`s, `type` y `replayed`).

Convencion: `flush` sin `commit`; el endpoint confirma. Eventos
(regla de oro 8): `wallet.link.created` se registra en el outbox en
la misma transaccion (`flush`, sin `commit`; payload sin PII).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.modules.wallet import repository as wallet_repo
from app.modules.wallet.events import WALLET_LINK_CREATED
from app.modules.wallet.service import tokenization as tok_facade
from app.modules.wallet.service.express import (
    ExpressConflictError,
    ExpressError,
    ExpressNotFoundError,
    ExpressValidationError,
    get_wallet_daily_limit_minor,
    get_wallet_express_limit_minor,
    pay_express,
    wallet_daily_limit_reader,
    wallet_threshold_reader,
)
from app.modules.wallet.service.qr import (
    QR_IDEMPOTENCY_ENDPOINT,
    QR_PERSONAL_IDEMPOTENCY_ENDPOINT,
    QrError,
    QrIdempotencyConflictError,
    QrMerchantNotFoundError,
    QrNotAuthorizedError,
    QrNotFoundError,
    QrValidationError,
    create_personal_qr_charge,
    create_personal_qr_charge_idempotent,
    create_qr_charge,
    create_qr_charge_idempotent,
    get_charge_status,
    get_personal_charge_status,
    get_qr_ttl_seconds,
    is_charge_expired,
    is_charge_valid,
    notify_merchant_charge_paid,
    notify_personal_charge_paid,
    require_merchant_for_user,
    resolve_holder_settlement_account,
    to_charge_view,
    verify_charge_signature,
)
from app.modules.wallet.service.qr_payment import (
    QR_PAYMENT_IDEMPOTENCY_ENDPOINT,
    InvalidQrError,
    QrAlreadyPaidError,
    QrExpiredError,
    QrMerchantUnknownError,
    QrPaymentConflictError,
    QrPaymentError,
    QrPaymentNotFoundError,
    finalize_qr_payment,
    pay_qr_charge,
    preview_qr_charge,
    resolve_merchant_settlement_account,
    resolve_personal_settlement_account,
)
from app.modules.wallet.service.services import (
    SERVICE_PAYMENT_IDEMPOTENCY_ENDPOINT,
    SETTLEMENT_ACCOUNT_ENV_VAR,
    TOPUP_IDEMPOTENCY_ENDPOINT,
    ServiceBillerNotFoundError,
    ServiceCatalogError,
    ServicePaymentConflictError,
    ServicePaymentError,
    ServiceProviderDownError,
    ServiceProviderGatewayTimeoutError,
    ServiceSettlementUnavailableError,
    TopupOperatorNotFoundError,
    build_receipt_no,
    finalize_service_payment,
    finalize_topup,
    list_catalog,
    pay_service,
    pay_topup,
    register_subscription,
    resolve_settlement_account,
    resolve_topup_operator,
    to_topup_receipt,
)

logger = logging.getLogger(__name__)

#: Endpoint logico para la idempotencia del alta (clave `shared`, UQ por usuario).
IDEMPOTENCY_ENDPOINT = "/wallet/links"

LINK_TYPES = ("ACCOUNT", "CARD")


class WalletAccountNotFoundError(LookupError):
    """La cuenta a enlazar no existe, es ajena o no esta ACTIVE (-> 404 neutro)."""


class WalletLinkNotFoundError(LookupError):
    """El vinculo no existe, es ajeno o ya esta dado de baja (-> 404 neutro)."""


class IdempotencyConflictError(ValueError):
    """Misma `Idempotency-Key` con distinto cuerpo (-> 409)."""


def _coerce_user(user_id: uuid.UUID | str) -> uuid.UUID:
    if isinstance(user_id, uuid.UUID):
        return user_id
    try:
        return uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"user_id debe ser UUID, recibido: {user_id!r}") from exc


def to_link_view(row) -> dict:
    """Vista enmascarada del vinculo (el PAN/numero nunca sale)."""
    view: dict = {
        "id": str(row.id),
        "type": "ACCOUNT" if row.account_id is not None else "CARD",
        "status": row.status,
        "linked_at": row.linked_at,
    }
    if row.account_id is not None:
        view["account_masked"] = f"****{row.last4}" if row.last4 else "****"
    else:
        if row.last4:
            view["last4"] = row.last4
        if row.brand:
            view["brand"] = row.brand
    return view


def _merchant_flag(session: Session, owner: uuid.UUID) -> tuple[bool, str | None]:
    """Rol de comercio del titular (E4-T23, solo lectura, sin PII).

    `True` solo con fila propia `ACTIVE` en `wallet.merchant_accounts`
    (mismo criterio que `require_merchant_for_user`); cualquier otro
    caso (`None`, otro estado, error de lectura) -> `(False, ...)`.
    `merchant_status` es el estado de la fila propia o `None` si no hay
    fila. Nunca expone RUC ni nombre comercial (solo rol/estado).
    La lectura es best-effort con fail-closed: si la tabla no existe
    aun (p. ej. BD anterior a `0039`) se responde no-comercio en vez de
    tumbar `GET /wallet` (el `403` real de `/merchants/qr*` sigue
    intacto; un fallo real de conexion lo levanta igual la consulta
    posterior de la billetera). Corre en `savepoint` para no alterar la
    transaccion externa ante un fallo del motor.
    """
    from sqlalchemy.exc import DBAPIError

    from app.modules.wallet.repository import qr as qr_repo

    try:
        with session.begin_nested():
            row = qr_repo.get_merchant_by_user(session, owner)
    except (DBAPIError, ValueError, TypeError, AttributeError):
        logger.debug("rol de comercio no resoluble user_id=%s", owner)
        return False, None
    if row is None:
        return False, None
    status = str(getattr(row, "status", "") or "")
    if status != "ACTIVE":
        return False, status or None
    return True, status


def get_wallet_view(session: Session, user_id: uuid.UUID | str) -> dict:
    """Billetera del titular con vinculos enmascarados (solo lectura).

    Incluye el rol de comercio (`is_merchant`/`merchant_status`, E4-T23)
    para que el cliente oculte la entrada de comercio a las cuentas
    personales (el backend sigue exigiendo el rol con `403` en
    `/merchants/qr*`; la UI oculta por UX, no por seguridad).
    """
    owner = _coerce_user(user_id)
    is_merchant, merchant_status = _merchant_flag(session, owner)
    wallet = wallet_repo.get_wallet_by_user(session, owner)
    if wallet is None:
        return {
            "wallet_id": None,
            "status": "NONE",
            "links": [],
            "is_merchant": is_merchant,
            "merchant_status": merchant_status,
        }
    return {
        "wallet_id": str(wallet.id),
        "status": wallet.status,
        "links": [to_link_view(row) for row in wallet_repo.list_links(session, wallet.id)],
        "is_merchant": is_merchant,
        "merchant_status": merchant_status,
    }


def _resolve_account_source(
    session: Session, owner: uuid.UUID, account_id: object
) -> tuple[str, str | None, None]:
    """Valida la cuenta por fachada y deriva (`token_ref`, `last4`, `brand`).

    La cuenta debe existir, ser del titular y estar `ACTIVE`; cualquier
    otro caso colapsa al mismo error neutro (sin oraculo). El numero en
    claro de la fachada solo aporta sus ultimos 4 y se descarta.
    """
    try:
        target = uuid.UUID(str(account_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"account_id debe ser UUID, recibido: {account_id!r}") from exc
    from app.modules.accounts.service import get_account as accounts_get_account

    try:
        view = accounts_get_account(session, target)
    except ValueError as exc:
        raise ValueError(f"account_id debe ser UUID, recibido: {account_id!r}") from exc
    if view is None or view.get("user_id") != owner or str(view.get("status")) != "ACTIVE":
        raise WalletAccountNotFoundError("cuenta no disponible para vincular")
    number = str(view.get("account_number") or "")
    last4 = number[-4:] if len(number) >= 4 else None
    return f"acct:{target}", last4, None


def _resolve_card_source(
    *, pan: object = None, token_ref: object = None, last4: object = None, brand: object = None
) -> tuple[str, str | None, str | None]:
    """Resuelve una tarjeta: tokeniza el PAN server-side o acepta un
    `token_ref` pre-tokenizado. El crudo nunca se persiste ni se loguea."""
    has_pan = pan is not None
    has_token = token_ref is not None
    if has_pan == has_token:
        raise ValueError("card exige exactamente uno de `pan` / `token_ref`")
    if has_pan:
        token = tok_facade.get_tokenization_provider().tokenize_card(str(pan))
        return token.token_ref, token.last4, token.brand
    ref = str(token_ref).strip() if isinstance(token_ref, str) else ""
    if not ref or len(ref) > 120:
        raise ValueError("card.token_ref con formato invalido")
    clean_last4: str | None = None
    if last4 is not None:
        if not isinstance(last4, str) or len(last4.strip()) != 4:
            raise ValueError("card.last4 debe tener 4 caracteres")
        clean_last4 = last4.strip()
    clean_brand: str | None = None
    if brand is not None:
        if not isinstance(brand, str) or not brand.strip() or len(brand.strip()) > 20:
            raise ValueError("card.brand con formato invalido")
        clean_brand = brand.strip().upper()
    return ref, clean_last4, clean_brand


def _record_link_created_event(session: Session, wallet_id: uuid.UUID, row) -> None:
    """Registra `wallet.link.created` en el outbox (payload sin PII)."""
    from app.modules.shared.repository.outbox import insert_event

    insert_event(
        session,
        aggregate_type="wallet_link",
        aggregate_id=row.id,
        event_type=WALLET_LINK_CREATED,
        payload={
            "wallet_id": str(wallet_id),
            "link_id": str(row.id),
            "type": "ACCOUNT" if row.account_id is not None else "CARD",
        },
    )


def link_source(
    session: Session,
    user_id: uuid.UUID | str,
    *,
    type: str,
    account_id: object = None,
    card: dict | None = None,
) -> dict:
    """Vincula la fuente de fondos (crea la billetera on-demand).

    `type=ACCOUNT` exige `account_id` (y prohibe `card`); `type=CARD`
    exige `card` (y prohibe `account_id`). Repetir una fuente `ACTIVE`
    -> `DuplicateLinkError` (409); re-vincular una `INACTIVE` la
    reactiva (sin borrar filas).
    """
    if type not in LINK_TYPES:
        raise ValueError(f"type debe ser uno de {list(LINK_TYPES)}")
    owner = _coerce_user(user_id)
    if type == "ACCOUNT":
        if account_id is None or card is not None:
            raise ValueError("type=ACCOUNT exige `account_id` y prohibe `card`")
        token_ref, last4, brand = _resolve_account_source(session, owner, account_id)
        account_uuid: uuid.UUID | None = uuid.UUID(str(account_id))
    else:
        if card is None or account_id is not None:
            raise ValueError("type=CARD exige `card` y prohibe `account_id`")
        if not isinstance(card, dict):
            raise ValueError("card debe ser objeto")
        token_ref, last4, brand = _resolve_card_source(
            pan=card.get("pan"),
            token_ref=card.get("token_ref"),
            last4=card.get("last4"),
            brand=card.get("brand"),
        )
        account_uuid = None

    wallet = wallet_repo.get_wallet_by_user(session, owner)
    if wallet is None:
        try:
            wallet = wallet_repo.create_wallet(session, owner)
        except wallet_repo.DuplicateLinkError:
            wallet = wallet_repo.get_wallet_by_user(session, owner)
            if wallet is None:  # pragma: no cover - carrera extrema
                raise

    existing = wallet_repo.find_by_token(session, token_ref)
    if existing is not None:
        if existing.wallet_id != wallet.id:
            # Token de otro titular: no reactivar ni exponer (neutro).
            # La UQ global impide duplicarlo; se responde como duplicado
            # sin tocar la fila ajena.
            raise wallet_repo.DuplicateLinkError("fuente ya vinculada")
        if str(existing.status) == "ACTIVE":
            raise wallet_repo.DuplicateLinkError("fuente ya vinculada")
        row = wallet_repo.reactivate_link(session, existing)
        _record_link_created_event(session, wallet.id, row)
        return to_link_view(row)
    row = wallet_repo.create_link(
        session,
        wallet_id=wallet.id,
        token_ref=token_ref,
        account_id=account_uuid,
        last4=last4,
        brand=brand,
    )
    _record_link_created_event(session, wallet.id, row)
    logger.debug("fuente vinculada type=%s wallet_id=%s link_id=%s", type, wallet.id, row.id)
    return to_link_view(row)


def _minimal_idempotency_snapshot(*, link_id: object, status: object) -> dict:
    """Snapshot de idempotencia SIN PII: solo referencia + estado."""
    return {"link_id": str(link_id), "status": str(status)}


def _replay_idempotent_view(session: Session, owner: uuid.UUID, stored) -> dict:
    """Reconstruye la respuesta del alta re-leyendo la fila (sin PII)."""
    snapshot = stored.response_snapshot
    if not isinstance(snapshot, dict):
        raise IdempotencyConflictError("operacion en curso para esta Idempotency-Key")
    link_id = snapshot.get("link_id")
    if link_id is None:
        raise IdempotencyConflictError("operacion en curso para esta Idempotency-Key")
    row = wallet_repo.get_link(session, link_id)
    if row is None or str(row.status) != "ACTIVE":
        raise IdempotencyConflictError("vinculo asociado a la clave ya no esta disponible")
    wallet = wallet_repo.get_wallet_by_user(session, owner)
    if wallet is None or row.wallet_id != wallet.id:
        raise IdempotencyConflictError("vinculo asociado a la clave ya no esta disponible")
    return to_link_view(row)


def _canonical_body(*, type: str, account_id: object, card: dict | None) -> dict:
    """Cuerpo canonico para el hash de idempotencia (SIN crudo: el PAN
    se tokeniza antes y solo entra su `token_ref` derivado)."""
    if type == "ACCOUNT":
        return {"type": type, "account_id": str(account_id)}
    assert card is not None
    token_ref, last4, brand = _resolve_card_source(
        pan=card.get("pan"),
        token_ref=card.get("token_ref"),
        last4=card.get("last4"),
        brand=card.get("brand"),
    )
    body: dict = {"type": type, "token_ref": token_ref}
    if last4 is not None:
        body["last4"] = last4
    if brand is not None:
        body["brand"] = brand
    return body


def link_source_idempotent(
    session: Session,
    user_id: uuid.UUID | str,
    *,
    body: dict,
    idempotency_key: str | None,
) -> tuple[dict, bool]:
    """Alta con `Idempotency-Key` opcional (no mueve dinero).

    Sin clave: vincula directo (`replayed=False`). Con clave: misma
    clave + mismo cuerpo -> replay (`replayed=True`, sin duplicar);
    misma clave + distinto cuerpo -> `IdempotencyConflictError` (409).
    """
    owner = _coerce_user(user_id)
    link_type = body.get("type")
    canonical = _canonical_body(
        type=link_type, account_id=body.get("account_id"), card=body.get("card")
    )
    if idempotency_key is None:
        return (
            link_source(
                session,
                owner,
                type=link_type,
                account_id=body.get("account_id"),
                card=body.get("card"),
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
            link_source(
                session,
                owner,
                type=link_type,
                account_id=body.get("account_id"),
                card=body.get("card"),
            ),
            False,
        )
    if len(key) > 80:
        raise ValueError("Idempotency-Key supera 80 caracteres")
    digest = compute_request_hash({"owner": str(owner), **canonical})
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
        view = link_source(
            session,
            owner,
            type=link_type,
            account_id=body.get("account_id"),
            card=body.get("card"),
        )
        store_response(
            session,
            row,
            response_snapshot=_minimal_idempotency_snapshot(
                link_id=view["id"], status=view.get("status", "ACTIVE")
            ),
            transaction_id=view["id"],
        )
        return view, False
    stored = find_key(session, key=key, user_id=owner)
    if stored is None:  # pragma: no cover - carrera extrema
        raise IdempotencyConflictError("clave de idempotencia en conflicto")
    if is_expired(stored):
        renew_expired(
            session,
            stored,
            request_hash=digest,
            endpoint=IDEMPOTENCY_ENDPOINT,
            method="POST",
        )
        view = link_source(
            session,
            owner,
            type=link_type,
            account_id=body.get("account_id"),
            card=body.get("card"),
        )
        store_response(
            session,
            stored,
            response_snapshot=_minimal_idempotency_snapshot(
                link_id=view["id"], status=view.get("status", "ACTIVE")
            ),
            transaction_id=view["id"],
        )
        return view, False
    if stored.request_hash != digest:
        raise IdempotencyConflictError("Idempotency-Key ya usada con un cuerpo distinto")
    return _replay_idempotent_view(session, owner, stored), True


# Pago express interno (E4-T02, HU13; sin endpoint publico).
def unlink_source(
    session: Session,
    user_id: uuid.UUID | str,
    link_id: uuid.UUID | str,
) -> dict:
    """Baja logica del vinculo del titular (`ACTIVE` -> `INACTIVE`).

    Solo el titular: vinculo inexistente, ajeno, con id malformado o ya
    `INACTIVE` colapsa al mismo `WalletLinkNotFoundError` neutro (sin
    oraculo, sin tocar filas ajenas). Solo `flush` (el endpoint confirma).
    """
    owner = _coerce_user(user_id)
    wallet = wallet_repo.get_wallet_by_user(session, owner)
    row = wallet_repo.get_link(session, link_id)
    if wallet is None or row is None or row.wallet_id != wallet.id:
        raise WalletLinkNotFoundError("vinculo inexistente")
    if str(row.status) != "ACTIVE":
        raise WalletLinkNotFoundError("vinculo inexistente")
    row = wallet_repo.deactivate_link(session, row)
    logger.debug("vinculo desvinculado link_id=%s", row.id)
    return to_link_view(row)


__all__ = [
    "IDEMPOTENCY_ENDPOINT",
    "LINK_TYPES",
    "QR_IDEMPOTENCY_ENDPOINT",
    "QR_PAYMENT_IDEMPOTENCY_ENDPOINT",
    "QR_PERSONAL_IDEMPOTENCY_ENDPOINT",
    "SERVICE_PAYMENT_IDEMPOTENCY_ENDPOINT",
    "SETTLEMENT_ACCOUNT_ENV_VAR",
    "TOPUP_IDEMPOTENCY_ENDPOINT",
    "ExpressConflictError",
    "ExpressError",
    "ExpressNotFoundError",
    "ExpressValidationError",
    "IdempotencyConflictError",
    "InvalidQrError",
    "QrAlreadyPaidError",
    "QrError",
    "QrExpiredError",
    "QrIdempotencyConflictError",
    "QrMerchantNotFoundError",
    "QrMerchantUnknownError",
    "QrNotAuthorizedError",
    "QrNotFoundError",
    "QrPaymentConflictError",
    "QrPaymentError",
    "QrPaymentNotFoundError",
    "QrValidationError",
    "ServiceBillerNotFoundError",
    "ServiceCatalogError",
    "ServicePaymentConflictError",
    "ServicePaymentError",
    "ServiceProviderDownError",
    "ServiceProviderGatewayTimeoutError",
    "ServiceSettlementUnavailableError",
    "TopupOperatorNotFoundError",
    "WalletAccountNotFoundError",
    "WalletLinkNotFoundError",
    "build_receipt_no",
    "create_personal_qr_charge",
    "create_personal_qr_charge_idempotent",
    "create_qr_charge",
    "create_qr_charge_idempotent",
    "finalize_qr_payment",
    "finalize_service_payment",
    "finalize_topup",
    "get_charge_status",
    "get_personal_charge_status",
    "get_qr_ttl_seconds",
    "get_wallet_daily_limit_minor",
    "get_wallet_express_limit_minor",
    "get_wallet_view",
    "is_charge_expired",
    "is_charge_valid",
    "link_source",
    "link_source_idempotent",
    "list_catalog",
    "notify_merchant_charge_paid",
    "notify_personal_charge_paid",
    "pay_express",
    "pay_qr_charge",
    "pay_service",
    "pay_topup",
    "preview_qr_charge",
    "register_subscription",
    "require_merchant_for_user",
    "resolve_holder_settlement_account",
    "resolve_merchant_settlement_account",
    "resolve_personal_settlement_account",
    "resolve_settlement_account",
    "resolve_topup_operator",
    "to_charge_view",
    "to_link_view",
    "to_topup_receipt",
    "unlink_source",
    "verify_charge_signature",
    "wallet_daily_limit_reader",
    "wallet_threshold_reader",
]
