"""Endpoints de firma del contrato (E3-T14, HU11 CA-02/CA-03/CA-04).

- `POST /credits/applications/{id}/sign/challenge` (Bearer; emite el
  `nonce` de un solo uso a firmar; **sin** `Idempotency-Key`: solo
  rota el reto, no mueve dinero, patron G39).
- `POST /credits/applications/{id}/sign` (Bearer, `Idempotency-Key`
  obligatoria; firma por biometria del dispositivo **o** fallback PIN
  —mutuamente excluyentes—, encola la copia certificada por correo y
  desembolsa **automaticamente** a la cuenta principal (E3-T24, HU12:
  inline idempotente `auto-disburse:<application_id>`, asiento
  `docs/04#7.5`; si el automatico falla la firma queda `SIGNED` con el
  bloque `disbursement: {FAILED}` para reintento por `POST .../disburse`).

Sin logica en el router (`05#1`): valida, traduce y delega al
`service/` (`contract_signing`, que orquesta `signing` de E3-T13 +
`notifications.send` best-effort + desembolso automatico en
`savepoint`). Solo el titular (ajeno/inexistente -> `404` neutro).

Respuestas segun `05#4` (`{"data", "meta"}`) y errores problem+json
via `AppError` (`NOT_AUTHENTICATED` 401, `NOT_AUTHORIZED` 403,
`EXPIRED_NONCE` 400, `NOT_FOUND` 404, `CONFLICT` /
`IDEMPOTENCY_CONFLICT` 409, `VALIDATION_ERROR` 422). OpenAPI
automatico por FastAPI. Sin PII ni secretos en logs (jamas `nonce`,
firma, `pin`, `device_id` ni correo).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.modules.credits.api import get_current_user_id
from app.modules.credits.repository.contracts import (
    ContractConflictError,
    ContractNotFoundError,
    ContractValidationError,
)
from app.modules.credits.schemas.signing import (
    ChallengeRequest,
    ChallengeResponse,
    SignContractRequest,
    SignContractResponse,
)
from app.modules.credits.service import contract_signing as sign_service
from app.modules.credits.service import signing as signing_service

router = APIRouter(tags=["credits"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


def _map_sign_error(exc: Exception) -> AppError:
    """Traduce errores del caso de uso a `AppError` (con rollback fuera)."""
    if isinstance(exc, ContractNotFoundError):
        return AppError(code="NOT_FOUND", message=str(exc), status_code=404)
    if isinstance(exc, signing_service.ContractNotAuthorizedError):
        return AppError(code="NOT_AUTHORIZED", message=str(exc), status_code=403)
    if isinstance(exc, signing_service.ContractNonceExpiredError):
        return AppError(code="EXPIRED_NONCE", message=str(exc), status_code=400)
    if isinstance(exc, ContractConflictError):
        code = "IDEMPOTENCY_CONFLICT" if "Idempotency-Key" in str(exc) else "CONFLICT"
        return AppError(code=code, message=str(exc), status_code=409)
    return AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422)


@router.post(
    "/credits/applications/{application_id}/sign/challenge",
    response_model=ChallengeResponse,
    status_code=200,
    summary="Emite el reto (`nonce`) a firmar para el contrato",
    responses={
        200: {"description": "Reto vigente (`nonce` + `expires_in`)"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        404: {"description": "`NOT_FOUND` (expediente ajeno/inexistente o sin juego)"},
        409: {"description": "`CONFLICT` (ya `SIGNED`)"},
        422: {"description": "`VALIDATION_ERROR` (`device_id` malformado)"},
    },
)
def request_contract_challenge(
    application_id: uuid.UUID,
    request: Request,
    body: ChallengeRequest | None = None,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """Rota el reto de firma del juego `GENERATED` del titular (HU11).

    Reutiliza `identity.domain.nonce` (un solo uso, TTL 120 s, via
    E3-T13): el cliente firma el `nonce` y lo envia a `POST .../sign`.
    Repetir la llamada reemite el reto (sin `Idempotency-Key`, como
    `POST /transactions/{id}/authorize/challenge`, G39); tras un fallo
    `EXPIRED_NONCE` el reintento del `sign` usa una clave NUEVA (el
    fallo quedo snapshotteado bajo la anterior). Sin PII en logs.
    """
    device_id = body.device_id if body is not None else None
    try:
        issued = signing_service.request_signature_challenge(
            db, user_id, application_id, device_id=device_id
        )
    except (ContractNotFoundError, ContractConflictError, ContractValidationError) as exc:
        db.rollback()
        raise _map_sign_error(exc) from exc
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise AppError(code="VALIDATION_ERROR", message=str(exc), status_code=422) from exc
    db.commit()
    return {"data": issued, "meta": {"request_id": _request_id(request)}}


@router.post(
    "/credits/applications/{application_id}/sign",
    response_model=SignContractResponse,
    status_code=200,
    summary="Firma el contrato (biometria o PIN) y envia la copia por correo",
    responses={
        200: {"description": "Juego `SIGNED` + copia encolada/enviada"},
        401: {"description": "`NOT_AUTHENTICATED` (sin JWT valido)"},
        403: {"description": "`NOT_AUTHORIZED` (firma/binding/PIN invalido, generico)"},
        400: {"description": "`EXPIRED_NONCE` (reto vencido: rotar y reintentar)"},
        404: {"description": "`NOT_FOUND` (expediente ajeno/inexistente)"},
        409: {"description": "`CONFLICT` (no `GENERATED`/ya `SIGNED`, o clave con otro cuerpo)"},
        422: {"description": "`VALIDATION_ERROR` (sin `Idempotency-Key` o factor invalido)"},
    },
)
def sign_credit_contract(
    application_id: uuid.UUID,
    body: SignContractRequest,
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Firma el juego, remite la copia certificada al correo y desembolsa (HU11+HU12).

    `Idempotency-Key` obligatoria (crea la firma): misma clave + mismo
    cuerpo = misma respuesta (`meta.replayed`, una firma y **un**
    correo); misma clave + cuerpo distinto = `409`. El correo viaja
    por `notifications.send(channel="email")` **best-effort** (un fallo
    del proveedor no rompe la firma: queda `SIGNED` y la notificacion
    `FAILED`). Tras firmar, el desembolso a la cuenta principal ocurre
    **automaticamente** (E3-T24: inline idempotente, un prestamo por
    expediente; la respuesta trae el bloque aditivo `disbursement` con
    `loan_id`, `transaction_id`, `amount_minor`, `account_masked` y
    `disbursed_at`, o `status="FAILED"` para reintento por
    `POST .../disburse`). Sin PII ni secretos en logs ni respuestas.
    """
    try:
        data, replayed = sign_service.sign_contract_idempotent(
            db,
            user_id,
            application_id,
            device_id=body.device_id,
            signature=body.signature,
            nonce=body.nonce,
            pin=body.pin,
            idempotency_key=idempotency_key,
        )
    except (
        signing_service.ContractNotAuthorizedError,
        signing_service.ContractNonceExpiredError,
    ) as exc:
        # Fallo del factor: ya quedo snapshotteado (patron E2-T19/OBS-3):
        # se confirma para que el reintento identico re-juegue el error.
        db.commit()
        raise _map_sign_error(exc) from exc
    except (
        ContractNotFoundError,
        ContractConflictError,
        ContractValidationError,
        ValueError,
        TypeError,
    ) as exc:
        # Validacion de titular/estado: no consume la clave (revertida,
        # reintentable con la misma clave tras corregir el llamado).
        db.rollback()
        raise _map_sign_error(exc) from exc
    db.commit()
    return {
        "data": data,
        "meta": {"request_id": _request_id(request), "replayed": replayed},
    }


__all__ = ["router"]
