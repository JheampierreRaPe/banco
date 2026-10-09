"""Integracion motor <-> autorizacion biometrica del dispositivo (E2-T17, HU08).

Cubre en el motor transaccional:

- Decision de exigir biometria: si el monto de la operacion
  (`amount_minor + fee_minor`, E2-T34, HU08) supera el umbral configurable
  (`transfer.biometric_threshold_minor`, fachada `shared.config.parameters`
  de E2-T18), la transaccion queda en `PENDING_AUTHORIZATION` con `nonce`
  firmado por el dispositivo. El umbral es POR OPERACION (no mira el
  acumulado del dia).
- Retencion en `PENDING_AUTHORIZATION`: las rutas con hold (terceros /
  interbancaria) crean el hold (fila + delta + asiento + `funds.held`) ANTES
  de debitar/liquidar; la ruta atomica propia (E2-T07, `04#7.1`) no crea
  hold para preservar su invariante de asiento unico (nada se mueve hasta
  `AUTHORIZED`). Tras la firma valida la continuacion reutiliza el hold
  previo sin duplicarlo.
- Verificacion por fachada de `identity` (D-03): firma con
  `identity.service.device_login.verify_signature` y lectura del binding
  con `identity.service.device_login.get_device_binding`; PIN del titular
  con `identity.service.pin_authorization.verify_holder_pin` (E2-T31,
  alternativa mutuamente excluyente con el mismo flujo y CA-02); jamas se
  importan modelos ni repositorios de `identity`.
- Limite diario por perfil (D-06, CA-03, E2-T36): si
  `acumulado + amount + fee > limite_del_perfil` (fachada `shared.config`,
  clave por perfil o generica) la operacion queda `REJECTED`
  (`DAILY_LIMIT_EXCEEDED`) sin hold ni movimientos. El acumulado incluye
  `PENDING_AUTHORIZATION` (las pendientes con hold si comprometen el dia) y
  el limite se re-chequea al autorizar (`PENDING_AUTHORIZATION ->
  AUTHORIZED`, firma/PIN/interbancaria): si a ese instante
  `acumulado + total > limite`, la operacion queda `REJECTED`
  (`DAILY_LIMIT_EXCEEDED`) sin liquidar, con el hold liberado
  (`HOLD_RELEASE` idempotente, ya existente).
- Bloqueo preventivo por N fallos (D-05, CA-02, dueno E2-T17): tras
  `auth.max_failed_attempts` intentos biometricos fallidos sobre la misma
  operacion, esta queda `REJECTED` (`BIOMETRIC_PREVENTIVE_BLOCK`, hold
  liberado si existia) y se emite `auth.failed_attempt` (con
  `blocked: true`) para `risk`/`E2-T20`. Cada intento fallido emite
  `auth.failed_attempt` (CA-04) sin mover dinero y sin filtrar el motivo
  exacto (sin oraculo: firma invalida, binding ausente/revocado y
  dispositivo inesperado responden el mismo generico).

Frontera (reglas de oro 1/3/4/6/7/8/9):
- Dinero entero en centimos; nunca `float`.
- `ledger`/`accounts` solo por fachada/puerto; `identity` solo por las dos
  funciones de fachada citadas (import perezoso, sin ciclos).
- Umbrales/limites/fallos en `config.parameters` (E2-T18); sin valores
  cableados salvo los fallbacks de la fachada.
- Sin PII ni secretos en logs (`nonce`, firmas y `public_key` jamas se
  loguean); el `nonce` vive en `transactions.metadata` solo para
  correlacion (nunca material biometrico).
- Eventos solo por `outbox` (import perezoso); `flush` sin `commit`.
- El `risk_profile` del titular lo resuelve la capa API con
  `resolve_risk_profile` (lectura best-effort en el propio motor con el
  usuario autenticado, E2-T19, OBS-1); por defecto rige el limite
  generico/`STANDARD` (semilla `0023`), sin leer tablas ajenas por ORM.

Concurrencia/idempotencia:
- La continuacion reutiliza el hold `ACTIVE` previo (no duplica asiento ni
  hold); el `authorize` repetido con la misma firma es replay (compara el
  digest `sha256` de la firma, nunca la firma en claro dos veces).
- Las transiciones usan el CAS de `transition_transaction`: el perdedor de
  una carrera recibe `ConcurrencyError` (E2-T19 lo mapea a reintento).
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.transactions import repository as tx_repository
from app.modules.transactions.domain.state_machine import ConcurrencyError
from app.modules.transactions.models import Transaction

logger = logging.getLogger(__name__)

#: Clave de `transactions.metadata` con el estado del reto biometrico.
META_KEY = "biometric"

#: Evento de seguridad por intento fallido / bloqueo (lo consume `risk`;
#: mismo nombre que `identity.service.device_login.AUDIT_FAILED_ATTEMPT`).
AUTH_FAILED_ATTEMPT_EVENT = "auth.failed_attempt"

#: Codigo de rechazo por limite diario (CA-03, D-06).
DAILY_LIMIT_EXCEEDED = "DAILY_LIMIT_EXCEEDED"
#: Codigo de rechazo por bloqueo preventivo tras N fallos (CA-02, D-05).
PREVENTIVE_BLOCK = "BIOMETRIC_PREVENTIVE_BLOCK"

#: TTL del `nonce` de autorizacion en segundos (corto, como el de login
#: `identity.domain.nonce.NONCE_TTL_SECONDS`; movible a
#: `config.parameters` cuando config adopte la clave).
NONCE_TTL_SECONDS = 120
#: Entropia del `nonce` (`token_urlsafe(32)`, igual que login).
NONCE_ENTROPY_BYTES = 32

#: Estados que NO cuentan para el acumulado diario: negocio fallido
#: (`REJECTED`/`FAILED`) y fondos devueltos (`REVERSED`). Las pendientes
#: (`PENDING_AUTHORIZATION`) SI cuentan desde E2-T36: retienen fondos con
#: hold y encoladas evadian el limite diario (bypass). Sin migracion.
EXCLUDED_ACCUMULATED_STATUSES = ("REJECTED", "FAILED", "REVERSED")


class BiometricError(ValueError):
    """Error base de la autorizacion biometrica (mapeable a 4xx)."""


class TransactionNotFoundError(BiometricError):
    """Transaccion inexistente (-> 404 `NOT_FOUND`, neutro)."""


class BiometricStateError(BiometricError):
    """La operacion no espera autorizacion (-> 409/422, sin oraculo)."""


class NotAuthorizedError(BiometricError):
    """Firma/binding/dispositivo invalido (-> 403 `NOT_AUTHORIZED`).

    Mensaje unico y generico: no distingue inexistente de revocado ni de
    firma erronea (sin oraculo, `docs/05#6.1` decision E1-T42).
    """


class ExpiredNonceError(BiometricError):
    """`nonce` vencido (-> 400 `EXPIRED_NONCE`, patron OTP)."""


#: Mensaje generico estable (identico para firma, binding o dispositivo).
NOT_AUTHORIZED_MESSAGE = "Autorizacion biometrica invalida"
#: Mensaje de `nonce` vencido (mismo patron que login con dispositivo).
EXPIRED_NONCE_MESSAGE = "Desafio vencido, solicite uno nuevo"
#: Motivo de historial al exigir biometria.
REQUIRED_REASON = "biometria requerida"
#: Motivo de historial al superar el reto.
PASSED_REASON = "biometria superada"
#: Motivo de historial al bloquear preventivamente.
BLOCKED_REASON = "bloqueo preventivo tras N intentos fallidos"
#: Motivo de historial al rechazar por limite diario.
DAILY_LIMIT_REASON = "limite diario excedido"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_aware(raw: object) -> datetime | None:
    """Interpreta un ISO-8601 como instante UTC (`None` si no parsea)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return _as_aware(parsed)


# ------------------------------------------------------- Puertos (fachadas)


def default_threshold_reader(session: Session) -> int:
    """Umbral de biometria via fachada `shared.config` (E2-T18)."""
    from app.modules.shared.config import parameters as config_params

    return config_params.get_biometric_threshold_minor(session)


def default_daily_limit_reader(session: Session, profile: str | None) -> int:
    """Limite diario del perfil via fachada `shared.config` (E2-T18)."""
    from app.modules.shared.config import parameters as config_params

    return config_params.get_daily_limit_minor(session, profile)


def default_max_attempts_reader(session: Session) -> int:
    """Tope de fallos antes del bloqueo via fachada `shared.config`."""
    from app.modules.shared.config import parameters as config_params

    return config_params.get_max_failed_attempts(session)


def resolve_risk_profile(session: Session, user_id: uuid.UUID | str | None) -> str | None:
    """Perfil de riesgo del titular para el limite diario (OBS-1, E2-T19, CA-03).

    Lee `risk_profile` del titular por SQL acotado en `SAVEPOINT`, sin
    importar modelos ajenos (la unica via a `identity` sigue siendo la
    fachada `device_login` para firma/binding, D-03; esta lectura vive en
    el propio motor con el usuario autenticado, best-effort).
    Tabla ausente, titular inexistente o valor invalido -> `None` (rige el
    limite generico/`STANDARD`, mismo fallback que `evaluate_gate`).
    Normaliza a `strip().upper()`; token valido `[A-Z0-9_]{1,20}` (misma
    forma que la fachada de parametros para `transfer.daily_limit_*`).
    Nunca lanza; sin PII en logs (solo si se resolvio o no).
    """
    if user_id is None:
        return None
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError):
        return None
    try:
        with session.begin_nested():
            stmt = sa.text("SELECT risk_profile FROM identity.users WHERE id = :uid").bindparams(
                sa.bindparam("uid", type_=sa.Uuid())
            )
            raw = session.execute(stmt, {"uid": uid}).scalar()
    except Exception:  # noqa: BLE001 - sin acceso rige el generico
        logger.debug("risk_profile sin acceso: limite generico")
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    token = raw.strip().upper()
    if len(token) > 20 or re.fullmatch(r"[A-Z0-9_]+", token) is None:
        return None
    logger.debug("risk_profile resuelto")
    return token


def default_binding_reader(session: Session, user_id: uuid.UUID, device_id: str) -> dict | None:
    """Binding por fachada `identity.service.device_login` (D-03).

    Retorna `{"device_id", "public_key", "status"}` o `None`; nunca importa
    modelos de `identity`.
    """
    from app.modules.identity.service import device_login as device_login_service

    return device_login_service.get_device_binding(session, user_id, device_id)


def default_signature_verifier(nonce: str, signature: str, public_key: str) -> bool:
    """Firma del `nonce` via fachada `identity.service` (D-03)."""
    from app.modules.identity.service import device_login as device_login_service

    return device_login_service.verify_signature(nonce, signature, public_key)


def default_pin_verifier(session: Session, user_id: uuid.UUID | str | None, pin: str) -> bool:
    """PIN del titular via fachada `identity.service` (D-03, E2-T31).

    Retorna `True` solo si el PIN coincide con la credencial del titular;
    nunca importa modelos de `identity` (import perezoso, sin ciclos).
    """
    from app.modules.identity.service import pin_authorization as pin_authorization_service

    return pin_authorization_service.verify_holder_pin(session, user_id, pin)


ThresholdReader = Callable[[Session], int]
DailyLimitReader = Callable[[Session, Any], int]
MaxAttemptsReader = Callable[[Session], int]
BindingReader = Callable[[Session, uuid.UUID, str], dict | None]
SignatureVerifier = Callable[[str, str, str], bool]
PinVerifier = Callable[[Session, uuid.UUID | str | None, str], bool]


# ------------------------------------------------------------------ Decision


@dataclass(frozen=True)
class BiometricDecision:
    """Evaluacion del gate biometrico (pura, sin escrituras)."""

    accumulated_minor: int
    total_minor: int
    threshold_minor: int
    daily_limit_minor: int
    profile: str | None
    limit_exceeded: bool
    requires_biometry: bool


def daily_accumulated_minor(
    session: Session,
    user_id: uuid.UUID,
    *,
    exclude_tx_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> int:
    """Suma del dia (UTC) del titular en estados que comprometen fondos.

    Incluye `amount_minor + fee_minor` por operacion; excluye la operacion
    en curso (`exclude_tx_id`) y los estados de
    `EXCLUDED_ACCUMULATED_STATUSES` (`REJECTED`/`FAILED`: negocio fallido;
    `REVERSED`: fondos devueltos). Desde E2-T36 `PENDING_AUTHORIZATION` SI
    cuenta (retiene fondos con hold; excluirla permitia encolar pendientes
    sobre el umbral y evadir el limite diario). Se conservan contando
    `FUNDS_HELD`/`POSTED`/`SETTLED`/`CONCILIATED` (fondos comprometidos o
    ya movidos) y los transitorios de la peticion en curso (`INITIATED`,
    `VALIDATED`, `AUTHORIZED`). Dinero entero; sin `float`. Un fallo de
    lectura se propaga (fallo tecnico: el llamante revierte, sin mover
    dinero a ciegas).
    """
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()
    day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = (
        sa.select(
            sa.func.coalesce(sa.func.sum(Transaction.amount_minor + Transaction.fee_minor), 0)
        )
        .where(Transaction.initiator_user_id == user_id)
        .where(Transaction.created_at >= day_start)
        .where(Transaction.status.notin_(EXCLUDED_ACCUMULATED_STATUSES))
    )
    if exclude_tx_id is not None:
        stmt = stmt.where(Transaction.id != exclude_tx_id)
    return int(session.scalar(stmt) or 0)


def evaluate_gate(
    session: Session,
    *,
    user_id: uuid.UUID | None,
    amount_minor: int,
    fee_minor: int = 0,
    exclude_tx_id: uuid.UUID | None = None,
    risk_profile: str | None = None,
    now: datetime | None = None,
    threshold_reader: ThresholdReader | None = None,
    daily_limit_reader: DailyLimitReader | None = None,
) -> BiometricDecision:
    """Decide limite diario y exigencia de biometria (sin escrituras).

    - Sin titular (`user_id=None`, llamadas internas) no se exige nada
      (ruta legacy): no hay contra que acumular.
    - `limit_exceeded`: `acumulado + total > limite_del_perfil` (el limite
      diario SIGUE siendo acumulado; el acumulado incluye
      `PENDING_AUTHORIZATION`, E2-T36) -> el llamante rechaza (`REJECTED`,
      sin hold), donde `total = amount_minor + fee_minor`. El mismo
      chequeo se repite al autorizar (`_continue_after_authorized` /
      `resume_after_authorization`): una pendiente creada bajo el limite
      que ya no cabe se rechaza ahi (`REJECTED`/`DAILY_LIMIT_EXCEEDED`,
      hold liberado, sin liquidar).
    - `requires_biometry`: `total > umbral` (umbral POR OPERACION, E2-T34;
      no mira el acumulado) y sin exceso de limite -> el llamante retiene
      en `PENDING_AUTHORIZATION`.
    Todo configurable via `config.parameters` (E2-T18); nunca lanza por
    configuracion (la fachada aplica fallbacks).
    """
    total = int(amount_minor) + int(fee_minor)
    read_threshold = threshold_reader or default_threshold_reader
    read_limit = daily_limit_reader or default_daily_limit_reader
    threshold = int(read_threshold(session))
    if user_id is None:
        return BiometricDecision(
            accumulated_minor=0,
            total_minor=total,
            threshold_minor=threshold,
            daily_limit_minor=int(read_limit(session, risk_profile)),
            profile=risk_profile,
            limit_exceeded=False,
            requires_biometry=False,
        )
    accumulated = daily_accumulated_minor(session, user_id, exclude_tx_id=exclude_tx_id, now=now)
    limit = int(read_limit(session, risk_profile))
    exceeded = (accumulated + total) > limit
    return BiometricDecision(
        accumulated_minor=accumulated,
        total_minor=total,
        threshold_minor=threshold,
        daily_limit_minor=limit,
        profile=risk_profile,
        limit_exceeded=exceeded,
        requires_biometry=(not exceeded) and (total > threshold),
    )


# ------------------------------------------------------------ Reto (`nonce`)


def _require_device_id(device_id: object, *, allow_none: bool) -> str | None:
    if device_id is None and allow_none:
        return None
    if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 128:
        raise BiometricStateError("dispositivo invalido para el reto biometrico")
    return device_id.strip()


def new_challenge_block(
    device_id: str | None = None,
    *,
    nonce: str | None = None,
    now: datetime | None = None,
    ttl_seconds: int | None = None,
) -> tuple[dict[str, Any], str, datetime]:
    """Construye el bloque `meta["biometric"]` del reto (puro).

    `(bloque, nonce, expira_en)`: `nonce` aleatorio de un solo uso con TTL
    corto; `failed_attempts` en `0`; sin material biometrico.
    `ttl_seconds` solo existe para pruebas (en produccion rige
    `NONCE_TTL_SECONDS`).
    """
    clean_device = _require_device_id(device_id, allow_none=True)
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()
    ttl = NONCE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
        raise ValueError(f"ttl_seconds debe ser entero > 0, recibido: {ttl_seconds!r}")
    token = nonce if isinstance(nonce, str) and nonce.strip() else None
    if token is None:
        token = secrets.token_urlsafe(NONCE_ENTROPY_BYTES)
    expires_at = moment + timedelta(seconds=ttl)
    block: dict[str, Any] = {
        "nonce": token,
        "expires_at": expires_at.isoformat(),
        "device_id": clean_device,
        "failed_attempts": 0,
        "used": False,
        "authorized": False,
        "authorized_signature_digest": None,
        "authorized_at": None,
    }
    return block, token, expires_at


def get_block(tx: Transaction) -> dict[str, Any] | None:
    """Bloque `meta["biometric"]` (`None` si la operacion no lo tiene)."""
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    block = meta.get(META_KEY)
    return dict(block) if isinstance(block, dict) else None


def save_block(session: Session, tx: Transaction, block: dict[str, Any]) -> None:
    """Guarda el bloque biometrico preservando el resto de `meta`.

    `flush` sin `commit`; el llamante transiciona despues (el CAS de
    `transition_transaction` relee fresco: llamar siempre antes de
    transicionar).
    """
    meta = dict(tx.meta) if isinstance(tx.meta, dict) else {}
    meta[META_KEY] = dict(block)
    tx.meta = meta
    session.add(tx)
    session.flush()


def failure_markers(code: str) -> dict[str, Any]:
    """Marcas de rechazo de negocio (columna + `meta`, sin hold)."""
    return {"failure_reason": code, "meta_failure_code": code}


def mark_rejected(session: Session, tx: Transaction, code: str) -> Transaction:
    """Fija `failure_reason` + `meta.failure_code` (`flush`, sin `commit`)."""
    tx.failure_reason = code
    meta = dict(tx.meta) if isinstance(tx.meta, dict) else {}
    meta["failure_code"] = code
    tx.meta = meta
    session.add(tx)
    session.flush()
    return tx


def _publish_security_event(
    session: Session,
    tx: Transaction,
    *,
    attempt: int,
    blocked: bool,
    reason: str,
    device_id: str | None = None,
) -> None:
    """Enlista `auth.failed_attempt` via outbox (best-effort, sin PII).

    Payload minimo para `risk`/`E2-T20`: ids (`transaction_id`, `user_id`),
    `device_id` esperado, contador y motivo generico (`invalid`/`expired`/
    `blocked`); jamas `nonce`, firmas, claves ni material biometrico.
    """
    try:
        from app.core.outbox import record as outbox_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("biometric outbox no disponible: evento no enlistado")
        return
    try:
        clean_device: str | None = None
        if isinstance(device_id, str) and device_id.strip() and len(device_id) <= 128:
            clean_device = device_id.strip()
        outbox_record(
            session,
            aggregate_type="transaction",
            aggregate_id=tx.id,
            event_type=AUTH_FAILED_ATTEMPT_EVENT,
            payload={
                "transaction_id": str(tx.id),
                "user_id": str(tx.initiator_user_id) if tx.initiator_user_id is not None else None,
                "device_id": clean_device,
                "attempt": int(attempt),
                "blocked": bool(blocked),
                "reason": reason,
            },
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning("biometric evento no enlistado error=%s", type(exc).__name__)


def _signature_digest(signature: str) -> str:
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def _resolve_port(balance_port: Any | None) -> Any:
    if balance_port is not None:
        return balance_port
    try:
        from app.composition import resolve_balance_port as _resolve_bp
    except ImportError:  # pragma: no cover - red de seguridad
        from app.modules.transactions.service import DEFAULT_BALANCE_PORT as _fallback

        return _fallback
    from app.modules.transactions.service import DEFAULT_BALANCE_PORT as _fallback

    return _resolve_bp(None, fallback=_fallback)


def release_pending_holds(session: Session, tx: Transaction, *, balance_port: Any = None) -> int:
    """Libera los holds `ACTIVE` de una operacion en `PENDING` (CA-02).

    Marca `RELEASED`, restaura el disponible por el puerto y postea el
    compensatorio `HOLD_RELEASE` (`2100 -> 2000`, idempotente via
    `find_hold_release`): sin perdida ni doble devolucion. Retorna cuantos
    libero. `flush` sin `commit`. Se reutiliza tambien para el rechazo por
    limite diario al autorizar (E2-T36): el hold sigue `ACTIVE` en
    `AUTHORIZED` (la transicion no toca holds) y se libera igual.
    """
    from app.modules.ledger import service as ledger_service

    port = _resolve_port(balance_port)
    released = 0
    for hold in tx_repository.list_holds_by_transaction(session, tx.id):
        if hold.status != "ACTIVE":
            continue
        tx_repository.update_hold_status(session, hold.id, "RELEASED")
        port.apply_delta(session, hold.account_id, int(hold.amount_minor), hold.currency)
        if (
            ledger_service.find_hold_release(session, transaction_id=tx.id, hold_id=hold.id)
            is not None
        ):
            released += 1
            continue
        available, retained = ledger_service.ensure_customer_accounts(
            session, hold.account_id, hold.currency
        )
        ledger_service.post_entry(
            session,
            entry_type="HOLD_RELEASE",
            transaction_id=tx.id,
            description=f"hold release {hold.id} (tx {tx.id})",
            postings=[
                {
                    "ledger_account_id": retained.id,
                    "direction": "DEBIT",
                    "amount_minor": int(hold.amount_minor),
                    "currency": hold.currency,
                    "account_ref": hold.account_id,
                },
                {
                    "ledger_account_id": available.id,
                    "direction": "CREDIT",
                    "amount_minor": int(hold.amount_minor),
                    "currency": hold.currency,
                    "account_ref": hold.account_id,
                },
            ],
        )
        released += 1
    return released


def request_biometric_challenge(
    session: Session,
    tx_id: uuid.UUID | str,
    *,
    device_id: str | None = None,
    now: datetime | None = None,
    ttl_seconds: int | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    """Emite (o rota) el `nonce` a firmar para una operacion en espera.

    La consumira `POST /transactions/{id}/authorize/challenge` (E2-T19):
    exige estado `PENDING_AUTHORIZATION`, rota `nonce` + vencimiento y
    conserva `failed_attempts` (la ventana de N fallos es por operacion).
    Retorna `{"nonce", "expires_in"}`. `flush` sin `commit`.
    """
    try:
        wanted = tx_id if isinstance(tx_id, uuid.UUID) else uuid.UUID(str(tx_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise TransactionNotFoundError("transaccion inexistente") from exc
    tx = tx_repository.get_transaction(session, wanted)
    if tx is None:
        raise TransactionNotFoundError("transaccion inexistente")
    if tx.status != "PENDING_AUTHORIZATION":
        raise BiometricStateError("la operacion no espera autorizacion biometrica")
    previous = get_block(tx) or {}
    clean_device = _require_device_id(
        device_id if device_id is not None else previous.get("device_id"),
        allow_none=True,
    )
    block, token, expires_at = new_challenge_block(
        clean_device, nonce=nonce, now=now, ttl_seconds=ttl_seconds
    )
    try:
        block["failed_attempts"] = int(previous.get("failed_attempts") or 0)
    except (ValueError, TypeError):
        block["failed_attempts"] = 0
    save_block(session, tx, block)
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()
    logger.info("biometric challenge emitido")
    return {"nonce": token, "expires_in": max(0, int((expires_at - moment).total_seconds()))}


def _fail_attempt(
    session: Session,
    tx: Transaction,
    block: dict[str, Any],
    *,
    reason: str,
    error: BiometricError,
    balance_port: Any = None,
    max_attempts_reader: MaxAttemptsReader | None = None,
) -> Transaction:
    """Registra un intento fallido; si alcanza N, bloquea (CA-02/CA-04).

    Retorna la tx `REJECTED` cuando se activa el bloqueo (el llamante la
    retorna sin completar el pago); si no, lanza `error` (intento
    registrado, la operacion sigue en `PENDING_AUTHORIZATION`).
    """
    read_max = max_attempts_reader or default_max_attempts_reader
    max_attempts = int(read_max(session))
    attempts = int(block.get("failed_attempts") or 0) + 1
    block["failed_attempts"] = attempts
    expected_device = block.get("device_id")
    device_hint = (
        expected_device if isinstance(expected_device, str) and expected_device.strip() else None
    )
    if attempts >= max_attempts:
        release_pending_holds(session, tx, balance_port=balance_port)
        mark_rejected(session, tx, PREVENTIVE_BLOCK)
        save_block(session, tx, block)
        fresh = tx_repository.transition_transaction(
            session, tx.id, "REJECTED", reason=BLOCKED_REASON
        )
        _publish_security_event(
            session,
            fresh,
            attempt=attempts,
            blocked=True,
            reason="blocked",
            device_id=device_hint,
        )
        logger.info("biometric bloqueo preventivo")
        return fresh
    save_block(session, tx, block)
    fresh = tx_repository.get_transaction(session, tx.id)
    _publish_security_event(
        session,
        fresh if fresh is not None else tx,
        attempt=attempts,
        blocked=False,
        reason=reason,
        device_id=device_hint,
    )
    raise error


def authorize_transaction(
    session: Session,
    tx_id: uuid.UUID | str,
    *,
    device_id: str,
    signature: str,
    balance_port: Any = None,
    gateway: Any = None,
    now: datetime | None = None,
    max_attempts_reader: MaxAttemptsReader | None = None,
    binding_reader: BindingReader | None = None,
    signature_verifier: SignatureVerifier | None = None,
    daily_limit_reader: DailyLimitReader | None = None,
) -> Transaction:
    """Autoriza una operacion en `PENDING_AUTHORIZATION` con firma valida.

    Verifica el `nonce` (un solo uso, TTL corto) contra el binding vigente
    por fachada de `identity` (D-03, generico ante cualquier desajuste) y,
    en exito, avanza a `AUTHORIZED` (actor `USER`) y continua el flujo
    (hold reutilizado sin duplicar; `FUNDS_HELD -> POSTED -> SETTLED`).
    Al continuar re-chequea el limite diario (E2-T36): si el acumulado a
    ese instante ya no deja sitio, la operacion queda `REJECTED`
    (`DAILY_LIMIT_EXCEEDED`) con el hold liberado y sin liquidar.
    `gateway` solo aplica a `INTERBANK_TRANSFER` (reanudacion del envio;
    por defecto rige la fabrica por entorno). El reintento con la misma
    firma tras el exito es replay (retorna la operacion sin duplicar
    asientos). Fallos: `NotAuthorizedError` (generico), `ExpiredNonceError`,
    `BiometricStateError`, `TransactionNotFoundError`; tras N fallos
    retorna la operacion `REJECTED` (bloqueo preventivo, CA-02).
    `flush` sin `commit`.
    """
    if (
        not isinstance(device_id, str)
        or not device_id.strip()
        or len(device_id) > 128
        or not isinstance(signature, str)
        or not signature.strip()
    ):
        raise NotAuthorizedError(NOT_AUTHORIZED_MESSAGE)
    presented_device = device_id.strip()
    presented_signature = signature.strip()
    try:
        wanted = tx_id if isinstance(tx_id, uuid.UUID) else uuid.UUID(str(tx_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise TransactionNotFoundError("transaccion inexistente") from exc
    tx = tx_repository.get_transaction(session, wanted)
    if tx is None:
        raise TransactionNotFoundError("transaccion inexistente")
    block = get_block(tx)
    if (
        tx.status != "PENDING_AUTHORIZATION"
        or not isinstance(block, dict)
        or not block.get("nonce")
    ):
        if (
            isinstance(block, dict)
            and block.get("authorized") is True
            and block.get("authorized_signature_digest") == _signature_digest(presented_signature)
        ):
            replayed = tx_repository.get_transaction(session, wanted)
            if replayed is None:  # pragma: no cover - defensivo
                raise TransactionNotFoundError("transaccion inexistente")
            return replayed
        raise BiometricStateError("la operacion no espera autorizacion biometrica")
    if block.get("used") is True:
        raise NotAuthorizedError(NOT_AUTHORIZED_MESSAGE)
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()
    expires_at = _parse_aware(block.get("expires_at"))
    if expires_at is None or expires_at <= moment:
        result = _fail_attempt(
            session,
            tx,
            block,
            reason="expired",
            error=ExpiredNonceError(EXPIRED_NONCE_MESSAGE),
            balance_port=balance_port,
            max_attempts_reader=max_attempts_reader,
        )
        return result
    expected_device = block.get("device_id")
    if isinstance(expected_device, str) and expected_device and expected_device != presented_device:
        return _fail_attempt(
            session,
            tx,
            block,
            reason="invalid",
            error=NotAuthorizedError(NOT_AUTHORIZED_MESSAGE),
            balance_port=balance_port,
            max_attempts_reader=max_attempts_reader,
        )
    read_binding = binding_reader or default_binding_reader
    verify = signature_verifier or default_signature_verifier
    binding: dict | None
    try:
        binding = read_binding(session, tx.initiator_user_id, presented_device)
    except (ValueError, AttributeError, TypeError):
        binding = None
    if (
        not isinstance(binding, dict)
        or binding.get("status") != "ACTIVE"
        or not isinstance(binding.get("public_key"), str)
        or not str(binding.get("public_key")).strip()
    ):
        return _fail_attempt(
            session,
            tx,
            block,
            reason="invalid",
            error=NotAuthorizedError(NOT_AUTHORIZED_MESSAGE),
            balance_port=balance_port,
            max_attempts_reader=max_attempts_reader,
        )
    assert isinstance(binding, dict)
    try:
        valid = bool(
            verify(str(block.get("nonce")), presented_signature, str(binding["public_key"]))
        )
    except Exception:  # noqa: BLE001 - cualquier fallo cripto es generico
        valid = False
    if not valid:
        return _fail_attempt(
            session,
            tx,
            block,
            reason="invalid",
            error=NotAuthorizedError(NOT_AUTHORIZED_MESSAGE),
            balance_port=balance_port,
            max_attempts_reader=max_attempts_reader,
        )
    block["used"] = True
    block["authorized"] = True
    block["device_id"] = presented_device
    block["authorized_signature_digest"] = _signature_digest(presented_signature)
    block["authorized_at"] = moment.isoformat()
    save_block(session, tx, block)
    version = int(tx.version)
    try:
        authorized = tx_repository.transition_transaction(
            session,
            tx.id,
            "AUTHORIZED",
            actor="USER",
            actor_id=tx.initiator_user_id,
            reason=PASSED_REASON,
            expected_version=version,
        )
    except ConcurrencyError:
        fresh = tx_repository.get_transaction(session, tx.id)
        fresh_block = get_block(fresh) if fresh is not None else None
        if (
            fresh is not None
            and fresh.status != "PENDING_AUTHORIZATION"
            and isinstance(fresh_block, dict)
            and fresh_block.get("authorized_signature_digest")
            == _signature_digest(presented_signature)
        ):
            return fresh
        raise
    logger.info("biometric autorizacion superada")
    return _continue_after_authorized(
        session,
        authorized,
        balance_port=balance_port,
        gateway=gateway,
        daily_limit_reader=daily_limit_reader,
    )


def reject_if_daily_limit_exceeded(
    session: Session,
    tx: Transaction,
    *,
    balance_port: Any = None,
    daily_limit_reader: DailyLimitReader | None = None,
) -> Transaction | None:
    """Re-chequea el limite diario al autorizar (E2-T36, HU08 CA-03).

    Calcula `acumulado(exclude_tx_id=tx.id) + total > limite_del_perfil`
    con el perfil del titular (best-effort via `resolve_risk_profile`;
    sin titular no se evalua, ruta legacy). Si excede, la operacion queda
    `REJECTED` (`DAILY_LIMIT_EXCEEDED`, negocio, sin liquidar): fija las
    marcas (`mark_rejected`), libera/compensa el hold con el asiento
    `HOLD_RELEASE` ya existente e idempotente (`release_pending_holds`;
    0 holds en la ruta atomica propia) y transiciona a `REJECTED` (actor
    `SYSTEM`, permitido desde `AUTHORIZED`; motivo `DAILY_LIMIT_REASON`).
    Retorna la tx rechazada, o `None` si cabe en el limite (el llamante
    continua el flujo normal). No emite `auth.failed_attempt` (no es un
    fallo de factor). `flush` sin `commit`.
    """
    user_id = tx.initiator_user_id
    if user_id is None:
        return None
    read_limit = daily_limit_reader or default_daily_limit_reader
    profile = resolve_risk_profile(session, user_id)
    limit = int(read_limit(session, profile))
    total = int(tx.amount_minor) + int(tx.fee_minor or 0)
    accumulated = daily_accumulated_minor(session, user_id, exclude_tx_id=tx.id)
    if (accumulated + total) <= limit:
        return None
    mark_rejected(session, tx, DAILY_LIMIT_EXCEEDED)
    release_pending_holds(session, tx, balance_port=balance_port)
    rejected = tx_repository.transition_transaction(
        session, tx.id, "REJECTED", actor="SYSTEM", reason=DAILY_LIMIT_REASON
    )
    logger.info("biometric rechazo por limite diario al autorizar")
    return rejected


def _continue_after_authorized(
    session: Session,
    authorized: Transaction,
    *,
    balance_port: Any = None,
    gateway: Any = None,
    daily_limit_reader: DailyLimitReader | None = None,
) -> Transaction:
    """Continua el flujo tras el reto superado (firma o PIN, identico).

    Antes de mover nada re-chequea el limite diario con el acumulado a
    ese instante (E2-T36): si `acumulado + total > limite`, la operacion
    queda `REJECTED` (`DAILY_LIMIT_EXCEEDED`) con el hold liberado y sin
    liquidar. En caso contrario `AUTHORIZED -> FUNDS_HELD -> POSTED ->
    SETTLED` reutilizando el hold previo sin duplicar asiento;
    `INTERBANK_TRANSFER` reanuda su saga (`resume_after_authorization`,
    que repite el mismo re-chequeo por defensa). `flush` sin `commit`.
    """
    rejected = reject_if_daily_limit_exceeded(
        session, authorized, balance_port=balance_port, daily_limit_reader=daily_limit_reader
    )
    if rejected is not None:
        return rejected
    if authorized.type == "INTERBANK_TRANSFER":
        from app.modules.transactions.service.interbank import (
            resume_after_authorization as _resume_interbank,
        )

        return _resume_interbank(
            session,
            authorized.id,
            balance_port=balance_port,
            gateway=gateway,
            daily_limit_reader=daily_limit_reader,
        )
    from app.modules.transactions.service import continue_from_authorized as _continue

    resume = (get_block(authorized) or {}).get("resume") or {}
    total_minor = int(authorized.amount_minor) + int(authorized.fee_minor or 0)
    resume_settle = resume.get("settle", True)
    return _continue(
        session,
        authorized,
        source=authorized.source_account_id,
        target=authorized.target_account_id,
        amount_minor=int(authorized.amount_minor),
        currency=authorized.currency,
        fee_minor=int(authorized.fee_minor or 0),
        total_minor=total_minor,
        tx_type=authorized.type,
        settle=True if resume_settle is None else bool(resume_settle),
        atomic=bool(resume.get("atomic", authorized.type == "OWN_TRANSFER")),
        balance_port=balance_port,
        reuse_hold=authorized.type == "THIRD_PARTY_TRANSFER",
    )


def authorize_transaction_with_pin(
    session: Session,
    tx_id: uuid.UUID | str,
    *,
    pin: str,
    balance_port: Any = None,
    gateway: Any = None,
    now: datetime | None = None,
    max_attempts_reader: MaxAttemptsReader | None = None,
    pin_verifier: PinVerifier | None = None,
    daily_limit_reader: DailyLimitReader | None = None,
) -> Transaction:
    """Autoriza una operacion en `PENDING_AUTHORIZATION` con el PIN del titular.

    Variante real alternativa a la firma (E2-T31, HU08): verifica el PIN
    contra la credencial del titular por fachada de `identity` (D-03,
    generico ante cualquier desajuste, sin enumeracion ni oraculo) y, en
    exito, avanza a `AUTHORIZED` (actor `USER`) y continua el flujo
    identico a la firma (hold reutilizado sin duplicar;
    `FUNDS_HELD -> POSTED -> SETTLED`). Al continuar re-chequea el limite
    diario (E2-T36, igual que la firma): si ya no cabe, `REJECTED`
    (`DAILY_LIMIT_EXCEEDED`) con el hold liberado y sin liquidar.
    `gateway` solo aplica a `INTERBANK_TRANSFER` (igual que la firma).

    El PIN incorrecto usa exactamente el mismo contador/bloqueo preventivo
    CA-02 que la firma (`_fail_attempt` + `auth.failed_attempt` sin motivo;
    tras N fallos `REJECTED` con `BIOMETRIC_PREVENTIVE_BLOCK` y hold
    liberado). No se usa el lockout de login del PIN. El `nonce` no aplica
    a esta via (el llamante lo ignora; tampoco hay `EXPIRED_NONCE` aqui:
    ese error es solo de la firma). El PIN jamas se persiste en el bloque
    (solo marcas `authorized`/`authorized_via`). `flush` sin `commit`.
    """
    presented = pin.strip() if isinstance(pin, str) else ""
    if not presented:
        raise NotAuthorizedError(NOT_AUTHORIZED_MESSAGE)
    try:
        wanted = tx_id if isinstance(tx_id, uuid.UUID) else uuid.UUID(str(tx_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise TransactionNotFoundError("transaccion inexistente") from exc
    tx = tx_repository.get_transaction(session, wanted)
    if tx is None:
        raise TransactionNotFoundError("transaccion inexistente")
    block = get_block(tx)
    if (
        tx.status != "PENDING_AUTHORIZATION"
        or not isinstance(block, dict)
        or not block.get("nonce")
    ):
        if (
            isinstance(block, dict)
            and block.get("authorized") is True
            and block.get("authorized_via") == "pin"
        ):
            replayed = tx_repository.get_transaction(session, wanted)
            if replayed is None:  # pragma: no cover - defensivo
                raise TransactionNotFoundError("transaccion inexistente")
            return replayed
        raise BiometricStateError("la operacion no espera autorizacion biometrica")
    if block.get("used") is True:
        raise NotAuthorizedError(NOT_AUTHORIZED_MESSAGE)
    verify = pin_verifier or default_pin_verifier
    try:
        valid = bool(verify(session, tx.initiator_user_id, presented))
    except Exception:  # noqa: BLE001 - cualquier fallo de fachada es generico
        valid = False
    if not valid:
        return _fail_attempt(
            session,
            tx,
            block,
            reason="invalid",
            error=NotAuthorizedError(NOT_AUTHORIZED_MESSAGE),
            balance_port=balance_port,
            max_attempts_reader=max_attempts_reader,
        )
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()
    block["used"] = True
    block["authorized"] = True
    block["authorized_via"] = "pin"
    block["authorized_at"] = moment.isoformat()
    save_block(session, tx, block)
    version = int(tx.version)
    try:
        authorized = tx_repository.transition_transaction(
            session,
            tx.id,
            "AUTHORIZED",
            actor="USER",
            actor_id=tx.initiator_user_id,
            reason=PASSED_REASON,
            expected_version=version,
        )
    except ConcurrencyError:
        fresh = tx_repository.get_transaction(session, tx.id)
        fresh_block = get_block(fresh) if fresh is not None else None
        if (
            fresh is not None
            and fresh.status != "PENDING_AUTHORIZATION"
            and isinstance(fresh_block, dict)
            and fresh_block.get("authorized_via") == "pin"
        ):
            return fresh
        raise
    logger.info("biometric autorizacion superada")
    return _continue_after_authorized(
        session,
        authorized,
        balance_port=balance_port,
        gateway=gateway,
        daily_limit_reader=daily_limit_reader,
    )


__all__ = [
    "AUTH_FAILED_ATTEMPT_EVENT",
    "BLOCKED_REASON",
    "DAILY_LIMIT_EXCEEDED",
    "DAILY_LIMIT_REASON",
    "EXPIRED_NONCE_MESSAGE",
    "META_KEY",
    "NONCE_ENTROPY_BYTES",
    "NONCE_TTL_SECONDS",
    "NOT_AUTHORIZED_MESSAGE",
    "PASSED_REASON",
    "PREVENTIVE_BLOCK",
    "REQUIRED_REASON",
    "BiometricDecision",
    "BiometricError",
    "BiometricStateError",
    "ExpiredNonceError",
    "NotAuthorizedError",
    "TransactionNotFoundError",
    "authorize_transaction",
    "authorize_transaction_with_pin",
    "daily_accumulated_minor",
    "default_binding_reader",
    "default_daily_limit_reader",
    "default_max_attempts_reader",
    "default_pin_verifier",
    "default_signature_verifier",
    "default_threshold_reader",
    "evaluate_gate",
    "failure_markers",
    "get_block",
    "mark_rejected",
    "new_challenge_block",
    "reject_if_daily_limit_exceeded",
    "release_pending_holds",
    "request_biometric_challenge",
    "resolve_risk_profile",
    "save_block",
]
