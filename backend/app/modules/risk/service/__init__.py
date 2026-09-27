"""Servicio minimo de perfiles de riesgo (E2-T18, HU08).

Punto de entrada estable para los consumidores (`E2-T17` motor,
`E2-T20` consumo de alertas) y para HU20 (E6-T03/E6-T04, que ampliara
el perfil con el historial): delega en el repositorio sin logica de
negocio propia. Sin endpoints aqui (estan en `risk/api`, HU20).
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.modules.risk import repository as risk_repo
from app.modules.risk.models import RiskProfile

__all__ = ["get_or_create_profile", "update_behavior"]


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
