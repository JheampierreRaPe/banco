"""Fachada de evaluacion crediticia (E3-T07, HU10).

Une el motor puro (``credits.domain.rules``) con los parametros vigentes
(``shared/config``, dueno de ``config.parameters``): lee la foto de
``loan.*`` por llamada (sin cache: un cambio se refleja de inmediato, sin
despliegue ni reinicio) y delega el dictamen al motor.

Solo lectura: no escribe parametros, no persiste expedientes (E3-T08), no
emite eventos y no mueve dinero. Nunca lanza por parametros (la fachada
duena aplica fallback a las semillas); los errores de negocio del motor
(``CreditRulesValidationError`` -> ``VALIDATION_ERROR``) si se propagan.

Sin PII ni secretos en logs (regla de oro 7): los logs llevan el dictamen
y la clave de tope aplicada, jamas montos, score ni identificadores.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from sqlalchemy.orm import Session

from app.adapters.credit_bureau import BureauScore
from app.modules.credits.domain import rules
from app.modules.shared.config import parameters as config_params

logger = logging.getLogger(__name__)

__all__ = ["evaluate_application"]


def evaluate_application(
    session: Session,
    *,
    requested_amount_minor: int,
    term_months: int,
    total_income_minor: int,
    total_debt_minor: int,
    bureau_score: BureauScore | int | None = None,
    profile: str | None = None,
    params_override: Mapping[str, object] | None = None,
) -> rules.CreditDecision:
    """Pre-califica con los parametros vigentes + el motor puro.

    - `params_override`: valores que pisan la foto de BD (claves `loan.*`
      crudas, como `value_json`); sirve para previsualizar un cambio sin
      escribirlo. `None` = solo BD + fallbacks.
    - Retorna `CreditDecision` (`APPROVED` / `REJECTED` / `IN_REVIEW` con
      `reason` para `decision_reason`).

    La foto incluye el override por perfil (`loan.max_amount_{P}_minor`)
    cuando existe en BD; si falta o es invalido, el motor cae al tope
    generico (mismo patron que el limite diario, E2-T18).
    """
    snapshot: dict[str, object] = dict(config_params.get_loan_params(session))
    amount_key = config_params.loan_amount_key_for_profile(profile)
    if amount_key is not None:
        raw = config_params.get_parameter(session, amount_key, None)
        if raw is not None:
            snapshot[amount_key] = raw
    if params_override is not None:
        if not isinstance(params_override, Mapping):
            raise rules.CreditRulesValidationError(
                f"params_override debe ser un mapeo, recibido: {params_override!r}"
            )
        snapshot.update(params_override)
    decision = rules.evaluate(
        requested_amount_minor=requested_amount_minor,
        term_months=term_months,
        total_income_minor=total_income_minor,
        total_debt_minor=total_debt_minor,
        bureau_score=bureau_score,
        profile=profile,
        params=snapshot,
    )
    logger.info(
        "credit evaluation decision=%s amount_key=%s",
        decision.decision,
        decision.applied_amount_key,
    )
    return decision
