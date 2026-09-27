"""Repositorio de `risk.risk_profiles` (E2-T18, minimo HU08).

Capa de datos sin endpoints ni logica de negocio: solo SQLAlchemy sobre
el schema propio. Convencion: `flush` sin `commit` (decide el llamante).
`get_or_create`/`update_behavior` son idempotentes por PK (`user_id`):
repetir no duplica filas; concurrencia = ultimo escritor gana (el
`INSERT` en carrera se resuelve por `IntegrityError` en savepoint y
relee la fila ganadora).

Validaciones minimas (HU20 podra endurecerlas sin romper esta API):
`avg_amount_minor` entero `>= 0`; `score` entero `>= 0`; `usual_hours`
lista de horas `0..23`; `usual_countries` lista de codigos no vacios
(normalizados a mayusculas); `last_device_id` texto `<= 128`. Sin PII
en logs (solo `user_id`).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.risk.models import RiskProfile

logger = logging.getLogger(__name__)


class RiskProfileError(ValueError):
    """Error base de perfiles de riesgo (mapeable a 4xx)."""


class RiskProfileValidationError(RiskProfileError):
    """Dato de perfil invalido (`VALIDATION_ERROR`)."""


def _coerce_user_id(value: uuid.UUID | str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise RiskProfileValidationError(f"user_id debe ser UUID, recibido: {value!r}") from exc


def _coerce_nonneg_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RiskProfileValidationError(f"{name} debe ser entero, recibido: {value!r}")
    if value < 0:
        raise RiskProfileValidationError(f"{name} debe ser >= 0, recibido: {value!r}")
    return value


def _coerce_countries(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise RiskProfileValidationError(f"usual_countries debe ser lista, recibido: {value!r}")
    countries: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 8:
            raise RiskProfileValidationError(f"usual_countries con codigo invalido: {item!r}")
        countries.append(item.strip().upper())
    return countries


def _coerce_hours(value: object) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise RiskProfileValidationError(f"usual_hours debe ser lista, recibido: {value!r}")
    hours: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 23:
            raise RiskProfileValidationError(f"usual_hours con hora invalida: {item!r}")
    hours.extend(int(item) for item in value)
    return hours


def _coerce_device_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise RiskProfileValidationError(f"last_device_id invalido: {value!r}")
    return value


def get_by_user(session: Session, user_id: uuid.UUID | str) -> RiskProfile | None:
    """Perfil del usuario (`None` si aun no existe)."""
    return session.get(RiskProfile, _coerce_user_id(user_id))


def get_or_create(session: Session, user_id: uuid.UUID | str) -> RiskProfile:
    """Perfil del usuario, creandolo con defaults (`03b#112`) si falta.

    Idempotente: dos llamadas (incluso en carrera) retornan una sola fila.
    Hace `flush`, no `commit`.
    """
    uid = _coerce_user_id(user_id)
    existing = session.get(RiskProfile, uid)
    if existing is not None:
        return existing
    row = RiskProfile(user_id=uid)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        logger.debug("risk_profiles carrera en get_or_create (%s): relee", uid)
        existing = session.get(RiskProfile, uid)
        if existing is None:  # pragma: no cover - defensivo
            raise
        return existing
    return row


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
    """Actualiza habitos/puntaje del perfil (creandolo si falta).

    Solo toca los campos informados (`None` = conservar). Valida antes
    de escribir: un dato invalido lanza `RiskProfileValidationError` sin
    dejar cambios. Hace `flush`, no `commit`.
    """
    row = get_or_create(session, user_id)
    if avg_amount_minor is not None:
        row.avg_amount_minor = _coerce_nonneg_int("avg_amount_minor", avg_amount_minor)
    if usual_countries is not None:
        row.usual_countries = _coerce_countries(usual_countries)
    if usual_hours is not None:
        row.usual_hours = _coerce_hours(usual_hours)
    if last_device_id is not None:
        row.last_device_id = _coerce_device_id(last_device_id)
    if score is not None:
        row.score = _coerce_nonneg_int("score", score)
    session.flush()
    return row
