"""Servicio de tasas y cotizaciones `fx` (E7-T09, HU25).

- `get_rate(session, base, quote)`: consulta el adaptador `E7-T08`
  (`app.adapters.fx_rate`, fabrica por entorno, default `mock`
  determinista), persiste el snapshot en `fx_rates` (`source`/
  `ttl_seconds` del proveedor) y devuelve la tasa vigente. No mueve
  dinero (solo inserta la fila de tasa).
- `create_quote(session, user_id, *, base, quote,
  source_amount_minor, spread=None)`: valida monedas (ISO-4217),
  monto `> 0` y `base != quote`; obtiene la tasa; aplica `spread`
  (`E7-T10` lo hara dinamico; default `0` en este brief); calcula
  `target_amount_minor` con `fx.domain.target_for` (`Decimal` exacto
  y redondeo `ROUND_HALF_UP` al centavo, supuesto aprobado por el
  orquestador); fija `expires_at = now(UTC) + fx.quote_ttl_seconds`
  (default `30`, `config.parameters`, regla de oro 6) y persiste
  `fx_quotes` en `ACTIVE`. Hace `flush`, no `commit`.
- `get_quote(session, user_id, quote_id)`: devuelve la cotizacion del
  titular; si `now >= expires_at` y sigue `ACTIVE` la marca `EXPIRED`
  (expiracion on-read, el backend es la autoridad de la vigencia) y
  devuelve `EXPIRED`. Ajeno/inexistente -> `QuoteNotFoundError`
  (`404` neutro).
- `create_quote_idempotent(...)`: `POST /fx/quotes` con
  `Idempotency-Key` opcional (patron `E2-T13`): misma clave + mismo
  cuerpo -> misma cotizacion (`replayed=True`, sin duplicar); misma
  clave + distinto cuerpo -> `QuoteConflictError` (`409`).

Errores: `QuoteValidationError` (`422`), `QuoteNotFoundError`
(`404` neutro), `QuoteConflictError` (`409`); los errores tipados del
adaptador (`FxRateValidationError`/`FxRateUnavailableError`/
`FxRateTimeoutError`) se propagan para que el `api/` los traduzca
(`422`/`503`/`502`). Auditoria best-effort `fx.quote.created` (sin
PII: solo `base`, `quote`, `status`; sin evento de negocio: la
ejecucion es `E7-T11`). Sin secretos ni PII en logs (reglas 7/9:
aqui solo pares de moneda y estados).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.adapters.fx_rate import FxRatePayload, FxRateProvider, create_fx_rate_provider
from app.modules.fx import repository as fx_repo
from app.modules.fx.domain import target_for

logger = logging.getLogger(__name__)

#: Clave configurable de la vigencia de la cotizacion (`0001`;
#: regla de oro 6: regla variable = parametro, fuera del codigo).
FX_QUOTE_TTL_SECONDS_KEY = "fx.quote_ttl_seconds"

#: Vigencia default en segundos (misma semilla que `0001`; rige si la
#: clave falta o el valor almacenado es invalido).
DEFAULT_FX_QUOTE_TTL_SECONDS = 30

#: Accion de auditoria best-effort de la cotizacion (sin PII).
QUOTE_CREATED_AUDIT_ACTION = "fx.quote.created"

#: Endpoint logico para la idempotencia del alta (clave `shared`, UQ por usuario).
QUOTES_IDEMPOTENCY_ENDPOINT = "/fx/quotes"

#: Monedas aceptadas en cotizaciones del MVP (espejo de `POCKET_CURRENCIES`).
QUOTE_CURRENCIES = ("PEN", "USD", "EUR")


class QuoteValidationError(fx_repo.FxValidationError):
    """Dato de cotizacion invalido (`VALIDATION_ERROR`)."""


class QuoteNotFoundError(fx_repo.FxNotFoundError):
    """Cotizacion inexistente o de otro titular (`NOT_FOUND` neutro)."""


class QuoteConflictError(fx_repo.FxConflictError):
    """Conflicto de cotizacion (`CONFLICT`: idempotencia)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _naive(moment: datetime) -> datetime:
    """SQLite devuelve datetimes naive y Postgres aware: comparar sin tz."""
    return moment.replace(tzinfo=None) if moment.tzinfo is not None else moment


def _coerce_owner(user_id: uuid.UUID | str) -> uuid.UUID:
    try:
        return user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise QuoteValidationError("user_id debe ser UUID") from exc


def _coerce_pair(base: object, quote: object) -> tuple[str, str]:
    """Par de monedas del MVP (`PEN`/`USD`/`EUR`); `base != quote`."""
    for label, value in (("base", base), ("quote", quote)):
        if not isinstance(value, str):
            raise QuoteValidationError(f"{label} debe ser ISO-4217 (3 letras)")
        code = value.strip().upper()
        if len(code) != 3 or not code.isalpha() or not code.isascii():
            raise QuoteValidationError(f"{label} debe ser ISO-4217 (3 letras)")
        if code not in QUOTE_CURRENCIES:
            raise QuoteValidationError(f"{label} debe ser una de PEN, USD, EUR")
    clean_base, clean_quote = str(base).strip().upper(), str(quote).strip().upper()
    if clean_base == clean_quote:
        raise QuoteValidationError("base y quote deben diferir")
    return clean_base, clean_quote


def _coerce_source_amount(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise QuoteValidationError("source_amount_minor debe ser entero de centimos")
    if value <= 0:
        raise QuoteValidationError("source_amount_minor debe ser > 0")
    return value


def _coerce_spread(value: object) -> Decimal:
    """`spread >= 0` (`None` = default `0`; `E7-T10` lo hara dinamico)."""
    if value is None:
        return Decimal(0)
    if isinstance(value, (bool, float)):
        raise QuoteValidationError("spread debe ser Decimal (nunca float)")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise QuoteValidationError("spread debe ser Decimal") from exc
    if amount.is_nan() or amount < 0:
        raise QuoteValidationError("spread debe ser >= 0")
    return amount


def _quote_ttl_seconds(session: Session) -> int:
    """Vigencia en segundos (`config.parameters`, best-effort, default `30`)."""
    from app.modules.shared.config.parameters import get_int_parameter

    return get_int_parameter(session, FX_QUOTE_TTL_SECONDS_KEY, DEFAULT_FX_QUOTE_TTL_SECONDS, 1)


def to_rate_view(payload: FxRatePayload, fetched_at: datetime) -> dict:
    """Vista publica de la tasa vigente (sin persistir nada aqui)."""
    return {
        "base": payload.base,
        "quote": payload.quote,
        "rate": payload.rate,
        "source": payload.source,
        "fetched_at": fetched_at,
        "ttl_seconds": payload.ttl_seconds,
    }


def to_quote_view(row) -> dict:
    """Vista publica de la cotizacion (montos enteros; tasa exacta)."""
    return {
        "quote_id": str(row.id),
        "base": row.base,
        "quote": row.quote,
        "rate": row.rate,
        "spread": row.spread,
        "source_amount_minor": int(row.source_amount_minor),
        "target_amount_minor": int(row.target_amount_minor),
        "expires_at": row.expires_at,
        "status": row.status,
    }


def get_rate(
    session: Session,
    base: object,
    quote: object,
    *,
    provider: FxRateProvider | None = None,
    now: datetime | None = None,
) -> dict:
    """Tasa vigente del par + snapshot persistido en `fx_rates`.

    Valida el par (`422` si no es MVP o `base == quote`), consulta el
    adaptador `E7-T08` (par no soportado -> `FxRateValidationError`;
    caida/timeout -> errores tipados `503`/`502`, sin persistir nada)
    y persiste la fila (`source`/`ttl_seconds` del proveedor).
    `provider`/`now` solo existen para pruebas deterministas.
    Hace `flush`, no `commit`. No mueve dinero.
    """
    clean_base, clean_quote = _coerce_pair(base, quote)
    active = provider if provider is not None else create_fx_rate_provider()
    payload = active.get_rate(base=clean_base, quote=clean_quote)
    moment = now if now is not None else _utcnow()
    fx_repo.create_rate(
        session,
        base=payload.base,
        quote=payload.quote,
        rate=payload.rate,
        source=payload.source,
        fetched_at=moment,
        ttl_seconds=payload.ttl_seconds,
    )
    logger.debug("fx rate vigente %s/%s source=%s", clean_base, clean_quote, payload.source)
    return to_rate_view(payload, moment)


def create_quote(
    session: Session,
    user_id: uuid.UUID | str,
    *,
    base: object,
    quote: object,
    source_amount_minor: object,
    spread: object = None,
    provider: FxRateProvider | None = None,
    now: datetime | None = None,
) -> dict:
    """Congela una cotizacion `ACTIVE` por `fx.quote_ttl_seconds` (E7-T09).

    Valida todo ANTES de crear nada; obtiene la tasa via `get_rate`
    (persiste su snapshot); calcula el destino con `fx.domain`
    (`ROUND_HALF_UP` al centavo) y persiste `fx_quotes` `ACTIVE` con
    `expires_at = now + TTL`. Emite auditoria best-effort
    `fx.quote.created` (sin PII). Hace `flush`, no `commit`.
    No mueve dinero (la ejecucion es `E7-T11`).
    """
    owner = _coerce_owner(user_id)
    clean_base, clean_quote = _coerce_pair(base, quote)
    source_minor = _coerce_source_amount(source_amount_minor)
    clean_spread = _coerce_spread(spread)
    moment = now if now is not None else _utcnow()

    rate_view = get_rate(session, clean_base, clean_quote, provider=provider, now=moment)
    rate = rate_view["rate"]
    target_minor = target_for(source_minor, rate, clean_spread)
    if target_minor <= 0:
        raise QuoteValidationError("target_amount_minor debe ser > 0")
    ttl = _quote_ttl_seconds(session)
    row = fx_repo.create_quote(
        session,
        user_id=owner,
        base=clean_base,
        quote=clean_quote,
        rate=rate,
        spread=clean_spread,
        source_amount_minor=source_minor,
        target_amount_minor=target_minor,
        expires_at=moment + timedelta(seconds=ttl),
    )
    try:
        from app.modules.audit.service import record as audit_record

        audit_record(
            session,
            actor=owner,
            action=QUOTE_CREATED_AUDIT_ACTION,
            entity="fx_quote",
            entity_id=row.id,
            metadata={"base": clean_base, "quote": clean_quote, "status": row.status},
        )
    except Exception as exc:  # noqa: BLE001 - la auditoria no bloquea la cotizacion
        logger.warning("quote audit best-effort omitida err=%s", type(exc).__name__)
    logger.debug("fx quote creada %s/%s status=ACTIVE", clean_base, clean_quote)
    return to_quote_view(row)


def get_quote(
    session: Session,
    user_id: uuid.UUID | str,
    quote_id: object,
    *,
    now: datetime | None = None,
) -> dict:
    """Cotizacion del titular con vigencia efectiva on-read.

    Si `now >= expires_at` y sigue `ACTIVE`, la marca `EXPIRED`
    (misma sesion, `flush` sin `commit`) y la devuelve `EXPIRED`;
    una cotizacion `EXPIRED` no se puede ejecutar (la ejecuta
    `E7-T11`; el cliente debe recotizar). Ajena/inexistente ->
    `QuoteNotFoundError` (`404` neutro).
    """
    owner = _coerce_owner(user_id)
    try:
        qid = quote_id if isinstance(quote_id, uuid.UUID) else uuid.UUID(str(quote_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise QuoteNotFoundError("cotizacion inexistente") from exc
    row = fx_repo.get_quote(session, qid)
    if row is None or row.user_id != owner:
        raise QuoteNotFoundError("cotizacion inexistente")
    moment = now if now is not None else _utcnow()
    if row.status == "ACTIVE" and _naive(moment) >= _naive(row.expires_at):
        fx_repo.expire_quote(session, qid)
    return to_quote_view(row)


def _canonical_body(*, base: str, quote: str, source_amount_minor: int) -> dict:
    """Cuerpo canonico (validado/normalizado) para el hash de idempotencia."""
    return {"base": base, "quote": quote, "source_amount_minor": source_amount_minor}


def create_quote_idempotent(
    session: Session,
    user_id: uuid.UUID | str,
    *,
    body: dict,
    idempotency_key: str | None,
    provider: FxRateProvider | None = None,
) -> tuple[dict, bool]:
    """Cotizacion con `Idempotency-Key` opcional (recomendada).

    Sin clave: cotiza directo (`replayed=False`). Con clave: misma
    clave + mismo cuerpo -> misma cotizacion (`replayed=True`, sin
    duplicar; se re-lee con vigencia on-read, asi una cotizacion ya
    vencida se reporta `EXPIRED`); misma clave + distinto cuerpo ->
    `QuoteConflictError` (`409`). `body` es el dict validado del
    request (`base`, `quote`, `source_amount_minor`).
    """
    owner = _coerce_owner(user_id)
    if idempotency_key is None:
        return create_quote(session, owner, provider=provider, **body), False
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
        return create_quote(session, owner, provider=provider, **body), False
    if len(key) > 80:
        raise QuoteValidationError("Idempotency-Key supera 80 caracteres")
    clean_base, clean_quote = _coerce_pair(body.get("base"), body.get("quote"))
    source_minor = _coerce_source_amount(body.get("source_amount_minor"))
    canonical = _canonical_body(
        base=clean_base, quote=clean_quote, source_amount_minor=source_minor
    )
    digest = compute_request_hash({"owner": str(owner), **canonical})
    row, created = try_claim(
        session,
        key=key,
        user_id=owner,
        endpoint=QUOTES_IDEMPOTENCY_ENDPOINT,
        method="POST",
        request_hash=digest,
        ttl_seconds=resolve_ttl_seconds(),
    )
    if created:
        view = create_quote(
            session,
            owner,
            base=clean_base,
            quote=clean_quote,
            source_amount_minor=source_minor,
            provider=provider,
        )
        store_response(
            session,
            row,
            response_snapshot={"quote_id": view["quote_id"], "status": view["status"]},
            transaction_id=view["quote_id"],
        )
        return view, False
    stored = find_key(session, key=key, user_id=owner)
    if stored is None:  # pragma: no cover - carrera extrema
        raise QuoteConflictError("clave de idempotencia en conflicto")
    if is_expired(stored):
        renew_expired(
            session,
            stored,
            request_hash=digest,
            endpoint=QUOTES_IDEMPOTENCY_ENDPOINT,
            method="POST",
        )
        view = create_quote(
            session,
            owner,
            base=clean_base,
            quote=clean_quote,
            source_amount_minor=source_minor,
            provider=provider,
        )
        store_response(
            session,
            stored,
            response_snapshot={"quote_id": view["quote_id"], "status": view["status"]},
            transaction_id=view["quote_id"],
        )
        return view, False
    if stored.request_hash != digest:
        raise QuoteConflictError("Idempotency-Key ya usada con un cuerpo distinto")
    return _replay_idempotent_view(session, owner, stored), True


def _replay_idempotent_view(session: Session, owner: uuid.UUID, stored) -> dict:
    """Reconstruye la respuesta de una cotizacion idempotente (sin duplicar)."""
    snapshot = stored.response_snapshot
    if not isinstance(snapshot, dict):
        raise QuoteConflictError("operacion en curso para esta Idempotency-Key")
    quote_id = snapshot.get("quote_id")
    if quote_id is None:
        raise QuoteConflictError("operacion en curso para esta Idempotency-Key")
    try:
        qid = quote_id if isinstance(quote_id, uuid.UUID) else uuid.UUID(str(quote_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise QuoteConflictError("cotizacion asociada a la clave no disponible") from exc
    row = fx_repo.get_quote(session, qid)
    if row is None or row.user_id != owner:
        raise QuoteConflictError("cotizacion asociada a la clave ya no esta disponible")
    return get_quote(session, owner, qid)


__all__ = [
    "DEFAULT_FX_QUOTE_TTL_SECONDS",
    "FX_QUOTE_TTL_SECONDS_KEY",
    "QUOTES_IDEMPOTENCY_ENDPOINT",
    "QUOTE_CREATED_AUDIT_ACTION",
    "QUOTE_CURRENCIES",
    "QuoteConflictError",
    "QuoteNotFoundError",
    "QuoteValidationError",
    "create_quote",
    "create_quote_idempotent",
    "get_quote",
    "get_rate",
    "to_quote_view",
    "to_rate_view",
]
