"""Caso de uso: alta de cliente (`onboard_customer`; E1-T03, HU01 CA-03).

Orquesta en UNA transaccion (misma sesion, `flush` sin `commit`): `User` +
`Credential` (schema propio) + cuenta digital (fachada `accounts`) +
subcuentas `2000`/`2100` (fachada `ledger`) + evento `kyc.completed` via
`outbox.record` en la misma sesion (regla de oro 8: nunca publicar dentro
de la transaccion de negocio, solo enlistar).

Todo o nada: no hay `try/except` que oculte el rollback; cualquier fallo
propaga y el llamante revierte (sin usuario a medias). Lo validable se
valida ANTES de crear nada. Sin endpoint (lo invoca E1-T04).

Si `overall_result=false`: no crea nada y retorna el rechazo documentado.

Con `overall_result=true` tambien emite el OTP inicial `ACTIVATION` en la
misma sesion (`flush` sin `commit`) y lo notifica best-effort via la fachada
`notifications.send` (canal/plantilla de E1-T10): si la notificacion falla
no se aborta el alta. Si ya existe un OTP `PENDING` vigente para
(usuario, `ACTIVATION`) se reutiliza sin duplicar. El codigo en claro jamas
sale en la respuesta del alta (solo viaja en la notificacion).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.modules.identity import repository as identity_repo

logger = logging.getLogger(__name__)

#: Evento emitido al completar el alta (regla de oro 8: solo via outbox).
KYC_COMPLETED_EVENT = "kyc.completed"

#: Tipo de agregado para el outbox.
USER_AGGREGATE_TYPE = "user"

#: Proposito OTP del alta (el mismo que activa E1-T10 en HU02).
ACTIVATION_PURPOSE = "ACTIVATION"

#: Canal y plantilla de entrega del codigo inicial (los de E1-T10:
#: `activation.DEFAULT_RESEND_CHANNEL` / `activation.OTP_TEMPLATE_CODE`).
ACTIVATION_OTP_CHANNEL = "sms"
ACTIVATION_OTP_TEMPLATE = "otp_code"


def _new_account_number() -> str:
    return f"001-{uuid.uuid4().hex[:12]}"


def _notify_activation_code(
    session: Session,
    *,
    user_id: uuid.UUID,
    destination: str | None,
    phone: str | None,
    email: str | None,
    plain_code: str,
    ttl_minutes: int,
) -> None:
    """Entrega el codigo inicial via fachada `notifications.send` (best-effort).

    Import perezoso como el resto del caso de uso (sin ciclos entre
    modulos). Un fallo (sin destinatario, canal/plantilla incompatibles,
    proveedor caido) se loguea sin PII ni codigo y NO revierte el alta:
    el OTP ya quedo persistido (hash) y el cliente puede pedir un reenvio
    (E1-T10).
    """
    try:
        from app.modules.notifications.service import send as notifications_send

        recipient = (destination or phone or email or "").strip()
        if not recipient:
            logger.warning("onboard notify_skipped reason=%s", "sin_destinatario")
            return
        notifications_send(
            session,
            channel=ACTIVATION_OTP_CHANNEL,
            recipient=recipient,
            template_code=ACTIVATION_OTP_TEMPLATE,
            data={"code": plain_code, "ttl_minutes": ttl_minutes},
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning("onboard notify_failed error=%s", type(exc).__name__)


def _ensure_initial_activation_otp(
    session: Session,
    *,
    user_id: uuid.UUID,
    phone: str | None,
    email: str | None,
) -> None:
    """Emite el OTP inicial `ACTIVATION` (`flush`, sin `commit`).

    Si ya existe un OTP `PENDING` vigente para (`user_id`, `ACTIVATION`)
    se reutiliza (sin duplicar emision). Si no, genera uno via
    `otp_service.generate_otp` en la misma sesion y lo notifica best-effort
    con el canal/destino de E1-T10. El codigo en claro solo existe en
    memoria para la notificacion (jamas se persiste ni se loguea).
    """
    # Import perezoso: `identity` no se deja importar de forma ciclica
    # (regla de oro 4, fachadas; mismo patron que `onboard_customer`).
    from app.modules.identity.service import otp_service

    existing = identity_repo.get_active_otp(session, user_id, ACTIVATION_PURPOSE)
    if existing is not None:
        expires_at = existing.expires_at
        aware = (
            expires_at
            if expires_at.tzinfo is not None
            else expires_at.replace(tzinfo=UTC)
        )
        if aware > datetime.now(UTC):
            return
    contact = (phone or email or "").strip() or None
    _, plain = otp_service.generate_otp(
        session,
        user_id=user_id,
        purpose=ACTIVATION_PURPOSE,
        destination=contact,
    )
    session.flush()
    _notify_activation_code(
        session,
        user_id=user_id,
        destination=contact,
        phone=phone,
        email=email,
        plain_code=plain,
        ttl_minutes=max(1, otp_service.OTP_TTL_SECONDS // 60),
    )


def onboard_customer(
    session: Session,
    *,
    kyc_result: dict[str, Any],
    first_name: str,
    last_name: str,
    doc_type: str,
    doc_number_masked: str | None = None,
    birth_date: date | None = None,
    email: str | None = None,
    phone: str | None = None,
    initial_pin_hash: str | None = None,
    account_number: str | None = None,
    account_type: str = "AHORRO",
    currency: str = "PEN",
) -> dict[str, Any]:
    """Da de alta al cliente si el KYC fue exitoso (misma sesion, sin `commit`).

    Entrada `kyc_result`: dict con `overall_result` (bool) y
    `doc_number_hash` (str, hash del documento; nunca el numero en claro
    ni material biometrico). Con `overall_result=true` crea en una sola
    transaccion: `User` (`PENDING_ACTIVATION`, `doc_number_hash` UQ),
    `Credential` (con `pin_hash=initial_pin_hash` si viene; si no, queda
    sin PIN y lo pone E1-T14 en la activacion HU02), cuenta digital via
    `accounts.create_account` (que asegura las subcuentas por su fachada
    `ledger`) + verificacion explicita via `ledger.ensure_customer_accounts`
    (idempotente, misma sesion) y `kyc.completed` via `outbox.record`.

    Al final del alta exitosa emite el OTP inicial `ACTIVATION` en la misma
    sesion (`_ensure_initial_activation_otp`: reutiliza el `PENDING` vigente
    si ya existe, si no genera uno + notificacion best-effort via
    `notifications.send` con canal/plantilla de E1-T10; un fallo de envio
    no aborta el alta). El codigo en claro jamas sale en la respuesta.

    Con `overall_result=false` no crea nada y retorna
    `{"status": "REJECTED", "reason": ...}`.
    """
    if not isinstance(kyc_result, dict):
        raise TypeError(f"kyc_result debe ser dict, recibido: {kyc_result!r}")
    overall = kyc_result.get("overall_result")
    if not isinstance(overall, bool):
        raise ValueError("kyc_result.overall_result debe ser bool")
    if not overall:
        return {
            "status": "REJECTED",
            "reason": "kyc_not_passed",
            "detail": "overall_result=false: no se crea usuario, cuenta ni evento",
        }
    doc_hash = kyc_result.get("doc_number_hash")
    if not isinstance(doc_hash, str) or not doc_hash.strip():
        raise ValueError("kyc_result.doc_number_hash es obligatorio")
    number = account_number.strip() if isinstance(account_number, str) else ""
    if not number:
        number = _new_account_number()

    # Imports perezosos: `identity` no se deja importar de forma ciclica
    # por `accounts`/`ledger`/`core.outbox` (regla de oro 4, fachadas).
    from app.core.outbox import record as outbox_record
    from app.modules.accounts.repository import create_account
    from app.modules.ledger.service import ensure_customer_accounts

    user = identity_repo.create_user(
        session,
        doc_type=doc_type,
        doc_number_hash=doc_hash.strip(),
        first_name=first_name,
        last_name=last_name,
        doc_number_masked=doc_number_masked,
        birth_date=birth_date,
        email=email,
        phone=phone,
        status="PENDING_ACTIVATION",
        kyc_status="VERIFIED",
    )
    identity_repo.create_credential(
        session,
        user.id,
        pin_hash=initial_pin_hash,
    )
    account = create_account(
        session,
        user_id=user.id,
        account_number=number,
        type=account_type,
        currency=currency,
    )
    available, hold = ensure_customer_accounts(session, account.id, account.currency)
    account.ledger_account_id = available.id
    account.ledger_hold_account_id = hold.id
    session.flush()

    outbox_record(
        session,
        aggregate_type=USER_AGGREGATE_TYPE,
        aggregate_id=user.id,
        event_type=KYC_COMPLETED_EVENT,
        payload={
            "user_id": str(user.id),
            "account_id": str(account.id),
            "doc_number_hash": user.doc_number_hash,
        },
    )
    session.flush()
    _ensure_initial_activation_otp(
        session, user_id=user.id, phone=phone, email=email
    )
    session.flush()
    return {
        "status": "ONBOARDED",
        "user_id": user.id,
        "account_id": account.id,
        "event_type": KYC_COMPLETED_EVENT,
    }


__all__ = [
    "KYC_COMPLETED_EVENT",
    "USER_AGGREGATE_TYPE",
    "onboard_customer",
]
