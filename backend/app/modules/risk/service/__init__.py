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
from app.modules.risk.domain import engine as risk_engine
from app.modules.risk.domain import profile as profile_domain
from app.modules.risk.domain.engine import RiskDecision
from app.modules.risk.models import RiskProfile, RiskRule

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIT_ACTION_ASSESSMENT",
    "AUDIT_ACTION_BLOCKED",
    "AUDIT_ACTION_FAILED",
    "AUDIT_ACTION_RULE_UPDATED",
    "AUDIT_ACTION_UNRECOGNIZED",
    "AUTH_FAILED_ATTEMPT_EVENT",
    "CONSUMER",
    "CONSUMER_PROFILE",
    "CONSUMER_TRANSACTION_INITIATED",
    "CONSUMER_TRANSFER_SETTLED",
    "CONSUMER_UNRECOGNIZED",
    "NOTIFY_CHANNEL",
    "PROFILE_WINDOW_DEFAULT_DAYS",
    "PROFILE_WINDOW_KEY",
    "RISK_BLOCKED",
    "RISK_HOLD_REQUIRED",
    "RISK_LEVEL_BY_ACTION",
    "RULE_ACTIONS",
    "RULE_CODES",
    "SCORE_EXTRA_ON_BLOCK",
    "SCORE_MAX",
    "SCORE_PER_FAILED_ATTEMPT",
    "SCORE_PER_UNRECOGNIZED_REPORT",
    "TEMPLATE_BLOCKED",
    "TEMPLATE_FAILED",
    "UNRECOGNIZED_REPORT_EVENT",
    "UNUSUAL_AMOUNT_FACTOR_DEFAULT",
    "UNUSUAL_AMOUNT_FACTOR_KEY",
    "VELOCITY_WINDOW_DEFAULT_SECONDS",
    "VELOCITY_WINDOW_KEY",
    "evaluate_config",
    "evaluate_pre_transaction",
    "get_or_create_profile",
    "get_profile",
    "get_rule",
    "handle_auth_failed_attempt",
    "handle_profile_failed_attempt",
    "handle_profile_login",
    "handle_profile_transaction",
    "handle_transaction_initiated",
    "handle_transfer_settled",
    "handle_unrecognized_operation_report",
    "list_rules",
    "profile_window_days",
    "risk_level_for",
    "update_behavior",
    "update_rule",
    "upsert_rule",
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


# ---------------------------------------------------------------------------
# Reglas antifraude configurables (E6-T01, HU20; cimiento de E6-T02/E6-T04).
# ---------------------------------------------------------------------------

#: Acciones validas de una regla (`docs/03b#11.1`, `docs/11#HU20`).
RULE_ACTIONS = frozenset({"ALERT", "HOLD", "BLOCK"})

#: Las 5 reglas iniciales (misma semilla que `0048_risk_rules`; la
#: calibracion fina es de `E6-T06`). Todas nacen `enabled=true`,
#: `action='ALERT'` con umbrales conservadores.
RULE_CODES = (
    "FACIAL_SPOOFING",
    "ATYPICAL_GEO",
    "UNUSUAL_AMOUNT",
    "VELOCITY",
    "NEW_DEVICE",
)

#: Auditoria de cambios de regla (best-effort, sin PII; `E6-T01`).
AUDIT_ACTION_RULE_UPDATED = "risk.rule.updated"

#: Umbrales globales leidos por `shared/config` con fallback (regla de
#: oro 6; `params_json` guarda los propios de cada regla).
VELOCITY_WINDOW_KEY = "risk.velocity_window_seconds"
VELOCITY_WINDOW_DEFAULT_SECONDS = 600
UNUSUAL_AMOUNT_FACTOR_KEY = "risk.unusual_amount_factor"
UNUSUAL_AMOUNT_FACTOR_DEFAULT = 3.0


def get_or_create_profile(session: Session, user_id: uuid.UUID | str) -> RiskProfile:
    """Perfil del usuario, creandolo con defaults si falta (idempotente)."""
    return risk_repo.get_or_create(session, user_id)


def get_profile(session: Session, user_id: uuid.UUID | str) -> RiskProfile | None:
    """Perfil del usuario (`None` si aun no existe; solo lectura)."""
    try:
        return risk_repo.get_by_user(session, user_id)
    except risk_repo.RiskProfileValidationError:
        return None


def update_behavior(
    session: Session,
    user_id: uuid.UUID | str,
    observation: profile_domain.BehaviorObservation | dict[str, Any] | None = None,
    *,
    event_id: uuid.UUID | str | None = None,
    avg_amount_minor: int | None = None,
    usual_countries: list[str] | tuple[str, ...] | None = None,
    usual_hours: list[int] | tuple[int, ...] | None = None,
    last_device_id: str | None = None,
    score: int | None = None,
) -> RiskProfile:
    """Actualiza habitos/puntaje del perfil (creandolo si falta).

    Dos modos (compatibles con `E2-T18`/`E2-T20`, que siguen intactos):

    - Legado (`observation is None`): escribe los campos informados
      (`None` = conservar), como antes. Sin idempotencia por evento.
    - Historial (`observation` dada, E6-T03): calcula el delta con el
      dominio puro `profile.next_profile` (media incremental entera con
      `ROUND_HALF_UP`, union acotada a 5 horas/paises, ultimo
      dispositivo; el `score` nunca se toca aqui) y lo persiste. Los
      `operations` ya reflejados son el conteo en la ventana
      `risk.profile_window_days` menos la operacion en curso (ya hizo
      `commit`); con `avg == 0` (ningun monto alimentado jamas) la
      observacion funda el promedio sin diluir con el historial previo
      al despliegue. Exacto cuando cada operacion de la ventana se
      alimento al menos una vez y en orden; ante rezagos converge con
      los eventos (heuristica documentada, sin migracion). Con
      `event_id` es idempotente por `(event_id, user_id)` via
      `processed_events` (consumidor `CONSUMER_PROFILE`): el replay
      retorna el perfil actual sin escribir. `flush` sin `commit` (el
      llamante, en general el handler D-04 con sesion propia, decide
      la transaccion).
    """
    if event_id is not None:
        from app.modules.shared import repository as shared_repo

        if not shared_repo.try_mark_processed(
            session, event_id=event_id, consumer=CONSUMER_PROFILE
        ):
            logger.info("risk perfil duplicado ignorado consumer=%s", CONSUMER_PROFILE)
            return risk_repo.get_or_create(session, user_id)
    if observation is None:
        return risk_repo.update_behavior(
            session,
            user_id,
            avg_amount_minor=avg_amount_minor,
            usual_countries=usual_countries,
            usual_hours=usual_hours,
            last_device_id=last_device_id,
            score=score,
        )
    if not isinstance(observation, (profile_domain.BehaviorObservation, dict)):
        raise risk_repo.RiskProfileValidationError(
            f"observation debe ser BehaviorObservation o dict, recibido: {type(observation).__name__}"
        )
    profile = risk_repo.get_or_create(session, user_id)
    obs = (
        observation
        if isinstance(observation, profile_domain.BehaviorObservation)
        else profile_domain.BehaviorObservation(
            amount_minor=observation.get("amount_minor"),
            currency=observation.get("currency"),
            country=observation.get("country"),
            hour=observation.get("hour"),
            device_id=observation.get("device_id"),
        )
    )
    prior_ops = 0
    if profile_domain._coerce_amount(obs.amount_minor) is not None:
        stored_avg = int(profile.avg_amount_minor or 0)
        if stored_avg > 0:
            # El promedio ya refleja montos: las operaciones aportadas
            # son el conteo en la ventana menos la operacion en curso
            # (ya hizo `commit`). Exacto cuando cada operacion de la
            # ventana se alimento al menos una vez y en orden; ante
            # rezagos converge con los eventos (heuristica).
            prior_ops = max(0, _window_operation_count(session, profile.user_id) - 1)
        # Con `avg == 0` ningun monto se alimento jamas (los montos son
        # `> 0` y el redondeo nunca devuelve a `0` desde `>= 1`): la
        # observacion funda el promedio (`prior_ops = 0`), sin diluir
        # con el historial previo al despliegue.
    update = profile_domain.next_profile(
        {
            "avg_amount_minor": int(profile.avg_amount_minor or 0),
            "usual_countries": list(profile.usual_countries or []),
            "usual_hours": list(profile.usual_hours or []),
            "last_device_id": profile.last_device_id,
        },
        obs,
        operations=prior_ops,
    )
    explicit = {
        "avg_amount_minor": update.avg_amount_minor,
        "usual_countries": update.usual_countries,
        "usual_hours": update.usual_hours,
        "last_device_id": update.last_device_id,
    }
    if avg_amount_minor is not None:
        explicit["avg_amount_minor"] = avg_amount_minor
    if usual_countries is not None:
        explicit["usual_countries"] = usual_countries
    if usual_hours is not None:
        explicit["usual_hours"] = usual_hours
    if last_device_id is not None:
        explicit["last_device_id"] = last_device_id
    if score is not None:
        explicit["score"] = score
    row = risk_repo.update_behavior(session, user_id, **explicit)  # type: ignore[arg-type]
    row.updated_at = datetime.now(UTC)
    session.flush()
    return row


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
    _assess_auth_failure_best_effort(session, signal)
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


def _assess_auth_failure_best_effort(session: Session, signal: dict[str, Any]) -> None:
    """Evaluacion antifraude del fallo de autorizacion (extension E6-T02 corte 2).

    Best-effort estricto: nunca altera el consumo E2-T20 (perfil +
    auditoria + notificacion intactos); ante cualquier fallo solo se
    loguea y el consumo continua. Solo deja auditoria
    `risk.assessment` (sin PII: `action`/`score`/`matched_rules`) cuando
    alguna regla coincide con la senal (`blocked`, intentos o motivo de
    spoofing); sin hallazgo no escribe nada (las suites E2-T20 con 0
    reglas siguen viendo una sola auditoria).
    """
    try:
        with session.begin_nested():
            rules = list(risk_repo.list_rules(session))
            profile = risk_repo.get_by_user(session, signal["user_id"])
        context: dict[str, Any] = {
            "auth_blocked": bool(signal.get("blocked")),
            "auth_failed_attempts": max(1, int(signal.get("attempt") or 0)),
            "auth_fail_reason": (
                "blocked" if signal.get("blocked") else str(signal.get("reason") or "unknown")
            ),
            "device_id": signal.get("device_id"),
        }
        decision = risk_engine.evaluate(rules, profile, context)
    except Exception as exc:  # noqa: BLE001 - extension best-effort documentada
        logger.debug("risk assessment de auth omitida error=%s", type(exc).__name__)
        return
    if decision.action == risk_engine.ALLOW:
        return
    try:
        from app.modules.audit.service import record as audit_record

        audit_record(
            session,
            actor=signal["user_id"],
            action=AUDIT_ACTION_ASSESSMENT,
            entity="transaction",
            entity_id=signal["transaction_id"],
            metadata=_assessment_metadata(decision, source="auth.failed_attempt"),
        )
    except Exception as exc:  # noqa: BLE001 - la auditoria no bloquea el consumo
        logger.warning("risk assessment de auth no registrada error=%s", type(exc).__name__)


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


def list_rules(session: Session, *, enabled_only: bool = False) -> list[RiskRule]:
    """Reglas ordenadas deterministicamente (`priority ASC, code ASC`).

    La evaluacion (`E6-T02`) lee en este orden en cada pasada; no hay
    cache en proceso obligatoria (si se cachea, el `PUT` la invalida
    al escribir en la misma transaccion). `flush` sin `commit`.
    """
    return risk_repo.list_rules(session, enabled_only=enabled_only)


def get_rule(session: Session, code: str) -> RiskRule:
    """Regla por `code` o `RiskRuleNotFoundError` (`NOT_FOUND`)."""
    return risk_repo.require_by_code(session, code)


def _audit_rule_updated(
    session: Session,
    row: RiskRule,
    *,
    actor: uuid.UUID | str | None,
) -> None:
    """Auditoria best-effort del cambio (sin PII: solo umbrales/estado).

    Nunca revierte el cambio: un fallo de auditoria se loguea (regla
    de oro del modulo `audit`).
    """
    try:
        from app.modules.audit.service import record as audit_record
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        logger.warning("risk audit no disponible: regla sin auditoria")
        return
    try:
        audit_record(
            session,
            actor=actor,
            action=AUDIT_ACTION_RULE_UPDATED,
            entity="risk_rule",
            entity_id=row.id,
            metadata={
                "code": row.code,
                "enabled": bool(row.enabled),
                "priority": int(row.priority),
                "action": row.action,
                "params_keys": (
                    sorted(row.params_json.keys()) if isinstance(row.params_json, dict) else []
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning("risk auditoria de regla no registrada error=%s", type(exc).__name__)


def update_rule(
    session: Session,
    code: str,
    *,
    actor: uuid.UUID | str | None,
    enabled: bool | None = None,
    priority: int | None = None,
    action: str | None = None,
    params_json: dict | None = None,
) -> RiskRule:
    """Actualiza una regla editable y audita (`risk.rule.updated`).

    `code` es inmutable; `None` = conservar. Se refleja en la
    siguiente evaluacion sin reinicio (la evaluacion lee `risk_rules`
    en cada pasada). `flush` sin `commit` (decide el llamante).
    """
    row = risk_repo.update_rule(
        session,
        code,
        enabled=enabled,
        priority=priority,
        action=action,
        params_json=params_json,
        updated_by=actor,
    )
    _audit_rule_updated(session, row, actor=actor)
    logger.info(
        "risk regla actualizada code=%s enabled=%s priority=%s action=%s",
        row.code,
        row.enabled,
        row.priority,
        row.action,
    )
    return row


def upsert_rule(
    session: Session,
    code: str,
    *,
    name: str,
    enabled: bool = True,
    priority: int = 100,
    action: str = "ALERT",
    params_json: dict | None = None,
) -> RiskRule:
    """Crea la regla si falta (idempotente por `code`; semilla E6-T01).

    Si ya existe, la conserva intacta (no pisa personalizaciones, como
    el `ON CONFLICT DO NOTHING` de `0048_risk_rules`). `flush` sin
    `commit`.
    """
    existing = risk_repo.get_by_code(session, code)
    if existing is not None:
        return existing
    row = RiskRule(
        code=str(code).strip().upper(),
        name=risk_repo._coerce_name(name),
        enabled=bool(enabled),
        priority=risk_repo._coerce_priority(priority),
        action=risk_repo._coerce_action(action),
        params_json=risk_repo._coerce_params(params_json if params_json is not None else {}),
    )
    session.add(row)
    session.flush()
    return row


def evaluate_config(session: Session) -> dict[str, object]:
    """Umbrales globales de las reglas (via `shared/config`, con fallback).

    Lee `risk.velocity_window_seconds` (entero `>= 1`, default `600`) y
    `risk.unusual_amount_factor` (numerico `> 0`, default `3.0`).
    Nunca lanza: clave ausente/ilegible/invalida -> fallback (mismo
    patron best-effort que `shared/config`).
    """
    from app.modules.shared.config import parameters as params

    window = params.get_int_parameter(
        session, VELOCITY_WINDOW_KEY, VELOCITY_WINDOW_DEFAULT_SECONDS, minimum=1
    )
    raw_factor = params.get_parameter(
        session, UNUSUAL_AMOUNT_FACTOR_KEY, UNUSUAL_AMOUNT_FACTOR_DEFAULT
    )
    factor = _coerce_factor(raw_factor)
    return {
        "velocity_window_seconds": window,
        "unusual_amount_factor": factor,
    }


def _coerce_factor(raw: object) -> float:
    """Factor numerico `> 0` o el default (nunca lanza, sin `float` sucio)."""
    import math

    if isinstance(raw, bool):
        return UNUSUAL_AMOUNT_FACTOR_DEFAULT
    try:
        candidate = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return UNUSUAL_AMOUNT_FACTOR_DEFAULT
    if candidate <= 0 or math.isnan(candidate):
        return UNUSUAL_AMOUNT_FACTOR_DEFAULT
    return candidate


# ---------------------------------------------------------------------------
# Fachada sincrona pre-transaccion + consumidores del motor (E6-T02 corte 2).
# ---------------------------------------------------------------------------

#: Codigo de negocio cuando una regla `BLOCK` rechaza la operacion: queda
#: `REJECTED` con `failure_reason` + `meta.failure_code = RISK_BLOCKED`,
#: sin mover saldos ni asientos (lo fija `transactions`).
RISK_BLOCKED = "RISK_BLOCKED"
#: Codigo interno cuando una regla `HOLD` retiene la operacion: queda
#: `PENDING_AUTHORIZATION` (reutiliza el gate biometrico existente; todos
#: los flujos cubiertos lo soportan, si no quedaria `REJECTED`).
RISK_HOLD_REQUIRED = "RISK_HOLD_REQUIRED"

#: Auditoria de cada evaluacion con hallazgo (sin PII: solo `action`,
#: `score` y `matched_rules`; jamas montos, paises ni dispositivos).
AUDIT_ACTION_ASSESSMENT = "risk.assessment"

#: Consumidores D-04 de los eventos del motor (idempotencia por
#: `processed_events`, un consumidor por evento).
CONSUMER_TRANSACTION_INITIATED = "risk-tx-initiated"
CONSUMER_TRANSFER_SETTLED = "risk-tx-settled"

#: Nivel de riesgo de la operacion segun la decision (evidencia en
#: `transactions.risk_level`; `transactions` lo fija en su hook).
RISK_LEVEL_BY_ACTION = {
    "ALLOW": "LOW",
    "ALERT": "MEDIUM",
    "HOLD": "HIGH",
    "BLOCK": "HIGH",
}


def risk_level_for(action: str) -> str:
    """Nivel `transactions.risk_level` para una `action` (`LOW` por defecto)."""
    return RISK_LEVEL_BY_ACTION.get(str(action or "").strip().upper(), "LOW")


def _coerce_eval_user(value: uuid.UUID | str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"user_id debe ser UUID, recibido: {value!r}") from exc


def _velocity_window_seconds(rules: list[RiskRule], config: dict[str, object]) -> int:
    """Ventana de `VELOCITY` (regla `window_seconds` > global `config`)."""
    for rule in rules:
        code = getattr(rule, "code", None)
        if not isinstance(code, str) or code.strip().upper() != "VELOCITY":
            continue
        params = getattr(rule, "params_json", None)
        if isinstance(params, dict):
            raw = params.get("window_seconds")
            if isinstance(raw, bool):
                continue
            try:
                candidate = int(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if candidate > 0:
                return candidate
    try:
        return max(1, int(config.get("velocity_window_seconds", VELOCITY_WINDOW_DEFAULT_SECONDS)))
    except (TypeError, ValueError):
        return VELOCITY_WINDOW_DEFAULT_SECONDS


def _recent_operations(session: Session, user_id: uuid.UUID, window_seconds: int) -> int | None:
    """Conteo de operaciones del titular en la ventana (`None` si falla).

    Solo lectura por la fachada de `transactions` (D-01: `risk` puede
    llamar a `transactions`; jamas importa sus modelos). Un fallo de
    lectura no bloquea: `None` = la regla `VELOCITY` no hace match.
    """
    try:
        from app.modules.transactions.service import (
            count_recent_operations as _count_recent,
        )
    except ImportError:
        return None
    try:
        with session.begin_nested():
            return int(_count_recent(session, user_id, window_seconds=window_seconds))
    except Exception:  # noqa: BLE001 - lectura best-effort, rige no-match
        logger.debug("risk velocidad sin conteo: la regla no participa")
        return None


def evaluate_pre_transaction(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    amount_minor: int,
    currency: str,
    device_id: str | None = None,
    country: str | None = None,
    hour: int | None = None,
    tx_type: str | None = None,
) -> RiskDecision:
    """Evalua las reglas antes de iniciar la operacion (solo lectura).

    Relee `risk_rules` y `risk_profiles` en cada pasada (sin cache: un
    `PUT /risk/rules/{code}` se refleja sin reiniciar); no escribe nada
    (ni siquiera crea el perfil: sin perfil no hay habito conocido y las
    reglas con base de comparacion simplemente no hacen match). Sin
    llamadas externas: todo es local y determinista (< 1 s en el camino
    local: lecturas indexadas por `user_id`/`code`).

    `operations_in_window` (regla `VELOCITY`) cuenta las operaciones
    comprometidas del titular en la ventana **incluyendo la operacion en
    curso** (en el hook ya hizo `flush`; en los consumidores ya hizo
    `commit`): el match dispara en la N-esima operacion de la ventana.
    Determinista ante `autoflush` (la fila en curso siempre esta
    flusheada al evaluar).

    Fail-open documentado: ante un fallo tecnico de lectura (p. ej.
    tablas aun no migradas) se loguea y retorna `ALLOW` —`risk` nunca
    bloquea a ciegas; el bloqueo solo ocurre ante un match de regla.
    La validacion de la entrada si es estricta (`ValueError`).
    """
    uid = _coerce_eval_user(user_id)
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise TypeError(f"amount_minor debe ser entero, recibido: {amount_minor!r}")
    if not isinstance(currency, str) or not currency.strip():
        raise ValueError(f"currency invalida: {currency!r}")
    clean_device = device_id.strip() if isinstance(device_id, str) and device_id.strip() else None
    clean_country = country.strip() if isinstance(country, str) and country.strip() else None
    try:
        with session.begin_nested():
            rules = list(risk_repo.list_rules(session))
            profile = risk_repo.get_by_user(session, uid)
            config = evaluate_config(session)
            window = _velocity_window_seconds(rules, config)
        operations = _recent_operations(session, uid, window)
    except Exception as exc:  # noqa: BLE001 - fail-open documentado arriba
        logger.warning("risk evaluacion no disponible, permite: error=%s", type(exc).__name__)
        return RiskDecision()
    context: dict[str, Any] = {
        "amount_minor": int(amount_minor),
        "currency": currency.strip().upper(),
        "device_id": clean_device,
        "country": clean_country,
        "hour": hour,
        "tx_type": tx_type,
        "operations_in_window": operations,
        "window_seconds": window,
    }
    try:
        factor = float(config.get("unusual_amount_factor", UNUSUAL_AMOUNT_FACTOR_DEFAULT))
    except (TypeError, ValueError):
        factor = UNUSUAL_AMOUNT_FACTOR_DEFAULT
    if factor > 0:
        context["unusual_amount_factor"] = factor
    return risk_engine.evaluate(rules, profile, context)


def _assessment_metadata(
    decision: RiskDecision, *, source: str, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Metadata de `risk.assessment` (sin PII: solo codigos y puntaje)."""
    metadata: dict[str, Any] = {
        "source": source,
        "action": decision.action,
        "score": int(decision.score),
        "matched_rules": list(decision.matched_rules),
    }
    if extra:
        metadata.update(extra)
    return metadata


def _record_assessment(
    session: Session,
    *,
    actor: uuid.UUID | None,
    entity_id: uuid.UUID | None,
    decision: RiskDecision,
    source: str,
) -> None:
    """Auditoria `risk.assessment` (estricta: un fallo revierte el consumo).

    Solo se invoca con hallazgo (`action != ALLOW`): sin reglas que
    coincidan no hay nada que evidenciar.
    """
    from app.modules.audit.service import record as audit_record

    audit_record(
        session,
        actor=actor,
        action=AUDIT_ACTION_ASSESSMENT,
        entity="transaction",
        entity_id=entity_id,
        metadata=_assessment_metadata(decision, source=source),
    )


def _parse_tx_signal(payload: Any) -> dict[str, Any]:
    """Referencia a la operacion del payload (tolerante, sin PII)."""
    data = dict(payload) if isinstance(payload, dict) else {}
    try:
        transaction_id = (
            uuid.UUID(str(data.get("transaction_id"))) if data.get("transaction_id") else None
        )
    except (ValueError, AttributeError, TypeError):
        transaction_id = None
    try:
        user_id = uuid.UUID(str(data.get("user_id"))) if data.get("user_id") else None
    except (ValueError, AttributeError, TypeError):
        user_id = None
    device = data.get("device_id")
    return {
        "transaction_id": transaction_id,
        "user_id": user_id,
        "device_id": (
            device.strip()
            if isinstance(device, str) and device.strip() and len(device) <= 128
            else None
        ),
    }


def handle_transaction_initiated(
    session: Session,
    *,
    event_id: uuid.UUID | str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Consume `transaction.initiated` (E6-T02 corte 2, HU20).

    Idempotente por `event_id` (consumidor `CONSUMER_TRANSACTION_INITIATED`):
    el replay retorna `consumed=False` sin efectos. Lee la operacion por la
    fachada de `transactions` (`find_transaction_by_id`, sin importar sus
    modelos), evalua con `evaluate_pre_transaction` y, solo con hallazgo
    (`ALERT`/`HOLD`/`BLOCK`), deja auditoria `risk.assessment` (sin PII).
    `flush` sin `commit` (el llamante, en general el handler del bus con
    sesion propia, decide la transaccion); ante fallo se revierte y se
    re-lanza para el backoff del worker.
    """
    from app.modules.shared import repository as shared_repo

    if not shared_repo.try_mark_processed(
        session, event_id=event_id, consumer=CONSUMER_TRANSACTION_INITIATED
    ):
        logger.info("risk consumo duplicado ignorado consumer=%s", CONSUMER_TRANSACTION_INITIATED)
        return {"consumed": False, "reason": "duplicate"}
    signal = _parse_tx_signal(payload)
    if signal["transaction_id"] is None:
        logger.warning("risk initiated sin transaction_id: registrado sin efectos")
        return {"consumed": True, "reason": "missing-transaction-id"}
    from app.modules.transactions.service import find_transaction_by_id as _find_tx

    tx = _find_tx(session, signal["transaction_id"])
    if tx is None:
        logger.warning("risk initiated sin operacion: registrado sin efectos")
        return {"consumed": True, "reason": "missing-transaction"}
    owner = tx.initiator_user_id
    if owner is None:
        logger.warning("risk initiated sin titular: registrado sin efectos")
        return {"consumed": True, "reason": "missing-user-id"}
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    device = signal["device_id"]
    if device is None and isinstance(meta.get("device_id"), str) and meta["device_id"].strip():
        device = meta["device_id"].strip()[:128]
    decision = evaluate_pre_transaction(
        session,
        user_id=owner,
        amount_minor=int(tx.amount_minor),
        currency=str(tx.currency),
        device_id=device,
        tx_type=str(tx.type),
    )
    if decision.action == risk_engine.ALLOW:
        return {"consumed": True, "action": decision.action, "reason": "allow"}
    _record_assessment(
        session,
        actor=owner,
        entity_id=tx.id,
        decision=decision,
        source="transaction.initiated",
    )
    logger.info("risk assessment action=%s score=%s", decision.action, decision.score)
    return {
        "consumed": True,
        "action": decision.action,
        "score": int(decision.score),
        "matched_rules": list(decision.matched_rules),
    }


def handle_transfer_settled(
    session: Session,
    *,
    event_id: uuid.UUID | str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Consume `transfer.settled` (E6-T02 corte 2, HU20).

    Mismo contrato D-04 que `handle_transaction_initiated` (consumidor
    `CONSUMER_TRANSFER_SETTLED`): reevalua tras liquidar (la ventana de
    `VELOCITY` ya incluye la operacion) y audita `risk.assessment` solo
    con hallazgo. Sin PII; `flush` sin `commit`; fallo -> rollback y
    re-lanza.
    """
    from app.modules.shared import repository as shared_repo

    if not shared_repo.try_mark_processed(
        session, event_id=event_id, consumer=CONSUMER_TRANSFER_SETTLED
    ):
        logger.info("risk consumo duplicado ignorado consumer=%s", CONSUMER_TRANSFER_SETTLED)
        return {"consumed": False, "reason": "duplicate"}
    signal = _parse_tx_signal(payload)
    if signal["transaction_id"] is None:
        logger.warning("risk settled sin transaction_id: registrado sin efectos")
        return {"consumed": True, "reason": "missing-transaction-id"}
    from app.modules.transactions.service import find_transaction_by_id as _find_tx

    tx = _find_tx(session, signal["transaction_id"])
    if tx is None:
        logger.warning("risk settled sin operacion: registrado sin efectos")
        return {"consumed": True, "reason": "missing-transaction"}
    owner = tx.initiator_user_id if tx.initiator_user_id is not None else signal["user_id"]
    if owner is None:
        logger.warning("risk settled sin titular: registrado sin efectos")
        return {"consumed": True, "reason": "missing-user-id"}
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    device = signal["device_id"]
    if device is None and isinstance(meta.get("device_id"), str) and meta["device_id"].strip():
        device = meta["device_id"].strip()[:128]
    decision = evaluate_pre_transaction(
        session,
        user_id=owner,
        amount_minor=int(tx.amount_minor),
        currency=str(tx.currency),
        device_id=device,
        tx_type=str(tx.type),
    )
    if decision.action == risk_engine.ALLOW:
        return {"consumed": True, "action": decision.action, "reason": "allow"}
    _record_assessment(
        session,
        actor=owner,
        entity_id=tx.id,
        decision=decision,
        source="transfer.settled",
    )
    logger.info("risk assessment action=%s score=%s", decision.action, decision.score)
    return {
        "consumed": True,
        "action": decision.action,
        "score": int(decision.score),
        "matched_rules": list(decision.matched_rules),
    }


# ---------------------------------------------------------------------------
# Perfil de comportamiento alimentado por historial (E6-T03, HU20).
# ---------------------------------------------------------------------------

#: Consumidor para `shared.processed_events` (idempotencia del perfil:
#: un evento repetido no actualiza dos veces el perfil).
CONSUMER_PROFILE = "risk-profile"

#: Ventana del perfil en `config.parameters` (regla de oro 6): las
#: operaciones de los ultimos N dias alimentan el promedio. Default 90.
PROFILE_WINDOW_KEY = "risk.profile_window_days"
PROFILE_WINDOW_DEFAULT_DAYS = 90


def profile_window_days(session: Session) -> int:
    """Dias de historial que alimentan el promedio (via `shared/config`).

    Nunca lanza: clave ausente/ilegible/invalida -> default `90`
    (mismo patron best-effort que `shared/config`).
    """
    from app.modules.shared.config import parameters as params

    return params.get_int_parameter(
        session, PROFILE_WINDOW_KEY, PROFILE_WINDOW_DEFAULT_DAYS, minimum=1
    )


def _window_operation_count(session: Session, user_id: uuid.UUID) -> int:
    """Operaciones del titular en la ventana del perfil (incluye la en curso).

    Solo lectura por la fachada de `transactions` (D-01: `risk` no
    importa sus modelos). Un fallo de lectura se propaga (D-04: el
    handler revierte y re-lanza para el backoff del worker).
    """
    from app.modules.transactions.service import count_recent_operations as _count_recent

    window_seconds = profile_window_days(session) * 86400
    return max(0, int(_count_recent(session, user_id, window_seconds=window_seconds)))


def _utc_hour(moment: datetime | None) -> int | None:
    """Hora UTC (`0..23`) de un instante o `None` (tolerante)."""
    if not isinstance(moment, datetime):
        return None
    current = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return int(current.astimezone(UTC).hour)


def _clean_country(*candidates: Any) -> str | None:
    """Primer pais valido (`strip` + `UPPER`, `1..8` chars) o `None`."""
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip() and len(candidate.strip()) <= 8:
            return candidate.strip().upper()
    return None


def _clean_device(*candidates: Any) -> str | None:
    """Primer dispositivo valido (no vacio, `<= 128`) o `None`."""
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip() and len(candidate) <= 128:
            return candidate.strip()
    return None


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    """UUID o `None` (sin lanzar; `user_id`/`transaction_id` tolerantes)."""
    try:
        return uuid.UUID(str(value)) if value else None
    except (ValueError, AttributeError, TypeError):
        return None


def _observation_from_transaction(
    tx: Any, payload: dict[str, Any]
) -> profile_domain.BehaviorObservation:
    """Observacion de una operacion del motor (sin PII).

    Monto/moneda/hora del registro de `transactions` (leido por
    fachada, sin importar sus modelos); pais/dispositivo del payload
    o de `tx.meta` (lo que traiga). La moneda se mapea pero no se
    persiste (el promedio del MVP es agnostico a la moneda, igual que
    la comparacion de `UNUSUAL_AMOUNT`).
    """
    meta = tx.meta if isinstance(getattr(tx, "meta", None), dict) else {}
    created = getattr(tx, "created_at", None)
    return profile_domain.BehaviorObservation(
        amount_minor=int(tx.amount_minor),
        currency=str(tx.currency).strip().upper() if isinstance(tx.currency, str) else None,
        country=_clean_country(
            payload.get("country"), payload.get("country_code"), meta.get("country")
        ),
        hour=_utc_hour(created),
        device_id=_clean_device(payload.get("device_id"), meta.get("device_id")),
    )


def handle_profile_transaction(
    session: Session,
    *,
    event_id: uuid.UUID | str,
    payload: dict[str, Any],
    source: str = "transaction.initiated",
) -> dict[str, Any]:
    """Alimenta el perfil desde `transaction.initiated`/`transfer.settled`.

    Idempotente por `event_id` (consumidor `CONSUMER_PROFILE`): el
    replay retorna `consumed=False` sin efectos. Lee la operacion por
    la fachada de `transactions` (el `initiated` no trae `user_id`) y
    actualiza monto promedio (media incremental entera con
    `ROUND_HALF_UP` en la ventana `risk.profile_window_days`), horas,
    paises y dispositivo. `flush` sin `commit`; fallo -> rollback y
    re-lanza (D-04). Nunca mueve dinero ni toca el `score`.
    """
    from app.modules.shared import repository as shared_repo

    if not shared_repo.try_mark_processed(session, event_id=event_id, consumer=CONSUMER_PROFILE):
        logger.info("risk consumo duplicado ignorado consumer=%s", CONSUMER_PROFILE)
        return {"consumed": False, "reason": "duplicate"}
    data = dict(payload) if isinstance(payload, dict) else {}
    transaction_id = _coerce_uuid(data.get("transaction_id"))
    if transaction_id is None:
        logger.warning("risk perfil sin transaction_id: registrado sin efectos")
        return {"consumed": True, "reason": "missing-transaction-id"}
    from app.modules.transactions.service import find_transaction_by_id as _find_tx

    tx = _find_tx(session, transaction_id)
    if tx is None:
        logger.warning("risk perfil sin operacion: registrado sin efectos")
        return {"consumed": True, "reason": "missing-transaction"}
    owner = tx.initiator_user_id if tx.initiator_user_id is not None else None
    if owner is None:
        owner = _coerce_uuid(data.get("user_id"))
    if owner is None:
        logger.warning("risk perfil sin titular: registrado sin efectos")
        return {"consumed": True, "reason": "missing-user-id"}
    observation = _observation_from_transaction(tx, data)
    profile = update_behavior(session, owner, observation)
    logger.info("risk perfil actualizado user_avg=%s", int(profile.avg_amount_minor or 0))
    return {
        "consumed": True,
        "source": source,
        "avg_amount_minor": int(profile.avg_amount_minor or 0),
        "usual_countries": list(profile.usual_countries or []),
        "usual_hours": list(profile.usual_hours or []),
        "last_device_id": profile.last_device_id,
    }


def handle_profile_login(
    session: Session,
    *,
    event_id: uuid.UUID | str,
    payload: dict[str, Any],
    at: datetime | None = None,
) -> dict[str, Any]:
    """Alimenta el perfil desde `auth.login_succeeded` (E6-T03).

    Solo hora/dispositivo/pais cuando el payload los trae (sin monto:
    el promedio se conserva; sin tocar el `score`). Idempotente por
    `event_id` (consumidor `CONSUMER_PROFILE`). `flush` sin `commit`;
    fallo -> rollback y re-lanza. Sin PII.
    """
    from app.modules.shared import repository as shared_repo

    if not shared_repo.try_mark_processed(session, event_id=event_id, consumer=CONSUMER_PROFILE):
        logger.info("risk consumo duplicado ignorado consumer=%s", CONSUMER_PROFILE)
        return {"consumed": False, "reason": "duplicate"}
    data = dict(payload) if isinstance(payload, dict) else {}
    owner = _coerce_uuid(data.get("user_id"))
    if owner is None:
        logger.warning("risk perfil login sin titular: registrado sin efectos")
        return {"consumed": True, "reason": "missing-user-id"}
    observation = profile_domain.BehaviorObservation(
        country=_clean_country(data.get("country"), data.get("country_code")),
        hour=_utc_hour(at),
        device_id=_clean_device(data.get("device_id")),
    )
    profile = update_behavior(session, owner, observation)
    return {
        "consumed": True,
        "source": "auth.login_succeeded",
        "usual_countries": list(profile.usual_countries or []),
        "usual_hours": list(profile.usual_hours or []),
        "last_device_id": profile.last_device_id,
    }


def handle_profile_failed_attempt(
    session: Session,
    *,
    event_id: uuid.UUID | str,
    payload: dict[str, Any],
    at: datetime | None = None,
) -> dict[str, Any]:
    """Complementa el perfil desde `auth.failed_attempt` (E6-T03).

    Solo hora/dispositivo (la severidad la posee `E2-T20`: este
    consumo NO toca `score` ni notifica, para no duplicar su efecto).
    Idempotente por `event_id` (consumidor `CONSUMER_PROFILE`).
    `flush` sin `commit`; fallo -> rollback y re-lanza. Sin PII.
    """
    from app.modules.shared import repository as shared_repo

    if not shared_repo.try_mark_processed(session, event_id=event_id, consumer=CONSUMER_PROFILE):
        logger.info("risk consumo duplicado ignorado consumer=%s", CONSUMER_PROFILE)
        return {"consumed": False, "reason": "duplicate"}
    data = dict(payload) if isinstance(payload, dict) else {}
    owner = _coerce_uuid(data.get("user_id"))
    if owner is None:
        logger.warning("risk perfil fallo sin titular: registrado sin efectos")
        return {"consumed": True, "reason": "missing-user-id"}
    observation = profile_domain.BehaviorObservation(
        hour=_utc_hour(at),
        device_id=_clean_device(data.get("device_id")),
    )
    profile = update_behavior(session, owner, observation)
    return {
        "consumed": True,
        "source": "auth.failed_attempt",
        "score": int(profile.score or 0),
        "usual_hours": list(profile.usual_hours or []),
        "last_device_id": profile.last_device_id,
    }
