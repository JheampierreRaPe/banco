"""Motor de reglas de credito configurable (E3-T07, HU10 CA-03 parte reglas).

Cubre (`docs/tasks/E3-T07.md#pruebas-requeridas`):

- Camino feliz: aprobado; rechazado por DTI; rechazado por tope;
  en revision por score (banda) y por bureau caido.
- Limites: `dti == max` (borde aprueba), `score == min_score` (revision),
  `score == min + margin` (aprueba).
- Configuracion: cambiar un parametro cambia el dictamen sin reiniciar;
  clave ausente -> fallback documentado; valor texto `"0.4"` (semilla
  `0001`) se acepta igual que el numero.
- Errores: ingreso `<= 0` / deuda negativa / monto-plazo `<= 0` ->
  `VALIDATION_ERROR`; `params` no mapeo y score corrupto tambien.
- Determinismo: mismas entradas -> mismo dictamen y mismo `reason`.

Parte A: pura (sin BD). Parte B: SQLite en memoria (`config`, solo la
fachada `shared/config` + `credits.service.evaluation`). Parte C:
Postgres de prueba (`db_session`, con rollback; se omite si no hay BD).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.modules.credits.domain import rules
from app.modules.credits.domain.rules import (
    APPROVED,
    IN_REVIEW,
    REJECTED,
    CreditDecision,
    CreditRulesValidationError,
    compute_dti,
    evaluate,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
RULES_PATH = BACKEND_DIR / "app" / "modules" / "credits" / "domain" / "rules.py"
MIGRATION_PATH = BACKEND_DIR / "migrations" / "versions" / "0030_credits_loan_rule_params.py"


def _base(**overrides):
    """Solicitud sana por defecto: aprueba con los defaults."""
    params = {
        "requested_amount_minor": 1_000_000,
        "term_months": 12,
        "total_income_minor": 400_000,
        "total_debt_minor": 100_000,  # dti = 0.25
        "bureau_score": 780,
    }
    params.update(overrides)
    return params


# ---------------------------------------------------------- Parte A: pura
def test_approved_happy_path():
    decision = evaluate(**_base())
    assert isinstance(decision, CreditDecision)
    assert decision.decision == APPROVED
    assert decision.dti_ratio == Decimal("0.2500")
    assert decision.score == 780
    assert decision.reason.startswith("aprobado:")
    assert decision.max_dti_ratio == Decimal("0.4")
    assert decision.max_amount_minor == 5_000_000
    assert decision.min_score == 650
    assert decision.review_margin == Decimal(50)
    assert decision.max_debt_minor == 10_000_000
    assert decision.profile is None


def test_rejected_by_dti():
    decision = evaluate(**_base(total_debt_minor=200_000))  # dti = 0.5
    assert decision.decision == REJECTED
    assert decision.dti_ratio == Decimal("0.5000")
    assert "DTI" in decision.reason


def test_rejected_by_amount_cap():
    decision = evaluate(**_base(requested_amount_minor=5_000_001))
    assert decision.decision == REJECTED
    assert "tope" in decision.reason


def test_rejected_by_debt_cap():
    params = _base(total_debt_minor=10_000_001, total_income_minor=100_000_000)
    decision = evaluate(**params)  # dti = 0.1, pero deuda sobre el maximo
    assert decision.decision == REJECTED
    assert "deuda" in decision.reason


def test_rejected_by_low_score():
    decision = evaluate(**_base(bureau_score=600))
    assert decision.decision == REJECTED
    assert "score" in decision.reason


def test_in_review_by_score_band():
    decision = evaluate(**_base(bureau_score=660))  # [650, 700)
    assert decision.decision == IN_REVIEW
    assert "revision" in decision.reason


def test_in_review_when_bureau_down():
    decision = evaluate(**_base(bureau_score=None))
    assert decision.decision == IN_REVIEW
    assert "bureau" in decision.reason


def test_bureau_score_contract_accepted():
    """Consume el contrato `BureauScore` de E3-T06 (incluida morosidad)."""
    from app.adapters.credit_bureau import BureauScore

    prime = BureauScore(score=780, deudas_minor=0, morosidad=False)
    assert evaluate(**_base(bureau_score=prime)).decision == APPROVED
    # La morosidad no altera el dictamen en esta version (historial E3-T08).
    with_mora = BureauScore(score=780, deudas_minor=500_00, morosidad=True)
    assert evaluate(**_base(bureau_score=with_mora)).decision == APPROVED


def test_border_dti_equals_max_approves():
    decision = evaluate(**_base(total_income_minor=400_000, total_debt_minor=160_000))
    assert decision.dti_ratio == Decimal("0.4000")
    assert decision.decision == APPROVED


def test_border_score_equals_min_goes_to_review():
    assert evaluate(**_base(bureau_score=650)).decision == IN_REVIEW


def test_border_score_equals_bound_approves():
    assert evaluate(**_base(bureau_score=700)).decision == APPROVED


def test_financial_rejection_wins_over_bureau_down():
    """Hecho financiero adverso rechaza aunque el bureau este caido."""
    decision = evaluate(**_base(total_debt_minor=200_000, bureau_score=None))
    assert decision.decision == REJECTED
    assert "DTI" in decision.reason


def test_config_change_flips_decision_without_restart():
    ok_params = {"loan.max_dti_ratio": Decimal("0.4")}
    strict_params = {"loan.max_dti_ratio": Decimal("0.2")}
    assert evaluate(**_base(params=ok_params)).decision == APPROVED
    changed = evaluate(**_base(params=strict_params))
    assert changed.decision == REJECTED
    assert changed.max_dti_ratio == Decimal("0.2")


def test_missing_params_use_documented_defaults():
    decision = evaluate(**_base(params={}))
    assert decision.decision == APPROVED
    assert decision.max_dti_ratio == rules.DEFAULT_MAX_DTI_RATIO
    assert decision.max_amount_minor == rules.DEFAULT_MAX_AMOUNT_MINOR
    assert decision.min_score == rules.DEFAULT_MIN_SCORE
    assert decision.review_margin == rules.DEFAULT_REVIEW_MARGIN
    assert decision.max_debt_minor == rules.DEFAULT_MAX_DEBT_MINOR


def test_string_seed_form_accepted_as_number():
    """Regresion: `0001` sembro `loan.max_dti_ratio` como texto `"0.4"`."""
    params = {
        "loan.max_dti_ratio": "0.4",
        "loan.max_amount_minor": "5000000",
        "loan.min_score": "650",
        "loan.review_margin": "50",
        "loan.max_debt_minor": "10000000",
    }
    decision = evaluate(**_base(params=params))
    assert decision.decision == APPROVED
    assert decision.max_dti_ratio == Decimal("0.4")
    assert isinstance(decision.max_dti_ratio, Decimal)


def test_invalid_params_fall_back_without_raising():
    params = {
        "loan.max_dti_ratio": "abc",
        "loan.max_amount_minor": -5,
        "loan.min_score": True,
        "loan.review_margin": "NaN",
        "loan.max_debt_minor": 12.5,
    }
    decision = evaluate(**_base(params=params))
    assert decision.decision == APPROVED
    assert decision.max_dti_ratio == Decimal("0.4")
    assert decision.max_amount_minor == 5_000_000


def test_per_profile_amount_override():
    params = {"loan.max_amount_STANDARD_minor": 500_000}
    generic_ok = evaluate(**_base(params=params, profile=None))
    assert generic_ok.decision == APPROVED
    assert generic_ok.applied_amount_key == "loan.max_amount_minor"
    profile_hit = evaluate(**_base(params=params, profile="standard"))
    assert profile_hit.decision == REJECTED
    assert profile_hit.applied_amount_key == "loan.max_amount_STANDARD_minor"
    assert profile_hit.profile == "STANDARD"


def test_invalid_profile_uses_generic_cap():
    params = {"loan.max_amount_STANDARD_minor": 500_000}
    decision = evaluate(**_base(params=params, profile="standard; DROP"))
    assert decision.decision == APPROVED
    assert decision.profile is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("total_income_minor", 0),
        ("total_income_minor", -1),
        ("total_debt_minor", -1),
        ("requested_amount_minor", 0),
        ("requested_amount_minor", -100),
        ("term_months", 0),
        ("term_months", -6),
        ("bureau_score", -5),
        ("bureau_score", "780"),
        ("total_income_minor", True),
        ("requested_amount_minor", 100.0),
    ],
)
def test_validation_errors(field, value):
    with pytest.raises(CreditRulesValidationError):
        evaluate(**_base(**{field: value}))


def test_params_must_be_mapping():
    with pytest.raises(CreditRulesValidationError):
        evaluate(**_base(params=[("loan.min_score", 650)]))


def test_determinism_same_inputs_same_decision():
    first = evaluate(**_base())
    second = evaluate(**_base())
    assert first == second
    assert first.reason == second.reason


def test_compute_dti_exact_and_validated():
    assert compute_dti(100_000, 400_000) == Decimal("0.25")
    assert compute_dti(1, 3) == Decimal(1) / Decimal(3)
    with pytest.raises(CreditRulesValidationError):
        compute_dti(0, 0)
    with pytest.raises(CreditRulesValidationError):
        compute_dti(-1, 100)


def test_defaults_mirror_shared_config():
    """El espejo de constantes no deriva del dueno (`shared/config`)."""
    from app.modules.shared.config import parameters as params

    assert rules.LOAN_MAX_DTI_RATIO_KEY == params.LOAN_MAX_DTI_RATIO_KEY
    assert rules.LOAN_MAX_AMOUNT_MINOR_KEY == params.LOAN_MAX_AMOUNT_MINOR_KEY
    assert rules.LOAN_MIN_SCORE_KEY == params.LOAN_MIN_SCORE_KEY
    assert rules.LOAN_REVIEW_MARGIN_KEY == params.LOAN_REVIEW_MARGIN_KEY
    assert rules.LOAN_MAX_DEBT_MINOR_KEY == params.LOAN_MAX_DEBT_MINOR_KEY
    assert rules.DEFAULT_MAX_DTI_RATIO == params.DEFAULT_LOAN_MAX_DTI_RATIO
    assert rules.DEFAULT_MAX_AMOUNT_MINOR == params.DEFAULT_LOAN_MAX_AMOUNT_MINOR
    assert rules.DEFAULT_MIN_SCORE == params.DEFAULT_LOAN_MIN_SCORE
    assert rules.DEFAULT_REVIEW_MARGIN == params.DEFAULT_LOAN_REVIEW_MARGIN
    assert rules.DEFAULT_MAX_DEBT_MINOR == params.DEFAULT_LOAN_MAX_DEBT_MINOR


def test_rules_module_is_pure():
    """`rules.py` no toca BD/red: sin imports de persistencia."""
    content = RULES_PATH.read_text(encoding="utf-8")
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "sqlalchemy" not in stripped, f"frontera rota: {stripped}"
            assert "app.modules.shared" not in stripped, f"frontera rota: {stripped}"
            assert "app.adapters" not in stripped, f"frontera rota: {stripped}"
    assert "config.parameters" in content  # documenta al dueno sin importarlo


def test_migration_0030_chains_after_0029():
    assert MIGRATION_PATH.exists(), "falta migracion 0030_credits_loan_rule_params.py"
    content = MIGRATION_PATH.read_text(encoding="utf-8")
    for token in (
        'revision = "0030_credits_loan_rule_params"',
        'down_revision = "0029_credits_loan_products"',
        '"loan.max_amount_minor"',
        '"loan.min_score"',
        '"loan.review_margin"',
        '"loan.max_debt_minor"',
        "ON CONFLICT (key) DO NOTHING",
        "def upgrade",
        "def downgrade",
    ):
        assert token in content, f"migracion sin {token}"
    for other in (
        "credits.loan_products",
        "loan_applications",
        "CREATE TABLE",
        "op.create_table",
        "op.drop_table",
        '"transactions"',
        '"ledger"',
    ):
        assert other not in content, f"la migracion no debe tocar otro objeto: {other}"


# --------------------------------------------------------- Parte B: SQLite
@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schema `config` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS config")
        cur.close()

    event.listen(engine, "connect", _attach)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE config.parameters (id TEXT PRIMARY KEY, "
                "key TEXT NOT NULL UNIQUE, value_json TEXT NOT NULL, "
                "module TEXT NOT NULL, description TEXT, updated_by TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
        )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_param(session: Session, key: str, value: str, module: str = "credits") -> None:
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, :module)"
        ),
        {"id": uuid.uuid4().hex, "key": key, "value": value, "module": module},
    )
    session.flush()


def _seed_loan_defaults(session: Session) -> None:
    from app.modules.shared.config import parameters as params

    _seed_param(session, params.LOAN_MAX_DTI_RATIO_KEY, "0.4")
    _seed_param(session, params.LOAN_MAX_AMOUNT_MINOR_KEY, "5000000")
    _seed_param(session, params.LOAN_MIN_SCORE_KEY, "650")
    _seed_param(session, params.LOAN_REVIEW_MARGIN_KEY, "50")
    _seed_param(session, params.LOAN_MAX_DEBT_MINOR_KEY, "10000000")


def test_loan_getters_read_seeds(sqlite_session: Session):
    from app.modules.shared.config import parameters as params

    _seed_loan_defaults(sqlite_session)
    assert params.get_loan_max_dti_ratio(sqlite_session) == Decimal("0.4")
    assert params.get_loan_max_amount_minor(sqlite_session) == 5_000_000
    assert params.get_loan_min_score(sqlite_session) == 650
    assert params.get_loan_review_margin(sqlite_session) == Decimal(50)
    assert params.get_loan_max_debt_minor(sqlite_session) == 10_000_000


def test_string_seed_form_read_as_decimal(sqlite_session: Session):
    """La forma texto `"0.4"` de `0001` se lee como `Decimal("0.4")`."""
    from app.modules.shared.config import parameters as params

    _seed_param(sqlite_session, params.LOAN_MAX_DTI_RATIO_KEY, '"0.4"')
    value = params.get_loan_max_dti_ratio(sqlite_session)
    assert value == Decimal("0.4")
    assert isinstance(value, Decimal)


def test_missing_loan_keys_fall_back(sqlite_session: Session):
    from app.modules.shared.config import parameters as params

    assert params.get_loan_max_dti_ratio(sqlite_session) == Decimal("0.4")
    assert params.get_loan_max_amount_minor(sqlite_session) == 5_000_000
    assert params.get_loan_min_score(sqlite_session) == 650
    assert params.get_loan_review_margin(sqlite_session) == Decimal(50)
    assert params.get_loan_max_debt_minor(sqlite_session) == 10_000_000


def test_invalid_stored_loan_values_fall_back(sqlite_session: Session):
    from app.modules.shared.config import parameters as params

    _seed_param(sqlite_session, params.LOAN_MAX_DTI_RATIO_KEY, '"alto"')
    _seed_param(sqlite_session, params.LOAN_MIN_SCORE_KEY, '"-3"')
    assert params.get_loan_max_dti_ratio(sqlite_session) == Decimal("0.4")
    assert params.get_loan_min_score(sqlite_session) == 650


def test_set_decimal_parameter_roundtrip(sqlite_session: Session):
    from app.modules.shared.config import parameters as params

    _seed_param(sqlite_session, params.LOAN_MAX_DTI_RATIO_KEY, "0.4")
    _seed_param(sqlite_session, params.LOAN_REVIEW_MARGIN_KEY, "50")
    stored = params.set_parameter(
        sqlite_session, key=params.LOAN_MAX_DTI_RATIO_KEY, value=Decimal("0.35")
    )
    assert stored == Decimal("0.35")
    assert params.get_loan_max_dti_ratio(sqlite_session) == Decimal("0.35")
    assert params.set_parameter(sqlite_session, key=params.LOAN_REVIEW_MARGIN_KEY, value="40") == (
        Decimal(40)
    )
    assert params.get_loan_review_margin(sqlite_session) == Decimal(40)
    assert params.set_parameter(sqlite_session, key=params.LOAN_MAX_DTI_RATIO_KEY, value=1) == (
        Decimal(1)
    )


def test_set_parameter_rejects_bad_decimal(sqlite_session: Session):
    from app.modules.shared.config import parameters as params

    _seed_param(sqlite_session, params.LOAN_MAX_DTI_RATIO_KEY, "0.4")
    for bad in (0.4, True, "alto", "-0.1", float("nan")):
        with pytest.raises(params.ParameterValidationError):
            params.set_parameter(sqlite_session, key=params.LOAN_MAX_DTI_RATIO_KEY, value=bad)


def test_set_parameter_int_keys_unchanged(sqlite_session: Session):
    """Compatibilidad E2-T18: las claves enteras siguen exigiendo `int`."""
    from app.modules.shared.config import parameters as params

    _seed_param(sqlite_session, params.LOAN_MIN_SCORE_KEY, "650")
    assert params.set_parameter(sqlite_session, key=params.LOAN_MIN_SCORE_KEY, value=700) == 700
    assert params.get_loan_min_score(sqlite_session) == 700
    for bad in (Decimal(700), "700", 700.0, True, -1):
        with pytest.raises(params.ParameterValidationError):
            params.set_parameter(sqlite_session, key=params.LOAN_MIN_SCORE_KEY, value=bad)
    with pytest.raises(params.ParameterNotFoundError):
        params.set_parameter(sqlite_session, key="loan.no_existe", value=1)


def test_profile_amount_key_helpers(sqlite_session: Session):
    from app.modules.shared.config import parameters as params

    assert params.loan_amount_key_for_profile("standard") == "loan.max_amount_STANDARD_minor"
    assert params.loan_amount_key_for_profile("  standard  ") == "loan.max_amount_STANDARD_minor"
    for bad in (None, "", "   ", "standard; DROP", 123):
        assert params.loan_amount_key_for_profile(bad) is None, bad
    _seed_loan_defaults(sqlite_session)
    _seed_param(sqlite_session, "loan.max_amount_STANDARD_minor", "500000")
    assert params.get_loan_max_amount_minor(sqlite_session, "STANDARD") == 500_000
    assert params.get_loan_max_amount_minor(sqlite_session, "VIP") == 5_000_000


def test_facade_hot_change_without_restart(sqlite_session: Session):
    """La fachada refleja un cambio de parametro sin reiniciar."""
    from app.modules.credits.service import evaluation

    _seed_loan_defaults(sqlite_session)
    assert evaluation.evaluate_application(sqlite_session, **_base()).decision == APPROVED
    from app.modules.shared.config import parameters as params

    params.set_parameter(sqlite_session, key=params.LOAN_MAX_DTI_RATIO_KEY, value=Decimal("0.10"))
    changed = evaluation.evaluate_application(sqlite_session, **_base())
    assert changed.decision == REJECTED
    assert "DTI" in changed.reason


def test_facade_params_override(sqlite_session: Session):
    from app.modules.credits.service import evaluation

    _seed_loan_defaults(sqlite_session)
    preview = evaluation.evaluate_application(
        sqlite_session, **_base(params_override={"loan.min_score": 800})
    )
    assert preview.decision == REJECTED
    assert "score" in preview.reason
    # La BD no cambio: sin override sigue aprobando.
    assert evaluation.evaluate_application(sqlite_session, **_base()).decision == APPROVED


# ------------------------------------------------------ Parte C: Postgres
def test_pg_loan_keys_seeded_and_dti_is_numeric(db_session: Session):
    """`0030` siembra las claves y el DTI queda como numero JSON."""
    keys = {
        row[0]
        for row in db_session.execute(
            sa.text("SELECT key FROM config.parameters WHERE key LIKE 'loan.%'")
        )
    }
    for key in (
        "loan.max_dti_ratio",
        "loan.max_amount_minor",
        "loan.min_score",
        "loan.review_margin",
        "loan.max_debt_minor",
    ):
        assert key in keys, f"falta semilla {key}"
    kind = db_session.execute(
        sa.text(
            "SELECT jsonb_typeof(value_json) FROM config.parameters WHERE key = 'loan.max_dti_ratio'"
        )
    ).scalar()
    assert kind == "number"


def test_pg_decimal_roundtrip_and_facade(db_session: Session):
    from decimal import Decimal as _Decimal

    from app.modules.credits.service import evaluation
    from app.modules.shared.config import parameters as params

    assert evaluation.evaluate_application(db_session, **_base()).decision == APPROVED
    params.set_parameter(db_session, key=params.LOAN_MAX_DTI_RATIO_KEY, value=_Decimal("0.10"))
    db_session.flush()
    assert params.get_loan_max_dti_ratio(db_session) == _Decimal("0.10")
    changed = evaluation.evaluate_application(db_session, **_base())
    assert changed.decision == REJECTED
