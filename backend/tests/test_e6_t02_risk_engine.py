"""Motor antifraude puro `risk.domain.engine` (E6-T02 corte 1, HU20).

Cubre el brief `docs/tasks/E6-T02.md#criterios-de-aceptacion` (corte 1):

- Cada regla por separado (camino feliz de match y de no-match).
- Combinacion de reglas (accion maxima + `matched_rules` + `score`).
- Prioridad/severidad (`BLOCK > HOLD > ALERT > ALLOW`, independiente del
  orden de entrada y de `priority`).
- `enabled=false` no participa.
- Caso feliz `ALLOW` (sin matches: `score=0`, `matched_rules=()`).
- `score` sumado y recortado a `0..100`.
- Determinismo (mismo resultado ante orden de entrada distinto).
- Sin PII en `reason` (solo `code`s) y sin E/S (puro, sin BD).

Puro stdlib: sin fixtures de BD ni HTTP (no mueve dinero).
"""

from __future__ import annotations

from types import SimpleNamespace

from app.modules.risk.domain import engine
from app.modules.risk.domain.engine import RiskDecision, evaluate


def _rule(code, action="ALERT", enabled=True, priority=100, params=None):
    """Regla con forma `RiskRule` (el motor tambien acepta dicts/ORM)."""
    return SimpleNamespace(
        code=code,
        name=code,
        enabled=enabled,
        priority=priority,
        action=action,
        params_json=dict(params or {}),
    )


def _profile(**overrides):
    base = {
        "avg_amount_minor": 50000,
        "usual_countries": ["PE"],
        "usual_hours": [9, 10, 11],
        "last_device_id": "dev-known",
        "score": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------- caso feliz ALLOW
def test_allow_when_no_rules_match():
    decision = evaluate(
        [
            _rule("FACIAL_SPOOFING", params={"failed_attempts_threshold": 3}),
            _rule("ATYPICAL_GEO"),
            _rule("UNUSUAL_AMOUNT", params={"factor": 3.0}),
            _rule("VELOCITY", params={"max_operations": 10}),
            _rule("NEW_DEVICE"),
        ],
        _profile(),
        {
            "country": "PE",
            "amount_minor": 10000,
            "device_id": "dev-known",
            "operations_in_window": 1,
            "auth_failed_attempts": 0,
            "auth_blocked": False,
        },
    )
    assert isinstance(decision, RiskDecision)
    assert decision.action == "ALLOW"
    assert decision.score == 0
    assert decision.matched_rules == ()
    assert decision.reason == "no rules matched"


def test_allow_with_empty_rules_and_missing_data():
    assert evaluate([], _profile(), {"country": "PE"}).action == "ALLOW"
    assert evaluate(None, None, None).action == "ALLOW"
    assert evaluate([_rule("ATYPICAL_GEO")], None, {}).action == "ALLOW"


# ------------------------------------------------------- FACIAL_SPOOFING
def test_facial_spoofing_matches_on_blocked():
    decision = evaluate(
        [_rule("FACIAL_SPOOFING", params={"block_on_lock": True})],
        _profile(),
        {"auth_blocked": True, "auth_fail_reason": "blocked"},
    )
    assert decision.action == "ALERT"
    assert decision.matched_rules == ("FACIAL_SPOOFING",)
    assert decision.score == 25


def test_facial_spoofing_matches_on_attempts_threshold():
    decision = evaluate(
        [_rule("FACIAL_SPOOFING", params={"failed_attempts_threshold": 3})],
        _profile(),
        {"auth_failed_attempts": 3, "auth_fail_reason": "invalid"},
    )
    assert decision.matched_rules == ("FACIAL_SPOOFING",)


def test_facial_spoofing_matches_on_spoofing_reason():
    decision = evaluate(
        [_rule("FACIAL_SPOOFING")],
        _profile(),
        {"auth_failed_attempts": 1, "auth_fail_reason": "liveness_failed"},
    )
    assert decision.matched_rules == ("FACIAL_SPOOFING",)


def test_facial_spoofing_ignores_generic_single_failure():
    decision = evaluate(
        [_rule("FACIAL_SPOOFING", params={"failed_attempts_threshold": 3})],
        _profile(),
        {"auth_failed_attempts": 1, "auth_fail_reason": "invalid"},
    )
    assert decision.action == "ALLOW"
    assert decision.matched_rules == ()


def test_facial_spoofing_respects_block_on_lock_false():
    decision = evaluate(
        [_rule("FACIAL_SPOOFING", params={"block_on_lock": False})],
        _profile(),
        {"auth_blocked": True, "auth_failed_attempts": 0},
    )
    assert decision.action == "ALLOW"


# ---------------------------------------------------------- ATYPICAL_GEO
def test_atypical_geo_matches_foreign_country():
    decision = evaluate(
        [_rule("ATYPICAL_GEO")],
        _profile(usual_countries=["PE"]),
        {"country": "CO"},
    )
    assert decision.matched_rules == ("ATYPICAL_GEO",)
    assert decision.score == 10


def test_atypical_geo_case_insensitive_no_match():
    decision = evaluate(
        [_rule("ATYPICAL_GEO")],
        _profile(usual_countries=["pe"]),
        {"country": " PE "},
    )
    assert decision.action == "ALLOW"


def test_atypical_geo_no_match_without_habit_or_country():
    assert (
        evaluate([_rule("ATYPICAL_GEO")], _profile(usual_countries=[]), {"country": "CO"}).action
        == "ALLOW"
    )
    assert evaluate([_rule("ATYPICAL_GEO")], _profile(), {}).action == "ALLOW"
    assert evaluate([_rule("ATYPICAL_GEO")], None, {"country": "CO"}).action == "ALLOW"


# -------------------------------------------------------- UNUSUAL_AMOUNT
def test_unusual_amount_matches_above_factor():
    decision = evaluate(
        [_rule("UNUSUAL_AMOUNT", params={"factor": 3.0})],
        _profile(avg_amount_minor=10000),
        {"amount_minor": 30001},
    )
    assert decision.matched_rules == ("UNUSUAL_AMOUNT",)
    assert decision.score == 15


def test_unusual_amount_boundary_does_not_match():
    decision = evaluate(
        [_rule("UNUSUAL_AMOUNT", params={"factor": 3.0})],
        _profile(avg_amount_minor=10000),
        {"amount_minor": 30000},
    )
    assert decision.action == "ALLOW", "el umbral es estricto: `>` no `>=`"


def test_unusual_amount_respects_min_floor():
    decision = evaluate(
        [_rule("UNUSUAL_AMOUNT", params={"factor": 2.0, "min_amount_minor": 10000})],
        _profile(avg_amount_minor=1000),
        {"amount_minor": 3000},
    )
    assert decision.action == "ALLOW", "piso `min_amount_minor` evita micro-montos"


def test_unusual_amount_no_baseline_no_match():
    assert (
        evaluate(
            [_rule("UNUSUAL_AMOUNT")], _profile(avg_amount_minor=0), {"amount_minor": 999999}
        ).action
        == "ALLOW"
    )
    assert evaluate([_rule("UNUSUAL_AMOUNT")], None, {"amount_minor": 999999}).action == "ALLOW"


def test_unusual_amount_factor_from_context():
    decision = evaluate(
        [_rule("UNUSUAL_AMOUNT", params={})],
        _profile(avg_amount_minor=10000),
        {"amount_minor": 25000, "unusual_amount_factor": 2.0},
    )
    assert decision.matched_rules == ("UNUSUAL_AMOUNT",)


# -------------------------------------------------------------- VELOCITY
def test_velocity_matches_at_threshold():
    decision = evaluate(
        [_rule("VELOCITY", params={"max_operations": 10})],
        _profile(),
        {"operations_in_window": 10},
    )
    assert decision.matched_rules == ("VELOCITY",)
    assert decision.score == 20


def test_velocity_below_threshold_no_match():
    assert (
        evaluate(
            [_rule("VELOCITY", params={"max_operations": 10})],
            _profile(),
            {"operations_in_window": 9},
        ).action
        == "ALLOW"
    )


def test_velocity_accepts_timestamp_list():
    decision = evaluate(
        [_rule("VELOCITY", params={"max_operations": 3})],
        _profile(),
        {"recent_timestamps": ["t1", "t2", "t3"]},
    )
    assert decision.matched_rules == ("VELOCITY",)


# ------------------------------------------------------------ NEW_DEVICE
def test_new_device_matches_unknown_device():
    decision = evaluate(
        [_rule("NEW_DEVICE")],
        _profile(last_device_id="dev-known"),
        {"device_id": "dev-other"},
    )
    assert decision.matched_rules == ("NEW_DEVICE",)
    assert decision.score == 5


def test_new_device_same_or_missing_no_match():
    assert (
        evaluate(
            [_rule("NEW_DEVICE")], _profile(last_device_id="dev-known"), {"device_id": "dev-known"}
        ).action
        == "ALLOW"
    )
    assert (
        evaluate(
            [_rule("NEW_DEVICE")], _profile(last_device_id=None), {"device_id": "dev-other"}
        ).action
        == "ALLOW"
    )
    assert evaluate([_rule("NEW_DEVICE")], _profile(), {}).action == "ALLOW"


# ---------------------------------------------- combinacion y severidad
def test_combination_reports_max_severity_and_sums_score():
    decision = evaluate(
        [
            _rule("ATYPICAL_GEO", action="ALERT", priority=20),
            _rule("NEW_DEVICE", action="HOLD", priority=50),
            _rule("VELOCITY", action="BLOCK", priority=40, params={"max_operations": 5}),
        ],
        _profile(usual_countries=["PE"], last_device_id="dev-known"),
        {"country": "CO", "device_id": "dev-other", "operations_in_window": 7},
    )
    assert decision.action == "BLOCK"
    assert decision.matched_rules == ("ATYPICAL_GEO", "VELOCITY", "NEW_DEVICE")
    assert decision.score == 10 + 20 + 5
    assert decision.reason == "matched 3 rule(s): ATYPICAL_GEO, VELOCITY, NEW_DEVICE"


def test_severity_order_block_over_hold_over_alert():
    base_ctx = {
        "country": "CO",
        "device_id": "dev-other",
        "operations_in_window": 99,
    }
    profile = _profile(usual_countries=["PE"], last_device_id="dev-known")
    rules = [
        _rule("ATYPICAL_GEO", action="ALERT", priority=10),
        _rule("NEW_DEVICE", action="HOLD", priority=20),
        _rule("VELOCITY", action="BLOCK", priority=30, params={"max_operations": 2}),
    ]
    assert evaluate(rules, profile, base_ctx).action == "BLOCK"
    hold_rules = [
        _rule("ATYPICAL_GEO", action="ALERT", priority=30),
        _rule("NEW_DEVICE", action="HOLD", priority=10),
    ]
    assert evaluate(hold_rules, profile, base_ctx).action == "HOLD"
    alert_rules = [_rule("ATYPICAL_GEO", action="ALERT"), _rule("NEW_DEVICE", action="ALERT")]
    assert evaluate(alert_rules, profile, base_ctx).action == "ALERT"


def test_severity_ignores_priority_order():
    """`priority` ordena la evaluacion; la `action` maxima gana igual."""
    decision = evaluate(
        [
            _rule("VELOCITY", action="ALERT", priority=99, params={"max_operations": 1}),
            _rule("NEW_DEVICE", action="BLOCK", priority=1),
        ],
        _profile(last_device_id="dev-known"),
        {"device_id": "dev-other", "operations_in_window": 5},
    )
    assert decision.action == "BLOCK"
    assert decision.matched_rules == ("NEW_DEVICE", "VELOCITY")


def test_unknown_action_degrades_to_alert():
    decision = evaluate(
        [_rule("VELOCITY", action="FREEZE", params={"max_operations": 1})],
        _profile(),
        {"operations_in_window": 5},
    )
    assert decision.action == "ALERT"


def test_unknown_rule_code_ignored():
    decision = evaluate(
        [_rule("NO_SUCH_RULE", action="BLOCK")],
        _profile(),
        {"country": "CO", "amount_minor": 1},
    )
    assert decision.action == "ALLOW"
    assert decision.matched_rules == ()


# ------------------------------------------------------- enabled / score
def test_enabled_false_does_not_participate():
    decision = evaluate(
        [
            _rule("VELOCITY", action="BLOCK", enabled=False, params={"max_operations": 1}),
            _rule("NEW_DEVICE", action="ALERT", enabled=False),
        ],
        _profile(last_device_id="dev-known"),
        {"device_id": "dev-other", "operations_in_window": 99},
    )
    assert decision.action == "ALLOW"
    assert decision.matched_rules == ()
    assert decision.score == 0


def test_disabled_rule_does_not_shadow_enabled_one():
    decision = evaluate(
        [
            _rule("ATYPICAL_GEO", action="BLOCK", enabled=False),
            _rule("NEW_DEVICE", action="ALERT", enabled=True),
        ],
        _profile(usual_countries=["PE"], last_device_id="dev-known"),
        {"country": "CO", "device_id": "dev-other"},
    )
    assert decision.action == "ALERT"
    assert decision.matched_rules == ("NEW_DEVICE",)


def test_score_sums_bumps_and_caps_at_100():
    decision = evaluate(
        [
            _rule("FACIAL_SPOOFING", params={"score_bump": 60}),
            _rule("VELOCITY", params={"score_bump": 60, "max_operations": 1}),
        ],
        _profile(),
        {"auth_blocked": True, "operations_in_window": 5},
    )
    assert decision.score == 100
    assert decision.matched_rules == ("FACIAL_SPOOFING", "VELOCITY")


def test_score_bump_override_from_params():
    decision = evaluate(
        [_rule("NEW_DEVICE", params={"score_bump": 42})],
        _profile(last_device_id="dev-known"),
        {"device_id": "dev-other"},
    )
    assert decision.score == 42


def test_accepts_dict_rules():
    decision = evaluate(
        [
            {
                "code": "new_device",
                "enabled": True,
                "priority": 50,
                "action": "alert",
                "params_json": {},
            }
        ],
        {"last_device_id": "dev-known"},
        {"device_id": "dev-other"},
    )
    assert decision.matched_rules == ("NEW_DEVICE",)
    assert decision.action == "ALERT"


# ----------------------------------------------- determinismo y pureza
def test_deterministic_regardless_of_input_order():
    rules_fwd = [
        _rule("ATYPICAL_GEO", priority=20),
        _rule("NEW_DEVICE", priority=50),
        _rule("VELOCITY", priority=40, params={"max_operations": 2}),
    ]
    rules_rev = list(reversed(rules_fwd))
    profile = _profile(usual_countries=["PE"], last_device_id="dev-known")
    context = {"country": "CO", "device_id": "dev-other", "operations_in_window": 3}
    first = evaluate(rules_fwd, profile, context)
    second = evaluate(rules_rev, profile, context)
    assert first == second
    assert first.matched_rules == ("ATYPICAL_GEO", "VELOCITY", "NEW_DEVICE")


def test_repeated_calls_are_stable():
    rules = [_rule("UNUSUAL_AMOUNT", params={"factor": 2.0})]
    profile = _profile(avg_amount_minor=1000)
    context = {"amount_minor": 5000}
    assert evaluate(rules, profile, context) == evaluate(rules, profile, context)


def test_reason_contains_no_pii():
    decision = evaluate(
        [
            _rule("ATYPICAL_GEO"),
            _rule("UNUSUAL_AMOUNT", params={"factor": 1.5}),
            _rule("NEW_DEVICE"),
        ],
        _profile(avg_amount_minor=1000, usual_countries=["PE"], last_device_id="dev-1"),
        {"country": "CO", "amount_minor": 5000, "device_id": "dev-2"},
    )
    assert decision.action == "ALERT"
    for secret in ("CO", "5000", "dev-2", "dev-1", "PE"):
        assert secret not in decision.reason, f"posible PII en reason: {secret}"
    assert set(
        decision.reason.replace("matched", "").replace("rule(s):", "").replace(",", " ").split()
    ) <= {
        "2",
        "3",
        "ATYPICAL_GEO",
        "UNUSUAL_AMOUNT",
        "NEW_DEVICE",
    }


def test_decision_serializes_without_pii():
    decision = evaluate([_rule("NEW_DEVICE")], _profile(last_device_id="a"), {"device_id": "b"})
    assert decision.to_dict() == {
        "action": "ALERT",
        "score": 5,
        "matched_rules": ["NEW_DEVICE"],
        "reason": "matched 1 rule(s): NEW_DEVICE",
    }


def test_engine_is_pure_without_io_or_money():
    """El motor no importa E/S, BD ni el motor de dinero (frontera)."""
    text = (engine.__file__ or "").replace("\\", "/")
    assert text.endswith("risk/domain/engine.py")
    import pathlib

    source = pathlib.Path(engine.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "app.modules.transactions",
        "app.modules.ledger",
        "app.modules.accounts",
        "app.modules.identity",
        "app.modules.wallet",
        "app.modules.credits",
        "sqlalchemy",
        "post_entry",
        "requests",
        "httpx",
        "open(",
    ):
        assert forbidden not in source, f"frontera rota en engine.py: {forbidden}"


def test_malformed_inputs_never_raise():
    assert evaluate("no-iterable-valido", _profile(), {"country": "CO"}).action == "ALLOW"
    assert evaluate([None, 42, {"sin": "code"}], _profile(), {"country": "CO"}).action == "ALLOW"
    assert (
        evaluate(
            [_rule("UNUSUAL_AMOUNT")],
            _profile(avg_amount_minor="mucho"),
            {"amount_minor": "poco"},
        ).action
        == "ALLOW"
    )
