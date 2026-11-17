"""Servicio de bolsillos de ahorro (E7-T02, HU24).

Abre bolsillos `PEN`/`USD`/`EUR` con su cuenta `POCKET` (misma moneda) y
permite listarlos/consultarlos. La cuenta la crea **solo `accounts`** por
su fachada `open_customer_account` (import perezoso; `fx` no escribe
tablas de `accounts`/`ledger`, regla de oro 4). No mueve dinero: la
apertura nace con `balance_minor=0` (sin asientos ni saldos).

Errores: `ValueError` (mapeable a `422 VALIDATION_ERROR`),
`PocketNotFoundError` (titular ajeno/inexistente -> `404` neutro),
`DuplicatePocketError` / `IdempotencyConflictError` (`409 CONFLICT`).

Eventos: `pocket.created` por `outbox` (misma sesion, sin publicar) +
auditoria best-effort `fx.pocket.opened` (sin PII). Sin secretos ni PII
en logs (reglas 7/9: aqui solo UUIDs, monedas y estados).
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.modules.fx import repository as fx_repo
from app.modules.fx.models import SavingsPocket
from app.modules.fx.service.quotes import (
    DEFAULT_FX_QUOTE_TTL_SECONDS,
    FX_QUOTE_TTL_SECONDS_KEY,
    QUOTE_CREATED_AUDIT_ACTION,
    QUOTE_CURRENCIES,
    QUOTES_IDEMPOTENCY_ENDPOINT,
    QuoteConflictError,
    QuoteNotFoundError,
    QuoteValidationError,
    create_quote,
    create_quote_idempotent,
    get_quote,
    get_rate,
    preview_spread,
    to_quote_view,
    to_rate_view,
)

logger = logging.getLogger(__name__)

#: Monedas del MVP (E7-T02, HU24): el bolsillo y su cuenta comparten
#: moneda. Espejo de `accounts.OPEN_CUSTOMER_ACCOUNT_CURRENCIES`;
#: constante del modulo (regla de oro 6).
POCKET_CURRENCIES = ("PEN", "USD", "EUR")

#: Accion de auditoria best-effort de la apertura (sin PII).
POCKET_OPENED_AUDIT_ACTION = "fx.pocket.opened"

#: Endpoint logico para la idempotencia del alta (clave `shared`, UQ por usuario).
POCKETS_IDEMPOTENCY_ENDPOINT = "/pockets"

#: Tipo de cuenta que `fx` pide a la fachada de `accounts` (E7-T02).
POCKET_ACCOUNT_TYPE = "POCKET"


class PocketNotFoundError(fx_repo.FxNotFoundError):
    """Bolsillo inexistente o de otro titular (`NOT_FOUND` neutro)."""


class DuplicatePocketError(fx_repo.FxConflictError):
    """Duplicado exacto (mismo nombre + moneda) del titular (`CONFLICT`)."""


class IdempotencyConflictError(fx_repo.FxConflictError):
    """Misma `Idempotency-Key` con distinto cuerpo (`CONFLICT`)."""


def _coerce_owner(user_id: uuid.UUID | str) -> uuid.UUID:
    try:
        return user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise fx_repo.FxValidationError("user_id debe ser UUID") from exc


def _coerce_currency(currency: object) -> str:
    """ISO-4217 en mayusculas dentro de las monedas MVP (sin eco del valor)."""
    if not isinstance(currency, str):
        raise fx_repo.FxValidationError("currency debe ser ISO-4217 (3 letras)")
    code = currency.strip().upper()
    if len(code) != 3 or not code.isalpha() or not code.isascii():
        raise fx_repo.FxValidationError("currency debe ser ISO-4217 (3 letras)")
    if code not in POCKET_CURRENCIES:
        raise fx_repo.FxValidationError("currency debe ser una de PEN, USD, EUR")
    return code


def _coerce_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        raise fx_repo.FxValidationError("name invalido")
    return name.strip()


def _coerce_target(target_minor: object) -> int | None:
    if target_minor is None:
        return None
    if isinstance(target_minor, bool) or not isinstance(target_minor, int):
        raise fx_repo.FxValidationError("target_minor debe ser entero de centimos")
    if target_minor < 0:
        raise fx_repo.FxValidationError("target_minor debe ser >= 0")
    return target_minor


def _coerce_roundup(roundup_enabled: object) -> bool:
    if not isinstance(roundup_enabled, bool):
        raise fx_repo.FxValidationError("roundup_enabled debe ser booleano")
    return roundup_enabled


def _coerce_yield(yield_rate: object) -> Decimal:
    if yield_rate is None:
        return Decimal(0)
    if isinstance(yield_rate, (bool, float)):
        raise fx_repo.FxValidationError("yield_rate debe ser Decimal (nunca float)")
    try:
        amount = yield_rate if isinstance(yield_rate, Decimal) else Decimal(str(yield_rate))
    except Exception as exc:
        raise fx_repo.FxValidationError("yield_rate debe ser Decimal") from exc
    if amount.is_nan() or amount < 0:
        raise fx_repo.FxValidationError("yield_rate debe ser >= 0")
    return amount


def _canonical_body(
    *,
    name: str,
    currency: str,
    target_minor: int | None,
    roundup_enabled: bool,
    yield_rate: Decimal,
) -> dict:
    """Cuerpo canonico (validado/normalizado) para el hash de idempotencia."""
    return {
        "name": name,
        "currency": currency,
        "target_minor": target_minor,
        "roundup_enabled": roundup_enabled,
        "yield_rate": str(yield_rate),
    }


def to_pocket_view(row: SavingsPocket) -> dict:
    """Vista publica del bolsillo (sin numero de cuenta: solo UUIDs)."""
    return {
        "pocket_id": str(row.id),
        "account_id": str(row.account_id) if row.account_id is not None else None,
        "name": row.name,
        "currency": row.currency,
        "target_minor": row.target_minor,
        "balance_minor": int(row.balance_minor),
        "roundup_enabled": bool(row.roundup_enabled),
        "yield_rate": row.yield_rate,
        "status": row.status,
    }


def _record_opened_events(session: Session, owner: uuid.UUID, pocket: SavingsPocket) -> None:
    """`pocket.created` por `outbox` + auditoria best-effort (sin PII)."""
    from app.core.events import POCKET_CREATED
    from app.core.outbox import record as outbox_record

    outbox_record(
        session,
        aggregate_type="pocket",
        aggregate_id=pocket.id,
        event_type=POCKET_CREATED,
        payload={
            "pocket_id": str(pocket.id),
            "account_id": str(pocket.account_id),
            "user_id": str(owner),
            "currency": pocket.currency,
            "status": pocket.status,
        },
    )
    try:
        from app.modules.audit.service import record as audit_record

        audit_record(
            session,
            actor=owner,
            action=POCKET_OPENED_AUDIT_ACTION,
            entity="savings_pocket",
            entity_id=pocket.id,
            metadata={"currency": pocket.currency, "status": pocket.status},
        )
    except Exception as exc:  # noqa: BLE001 - la auditoria no bloquea la apertura
        logger.warning("pocket audit best-effort omitida err=%s", type(exc).__name__)


def _find_active_duplicate(session: Session, owner: uuid.UUID, *, name: str, currency: str):
    """Bolsillo `ACTIVE` del titular con mismo nombre + moneda, o `None`."""
    for row in fx_repo.list_pockets(session, owner):
        if row.status == "ACTIVE" and row.name == name and row.currency == currency:
            return row
    return None


def open_pocket(
    session: Session,
    user_id: uuid.UUID | str,
    *,
    name: object,
    currency: object,
    target_minor: object = None,
    roundup_enabled: object = False,
    yield_rate: object = None,
) -> dict:
    """Abre un bolsillo del titular con su cuenta `POCKET` (E7-T02, HU24).

    Valida todo ANTES de crear nada; luego abre la cuenta por la fachada
    de `accounts` (`open_customer_account`, import perezoso, misma
    sesion: `flush` sin `commit`) y crea `savings_pockets` con
    `balance_minor=0`, `status=ACTIVE`. Duplicado exacto (mismo nombre +
    moneda en `ACTIVE`) -> `DuplicatePocketError` (409). Emite
    `pocket.created` por `outbox` + auditoria best-effort (sin PII).
    No mueve dinero. Hace `flush`, no `commit`.
    """
    owner = _coerce_owner(user_id)
    clean_name = _coerce_name(name)
    code = _coerce_currency(currency)
    clean_target = _coerce_target(target_minor)
    clean_roundup = _coerce_roundup(roundup_enabled)
    clean_yield = _coerce_yield(yield_rate)

    if _find_active_duplicate(session, owner, name=clean_name, currency=code) is not None:
        raise DuplicatePocketError("ya existe un bolsillo con ese nombre y moneda")

    from app.modules.accounts.service import open_customer_account

    account = open_customer_account(session, owner, currency=code, type=POCKET_ACCOUNT_TYPE)

    pocket = fx_repo.create_pocket(
        session,
        user_id=owner,
        name=clean_name,
        currency=code,
        account_id=account["account_id"],
        target_minor=clean_target,
        balance_minor=0,
        roundup_enabled=clean_roundup,
        yield_rate=clean_yield,
    )
    _record_opened_events(session, owner, pocket)
    logger.debug("pocket abierto currency=%s", code)
    return to_pocket_view(pocket)


def open_pocket_idempotent(
    session: Session,
    user_id: uuid.UUID | str,
    *,
    body: dict,
    idempotency_key: str | None,
) -> tuple[dict, bool]:
    """Apertura con `Idempotency-Key` opcional (recomendada).

    Sin clave: abre directo (`replayed=False`; el duplicado exacto da
    409). Con clave: misma clave + mismo cuerpo -> replay del snapshot
    (`replayed=True`, sin duplicar); misma clave + distinto cuerpo ->
    `IdempotencyConflictError` (409). `body` es el dict ya validado del
    request (`name`, `currency`, `target_minor`, `roundup_enabled`,
    `yield_rate`).
    """
    owner = _coerce_owner(user_id)
    if idempotency_key is None:
        return open_pocket(session, owner, **body), False
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
        return open_pocket(session, owner, **body), False
    if len(key) > 80:
        raise fx_repo.FxValidationError("Idempotency-Key supera 80 caracteres")
    canonical = _canonical_body(
        name=_coerce_name(body.get("name")),
        currency=_coerce_currency(body.get("currency")),
        target_minor=_coerce_target(body.get("target_minor")),
        roundup_enabled=_coerce_roundup(body.get("roundup_enabled", False)),
        yield_rate=_coerce_yield(body.get("yield_rate")),
    )
    digest = compute_request_hash({"owner": str(owner), **canonical})
    row, created = try_claim(
        session,
        key=key,
        user_id=owner,
        endpoint=POCKETS_IDEMPOTENCY_ENDPOINT,
        method="POST",
        request_hash=digest,
        ttl_seconds=resolve_ttl_seconds(),
    )
    if created:
        view = open_pocket(session, owner, **canonical)
        store_response(
            session,
            row,
            response_snapshot={"pocket_id": view["pocket_id"], "status": view["status"]},
            transaction_id=view["pocket_id"],
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
            endpoint=POCKETS_IDEMPOTENCY_ENDPOINT,
            method="POST",
        )
        view = open_pocket(session, owner, **canonical)
        store_response(
            session,
            stored,
            response_snapshot={"pocket_id": view["pocket_id"], "status": view["status"]},
            transaction_id=view["pocket_id"],
        )
        return view, False
    if stored.request_hash != digest:
        raise IdempotencyConflictError("Idempotency-Key ya usada con un cuerpo distinto")
    return _replay_idempotent_view(session, owner, stored), True


def _replay_idempotent_view(session: Session, owner: uuid.UUID, stored) -> dict:
    """Reconstruye la respuesta de una apertura idempotente (sin duplicar)."""
    snapshot = stored.response_snapshot
    if not isinstance(snapshot, dict):
        raise IdempotencyConflictError("operacion en curso para esta Idempotency-Key")
    pocket_id = snapshot.get("pocket_id")
    if pocket_id is None:
        raise IdempotencyConflictError("operacion en curso para esta Idempotency-Key")
    try:
        pocket_uuid = pocket_id if isinstance(pocket_id, uuid.UUID) else uuid.UUID(str(pocket_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise IdempotencyConflictError("bolsillo asociado a la clave no disponible") from exc
    pocket = fx_repo.get_pocket(session, pocket_uuid)
    if pocket is None or pocket.user_id != owner or pocket.status != "ACTIVE":
        raise IdempotencyConflictError("bolsillo asociado a la clave ya no esta disponible")
    return to_pocket_view(pocket)


def list_pockets(session: Session, user_id: uuid.UUID | str) -> list[dict]:
    """Bolsillos del titular (orden estable `created_at, id`). Solo lectura."""
    owner = _coerce_owner(user_id)
    return [to_pocket_view(row) for row in fx_repo.list_pockets(session, owner)]


def get_pocket(session: Session, user_id: uuid.UUID | str, pocket_id: object) -> dict:
    """Detalle del bolsillo si es del titular; ajeno/inexistente -> neutro."""
    owner = _coerce_owner(user_id)
    try:
        pid = pocket_id if isinstance(pocket_id, uuid.UUID) else uuid.UUID(str(pocket_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise PocketNotFoundError("bolsillo inexistente") from exc
    pocket = fx_repo.get_pocket(session, pid)
    if pocket is None or pocket.user_id != owner:
        raise PocketNotFoundError("bolsillo inexistente")
    return to_pocket_view(pocket)


__all__ = [
    "DEFAULT_FX_QUOTE_TTL_SECONDS",
    "FX_QUOTE_TTL_SECONDS_KEY",
    "POCKETS_IDEMPOTENCY_ENDPOINT",
    "POCKET_ACCOUNT_TYPE",
    "POCKET_CURRENCIES",
    "POCKET_OPENED_AUDIT_ACTION",
    "QUOTES_IDEMPOTENCY_ENDPOINT",
    "QUOTE_CREATED_AUDIT_ACTION",
    "QUOTE_CURRENCIES",
    "DuplicatePocketError",
    "IdempotencyConflictError",
    "PocketNotFoundError",
    "QuoteConflictError",
    "QuoteNotFoundError",
    "QuoteValidationError",
    "create_quote",
    "create_quote_idempotent",
    "get_pocket",
    "get_quote",
    "get_rate",
    "list_pockets",
    "open_pocket",
    "open_pocket_idempotent",
    "preview_spread",
    "to_pocket_view",
    "to_quote_view",
    "to_rate_view",
]
