"""Servicio minimo de perfiles de riesgo (E2-T18, HU08) + consumo de
fallos de autorizacion (E2-T20, HU08 CA-04/CA-02).

Punto de entrada estable para los consumidores (`E2-T17` motor,
`E2-T20` consumo de alertas) y para HU20 (E6-T03/E6-T04, que ampliara
el perfil con el historial y creara `alerts`/`risk_rules`): delega en
el repositorio sin logica de negocio propia. Sin endpoints aqui (estan
en `risk/api`, HU20).

Consumo `auth.failed_attempt` (E2-T20, fuera de la transaccion de
negocio, via outbox -> bus -> `risk.events.on_auth_failed_attempt`):

- Idempotente por `event_id` (`shared.processed_events`, consumidor
  `"risk-auth-failures"`): el replay no duplica perfil, auditoria ni
  notificacion.
- Registra el intento en `risk_profiles` (contador via `score`,
  `last_device_id`): `+1` por intento, `+4` extra ante bloqueo
  preventivo (`blocked: true`, CA-02). Constantes provisionales de este
  modulo: el motor antifraude HU20 (E6-T03/E6-T04) es dueno de la
  sintonia fina (D-16); aqui solo se materializa la severidad minima.
- Notifica al cliente con la fachada `notifications.send` (canal push,
  plantillas `auth_failed_attempt`/`auth_blocked`, sin PII en el texto;
  entrega best-effort con reintentos propios de la fachada).
- Deja auditoria (`audit.record`, best-effort: un fallo de auditoria se
  loguea sin revertir el registro de riesgo ni la notificacion).
- Nunca mueve dinero (regla de oro 1; este modulo no toca `ledger`).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.modules.risk import repository as risk_repo
from app.modules.risk.models import RiskProfile

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIT_ACTION_BLOCKED",
    "AUDIT_ACTION_FAILED",
    "AUDIT_ACTION_UNRECOGNIZED",
    "AUTH_FAILED_ATTEMPT_EVENT",
    "CONSUMER",
    "CONSUMER_UNRECOGNIZED",
    "NOTIFY_CHANNEL",
    "SCORE_EXTRA_ON_BLOCK",
    "SCORE_MAX",
    "SCORE_PER_FAILED_ATTEMPT",
    "SCORE_PER_UNRECOGNIZED_REPORT",
    "TEMPLATE_BLOCKED",
    "TEMPLATE_FAILED",
    "UNRECOGNIZED_REPORT_EVENT",
    "get_or_create_profile",
    "handle_auth_failed_attempt",
    "handle_unrecognized_operation_report",
    "update_behavior",
]

#: Evento de seguridad que consume este modulo (lo emite `transactions`
#: por outbox ante cada fallo de autorizacion biometrica, E2-T17).
AUTH_FAILED_ATTEMPT_EVENT = "auth.failed_attempt"
#: Consumidor para `shared.processed_events` (idempotencia del consumo).
CONSUMER = "risk-auth-failures"

#: Canal y plantillas de la notificacion al cliente (E2-T20, sin PII).
NOTIFY_CHANNEL = "push"
TEMPLATE_FAILED = "auth_failed_attempt"
TEMPLATE_BLOCKED = "auth_blocked"

#: Severidad minima por intento fallido (provisional E2-T20; HU20 sintoniza).
SCORE_PER_FAILED_ATTEMPT = 1
#: Puntos extra ante bloqueo preventivo (`blocked: true`, CA-02).
SCORE_EXTRA_ON_BLOCK = 4
#: Tope del contador (el perfil solo senala; el motor HU20 decide).
SCORE_MAX = 100

#: Evento de reporte "no reconozco" que consume este modulo (lo emite
#: `notifications` por outbox, E6-T14 HU22).
UNRECOGNIZED_REPORT_EVENT = "security.operation_reported"
#: Consumidor para `shared.processed_events` (idempotencia del consumo).
CONSUMER_UNRECOGNIZED = "risk-unrecognized-reports"
#: Severidad minima por reporte "no reconozco" (provisional E6-T14;
#: HU20 sintoniza; D-16: sin escribir `risk.alerts`).
SCORE_PER_UNRECOGNIZED_REPORT = 10

#: Acciones de auditoria del consumo (sin PII en `metadata`).
AUDIT_ACTION_FAILED = "risk.auth_failed_attempt"
AUDIT_ACTION_BLOCKED = "risk.auth_preventive_block"
#: Auditoria del reporte "no reconozco" (E6-T14; sin PII en `metadata`).
AUDIT_ACTION_UNRECOGNIZED = "risk.unrecognized_operation"

#: Motivos genericos aceptados en el payload (otros -> `"unknown"`).
_KNOWN_REASONS = frozenset({"invalid", "expired", "blocked"})


def get_or_create_profile(session: Session, user_id: uuid.UUID | str) -> RiskProfile:
    """Perfil del usuario, creandolo con defaults si falta (idempotente)."""
    return risk_repo.get_or_create(session, user_id)


def update_behavior(
    session: Session,
    user_id: uuid.UUID | str,
    *,
    avg_amount_minor: int | None = None,
    usual_countries: list[str] | tuple[str, ...] | None = None,
    usual_hours: list[int] | tuple[int, ...] | None = None,
    last_device_id: str | None = None,
    score: int | None = None,
) -> RiskProfile:
    """Actualiza habitos/puntaje del perfil (creandolo si falta)."""
    return risk_repo.update_behavior(
        session,
        user_id,
        avg_amount_minor=avg_amount_minor,
        usual_countries=usual_countries,
        usual_hours=usual_hours,
        last_device_id=last_device_id,
        score=score,
    )


def _parse_signal(payload: Any) -> dict[str, Any]:
    """Normaliza el payload de `auth.failed_attempt` (tolerante, sin PII).

    Campos desconocidos se ignoran; tipos invalidos se degradan a `None`
    (el consumo nunca lanza por forma del payload: lo malformado se
    registra como procesado sin efectos, para no envenenar el worker).
    """
    data = dict(payload) if isinstance(payload, dict) else {}
    try:
        user_id = uuid.UUID(str(data.get("user_id"))) if data.get("user_id") else None
    except (ValueError, AttributeError, TypeError):
        user_id = None
    try:
        transaction_id = (
            uuid.UUID(str(data.get("transaction_id"))) if data.get("transaction_id") else None
        )
    except (ValueError, AttributeError, TypeError):
        transaction_id = None
    try:
        attempt = int(data.get("attempt") or 0)
    except (ValueError, TypeError):
        attempt = 0
    reason = data.get("reason")
    reason = reason if isinstance(reason, str) and reason in _KNOWN_REASONS else "unknown"
    device = data.get("device_id")
    device_id = (
        device.strip()
        if isinstance(device, str) and device.strip() and len(device) <= 128
        else None
    )
    return {
        "user_id": user_id,
        "transaction_id": transaction_id,
        "attempt": max(0, attempt),
        "blocked": bool(data.get("blocked")),
        "reason": reason,
        "device_id": device_id,
    }


def _record_audit(
    session: Session,
    signal: dict[str, Any],
    *,
    action: str,
) -> None:
    """Auditoria del consumo (best-effort: nunca revierte el registro).

    Solo resumen de resultado (`attempt`, `blocked`, `reason`); sin PII
    ni material biometrico en `metadata`.
    """
    try:
        from app.modules.audit.service import record as audit_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("risk audit no disponible: consumo sin auditoria")
        return
    try:
        audit_record(
            session,
            actor=signal["user_id"],
            action=action,
            entity="transaction",
            entity_id=signal["transaction_id"],
            metadata={
                "attempt": signal["attempt"],
                "blocked": signal["blocked"],
                "reason": signal["reason"],
            },
            device_id=signal["device_id"],
        )
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning("risk auditoria no registrada error=%s", type(exc).__name__)


def _notify_customer(
    session: Session,
    signal: dict[str, Any],
    *,
    sender: Any | None = None,
    now: datetime | None = None,
) -> Any:
    """Notifica al cliente via la fachada `notifications.send` (E2-T20).

    Canal push con `recipient = user_id` (mismo fallback que
    `identity.pin_login` ante falta de contacto: la fila conserva el
    `user_id` y el proveedor push resuelve el dispositivo; jamas se lee
    `identity` desde aqui, regla de oro 4). Texto generico sin PII (solo
    el instante). La entrega es best-effort con reintentos dentro de la
    fachada; un fallo del proveedor deja la fila `FAILED` (reimpulsable
    con `retry_notification`), sin revertir el registro de riesgo.
    """
    from app.modules.notifications.service import send as notifications_send

    moment = now if isinstance(now, datetime) else datetime.now(UTC)
    template = TEMPLATE_BLOCKED if signal["blocked"] else TEMPLATE_FAILED
    return notifications_send(
        session,
        channel=NOTIFY_CHANNEL,
        recipient=str(signal["user_id"]),
        template_code=template,
        data={"at": moment.isoformat()},
        user_id=signal["user_id"],
        sender=sender,
    )


def handle_auth_failed_attempt(
    session: Session,
    *,
    event_id: uuid.UUID | str,
    payload: dict[str, Any],
    sender: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Consume `auth.failed_attempt` (E2-T20, HU08 CA-04/CA-02).

    Idempotente por `event_id` (consumidor `CONSUMER`): el primer consumo
    registra el intento en `risk_profiles`, audita y notifica; el replay
    retorna `consumed=False` sin efectos. Sin `user_id` valido no hay
    perfil ni notificacion (se audita y se marca procesado, sin reintento
    infinito). Nunca mueve dinero. `flush` sin `commit` (el llamante, en
    general el handler del bus con sesion propia, decide la transaccion).
    """
    from app.modules.shared import repository as shared_repo

    signal = _parse_signal(payload)
    if not shared_repo.try_mark_processed(session, event_id=event_id, consumer=CONSUMER):
        logger.info("risk consumo duplicado ignorado consumer=%s", CONSUMER)
        return {"consumed": False, "reason": "duplicate"}
    if signal["user_id"] is None:
        _record_audit(
            session,
            signal,
            action=AUDIT_ACTION_FAILED if not signal["blocked"] else AUDIT_ACTION_BLOCKED,
        )
        logger.warning("risk evento sin user_id: registrado sin perfil ni notificacion")
        return {"consumed": True, "reason": "missing-user-id"}
    bump = SCORE_PER_FAILED_ATTEMPT + (SCORE_EXTRA_ON_BLOCK if signal["blocked"] else 0)
    profile = risk_repo.get_or_create(session, signal["user_id"])
    current = int(profile.score or 0)
    new_score = min(SCORE_MAX, max(0, current + bump))
    risk_repo.update_behavior(
        session,
        signal["user_id"],
        score=new_score,
        last_device_id=signal["device_id"],
    )
    action = AUDIT_ACTION_BLOCKED if signal["blocked"] else AUDIT_ACTION_FAILED
    _record_audit(session, signal, action=action)
    notification = _notify_customer(session, signal, sender=sender, now=now)
    logger.info(
        "risk intento registrado blocked=%s score=%s",
        signal["blocked"],
        new_score,
    )
    return {
        "consumed": True,
        "blocked": signal["blocked"],
        "attempt": signal["attempt"],
        "score": new_score,
        "notification_id": str(notification.id),
    }


def _parse_unrecognized_report(payload: Any) -> dict[str, Any]:
    """Normaliza el payload de `security.operation_reported` (tolerante).

    Campos desconocidos se ignoran; tipos invalidos se degradan a `None`
    (lo malformado se registra como procesado sin efectos, para no
    envenenar el worker). Sin PII: solo ids y `reference` no PII.
    """
    data = dict(payload) if isinstance(payload, dict) else {}
    try:
        user_id = uuid.UUID(str(data.get("user_id"))) if data.get("user_id") else None
    except (ValueError, AttributeError, TypeError):
        user_id = None
    try:
        notification_id = (
            uuid.UUID(str(data.get("notification_id"))) if data.get("notification_id") else None
        )
    except (ValueError, AttributeError, TypeError):
        notification_id = None
    reference = data.get("reference")
    reference = (
        " ".join(str(reference).split())[:64]
        if isinstance(reference, str) and str(reference).strip()
        else None
    )
    at = data.get("at")
    at_iso = (
        at.isoformat()
        if isinstance(at, datetime)
        else (" ".join(str(at).split())[:64] if at is not None and str(at).strip() else None)
    )
    return {
        "user_id": user_id,
        "notification_id": notification_id,
        "reference": reference or (str(notification_id)[:8] if notification_id else None),
        "at": at_iso or datetime.now(UTC).isoformat(),
    }


def handle_unrecognized_operation_report(
    session: Session,
    *,
    event_id: uuid.UUID | str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Consume `security.operation_reported` (E6-T14, HU22 CA-02).

    Idempotente por `event_id` (consumidor `CONSUMER_UNRECOGNIZED`): el
    primer consumo sube la severidad del titular en `risk_profiles`,
    audita (`risk.unrecognized_operation`) y emite `fraud.alert.raised`
    por `outbox` (lo consume `identity`, que revoca las sesiones); el
    replay retorna `consumed=False` sin efectos. **No** crea
    `risk.alerts` (tabla reservada a HU20, D-16): la alerta queda como
    evento + auditoria + severidad. Nunca mueve dinero. `flush` sin
    `commit` (el llamante decide la transaccion).
    """
    from app.core import events as domain_events
    from app.modules.shared import repository as shared_repo

    signal = _parse_unrecognized_report(payload)
    if not shared_repo.try_mark_processed(
        session, event_id=event_id, consumer=CONSUMER_UNRECOGNIZED
    ):
        logger.info("risk consumo duplicado ignorado consumer=%s", CONSUMER_UNRECOGNIZED)
        return {"consumed": False, "reason": "duplicate"}
    if signal["user_id"] is None:
        _record_audit(
            session,
            {
                "user_id": None,
                "transaction_id": signal["notification_id"],
                "attempt": 0,
                "blocked": True,
                "reason": "unknown",
                "device_id": None,
            },
            action=AUDIT_ACTION_UNRECOGNIZED,
        )
        logger.warning("risk reporte sin user_id: registrado sin perfil ni alerta")
        return {"consumed": True, "reason": "missing-user-id"}
    profile = risk_repo.get_or_create(session, signal["user_id"])
    current = int(profile.score or 0)
    new_score = min(SCORE_MAX, max(0, current + SCORE_PER_UNRECOGNIZED_REPORT))
    risk_repo.update_behavior(session, signal["user_id"], score=new_score)
    try:
        from app.modules.audit.service import record as audit_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("risk audit no disponible: reporte sin auditoria")
        audit_record = None
    if audit_record is not None:
        try:
            audit_record(
                session,
                actor=signal["user_id"],
                action=AUDIT_ACTION_UNRECOGNIZED,
                entity="notification",
                entity_id=signal["notification_id"],
                metadata={
                    "notification_id": (
                        str(signal["notification_id"]) if signal["notification_id"] else None
                    ),
                    "reference": signal["reference"],
                    "score": new_score,
                },
            )
        except Exception as exc:  # noqa: BLE001 - best-effort documentado
            logger.warning("risk auditoria no registrada error=%s", type(exc).__name__)
    alert = shared_repo.insert_event(
        session,
        aggregate_type="risk",
        aggregate_id=signal["user_id"],
        event_type=domain_events.FRAUD_ALERT_RAISED,
        payload={
            "user_id": str(signal["user_id"]),
            "notification_id": (
                str(signal["notification_id"]) if signal["notification_id"] else None
            ),
            "reference": signal["reference"],
            "reason": "unrecognized_operation",
            "at": signal["at"],
        },
    )
    logger.info(
        "risk reporte registrado score=%s alert=%s",
        new_score,
        alert.id,
    )
    return {
        "consumed": True,
        "score": new_score,
        "alert_event_id": str(alert.id),
    }
