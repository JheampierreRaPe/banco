"""Casos de uso del expediente de credito (E3-T08, HU10).

Orquesta la solicitud de punta a punta sobre lo ya hecho (E3-T02
catalogo, E3-T06 bureau, E3-T07 reglas, E2-T18 config):

1. Valida campos + topes del producto (sin consentimiento no se
   evalua: HU10 CA-01).
2. Crea el expediente `SUBMITTED -> IN_EVALUATION` con el
   consentimiento estampado (`bureau_authorized`/`bureau_authorized_at`).
3. Consulta el bureau (E3-T06, con sus reintentos internos); si cae
   (`BUREAU_UNAVAILABLE`/`BUREAU_TIMEOUT`) NO es error 500: el motor
   (E3-T07) lo manda a `IN_REVIEW`.
4. Evalua con `evaluate_application` (parametros `loan.*` vigentes),
   persiste el dictamen (`score`, `dti_ratio`, ingresos/deuda
   efectivos, `decision_reason`, `decided_at`) y emite eventos por
   `outbox` + notificacion best-effort por la fachada `notifications`.
5. La revision del analista resuelve `IN_REVIEW -> APPROVED|REJECTED`
   (aprobacion separada de la solicitud, auditada).

Deuda efectiva: la declarada + la externa que reporte el bureau
(`deudas_minor`); ambas en centimos, nunca `float` (regla de oro 3).

Sin PII ni secretos en logs (regla de oro 7): los logs llevan el
dictamen y conteos, jamas documento, ingresos, deuda, score ni razones
con cifras. El snapshot de idempotencia es minimo (`application_id` +
`status`). Convencion: `flush` sin `commit`; el endpoint confirma.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.credit_bureau import (
    BUREAU_TIMEOUT,
    BUREAU_UNAVAILABLE,
    BureauScore,
    CreditBureauError,
    CreditBureauProvider,
)
from app.modules.credits import events as credit_events
from app.modules.credits import repository as credits_repo
from app.modules.credits.domain import rules
from app.modules.credits.models import LoanApplication
from app.modules.credits.repository import applications as apps_repo
from app.modules.credits.repository.applications import (
    ApplicationConflictError,
    ApplicationError,
    ApplicationNotFoundError,
    ApplicationValidationError,
    IdempotencyKeyConflictError,
)
from app.modules.credits.service.evaluation import evaluate_application

logger = logging.getLogger(__name__)

#: Endpoint logico para la idempotencia del `POST` (clave `shared`, UQ por usuario).
IDEMPOTENCY_ENDPOINT = "/credits/applications"

#: Plantilla push por dictamen (canal natural `push`, sin PII ni cifras).
_DECISION_TEMPLATES = {
    "APPROVED": "credit_application_approved",
    "REJECTED": "credit_application_rejected",
    "IN_REVIEW": "credit_review_required",
}

#: Evento terminal por dictamen (el `submitted` siempre se emite).
_DECISION_EVENTS = {
    "APPROVED": credit_events.CREDIT_APPROVED,
    "REJECTED": credit_events.CREDIT_REJECTED,
    "IN_REVIEW": credit_events.CREDIT_REVIEW_REQUIRED,
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_owner(owner_user_id: uuid.UUID | str) -> uuid.UUID:
    if isinstance(owner_user_id, uuid.UUID):
        return owner_user_id
    try:
        return uuid.UUID(str(owner_user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApplicationValidationError(
            f"user_id debe ser UUID, recibido: {owner_user_id!r}"
        ) from exc


def to_view(row: LoanApplication) -> dict[str, Any]:
    """Vista del expediente para respuestas API (solo el titular/analista)."""

    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "id": str(row.id),
        "user_id": str(row.user_id),
        "product_id": str(row.product_id),
        "requested_amount_minor": int(row.requested_amount_minor),
        "term_months": int(row.term_months),
        "status": row.status,
        "bureau_authorized": bool(row.bureau_authorized),
        "bureau_authorized_at": _iso(row.bureau_authorized_at),
        "score": None if row.score is None else int(row.score),
        "dti_ratio": None if row.dti_ratio is None else str(row.dti_ratio),
        "total_income_minor": (
            None if row.total_income_minor is None else int(row.total_income_minor)
        ),
        "total_debt_minor": (None if row.total_debt_minor is None else int(row.total_debt_minor)),
        "decision_reason": row.decision_reason,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "decided_at": _iso(row.decided_at),
    }


def http_status_for(status: str) -> int:
    """`201` con dictamen final, `202` si se difiere a revision (HU10)."""
    return 202 if status == "IN_REVIEW" else 201


def _require_consent(bureau_authorized: object) -> None:
    """Sin consentimiento explicito no hay evaluacion (HU10 CA-01)."""
    if bureau_authorized is not True:
        raise ApplicationValidationError(
            "la solicitud exige consentimiento explicito de bureau " "(bureau_authorized=true)"
        )


def _to_product_dict(session: Session, product_id: object) -> dict[str, Any]:
    from app.modules.credits import service as catalog

    try:
        return catalog.get_product(session, product_id=product_id)  # type: ignore[arg-type]
    except credits_repo.LoanProductNotFoundError as exc:
        raise ApplicationNotFoundError("producto inexistente") from exc
    except credits_repo.LoanProductError as exc:
        raise ApplicationValidationError(str(exc)) from exc


def _check_product_range(
    product: dict[str, Any], *, requested_amount_minor: int, term_months: int
) -> None:
    """Monto/plazo dentro de los topes del producto (HU10 CA-02)."""
    if not product.get("enabled", True):
        raise ApplicationValidationError("el producto no esta habilitado")
    if not (
        int(product["min_amount_minor"])
        <= requested_amount_minor
        <= int(product["max_amount_minor"])
    ):
        raise ApplicationValidationError("monto fuera del rango del producto")
    if not (int(product["min_term_months"]) <= term_months <= int(product["max_term_months"])):
        raise ApplicationValidationError("plazo fuera del rango del producto")
    allowed = product.get("allowed_terms")
    if allowed is not None and term_months not in list(allowed):
        raise ApplicationValidationError("plazo no permitido por el producto")


def _record_event(session: Session, row: LoanApplication, *, event_type: str) -> None:
    """Registra el evento en el outbox (payload sin PII: solo IDs+estado)."""
    from app.modules.shared.repository.outbox import insert_event

    insert_event(
        session,
        aggregate_type="loan_application",
        aggregate_id=row.id,
        event_type=event_type,
        payload={
            "application_id": str(row.id),
            "user_id": str(row.user_id),
            "product_id": str(row.product_id),
            "status": row.status,
        },
    )


def _audit_decision(
    session: Session,
    row: LoanApplication,
    *,
    actor: uuid.UUID,
    action: str,
) -> None:
    """Auditoria best-effort del dictamen (sin PII: sin ingresos/deuda)."""
    try:
        from app.modules.audit.service import record as audit_record

        audit_record(
            session,
            actor=actor,
            action=action,
            entity="loan_application",
            entity_id=row.id,
            metadata={
                "application_id": str(row.id),
                "product_id": str(row.product_id),
                "status": row.status,
                "decision_reason": row.decision_reason,
            },
        )
    except Exception as exc:  # noqa: BLE001 - la auditoria no bloquea el dictamen
        logger.warning(
            "credit audit best-effort omitida action=%s err=%s", action, type(exc).__name__
        )


def _notify_decision(session: Session, row: LoanApplication) -> None:
    """Notificacion push best-effort al titular (una por dictamen)."""
    try:
        from app.modules.notifications import service as notifications_service

        template = _DECISION_TEMPLATES[row.status]
        notifications_service.send(
            session,
            channel="push",
            recipient=str(row.user_id),
            template_code=template,
            data={},
            user_id=row.user_id,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort: no bloquea el dictamen
        logger.warning(
            "credit notify best-effort omitida status=%s err=%s",
            row.status,
            type(exc).__name__,
        )


def _query_bureau(
    provider: CreditBureauProvider,
    *,
    user_id: uuid.UUID,
    income_minor: int,
    debt_minor: int,
) -> BureauScore | None:
    """Consulta al bureau; caido (`UNAVAILABLE`/`TIMEOUT`) -> `None`.

    `None` lo manda a `IN_REVIEW` el motor (no es error 500). Un error
    de validacion del adaptador (entradas propias) si es `422`.
    """
    try:
        return provider.get_score(
            subject_ref=str(user_id),
            income_minor=income_minor,
            debt_minor=debt_minor,
        )
    except CreditBureauError as exc:
        if exc.code in (BUREAU_UNAVAILABLE, BUREAU_TIMEOUT):
            logger.warning("credit bureau no disponible code=%s", exc.code)
            return None
        raise ApplicationValidationError(str(exc)) from exc


def submit_application(
    session: Session,
    owner_user_id: uuid.UUID | str,
    *,
    product_id: object,
    requested_amount_minor: int,
    term_months: int,
    total_income_minor: int,
    total_debt_minor: int,
    bureau_authorized: object,
    bureau_provider: CreditBureauProvider,
    notify: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Solicitud + evaluacion + dictamen persistido (HU10 CA-01..CA-03).

    Flujo `SUBMITTED -> IN_EVALUATION -> {APPROVED|REJECTED|IN_REVIEW}`
    con `decided_at` y `decision_reason`. Emite `submitted` + evento
    terminal por `outbox`, audita y notifica (best-effort). Retorna la
    vista del expediente.
    """
    owner = _coerce_owner(owner_user_id)
    _require_consent(bureau_authorized)
    if not isinstance(requested_amount_minor, bool) and isinstance(requested_amount_minor, int):
        requested = requested_amount_minor
    else:
        raise ApplicationValidationError("requested_amount_minor debe ser entero")
    if not isinstance(term_months, bool) and isinstance(term_months, int):
        term = term_months
    else:
        raise ApplicationValidationError("term_months debe ser entero")
    if not isinstance(total_income_minor, bool) and isinstance(total_income_minor, int):
        income = total_income_minor
    else:
        raise ApplicationValidationError("total_income_minor debe ser entero")
    if not isinstance(total_debt_minor, bool) and isinstance(total_debt_minor, int):
        debt = total_debt_minor
    else:
        raise ApplicationValidationError("total_debt_minor debe ser entero")
    product = _to_product_dict(session, product_id)
    _check_product_range(product, requested_amount_minor=requested, term_months=term)

    moment = now or _utcnow()
    row = apps_repo.create_application(
        session,
        user_id=owner,
        product_id=product["id"],
        requested_amount_minor=requested,
        term_months=term,
        status="SUBMITTED",
    )
    apps_repo.set_evaluating(session, row)
    row.bureau_authorized = True
    row.bureau_authorized_at = moment
    session.flush()

    bureau = _query_bureau(bureau_provider, user_id=owner, income_minor=income, debt_minor=debt)
    external_debt = int(bureau.deudas_minor) if bureau is not None else 0
    effective_debt = debt + external_debt
    try:
        decision = evaluate_application(
            session,
            requested_amount_minor=requested,
            term_months=term,
            total_income_minor=income,
            total_debt_minor=effective_debt,
            bureau_score=bureau,
        )
    except rules.CreditRulesValidationError as exc:
        raise ApplicationValidationError(str(exc)) from exc

    apps_repo.set_decision(
        session,
        row,
        status=decision.decision,
        score=decision.score,
        dti_ratio=decision.dti_ratio,
        total_income_minor=income,
        total_debt_minor=effective_debt,
        decision_reason=decision.reason,
        bureau_authorized_at=moment,
        now=moment,
    )
    _record_event(session, row, event_type=credit_events.CREDIT_APPLICATION_SUBMITTED)
    _record_event(session, row, event_type=_DECISION_EVENTS[decision.decision])
    _audit_decision(session, row, actor=owner, action="credit.application.decided")
    if notify:
        _notify_decision(session, row)
    logger.info("credit application decision=%s", decision.decision)
    return to_view(row)


def _minimal_idempotency_snapshot(*, application_id: object, status: object) -> dict:
    """Snapshot de idempotencia SIN PII: solo referencia + estado."""
    return {"application_id": str(application_id), "status": str(status)}


def _replay_idempotent_view(session: Session, owner: uuid.UUID, stored) -> dict:
    """Reconstruye la vista desde la BD (sin re-evaluar ni re-notificar)."""
    snapshot = stored.response_snapshot
    if not isinstance(snapshot, dict) or snapshot.get("application_id") is None:
        raise IdempotencyKeyConflictError("operacion en curso para esta Idempotency-Key")
    row = apps_repo.get_application(session, snapshot["application_id"])
    if row is None or row.user_id != owner:
        raise IdempotencyKeyConflictError("expediente asociado a la clave ya no disponible")
    return to_view(row)


def submit_application_idempotent(
    session: Session,
    owner_user_id: uuid.UUID | str,
    *,
    body: dict,
    idempotency_key: str | None,
    bureau_provider: CreditBureauProvider,
    notify: bool = True,
) -> tuple[dict, bool, int]:
    """Solicitud con `Idempotency-Key` obligatoria (crea recurso).

    Misma clave + mismo cuerpo -> replay sin duplicar expediente ni
    notificacion (`replayed=True`); misma clave + distinto cuerpo ->
    `CONFLICT` (409); sin clave -> `VALIDATION_ERROR` (422). Retorna
    `(vista, replayed, http_status)`: `201` con dictamen final o `202`
    si se difiere a revision.
    """
    from app.modules.shared.repository.idempotency import (
        compute_request_hash,
        find_key,
        is_expired,
        renew_expired,
        resolve_ttl_seconds,
        store_response,
        try_claim,
    )

    owner = _coerce_owner(owner_user_id)
    key = str(idempotency_key).strip() if isinstance(idempotency_key, str) else ""
    if not key:
        raise ApplicationValidationError("cabecera Idempotency-Key obligatoria")
    if len(key) > 80:
        raise ApplicationValidationError("Idempotency-Key supera 80 caracteres")
    if not isinstance(body, dict):
        raise ApplicationValidationError("cuerpo invalido")
    _require_consent(body.get("bureau_authorized"))
    digest = compute_request_hash({"owner": str(owner), **{k: str(v) for k, v in body.items()}})

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
        view = submit_application(
            session,
            owner,
            product_id=body.get("product_id"),
            requested_amount_minor=body.get("requested_amount_minor"),
            term_months=body.get("term_months"),
            total_income_minor=body.get("total_income_minor"),
            total_debt_minor=body.get("total_debt_minor"),
            bureau_authorized=body.get("bureau_authorized"),
            bureau_provider=bureau_provider,
            notify=notify,
        )
        store_response(
            session,
            row,
            response_snapshot=_minimal_idempotency_snapshot(
                application_id=view["id"], status=view["status"]
            ),
            transaction_id=view["id"],
        )
        return view, False, http_status_for(view["status"])
    stored = find_key(session, key=key, user_id=owner)
    if stored is None:  # pragma: no cover - carrera extrema
        raise IdempotencyKeyConflictError("clave de idempotencia en conflicto")
    if is_expired(stored):
        renew_expired(
            session,
            stored,
            request_hash=digest,
            endpoint=IDEMPOTENCY_ENDPOINT,
            method="POST",
        )
        view = submit_application(
            session,
            owner,
            product_id=body.get("product_id"),
            requested_amount_minor=body.get("requested_amount_minor"),
            term_months=body.get("term_months"),
            total_income_minor=body.get("total_income_minor"),
            total_debt_minor=body.get("total_debt_minor"),
            bureau_authorized=body.get("bureau_authorized"),
            bureau_provider=bureau_provider,
            notify=notify,
        )
        store_response(
            session,
            stored,
            response_snapshot=_minimal_idempotency_snapshot(
                application_id=view["id"], status=view["status"]
            ),
            transaction_id=view["id"],
        )
        return view, False, http_status_for(view["status"])
    if stored.request_hash != digest:
        raise IdempotencyKeyConflictError("Idempotency-Key ya usada con un cuerpo distinto")
    view = _replay_idempotent_view(session, owner, stored)
    return view, True, http_status_for(view["status"])


def get_own_application(
    session: Session, owner_user_id: uuid.UUID | str, application_id: object
) -> dict[str, Any]:
    """Estado del expediente del titular (HU10 CA-04).

    Inexistente o ajeno -> `NOT_FOUND` neutro (mismo cuerpo, sin
    oraculo de existencia).
    """
    owner = _coerce_owner(owner_user_id)
    row = apps_repo.get_application(session, application_id)  # type: ignore[arg-type]
    if row is None or row.user_id != owner:
        raise ApplicationNotFoundError("expediente inexistente")
    return to_view(row)


def list_review_queue(
    session: Session, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    """Bandeja del analista (`IN_REVIEW`)."""
    return [to_view(row) for row in apps_repo.list_in_review(session, limit=limit, offset=offset)]


def review_application(
    session: Session,
    *,
    analyst_id: uuid.UUID | str,
    application_id: object,
    decision: object,
    reason: object,
    notify: bool = True,
) -> dict[str, Any]:
    """Dictamen manual del analista (aprobacion separada, HU10).

    Solo `IN_REVIEW -> APPROVED|REJECTED` con justificacion; otro
    estado (incluido ya decidido) -> `CONFLICT`. Separacion de
    funciones: el solicitante no puede dictaminar su propio expediente
    aunque su token traiga rol analista -> `CONFLICT`. Emite el evento
    terminal por `outbox`, audita con el analista como actor y
    notifica al titular (best-effort).
    """
    try:
        analyst = analyst_id if isinstance(analyst_id, uuid.UUID) else uuid.UUID(str(analyst_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApplicationValidationError("analista invalido") from exc
    row = apps_repo.get_application(session, application_id)  # type: ignore[arg-type]
    if row is None:
        raise ApplicationNotFoundError("expediente inexistente")
    if row.user_id == analyst:
        raise ApplicationConflictError("el solicitante no puede dictaminar su propio expediente")
    if not isinstance(decision, str) or decision not in ("APPROVED", "REJECTED"):
        raise ApplicationValidationError("decision debe ser APPROVED o REJECTED")
    apps_repo.set_manual_decision(
        session, row, status=decision, decision_reason=reason  # type: ignore[arg-type]
    )
    _record_event(session, row, event_type=_DECISION_EVENTS[decision])
    _audit_decision(session, row, actor=analyst, action="credit.application.reviewed")
    if notify:
        _notify_decision(session, row)
    logger.info("credit application reviewed decision=%s", decision)
    return to_view(row)


__all__ = [
    "IDEMPOTENCY_ENDPOINT",
    "ApplicationConflictError",
    "ApplicationError",
    "ApplicationNotFoundError",
    "ApplicationValidationError",
    "IdempotencyKeyConflictError",
    "get_own_application",
    "http_status_for",
    "list_review_queue",
    "review_application",
    "submit_application",
    "submit_application_idempotent",
    "to_view",
]
