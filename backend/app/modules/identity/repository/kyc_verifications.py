"""Persistencia de verificaciones KYC + auditoria via fachada (E1-T04).

HU01 CA-02 (registrar el resultado del KYC) y CA-04 (motivo del rechazo):
un registro en `identity.kyc_verifications` por intento (exito o fallo con
`failure_reason`), mas una fila en `audit_log` via `audit.service.record`
en la misma sesion (`kyc.verified`/`kyc.failed`, entidad
`kyc_verification`).

Convencion (igual que el resto del repositorio identity): `flush` sin
`commit`; quien llama decide la transaccion. Este repositorio NO escribe
`audit_log` directamente (regla de oro 4): importa la fachada de forma
perezosa dentro de la funcion (sin ciclos entre modulos).

Prohibido persistir frames/imagenes (regla de oro 7 y `03b#4.4`):
`_reject_biometric_material` inspecciona los JSON y el hash del token antes
de insertar y rechaza claves de imagen/frame/base64 o URIs
`data:image/...`; jamas se guardan frames.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.identity.models import KycVerification

PROVIDER_DEFAULT = "facial-kyc-service"

MAX_FAILURE_REASON = 500

_FORBIDDEN_KEY_PARTS = ("frame", "image", "base64")
_FORBIDDEN_KEY_EXACT = {
    "photo",
    "selfie",
    "portrait",
    "picture",
    "video",
    "biometric",
    "liveness_image",
}
_DATA_URI_PREFIXES = ("data:image/", "data:video/")


def _iter_strings(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            yield ("key", key)
            yield from _iter_strings(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_strings(item)
    elif isinstance(node, str):
        yield ("value", node)


def _reject_biometric_material(*payloads: dict | None, field: str) -> None:
    """Rechaza material biometrico en un payload JSON (claves o data-URIs)."""
    for payload in payloads:
        if payload is None:
            continue
        if not isinstance(payload, dict):
            raise TypeError(f"{field} debe ser dict o None, recibido: {payload!r}")
        for kind, text in _iter_strings(payload):
            lowered = text.lower()
            if kind == "key" and (
                lowered in _FORBIDDEN_KEY_EXACT
                or any(part in lowered for part in _FORBIDDEN_KEY_PARTS)
            ):
                raise ValueError(
                    f"{field} contiene material biometrico prohibido (clave: {text!r})"
                )
            if kind == "value" and lowered.startswith(_DATA_URI_PREFIXES):
                raise ValueError(f"{field} contiene material biometrico prohibido (data-URI)")


def _coerce_user_id(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"user_id debe ser UUID, recibido: {value!r}") from exc


def save_verification(
    session: Session,
    *,
    user_id: uuid.UUID | str | None,
    overall_result: bool,
    document_json: dict | None = None,
    liveness_json: dict | None = None,
    face_match_json: dict | None = None,
    challenge_token_hash: str | None = None,
    failure_reason: str | None = None,
    provider: str = PROVIDER_DEFAULT,
) -> KycVerification:
    """Guarda un intento de KYC y lo audita (`flush`, sin `commit`).

    `user_id` puede ser `None` (aun no existe usuario, segun `03b#4.4`).
    El fallo exige `failure_reason` (CA-04); el exito no admite motivo.
    Tras el `flush` del intento, registra `kyc.verified`/`kyc.failed` con
    la fachada `audit.service.record` (misma sesion, `actor=user_id`,
    `entity=kyc_verification`).
    """
    uid = _coerce_user_id(user_id)
    if not isinstance(overall_result, bool):
        raise TypeError(f"overall_result debe ser bool, recibido: {overall_result!r}")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider es obligatorio")
    if len(provider.strip()) > 50:
        raise ValueError("provider supera 50 caracteres")
    _reject_biometric_material(document_json, field="document_json")
    _reject_biometric_material(liveness_json, field="liveness_json")
    _reject_biometric_material(face_match_json, field="face_match_json")
    if challenge_token_hash is not None:
        if not isinstance(challenge_token_hash, str) or not challenge_token_hash.strip():
            raise ValueError("challenge_token_hash debe ser texto no vacio")
        if len(challenge_token_hash.strip()) > 128:
            raise ValueError("challenge_token_hash supera 128 caracteres")
        lowered = challenge_token_hash.strip().lower()
        if lowered.startswith(_DATA_URI_PREFIXES) or any(
            part in lowered for part in _FORBIDDEN_KEY_PARTS
        ):
            raise ValueError("challenge_token_hash contiene material biometrico prohibido")
    if failure_reason is not None and (
        not isinstance(failure_reason, str) or not failure_reason.strip()
    ):
        raise ValueError("failure_reason debe ser texto no vacio")
    if not overall_result and failure_reason is None:
        raise ValueError("failure_reason es obligatorio cuando overall_result es false")
    if overall_result and failure_reason is not None:
        raise ValueError("failure_reason solo aplica cuando overall_result es false")
    if failure_reason is not None and len(failure_reason.strip()) > MAX_FAILURE_REASON:
        raise ValueError(f"failure_reason supera {MAX_FAILURE_REASON} caracteres")

    row = KycVerification(
        user_id=uid,
        provider=provider.strip(),
        overall_result=overall_result,
        document_json=document_json,
        liveness_json=liveness_json,
        face_match_json=face_match_json,
        challenge_token_hash=(
            challenge_token_hash.strip() if challenge_token_hash is not None else None
        ),
        failure_reason=(failure_reason.strip() if failure_reason is not None else None),
    )
    session.add(row)
    session.flush()

    from app.modules.audit import service as audit_service  # perezoso: sin ciclos

    audit_service.record(
        session,
        actor=uid,
        action="kyc.verified" if overall_result else "kyc.failed",
        entity="kyc_verification",
        entity_id=row.id,
        metadata={
            "overall_result": overall_result,
            "provider": row.provider,
            "failure_reason": row.failure_reason,
        },
    )
    return row


def get_verification(session: Session, verification_id: uuid.UUID | str) -> KycVerification | None:
    """Lee un intento de KYC por id (`None` si no existe)."""
    if isinstance(verification_id, uuid.UUID):
        key = verification_id
    else:
        try:
            key = uuid.UUID(str(verification_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError(
                f"verification_id debe ser UUID, recibido: {verification_id!r}"
            ) from exc
    return session.get(KycVerification, key)


def list_by_user(session: Session, user_id: uuid.UUID | str | None) -> list[KycVerification]:
    """Intentos de KYC de un usuario (mas recientes primero)."""
    stmt = (
        sa.select(KycVerification)
        .where(KycVerification.user_id == _coerce_user_id(user_id))
        .order_by(KycVerification.created_at.desc())
    )
    return list(session.scalars(stmt).all())


__all__ = [
    "MAX_FAILURE_REASON",
    "PROVIDER_DEFAULT",
    "get_verification",
    "list_by_user",
    "save_verification",
]
