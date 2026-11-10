"""Repositorio del expediente `credits.loan_applications` (E3-T08, HU10).

Capa de datos sin endpoints ni logica de negocio: solo SQLAlchemy sobre
el schema propio (`credits`), sin `JOIN` entre schemas ni imports de
modelos ajenos (regla de oro 4). Convencion: `flush` sin `commit`
(decide el llamante).

Errores de negocio con `code` estable (`docs/05`, formato de errores):
`NOT_FOUND` (expediente inexistente), `VALIDATION_ERROR` (tipos/rangos
invalidos) y `CONFLICT` (transicion de estado no permitida: expediente
ya decidido o revision sobre estado no `IN_REVIEW`).

Sin PII ni secretos en logs (regla de oro 7): aqui no se loguean
montos, score, razones ni identificadores (solo `id` y conteos).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.credits.models import (
    DECIDED_STATUSES,
    LOAN_APPLICATION_STATUSES,
    Contract,
    LoanApplication,
)

logger = logging.getLogger(__name__)

_TERMINAL_BY_REVIEW = ("APPROVED", "REJECTED")

#: Tope informativo de `dti_ratio` (`NUMERIC(12,4)` en `docs/03b#82`).
#: El DTI es solo informativo (el dictamen lo decide el motor E3-T07 con
#: el valor exacto): por encima del tope se acota, nunca se rechaza ni
#: se deja desbordar la columna (antes: `DataError` -> HTTP 500).
_DTI_CAP = Decimal("99999999.9999")


class ApplicationError(ValueError):
    """Error base del expediente (mapeable a 4xx)."""

    code = "CREDITS_APPLICATION_ERROR"


class ApplicationNotFoundError(ApplicationError, LookupError):
    """Expediente inexistente o de otro titular (`NOT_FOUND` neutro)."""

    code = "NOT_FOUND"


class ApplicationValidationError(ApplicationError):
    """Dato del expediente invalido (`VALIDATION_ERROR`)."""

    code = "VALIDATION_ERROR"


class ApplicationConflictError(ApplicationError):
    """Transicion de estado no permitida (`CONFLICT`)."""

    code = "CONFLICT"


class IdempotencyKeyConflictError(ApplicationConflictError):
    """Misma `Idempotency-Key` con distinto cuerpo o marca en vuelo (409)."""

    code = "IDEMPOTENCY_CONFLICT"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApplicationValidationError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def _coerce_minor(name: str, value: object, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApplicationValidationError(f"{name} debe ser entero en centimos, recibido: {value!r}")
    if value < minimum:
        raise ApplicationValidationError(f"{name} debe ser >= {minimum}")
    return value


def _coerce_term_months(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApplicationValidationError(
            f"term_months debe ser entero de meses, recibido: {value!r}"
        )
    if not 1 <= value <= 360:
        raise ApplicationValidationError("term_months debe estar en 1..360")
    return value


def _coerce_dti(value: Decimal | int | str | None) -> Decimal | None:
    """DTI persistible: numerico acotado a `±_DTI_CAP` (`None` se respeta).

    Acota en lugar de rechazar (el valor es informativo; la decision ya
    la tomo el motor con el DTI exacto). No numerico o `NaN`/`inf` ->
    `VALIDATION_ERROR`. Sin PII en el mensaje (sin eco del valor).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ApplicationValidationError("dti_ratio debe ser numerico")
    try:
        candidate = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, AttributeError, TypeError) as exc:
        raise ApplicationValidationError("dti_ratio debe ser numerico") from exc
    if candidate.is_nan() or candidate.is_infinite():
        raise ApplicationValidationError("dti_ratio debe ser numerico finito")
    if candidate > _DTI_CAP:
        return _DTI_CAP
    if candidate < -_DTI_CAP:
        return -_DTI_CAP
    return candidate


def create_application(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    product_id: uuid.UUID | str,
    requested_amount_minor: int,
    term_months: int,
    status: str = "SUBMITTED",
) -> LoanApplication:
    """Crea el expediente en estado inicial (`SUBMITTED`).

    Valida tipos/rangos antes de escribir (el caso de uso valida el
    consentimiento y los topes del producto antes de llamar). Hace
    `flush`, no `commit`.
    """
    owner = _coerce_uuid(user_id, "user_id")
    product = _coerce_uuid(product_id, "product_id")
    amount = _coerce_minor("requested_amount_minor", requested_amount_minor, minimum=1)
    term = _coerce_term_months(term_months)
    if status not in LOAN_APPLICATION_STATUSES:
        raise ApplicationValidationError(f"status invalido: {status!r}")
    row = LoanApplication(
        user_id=owner,
        product_id=product,
        requested_amount_minor=amount,
        term_months=term,
        status=status,
        bureau_authorized=False,
        bureau_authorized_at=None,
    )
    session.add(row)
    session.flush()
    logger.debug("loan_applications creada status=%s", status)
    return row


def get_application(session: Session, application_id: uuid.UUID | str) -> LoanApplication | None:
    """Expediente por `id` (`None` si no existe o el `id` es malformado)."""
    try:
        aid = _coerce_uuid(application_id, "application_id")
    except ApplicationValidationError:
        return None
    return session.get(LoanApplication, aid)


def set_evaluating(session: Session, row: LoanApplication) -> LoanApplication:
    """Marca `SUBMITTED -> IN_EVALUATION` (el consentimiento ya se valido).

    Cualquier otro estado -> `CONFLICT` (no se reevalua un expediente ya
    decidido ni en curso).
    """
    if row.status != "SUBMITTED":
        raise ApplicationConflictError(f"el expediente en estado {row.status} no admite evaluacion")
    row.status = "IN_EVALUATION"
    row.updated_at = _utcnow()
    session.flush()
    return row


def set_decision(
    session: Session,
    row: LoanApplication,
    *,
    status: str,
    score: int | None = None,
    dti_ratio: Decimal | None = None,
    total_income_minor: int | None = None,
    total_debt_minor: int | None = None,
    decision_reason: str | None = None,
    bureau_authorized_at: datetime | None = None,
    now: datetime | None = None,
) -> LoanApplication:
    """Persiste el dictamen (`IN_EVALUATION -> APPROVED|REJECTED|IN_REVIEW`).

    Guarda `score`, `dti_ratio`, ingresos/deuda efectivos,
    `decision_reason` y `decided_at` para auditoria. Fuera de
    `IN_EVALUATION` -> `CONFLICT` (un decidido no se reevalua).
    `dti_ratio` se acota a `±99999999.9999` (`NUMERIC(12,4)`): un DTI
    extremo nunca desborda la columna (el dictamen ya decidio el motor
    con el valor exacto).
    """
    if status not in _TERMINAL_BY_REVIEW + ("IN_REVIEW",):
        raise ApplicationValidationError(f"dictamen invalido: {status!r}")
    if row.status != "IN_EVALUATION":
        raise ApplicationConflictError(f"el expediente en estado {row.status} no admite dictamen")
    if score is not None and (isinstance(score, bool) or not isinstance(score, int)):
        raise ApplicationValidationError(f"score debe ser entero, recibido: {score!r}")
    moment = now or _utcnow()
    row.status = status
    row.score = score
    row.dti_ratio = _coerce_dti(dti_ratio)
    if total_income_minor is not None:
        row.total_income_minor = _coerce_minor("total_income_minor", total_income_minor, minimum=1)
    if total_debt_minor is not None:
        row.total_debt_minor = _coerce_minor("total_debt_minor", total_debt_minor, minimum=0)
    if decision_reason is not None:
        if not isinstance(decision_reason, str) or not decision_reason.strip():
            raise ApplicationValidationError("decision_reason debe ser texto no vacio")
        row.decision_reason = decision_reason.strip()[:2000]
    if bureau_authorized_at is not None:
        row.bureau_authorized = True
        row.bureau_authorized_at = bureau_authorized_at
    row.decided_at = moment
    row.updated_at = moment
    session.flush()
    logger.debug("loan_applications dictamen status=%s", status)
    return row


def set_manual_decision(
    session: Session,
    row: LoanApplication,
    *,
    status: str,
    decision_reason: str,
    now: datetime | None = None,
) -> LoanApplication:
    """Dictamen manual del analista (`IN_REVIEW -> APPROVED|REJECTED`).

    La aprobacion queda separada de la solicitud (HU10): solo un
    expediente `IN_REVIEW` cambia por esta via; ya decidido o en otro
    estado -> `CONFLICT`. La `decision_reason` manual es obligatoria.
    """
    if status not in _TERMINAL_BY_REVIEW:
        raise ApplicationValidationError(f"dictamen manual invalido: {status!r}")
    if row.status != "IN_REVIEW":
        raise ApplicationConflictError(
            f"el expediente en estado {row.status} no admite revision manual"
        )
    if not isinstance(decision_reason, str) or not decision_reason.strip():
        raise ApplicationValidationError("la revision manual exige justificacion")
    if row.status in DECIDED_STATUSES:  # pragma: no cover - defensa en profundidad
        raise ApplicationConflictError("el expediente ya tiene dictamen final")
    moment = now or _utcnow()
    row.status = status
    row.decision_reason = decision_reason.strip()[:2000]
    row.decided_at = moment
    row.updated_at = moment
    session.flush()
    logger.debug("loan_applications revision manual status=%s", status)
    return row


def list_in_review(session: Session, *, limit: int = 50, offset: int = 0) -> list[LoanApplication]:
    """Bandeja del analista: expedientes `IN_REVIEW` (paginada)."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ApplicationValidationError("limit debe estar en 1..100")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ApplicationValidationError("offset debe ser >= 0")
    stmt = (
        sa.select(LoanApplication)
        .where(LoanApplication.status == "IN_REVIEW")
        .order_by(LoanApplication.created_at, LoanApplication.id)
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt).all())


def list_by_user(
    session: Session, user_id: uuid.UUID | str, *, limit: int = 50, offset: int = 0
) -> list[LoanApplication]:
    """Listado del titular: sus expedientes, mas reciente primero (E3-T23).

    Filtra por `user_id` en la consulta (aislamiento: jamas entrega
    expedientes ajenos) y ordena `created_at DESC, id DESC`
    (determinista para paginar). Sin PII en logs (solo conteos).
    """
    owner = _coerce_uuid(user_id, "user_id")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ApplicationValidationError("limit debe estar en 1..100")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ApplicationValidationError("offset debe ser >= 0")
    stmt = (
        sa.select(LoanApplication)
        .where(LoanApplication.user_id == owner)
        .order_by(LoanApplication.created_at.desc(), LoanApplication.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = list(session.scalars(stmt).all())
    logger.debug("loan_applications listado propio count=%d", len(rows))
    return rows


def count_by_user(session: Session, user_id: uuid.UUID | str) -> int:
    """Conteo de expedientes del titular (trazabilidad/idempotencia)."""
    owner = _coerce_uuid(user_id, "user_id")
    stmt = (
        sa.select(sa.func.count())
        .select_from(LoanApplication)
        .where(LoanApplication.user_id == owner)
    )
    return int(session.scalar(stmt) or 0)


def signed_flags_by_application(
    session: Session, application_ids: list[uuid.UUID | str]
) -> dict[str, bool]:
    """Firma del contrato por expediente (E3-T23 mejora "Mis creditos").

    Deriva de `credits.contracts` (mismo schema, sin migracion): un
    expediente esta firmado solo si tiene filas de contrato y **todas**
    estan `SIGNED` (el juego se firma completo o no se firma). Sin
    filas -> `False` (no firmado / juego aun no generado). Retorna
    `{str(application_id): firmado}` solo para los expedientes con
    filas; el llamante interpreta la ausencia como `False`.

    Solo lectura de estados (sin PII: ni `doc_ref` ni `doc_hash`).

    Si la tabla no existe en el entorno (humo SQLite minimo sin el
    juego contractual), retorna `{}` (no firmado) en vez de romper la
    lectura del expediente.
    """
    ids: list[uuid.UUID] = []
    for raw in application_ids:
        try:
            aid = raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
        except (ValueError, AttributeError, TypeError):
            continue
        ids.append(aid)
    if not ids:
        return {}
    stmt = sa.select(Contract.application_id, Contract.status).where(
        Contract.application_id.in_(ids)
    )
    try:
        rows = session.execute(stmt).all()
    except sa.exc.SQLAlchemyError:
        logger.debug("contracts no legibles: se asume no firmado")
        return {}
    seen: dict[str, set[str]] = {}
    for app_id, status in rows:
        seen.setdefault(str(app_id), set()).add(str(status))
    return {key: statuses == {"SIGNED"} for key, statuses in seen.items()}


__all__ = [
    "ApplicationConflictError",
    "ApplicationError",
    "ApplicationNotFoundError",
    "ApplicationValidationError",
    "IdempotencyKeyConflictError",
    "count_by_user",
    "create_application",
    "get_application",
    "list_by_user",
    "list_in_review",
    "set_decision",
    "set_evaluating",
    "set_manual_decision",
    "signed_flags_by_application",
]
