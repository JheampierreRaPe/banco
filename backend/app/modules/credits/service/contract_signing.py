"""Firma idempotente del contrato + copia certificada por correo (E3-T14, HU11).

Orquesta, en la misma transaccion del llamante (`flush` sin `commit`):

1. la firma del juego via `service/signing.py` (E3-T13) en sus dos
   vias mutuamente excluyentes (decision del dueno, patron
   E2-T19/E2-T31): biometria del dispositivo (`device_id` +
   `signature` del `nonce`) o fallback PIN (`pin`);
2. el envio best-effort de la copia certificada por correo via la
   fachada `notifications.send(channel="email", ...)` (plantilla
   aditiva `credit_contract_signed`; sender real por env
   `EMAIL_PROVIDER=gmail` via `GmailNotificationSender`, mock por
   defecto);
3. la idempotencia por `Idempotency-Key` obligatoria sobre
   `shared.idempotency_keys` (patron `documents.py`/`04#5`).

Semantica de la clave (patron E2-T19/E2-T21/G39):

- misma clave + mismo cuerpo -> replay (`replayed=True`): una sola
  firma y **un solo correo** (el replay no re-firma ni reenvia);
- misma clave + cuerpo distinto -> `409` (`IDEMPOTENCY_CONFLICT`);
- el fallo del factor (`NOT_AUTHORIZED`/`EXPIRED_NONCE`) tambien se
  snapshottea: el reintento identico re-juega el error y el reintento
  real usa **clave nueva** tras rotar el reto (`.../sign/challenge`);
- las validaciones de titular/estado (`NOT_FOUND`/`CONFLICT`/
  `VALIDATION_ERROR`) **no consumen la clave** (el endpoint revierte).

El correo se dispara **solo tras** la firma exitosa y es
**best-effort**: si no hay correo resoluble o el proveedor cae, la
firma queda `SIGNED` igual (la notificacion queda `FAILED` u omitida;
sin PII en logs). Sin PII ni secretos en logs ni en el snapshot
(`pin`/`signature`/`nonce` jamas se persisten: solo digests HMAC en el
`request_hash`, patron E2-T31 endurecido: el PIN usa HMAC con el secreto
del servidor, no SHA-256 sin sal).

Desembolso automatico (E3-T24, HU12, decision del dueno: inline, no
consumidor): tras firmar, invoca en `savepoint`
`disburse_application_idempotent` con `account_id=None` (cuenta
principal) y clave estable `auto-disburse:<application_id>` (un
prestamo por expediente; asiento `docs/04#7.5` por el motor). Si el
automatico falla, la firma queda `SIGNED` y la respuesta expone el
bloque `disbursement: {FAILED}` para reintento por
`POST .../disburse` (contrato intacto).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.modules.credits.repository.contracts import (
    ContractConflictError,
    ContractNotFoundError,
    ContractValidationError,
)
from app.modules.credits.service import signing as signing_service

logger = logging.getLogger(__name__)

#: Endpoint logico para la idempotencia del `POST .../sign` (clave `shared`, UQ por usuario).
IDEMPOTENCY_ENDPOINT = "/credits/applications/sign"

#: Plantilla email de la copia certificada (aditiva, `notifications`, canal `email`).
EMAIL_TEMPLATE_CODE = "credit_contract_signed"

#: Prefijo de la `Idempotency-Key` estable del desembolso automatico
#: (E3-T24, HU12): un prestamo por expediente aunque la firma se
#: reintente o el worker re-juegue el evento (`auto-disburse:<application_id>`).
AUTO_DISBURSE_KEY_PREFIX = "auto-disburse"

#: Mensaje generico del fallo del automatico (sin PII; el reintento va
#: por `POST .../disburse`, que se conserva con su contrato intacto).
_AUTO_DISBURSE_RETRY_MESSAGE = (
    "La firma quedo registrada, pero el desembolso automatico no liquido. "
    "Reintenta el desembolso desde tu expediente."
)

#: Marca de snapshot de error (los `data` de exito llevan `status=SIGNED`).
_ERROR_STATUS = "ERROR"


def _utcnow_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _coerce_owner(owner_user_id: uuid.UUID | str) -> uuid.UUID:
    if isinstance(owner_user_id, uuid.UUID):
        return owner_user_id
    try:
        return uuid.UUID(str(owner_user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ContractValidationError(
            f"user_id debe ser UUID, recibido: {owner_user_id!r}"
        ) from exc


#: Contexto del HMAC del PIN (separa este dominio de otros usos del secreto).
_PIN_HMAC_CONTEXT = b"credit-sign-pin/v1:"

#: Fallback solo para `local`/pruebas cuando no hay secreto configurado
#: (nunca se loguea; fuera de `local` el arranque ya exige `jwt_secret`).
_DEV_PIN_HMAC_KEY = "banca-online-dev-pin-hmac"


def _pin_hmac_key() -> bytes:
    """Secreto de servidor para el digest del PIN (jamas se persiste ni se loguea)."""
    from app.core.config import get_settings

    secret = str(get_settings().jwt_secret or "")
    if not secret.strip():
        secret = _DEV_PIN_HMAC_KEY
    return secret.encode("utf-8")


def _pin_digest(pin: str) -> str:
    """Digest HMAC del PIN para el `request_hash` (fix validador sprint-2).

    El PIN jamas queda en `idempotency_keys` (es un secreto): solo su
    HMAC-SHA256 hex con el secreto del servidor, que distingue un PIN
    de otro (mismo PIN + misma clave = replay; PIN distinto + misma
    clave = `409`) sin ser reversible por fuerza bruta (un SHA-256 sin
    sal de un PIN de 6 digitos si lo era).
    """
    return hmac.new(
        _pin_hmac_key(), _PIN_HMAC_CONTEXT + pin.strip().encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _digest_for(
    application_id: object,
    *,
    device_id: str | None,
    signature: str | None,
    nonce: str | None,
    pin: str | None,
) -> str:
    """Hash canonico del cuerpo (solo el digest persiste, nunca secretos)."""
    from app.modules.shared.repository.idempotency import compute_request_hash

    if pin is not None:
        body: dict[str, Any] = {
            "application_id": str(application_id),
            "pin_digest": _pin_digest(pin),
        }
    else:
        body = {
            "application_id": str(application_id),
            "device_id": device_id,
            "signature": signature,
            "nonce": nonce,
        }
    return compute_request_hash(body)


def _check_factor(*, signature: str | None, pin: str | None) -> None:
    """Defensa en profundidad (el esquema ya exige un solo factor)."""
    if (signature is None) == (pin is None):
        raise ContractValidationError("se exige exactamente un factor: `signature` o `pin`")


def _default_email_sender():
    """Sender de email vigente (`EMAIL_PROVIDER`, patron `notifications`)."""
    from app.modules.notifications.service import sender_for_channel

    return sender_for_channel("email")


def _default_email_resolver():
    """Resolutor del correo del titular (fachada `identity`, D-03)."""
    from app.modules.identity.service import holder_contact as holder_contact_service

    return holder_contact_service.get_holder_email


def _send_copy_email(
    session: Session,
    owner: uuid.UUID,
    application_id: object,
    view: dict[str, Any],
    *,
    sender=None,
    email_resolver=None,
) -> None:
    """Copia certificada por correo (best-effort, tras la firma exitosa).

    Resuelve el destinatario (`users.email` por fachada de `identity`),
    envia por la fachada `notifications.send` con el sender vigente
    (real `GmailNotificationSender` con `EMAIL_PROVIDER=gmail` +
    `GMAIL_USER`/`GMAIL_APP_PASSWORD` en `.env`; mock sin red por
    defecto) y nunca bloquea la firma: sin correo resoluble o con el
    proveedor caido se registra el motivo (sin PII) y la firma queda
    `SIGNED` igual. `flush` sin `commit` (decide el endpoint).
    """
    resolve = email_resolver or _default_email_resolver()
    try:
        recipient = resolve(session, owner)
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning("credit sign copy_skipped reason=%s", type(exc).__name__)
        return
    if not isinstance(recipient, str) or not recipient.strip():
        logger.warning("credit sign copy_skipped reason=sin_destinatario")
        return
    sender_obj = sender if sender is not None else _default_email_sender()
    try:
        from app.modules.notifications import service as notifications_service

        notifications_service.send(
            session,
            channel="email",
            recipient=recipient.strip(),
            template_code=EMAIL_TEMPLATE_CODE,
            data={
                "application_ref": str(application_id)[:8],
                "signed_at": str(view.get("signed_at") or _utcnow_iso()),
            },
            user_id=owner,
            sender=sender_obj,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort: no bloquea la firma
        logger.warning(
            "credit sign copy_failed channel=email template=%s error=%s",
            EMAIL_TEMPLATE_CODE,
            type(exc).__name__,
        )
        return
    logger.info("credit sign copy_queued channel=email template=%s", EMAIL_TEMPLATE_CODE)


def _view_to_data(application_id: object, view: dict[str, Any]) -> dict[str, Any]:
    """Proyecta la vista de `signing` al `data` del `POST .../sign`."""
    documents = view.get("documents") or []
    return {
        "application_id": str(application_id),
        "contract_id": str(documents[0]["contract_id"]) if documents else "",
        "status": str(view.get("status") or "SIGNED"),
        "signed_at": str(view.get("signed_at") or ""),
        "via": str(view.get("via") or ""),
        "documents": [
            {
                "contract_id": str(doc.get("contract_id")),
                "doc_type": str(doc.get("doc_type")),
                "doc": str(doc.get("doc")),
                "doc_ref": str(doc.get("doc_ref")),
                "doc_hash": str(doc.get("doc_hash")),
                "status": str(doc.get("status")),
                "signed_at": str(doc.get("signed_at")),
                "seal": str(doc.get("seal")),
            }
            for doc in documents
        ],
    }


def _error_snapshot(application_id: object, code: str, message: str) -> dict[str, Any]:
    """Snapshot de un fallo del factor (re-jugable, sin PII ni secretos)."""
    return {
        "application_id": str(application_id),
        "status": _ERROR_STATUS,
        "code": code,
        "message": message,
    }


def _error_for_snapshot(snapshot: dict[str, Any]) -> Exception:
    """Reconstruye el error desde un snapshot `ERROR` (para el replay)."""
    code = str(snapshot.get("code") or "")
    message = str(snapshot.get("message") or "firma invalida")
    if code == "EXPIRED_NONCE":
        return signing_service.ContractNonceExpiredError(message)
    if code == "NOT_AUTHORIZED":
        return signing_service.ContractNotAuthorizedError(message)
    if code == "NOT_FOUND":
        return ContractNotFoundError(message)
    return ContractConflictError(message)


def _execute_sign(
    session: Session,
    row,
    owner: uuid.UUID,
    application_id: object,
    *,
    device_id: str | None,
    signature: str | None,
    nonce: str | None,
    pin: str | None,
    sender=None,
    email_resolver=None,
    binding_reader=None,
    signature_verifier=None,
    pin_verifier=None,
) -> tuple[dict[str, Any], uuid.UUID | None]:
    """Ejecuta firma + correo y persiste el snapshot (exito o fallo del factor).

    El fallo del factor (`NOT_AUTHORIZED`/`EXPIRED_NONCE`) se guarda
    ANTES de responder (patron E2-T19/OBS-3: el reintento identico
    re-juega el error sin consumir otro `nonce`; el real usa clave
    nueva tras rotar el reto). Retorna `(data, transaction_id)`.
    """
    from app.modules.shared.repository.idempotency import store_response

    try:
        if pin is not None:
            view = signing_service.sign_contract_with_pin(
                session,
                owner,
                application_id,
                pin=pin,
                pin_verifier=pin_verifier,
            )
        else:
            view = signing_service.sign_contract(
                session,
                owner,
                application_id,
                device_id=device_id or "",
                signature=signature or "",
                nonce=nonce or "",
                binding_reader=binding_reader,
                signature_verifier=signature_verifier,
            )
    except (
        signing_service.ContractNotAuthorizedError,
        signing_service.ContractNonceExpiredError,
    ) as exc:
        snapshot = _error_snapshot(application_id, exc.code, str(exc))
        store_response(session, row, response_snapshot=snapshot, transaction_id=None)
        raise
    data = _view_to_data(application_id, view)
    _send_copy_email(
        session,
        owner,
        application_id,
        view,
        sender=sender,
        email_resolver=email_resolver,
    )
    # Desembolso automatico a la cuenta principal (E3-T24, HU12,
    # decision del dueno: inline tras firmar, no consumidor del evento).
    # En `savepoint`: si el automatico falla, se revierte SOLO el
    # desembolso y la firma queda `SIGNED` con el fallo expuesto para
    # reintento (`POST .../disburse`, contrato intacto). Campos aditivos
    # en la respuesta (`loan_id`, `transaction_id`, `amount_minor`,
    # `account_masked`, `disbursed_at`).
    data["disbursement"] = _try_auto_disburse(session, owner, application_id)
    first_id = data["contract_id"]
    try:
        transaction_id = uuid.UUID(str(first_id))
    except (ValueError, AttributeError, TypeError):
        transaction_id = None
    store_response(session, row, response_snapshot=data, transaction_id=transaction_id)
    logger.info("credit sign ok via=%s", data["via"])
    return data, transaction_id


def _failure_code(exc: Exception) -> str:
    """Codigo de negocio del fallo del automatico (sin PII, sin oraculo)."""
    from app.modules.credits.repository.applications import (
        ApplicationConflictError,
        ApplicationNotFoundError,
    )

    if isinstance(exc, ApplicationNotFoundError):
        return "NOT_FOUND"
    if isinstance(exc, ApplicationConflictError):
        return "CONFLICT"
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()[:32]
    return "VALIDATION_ERROR"


def _try_auto_disburse(
    session: Session,
    owner: uuid.UUID,
    application_id: object,
) -> dict[str, Any]:
    """Desembolso automatico tras la firma (E3-T24, best-effort parcial).

    Invoca `disburse_application_idempotent` con `account_id=None`
    (cuenta principal: la `ACTIVE` mas antigua) y clave estable
    `auto-disburse:<application_id>` (un prestamo por expediente). En
    `savepoint`: el exito deja `loans`+cronograma+asiento `docs/04#7.5`
    + `credit.disbursed` + notificacion dedicada (todo en la misma
    transaccion de la firma); el fallo revierte solo el desembolso y
    retorna el bloque `FAILED` para reintento, sin romper la firma.
    Sin PII ni secretos en logs (la cuenta solo enmascarada).
    """
    from app.modules.credits.service import disbursement as disburse_service

    key = f"{AUTO_DISBURSE_KEY_PREFIX}:{application_id}"
    savepoint = session.begin_nested()
    try:
        view, _replayed = disburse_service.disburse_application_idempotent(
            session,
            owner,
            application_id,
            account_id=None,
            idempotency_key=key,
        )
    except Exception as exc:  # noqa: BLE001 - el fallo no rompe la firma (reintento manual)
        try:
            savepoint.rollback()
        except Exception:  # noqa: BLE001, S110 - limpieza best-effort
            pass
        code = _failure_code(exc)
        logger.warning("credit sign auto_disburse_failed code=%s", code)
        return {
            "status": "FAILED",
            "code": code,
            "message": _AUTO_DISBURSE_RETRY_MESSAGE,
        }
    else:
        try:
            savepoint.commit()
        except Exception as exc:  # noqa: BLE001 - defensivo: el fallo revierte lo parcial
            try:
                savepoint.rollback()
            except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                pass
            logger.warning("credit sign auto_disburse_failed code=%s", _failure_code(exc))
            return {
                "status": "FAILED",
                "code": "VALIDATION_ERROR",
                "message": _AUTO_DISBURSE_RETRY_MESSAGE,
            }
    logger.info(
        "credit sign auto_disburse_ok status=%s account=%s",
        view.get("status"),
        view.get("account_masked"),
    )
    return {
        "status": str(view.get("status") or "SETTLED"),
        "loan_id": str(view.get("loan_id") or ""),
        "transaction_id": str(view.get("transaction_id") or ""),
        "amount_minor": int(view.get("amount_minor") or 0),
        "account_masked": str(view.get("account_masked") or ""),
        "disbursed_at": str(view.get("disbursed_at") or ""),
    }


def sign_contract_idempotent(
    session: Session,
    owner_user_id: uuid.UUID | str,
    application_id: object,
    *,
    device_id: str | None = None,
    signature: str | None = None,
    nonce: str | None = None,
    pin: str | None = None,
    idempotency_key: str | None,
    sender=None,
    email_resolver=None,
    binding_reader=None,
    signature_verifier=None,
    pin_verifier=None,
) -> tuple[dict[str, Any], bool]:
    """Firma con `Idempotency-Key` obligatoria + copia por correo (crea firma).

    Retorna `(data, replayed)`: `201/200` lo decide el endpoint (`200`
    en este contrato, `05#64`). Los errores de titular/estado
    (`NOT_FOUND`/`CONFLICT`/`VALIDATION_ERROR`) no consumen la clave
    (el endpoint revierte); los del factor ya quedaron snapshotteados
    (el endpoint confirma). Sin `commit` (decide el llamante).
    """
    from app.modules.shared.repository.idempotency import (
        find_key,
        is_expired,
        renew_expired,
        resolve_ttl_seconds,
        try_claim,
    )

    owner = _coerce_owner(owner_user_id)
    key = str(idempotency_key).strip() if isinstance(idempotency_key, str) else ""
    if not key:
        raise ContractValidationError("cabecera Idempotency-Key obligatoria")
    if len(key) > 80:
        raise ContractValidationError("Idempotency-Key supera 80 caracteres")
    _check_factor(signature=signature, pin=pin)
    digest = _digest_for(
        application_id,
        device_id=device_id,
        signature=signature,
        nonce=nonce,
        pin=pin,
    )

    def _run(row) -> tuple[dict[str, Any], uuid.UUID | None]:
        return _execute_sign(
            session,
            row,
            owner,
            application_id,
            device_id=device_id,
            signature=signature,
            nonce=nonce,
            pin=pin,
            sender=sender,
            email_resolver=email_resolver,
            binding_reader=binding_reader,
            signature_verifier=signature_verifier,
            pin_verifier=pin_verifier,
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
        data, _ = _run(row)
        return data, False
    stored = find_key(session, key=key, user_id=owner)
    if stored is None:  # pragma: no cover - carrera extrema
        raise ContractConflictError("clave de idempotencia en conflicto")
    if is_expired(stored):
        renew_expired(
            session,
            stored,
            request_hash=digest,
            endpoint=IDEMPOTENCY_ENDPOINT,
            method="POST",
        )
        data, _ = _run(stored)
        return data, False
    if stored.request_hash != digest:
        raise ContractConflictError("Idempotency-Key ya usada con un cuerpo distinto")
    snapshot = stored.response_snapshot
    if not isinstance(snapshot, dict) or not snapshot:
        raise ContractConflictError("operacion en curso para esta Idempotency-Key")
    if snapshot.get("status") == _ERROR_STATUS:
        raise _error_for_snapshot(snapshot)
    logger.info("credit sign replay")
    return dict(snapshot), True


__all__ = [
    "EMAIL_TEMPLATE_CODE",
    "IDEMPOTENCY_ENDPOINT",
    "sign_contract_idempotent",
]
