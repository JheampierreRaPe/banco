"""Firma del juego contractual (E3-T13, HU11 CA-02/CA-03).

Con el juego `GENERATED` (E3-T12) emite un reto (`nonce` de un solo uso
via `identity.domain.nonce`, TTL 120 s), verifica el factor por
**fachada** de `identity` (D-03) en dos vias mutuamente excluyentes
(decision del dueno, patron E2-T19/E2-T31):

- **dispositivo:** firma del `nonce` con la clave del `device_binding`
  vigente (`device_login.verify_signature` + `get_device_binding`);
- **PIN:** PIN del titular (`pin_authorization.verify_holder_pin`).

En exito sella las tres filas (`doc_hash` + `signed_at` del servidor +
`signature_token_hash`, sello derivado en `domain/signing.py`, sin
migracion), las pasa a `SIGNED` y emite `credit.contract.signed` por
`outbox` + auditoria best-effort. Sin router propio (los endpoints van
en E3-T14); este modulo expone las funciones para ese router.

Frontera (reglas de oro 4/7/8/9):

- `identity` solo por fachada (`device_login`, `pin_authorization`,
  `domain.nonce` reutilizado, decision 4 del brief): jamas se importan
  sus modelos ni repositorios.
- Sin PII ni secretos en logs (jamas firmas, nonces, PIN, claves ni
  contenido): solo IDs/estado/conteos.
- Eventos solo por `outbox` (import perezoso); `flush` sin `commit`.
- **No mueve dinero**: sin `loans`/`loan_schedules`, sin
  `transactions`/`ledger`, sin asientos.
- Cualquier desajuste de firma/binding responde **generico**
  (`NOT_AUTHORIZED`, sin oraculo); solo el TTL agotado distingue
  (`EXPIRED_NONCE`, patron OTP/login).
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.modules.credits import events as credit_events
from app.modules.credits.domain import signing as signing_domain
from app.modules.credits.models import Contract
from app.modules.credits.repository import contracts as contracts_repo
from app.modules.credits.repository.contracts import (
    ContractConflictError,
    ContractError,
    ContractNotFoundError,
    ContractValidationError,
)
from app.modules.identity.domain import nonce as nonce_domain

logger = logging.getLogger(__name__)

#: Mensaje generico estable (firma, binding, dispositivo o PIN: identico,
#: sin oraculo; misma forma que E2-T17/E2-T31).
NOT_AUTHORIZED_MESSAGE = "Firma del contrato invalida"
#: Mensaje de `nonce` vencido (mismo patron que login/biometria).
EXPIRED_NONCE_MESSAGE = "Desafio vencido, solicite uno nuevo"


class ContractNotAuthorizedError(ContractError):
    """Firma/binding/PIN invalido (`NOT_AUTHORIZED`, generico)."""

    code = "NOT_AUTHORIZED"


class ContractNonceExpiredError(ContractError):
    """`nonce` vencido o ya consumido fuera de ventana (`EXPIRED_NONCE`)."""

    code = "EXPIRED_NONCE"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _coerce_owner(owner_user_id: uuid.UUID | str) -> uuid.UUID:
    if isinstance(owner_user_id, uuid.UUID):
        return owner_user_id
    try:
        return uuid.UUID(str(owner_user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ContractValidationError(
            f"user_id debe ser UUID, recibido: {owner_user_id!r}"
        ) from exc


def _load_own_application(session: Session, owner: uuid.UUID, application_id: object):
    """Expediente del titular (ajeno/inexistente -> `NOT_FOUND` neutro)."""
    from app.modules.credits.repository import applications as apps_repo

    row = apps_repo.get_application(session, application_id)  # type: ignore[arg-type]
    if row is None or row.user_id != owner:
        raise ContractNotFoundError("expediente inexistente")
    return row


def _load_signable_set(session: Session, owner: uuid.UUID, application_id: object):
    """Juego `GENERATED` firmable (sin juego -> `NOT_FOUND`; ya firmado -> `CONFLICT`)."""
    app_row = _load_own_application(session, owner, application_id)
    rows = contracts_repo.list_by_application(session, app_row.id)
    if not rows:
        raise ContractNotFoundError("contrato inexistente")
    if any(row.status != "GENERATED" for row in rows):
        raise ContractConflictError("el contrato ya fue firmado o no admite firma")
    return app_row, rows


def _clean_device_id(device_id: object) -> str:
    if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 128:
        raise ContractNotAuthorizedError(NOT_AUTHORIZED_MESSAGE)
    return device_id.strip()


# ------------------------------------------------------- Fachadas (D-03)


def default_binding_reader(session: Session, user_id: uuid.UUID, device_id: str) -> dict | None:
    """Binding por fachada `identity.service.device_login` (solo lectura)."""
    from app.modules.identity.service import device_login as device_login_service

    return device_login_service.get_device_binding(session, user_id, device_id)


def default_signature_verifier(nonce: str, signature: str, public_key: str) -> bool:
    """Firma del `nonce` via fachada `identity.service`."""
    from app.modules.identity.service import device_login as device_login_service

    return device_login_service.verify_signature(nonce, signature, public_key)


def default_pin_verifier(session: Session, user_id: uuid.UUID | str | None, pin: str) -> bool:
    """PIN del titular via fachada `identity.service` (E2-T31)."""
    from app.modules.identity.service import pin_authorization as pin_authorization_service

    return pin_authorization_service.verify_holder_pin(session, user_id, pin)


BindingReader = Callable[[Session, uuid.UUID, str], dict | None]
SignatureVerifier = Callable[[str, str, str], bool]
PinVerifier = Callable[[Session, uuid.UUID | str | None, str], bool]


# ------------------------------------------------------------------ Reto


def request_signature_challenge(
    session: Session,
    owner_user_id: uuid.UUID | str,
    application_id: object,
    *,
    device_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Emite el `nonce` a firmar para el juego `GENERATED` del titular.

    Reutiliza `identity.domain.nonce` (un solo uso, TTL 120 s, sin
    persistir; decision 4 del brief). Sin juego -> `NOT_FOUND` neutro;
    juego ya firmado -> `CONFLICT`. `device_id` malformado ->
    `VALIDATION_ERROR`. Retorna `{"application_id", "nonce",
    "expires_in", "status"}`. `flush` sin `commit` (el dominio nonce es
    en memoria; aqui no hay writes en BD).
    """
    owner = _coerce_owner(owner_user_id)
    app_row, _ = _load_signable_set(session, owner, application_id)
    clean_device: str | None = None
    if device_id is not None:
        if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 128:
            raise ContractValidationError("device_id debe ser texto de 1..128 caracteres")
        clean_device = device_id.strip()
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()
    try:
        nonce, expires_at = nonce_domain.issue_nonce(owner, device_id=clean_device, now=moment)
    except ValueError as exc:
        raise ContractValidationError(str(exc)) from exc
    expires_in = max(0, int((_as_aware(expires_at) - moment).total_seconds()))
    logger.info("credit signature challenge emitido")
    return {
        "application_id": str(app_row.id),
        "nonce": nonce,
        "expires_in": expires_in,
        "status": "GENERATED",
    }


# -------------------------------------------------------------- Sellado


def _record_signed_event(
    session: Session, owner: uuid.UUID, app_id: uuid.UUID, rows: list[Contract], moment: datetime
) -> None:
    """Registra `credit.contract.signed` en el outbox (sin PII)."""
    from app.modules.shared.repository.outbox import insert_event

    insert_event(
        session,
        aggregate_type="credit_contract",
        aggregate_id=rows[0].id,
        event_type=credit_events.CREDIT_CONTRACT_SIGNED,
        payload={
            "contract_ids": [str(row.id) for row in rows],
            "application_id": str(app_id),
            "user_id": str(owner),
            "status": "SIGNED",
            "signed_at": moment.isoformat(),
        },
    )


def _audit_signing(
    session: Session,
    owner: uuid.UUID,
    app_id: uuid.UUID,
    rows: list[Contract],
    *,
    via: str,
    device_id: str | None = None,
) -> None:
    """Auditoria best-effort de la firma (sin PII ni material)."""
    try:
        from app.modules.audit.service import record as audit_record

        audit_record(
            session,
            actor=owner,
            action="credit.contract.signed",
            entity="credit_contract",
            entity_id=app_id,
            metadata={
                "application_id": str(app_id),
                "status": "SIGNED",
                "doc_types": [row.doc_type for row in rows],
                "via": via,
            },
            device_id=device_id,
        )
    except Exception as exc:  # noqa: BLE001 - la auditoria no bloquea la firma
        logger.warning("credit signature audit best-effort omitida err=%s", type(exc).__name__)


def _signed_view(row: Contract, *, signed_at_iso: str, token_hash: str) -> dict[str, Any]:
    """Vista de un documento firmado (metadatos + sello derivado)."""
    from app.modules.credits.service.documents import PUBLIC_DOC_BY_TYPE

    return {
        "contract_id": str(row.id),
        "doc_type": row.doc_type,
        "doc": PUBLIC_DOC_BY_TYPE[row.doc_type],
        "doc_ref": row.doc_ref,
        "doc_hash": row.doc_hash,
        "status": row.status,
        "signed_at": signed_at_iso,
        "seal": signing_domain.seal_for(
            doc_hash=row.doc_hash,
            signed_at_iso=signed_at_iso,
            signature_token_hash=token_hash,
        ),
    }


def _finalize_signing(
    session: Session,
    owner: uuid.UUID,
    app_id: uuid.UUID,
    rows: list[Contract],
    token_hash: str,
    *,
    via: str,
    device_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Estampa, persiste, emite y audita (comun a ambas vias).

    Regla de no reutilizacion: si el token ya sello otra firma se
    rechaza (`CONFLICT`) sin tocar el juego. `flush` sin `commit`.
    """
    if contracts_repo.find_by_token_hash(session, token_hash) is not None:
        raise ContractConflictError("firma ya utilizada en otro contrato")
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()
    for row in rows:
        row.status = "SIGNED"
        row.signed_at = moment
        row.signature_token_hash = token_hash
        session.add(row)
    session.flush()
    _record_signed_event(session, owner, app_id, rows, moment)
    _audit_signing(session, owner, app_id, rows, via=via, device_id=device_id)
    session.flush()
    signed_at_iso = moment.isoformat()
    logger.info("credit contract signed doc_count=%s via=%s", len(rows), via)
    return {
        "application_id": str(app_id),
        "status": "SIGNED",
        "signed_at": signed_at_iso,
        "via": via,
        "documents": [
            _signed_view(row, signed_at_iso=signed_at_iso, token_hash=token_hash) for row in rows
        ],
    }


# ---------------------------------------------------- Via dispositivo


def sign_contract(
    session: Session,
    owner_user_id: uuid.UUID | str,
    application_id: object,
    *,
    device_id: str,
    signature: str,
    nonce: str,
    now: datetime | None = None,
    binding_reader: BindingReader | None = None,
    signature_verifier: SignatureVerifier | None = None,
) -> dict[str, Any]:
    """Firma el juego con la biometria del dispositivo (HU11 CA-02/CA-03).

    Consume el `nonce` de un solo uso, lo liga al titular y verifica la
    firma contra el binding **vigente** (`ACTIVE`) por fachada de
    `identity` (D-03). Cualquier desajuste (nonce inexistente o
    reutilizado, titular distinto, binding ausente/revocado, firma
    invalida) responde el generico `NOT_AUTHORIZED`; solo el TTL
    agotado distingue (`EXPIRED_NONCE`). En exito sella las tres filas
    (`SIGNED` + `signed_at` + `signature_token_hash`, no repudio) y
    emite `credit.contract.signed`. La firma en claro jamas se persiste
    ni se loguea. `flush` sin `commit`.
    """
    owner = _coerce_owner(owner_user_id)
    app_row, rows = _load_signable_set(session, owner, application_id)
    presented_device = _clean_device_id(device_id)
    if not isinstance(signature, str) or not signature.strip():
        raise ContractNotAuthorizedError(NOT_AUTHORIZED_MESSAGE)
    if not isinstance(nonce, str) or not nonce.strip():
        raise ContractNotAuthorizedError(NOT_AUTHORIZED_MESSAGE)
    presented_signature = signature.strip()
    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()

    try:
        entry = nonce_domain.consume_nonce(nonce.strip(), now=moment)
    except nonce_domain.NonceExpiredError as exc:
        raise ContractNonceExpiredError(EXPIRED_NONCE_MESSAGE) from exc
    except (nonce_domain.NonceError, ValueError) as exc:
        # Inexistente o reutilizado (un solo uso): generico, sin filtrar.
        raise ContractNotAuthorizedError(NOT_AUTHORIZED_MESSAGE) from exc
    if entry.user_id != owner:
        raise ContractNotAuthorizedError(NOT_AUTHORIZED_MESSAGE)
    if entry.device_id is not None and entry.device_id != presented_device:
        raise ContractNotAuthorizedError(NOT_AUTHORIZED_MESSAGE)

    read_binding = binding_reader or default_binding_reader
    verify = signature_verifier or default_signature_verifier
    try:
        binding = read_binding(session, owner, presented_device)
    except (ValueError, AttributeError, TypeError):
        binding = None
    if (
        not isinstance(binding, dict)
        or binding.get("status") != "ACTIVE"
        or not isinstance(binding.get("public_key"), str)
        or not str(binding.get("public_key")).strip()
    ):
        raise ContractNotAuthorizedError(NOT_AUTHORIZED_MESSAGE)
    try:
        valid = bool(verify(entry.nonce, presented_signature, str(binding["public_key"])))
    except Exception:  # noqa: BLE001 - cualquier fallo cripto es generico
        valid = False
    if not valid:
        raise ContractNotAuthorizedError(NOT_AUTHORIZED_MESSAGE)

    token_hash = signing_domain.token_hash_for(
        signature=presented_signature,
        nonce=entry.nonce,
        device_id=presented_device,
        application_id=str(app_row.id),
    )
    return _finalize_signing(
        session,
        owner,
        app_row.id,
        rows,
        token_hash,
        via="device",
        device_id=presented_device,
        now=moment,
    )


# ----------------------------------------------------------- Via PIN


def sign_contract_with_pin(
    session: Session,
    owner_user_id: uuid.UUID | str,
    application_id: object,
    *,
    pin: str,
    now: datetime | None = None,
    pin_verifier: PinVerifier | None = None,
) -> dict[str, Any]:
    """Firma el juego con el PIN del titular (fallback, patron E2-T31).

    Verifica el PIN por fachada de `identity` (comparacion en tiempo
    constante, rama ciega sin credencial) y sella igual que la via
    dispositivo. El PIN en claro jamas se persiste (solo el hash del
    token con sal aleatoria) ni se refleja en respuestas/logs; el fallo
    responde el mismo generico `NOT_AUTHORIZED` (sin oraculo entre
    vias). El `nonce` no aplica a esta via (igual que E2-T31).
    `flush` sin `commit`.
    """
    owner = _coerce_owner(owner_user_id)
    app_row, rows = _load_signable_set(session, owner, application_id)
    presented = pin.strip() if isinstance(pin, str) else ""
    if not presented:
        raise ContractNotAuthorizedError(NOT_AUTHORIZED_MESSAGE)
    verify = pin_verifier or default_pin_verifier
    try:
        valid = bool(verify(session, owner, presented))
    except Exception:  # noqa: BLE001 - cualquier fallo de fachada es generico
        valid = False
    if not valid:
        raise ContractNotAuthorizedError(NOT_AUTHORIZED_MESSAGE)

    moment = _as_aware(now) if isinstance(now, datetime) else _utcnow()
    token_hash = signing_domain.token_hash_for_pin(
        application_id=str(app_row.id),
        signed_at_iso=moment.isoformat(),
        salt_hex=secrets.token_hex(16),
    )
    return _finalize_signing(session, owner, app_row.id, rows, token_hash, via="pin", now=moment)


__all__ = [
    "EXPIRED_NONCE_MESSAGE",
    "NOT_AUTHORIZED_MESSAGE",
    "BindingReader",
    "ContractError",
    "ContractNonceExpiredError",
    "ContractNotAuthorizedError",
    "PinVerifier",
    "SignatureVerifier",
    "default_binding_reader",
    "default_pin_verifier",
    "default_signature_verifier",
    "request_signature_challenge",
    "sign_contract",
    "sign_contract_with_pin",
]
