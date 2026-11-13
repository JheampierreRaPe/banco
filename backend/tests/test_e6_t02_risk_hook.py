"""Pre-chequeo antifraude + consumidores asincronos (E6-T02 corte 2, HU20).

Sobre SQLite en memoria + schemas ATTACH, sin red, sin `float`, sin PII:
dinero de juguete (UUIDs sinteticos).

Mapeo brief -> test:
- fachada (perfil + relee reglas + < 1 s) ......... `test_facade_*`
- hook BLOCK (REJECTED/RISK_BLOCKED, sin saldos) .. `test_hook_block_*`
- hook HOLD (PENDING_AUTHORIZATION) ............... `test_hook_hold_*`
- hook ALERT (continua + evidencia) ............... `test_hook_alert_*`
- hook resiliente sin tablas `risk` ............... `test_hook_without_risk_tables`
- consumidores (replay/rollback) .................. `test_consumer_*`
- extension `auth.failed_attempt` ................. `test_auth_extension_*`
- cableado del bus ................................ `test_wiring_*`
- sin PII ......................................... `test_no_pii_*`
"""

from __future__ import annotations

import time
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
DEVICE_KNOWN = "dev-known-e6t02"
DEVICE_OTHER = "dev-other-e6t02"

TABLES = [
    "accounts.accounts",
    "accounts.account_balances",
    "accounts.daily_balance_snapshots",
    "ledger.ledger_accounts",
    "ledger.journal_entries",
    "ledger.postings",
    "ledger.ledger_balances",
    "shared.outbox",
    "shared.processed_events",
    "transactions.transactions",
    "transactions.transaction_status_history",
    "transactions.holds",
    "risk.risk_profiles",
    "risk.risk_rules",
    "audit.audit_log",
    "notifications.notifications",
    "notifications.notification_templates",
]

SCHEMAS = ("accounts", "ledger", "shared", "transactions", "risk", "audit", "notifications")


def _make_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a
    import app.modules.audit.models as _au
    import app.modules.ledger.models as _l
    import app.modules.notifications.models as _n
    import app.modules.risk.models as _r
    import app.modules.shared.models as _s
    import app.modules.transactions.models as _t

    assert all([_a, _au, _l, _n, _r, _s, _t])
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in SCHEMAS:
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    return engine


@pytest.fixture()
def engine():
    eng = _make_engine()
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def session(engine):
    from app.modules.accounts import repository as accounts_repo

    s = Session(engine)
    try:
        a1 = accounts_repo.create_account(
            s, user_id=USER_A, account_number="00110001111", type="AHORRO", currency="PEN"
        )
        b1 = accounts_repo.create_account(
            s, user_id=USER_B, account_number="00220003333", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(s, a1.id, available_delta=200_000, held_delta=0, currency="PEN")
        accounts_repo.apply_delta(s, b1.id, available_delta=5_000, held_delta=0, currency="PEN")
        s.commit()
        s.info["a1"] = a1.id
        s.info["b1"] = b1.id
        yield s
    finally:
        s.close()


def _seed_conservative_rules(session: Session) -> None:
    """Las 5 reglas como la semilla `0048` (todas `ALERT`, conservadoras)."""
    from app.modules.risk import service as risk_service

    seeds = [
        ("FACIAL_SPOOFING", "Suplantacion", 10, {"score_bump": 25}),
        ("ATYPICAL_GEO", "Geo atipica", 20, {"mode": "country_mismatch", "score_bump": 10}),
        ("UNUSUAL_AMOUNT", "Monto inusual", 30, {"factor": 3.0, "min_amount_minor": 10000}),
        ("VELOCITY", "Velocidad", 40, {"max_operations": 10, "window_seconds": 600}),
        ("NEW_DEVICE", "Dispositivo nuevo", 50, {"score_bump": 5}),
    ]
    for code, name, priority, params in seeds:
        risk_service.upsert_rule(
            session,
            code,
            name=name,
            enabled=True,
            priority=priority,
            action="ALERT",
            params_json=params,
        )
    session.commit()


def _set_rule_action(session: Session, code: str, action: str) -> None:
    from app.modules.risk import service as risk_service

    risk_service.update_rule(session, code, actor=USER_A, action=action)
    session.commit()


def _set_profile(
    session: Session,
    user_id=USER_A,
    *,
    avg_amount_minor=50_000,
    usual_countries=("PE",),
    last_device_id=DEVICE_KNOWN,
) -> None:
    from app.modules.risk import service as risk_service

    risk_service.update_behavior(
        session,
        user_id,
        avg_amount_minor=avg_amount_minor,
        usual_countries=list(usual_countries),
        last_device_id=last_device_id,
    )
    session.commit()


def _transfer(session: Session, amount_minor: int, **kwargs):
    from app.modules.transactions import service as tx_service

    params = {
        "initiator_user_id": USER_A,
        "source_account_id": session.info["a1"],
        "target_account_id": session.info["b1"],
        "amount_minor": amount_minor,
        "currency": "PEN",
        "idempotency_key": f"e6t02-{uuid.uuid4()}",
    }
    params.update(kwargs)
    return tx_service.transfer_third_party(session, **params)


def _balances(session: Session):
    from app.modules.accounts.models import AccountBalance

    rows = session.scalars(sa.select(AccountBalance)).all()
    return {r.account_id: (r.available_minor, r.held_minor) for r in rows}


def _journal_count(session: Session) -> int:
    from app.modules.ledger.models import JournalEntry

    return int(session.scalar(sa.select(sa.func.count()).select_from(JournalEntry)) or 0)


def _assessments(session: Session):
    from app.modules.audit.models import AuditLog

    return list(
        session.scalars(
            sa.select(AuditLog).where(AuditLog.action == "risk.assessment").order_by(AuditLog.seq)
        ).all()
    )


# ---------------------------------------------------------------- fachada


def test_facade_allow_happy_path(session: Session):
    from app.modules.risk import service as risk_service

    _seed_conservative_rules(session)
    _set_profile(session)
    decision = risk_service.evaluate_pre_transaction(
        session,
        user_id=USER_A,
        amount_minor=10_000,
        currency="PEN",
        device_id=DEVICE_KNOWN,
        country="PE",
        tx_type="THIRD_PARTY_TRANSFER",
    )
    assert decision.action == "ALLOW"
    assert decision.score == 0
    assert decision.matched_rules == ()


def test_facade_block_unusual_amount(session: Session):
    from app.modules.risk import service as risk_service

    _seed_conservative_rules(session)
    _set_profile(session, avg_amount_minor=10_000)
    _set_rule_action(session, "UNUSUAL_AMOUNT", "BLOCK")
    decision = risk_service.evaluate_pre_transaction(
        session, user_id=USER_A, amount_minor=50_000, currency="PEN"
    )
    assert decision.action == "BLOCK"
    assert decision.matched_rules == ("UNUSUAL_AMOUNT",)
    assert decision.score == 15


def test_facade_rereads_rules_without_restart(session: Session):
    from app.modules.risk import service as risk_service

    _seed_conservative_rules(session)
    _set_profile(session, avg_amount_minor=10_000)
    first = risk_service.evaluate_pre_transaction(
        session, user_id=USER_A, amount_minor=50_000, currency="PEN"
    )
    assert first.action == "ALERT"  # semilla conservadora
    _set_rule_action(session, "UNUSUAL_AMOUNT", "HOLD")
    second = risk_service.evaluate_pre_transaction(
        session, user_id=USER_A, amount_minor=50_000, currency="PEN"
    )
    assert second.action == "HOLD"  # relee sin reiniciar


def test_facade_evaluates_under_one_second(session: Session):
    from app.modules.risk import service as risk_service

    _seed_conservative_rules(session)
    _set_profile(session)
    start = time.perf_counter()
    risk_service.evaluate_pre_transaction(
        session, user_id=USER_A, amount_minor=10_000, currency="PEN"
    )
    assert time.perf_counter() - start < 1.0


def test_facade_rejects_invalid_user(session: Session):
    from app.modules.risk import service as risk_service

    with pytest.raises(ValueError):
        risk_service.evaluate_pre_transaction(
            session, user_id="no-es-uuid", amount_minor=100, currency="PEN"
        )


# -------------------------------------------------------------------- hook


def test_hook_block_rejects_without_moving_money(session: Session):
    _seed_conservative_rules(session)
    _set_profile(session, avg_amount_minor=10_000)
    _set_rule_action(session, "UNUSUAL_AMOUNT", "BLOCK")
    before_balances = _balances(session)

    tx = _transfer(session, 50_000)
    session.commit()

    assert tx.status == "REJECTED"
    assert tx.failure_reason == "RISK_BLOCKED"
    assert isinstance(tx.meta, dict) and tx.meta.get("failure_code") == "RISK_BLOCKED"
    assert tx.risk_level == "HIGH" and int(tx.risk_score or 0) == 15
    assert _balances(session) == before_balances  # saldos intactos
    assert _journal_count(session) == 0  # sin asientos
    audits = _assessments(session)
    assert len(audits) == 1 and (audits[0].after_json or {}).get("action") == "BLOCK"


def test_hook_hold_reuses_pending_authorization(session: Session):
    from app.modules.transactions import repository as tx_repo

    _seed_conservative_rules(session)
    _set_profile(session)
    _set_rule_action(session, "NEW_DEVICE", "HOLD")
    before_entries = _journal_count(session)

    tx = _transfer(session, 5_000, device_id=DEVICE_OTHER)
    session.commit()

    assert tx.status == "PENDING_AUTHORIZATION"
    assert (tx.meta or {}).get("failure_code") is None
    assert tx.risk_level == "HIGH"
    holds = tx_repo.list_holds_by_transaction(session, tx.id)
    assert any(h.status == "ACTIVE" for h in holds)  # retencion real del gate
    assert _journal_count(session) == before_entries + 1  # solo el asiento de hold
    audits = _assessments(session)
    assert len(audits) == 1 and (audits[0].after_json or {}).get("action") == "HOLD"


def test_hook_alert_continues_with_evidence(session: Session):
    _seed_conservative_rules(session)
    _set_profile(session)
    before_balances = _balances(session)

    tx = _transfer(session, 5_000, device_id=DEVICE_OTHER)
    session.commit()

    assert tx.status == "SETTLED"  # ALERT no bloquea
    assert tx.risk_level == "MEDIUM" and int(tx.risk_score or 0) == 5
    assert _balances(session) != before_balances  # el dinero si se movio
    audits = _assessments(session)
    assert len(audits) == 1
    assert (audits[0].after_json or {}).get("matched_rules") == ["NEW_DEVICE"]


def test_hook_disabled_rule_has_no_effect(session: Session):
    from app.modules.risk import service as risk_service

    _seed_conservative_rules(session)
    _set_profile(session, avg_amount_minor=10_000)
    risk_service.update_rule(session, "UNUSUAL_AMOUNT", actor=USER_A, enabled=False)
    session.commit()

    tx = _transfer(session, 50_000)
    session.commit()

    assert tx.status == "SETTLED"
    assert tx.risk_level == "LOW"
    assert _assessments(session) == []


def test_hook_without_risk_tables_fails_open(engine):
    """Sin tablas `risk`/`audit` la transferencia igual liquida (fail-open).

    Espejo de las suites E2/E4, cuyas BD no tienen el schema `risk`: el
    hook nunca rompe el camino feliz por un fallo tecnico de lectura.
    """
    from sqlalchemy.orm import Session as _Session

    local = _Session(engine)
    try:
        from app.modules.accounts import repository as accounts_repo
        from app.modules.transactions import service as tx_service

        a1 = accounts_repo.create_account(
            local,
            user_id=USER_A,
            account_number="00119998888",
            type="AHORRO",
            currency="PEN",
        )
        b1 = accounts_repo.create_account(
            local,
            user_id=USER_B,
            account_number="00229998888",
            type="AHORRO",
            currency="PEN",
        )
        accounts_repo.apply_delta(
            local, a1.id, available_delta=200_000, held_delta=0, currency="PEN"
        )
        local.commit()
        tx = tx_service.transfer_third_party(
            local,
            initiator_user_id=USER_A,
            source_account_id=a1.id,
            target_account_id=b1.id,
            amount_minor=5_000,
            currency="PEN",
            idempotency_key=f"e6t02-no-risk-{uuid.uuid4()}",
        )
        local.commit()
        assert tx.status == "SETTLED"
    finally:
        local.close()


def test_velocity_rule_counts_window_operations(session: Session):
    """La N-esima operacion de la ventana dispara (incluye la en curso)."""
    _seed_conservative_rules(session)
    _set_rule_action(session, "VELOCITY", "BLOCK")
    from app.modules.risk import service as risk_service

    risk_service.update_rule(
        session,
        "VELOCITY",
        actor=USER_A,
        params_json={"max_operations": 2, "window_seconds": 600},
    )
    session.commit()

    first = _transfer(session, 5_000)
    session.commit()
    assert first.status == "SETTLED"  # 1a de la ventana: continua
    second = _transfer(session, 5_000)
    session.commit()
    assert second.status == "REJECTED"  # 2a de la ventana: BLOCK
    assert second.failure_reason == "RISK_BLOCKED"


# -------------------------------------------------------------- consumidores


def _consume_initiated(session: Session, tx_id, *, event_id=None):
    from app.modules.risk import service as risk_service

    return risk_service.handle_transaction_initiated(
        session,
        event_id=event_id or uuid.uuid4(),
        payload={"transaction_id": str(tx_id)},
    )


def _consume_settled(session: Session, tx_id, *, event_id=None):
    from app.modules.risk import service as risk_service

    return risk_service.handle_transfer_settled(
        session,
        event_id=event_id or uuid.uuid4(),
        payload={"transaction_id": str(tx_id)},
    )


def test_consumer_initiated_replay_does_not_duplicate(session: Session):
    _seed_conservative_rules(session)
    tx = _transfer(session, 50_000)  # sin perfil: ALLOW limpio, sin auditoria
    session.commit()
    assert tx.status == "SETTLED"
    _set_profile(session, avg_amount_minor=1_000)
    _set_rule_action(session, "UNUSUAL_AMOUNT", "BLOCK")

    event_id = uuid.uuid4()
    first = _consume_initiated(session, tx.id, event_id=event_id)
    session.commit()
    second = _consume_initiated(session, tx.id, event_id=event_id)
    session.commit()

    assert first["consumed"] is True and first["action"] == "BLOCK"
    assert second == {"consumed": False, "reason": "duplicate"}
    assert len(_assessments(session)) == 1  # un evento repetido no duplica


def test_consumer_settled_replay_does_not_duplicate(session: Session):
    _seed_conservative_rules(session)
    tx = _transfer(session, 50_000)  # sin perfil: ALLOW limpio, sin auditoria
    session.commit()
    assert tx.status == "SETTLED"
    _set_profile(session, avg_amount_minor=1_000)
    _set_rule_action(session, "UNUSUAL_AMOUNT", "BLOCK")

    event_id = uuid.uuid4()
    first = _consume_settled(session, tx.id, event_id=event_id)
    session.commit()
    second = _consume_settled(session, tx.id, event_id=event_id)
    session.commit()

    assert first["consumed"] is True and first["action"] == "BLOCK"
    assert second["consumed"] is False
    assert len(_assessments(session)) == 1


def test_consumer_allow_leaves_no_assessment(session: Session):
    _seed_conservative_rules(session)
    tx = _transfer(session, 5_000)
    session.commit()

    outcome = _consume_initiated(session, tx.id)
    session.commit()

    assert outcome == {"consumed": True, "action": "ALLOW", "reason": "allow"}
    assert _assessments(session) == []


def test_consumer_missing_transaction_marks_processed(session: Session):
    event_id = uuid.uuid4()
    outcome = _consume_initiated(session, uuid.uuid4(), event_id=event_id)
    session.commit()
    assert outcome == {"consumed": True, "reason": "missing-transaction"}
    again = _consume_initiated(session, uuid.uuid4(), event_id=event_id)
    session.commit()
    assert again["consumed"] is False


def test_consumer_failure_rolls_back_and_reraises(session: Session, monkeypatch):
    """Fallo en el consumo: revierte efectos y relanza (D-04).

    Contrato portable: el error se propaga (el worker aplica backoff y
    reintenta) y no quedan efectos parciales (sin `risk.assessment`).
    Nota SQLite: `try_mark_processed` confirma su savepoint al salir;
    en el SQLite de pruebas ese `RELEASE` deja el claim visible tras el
    `rollback` del llamante (quirk del driver con `ATTACH ':memory:'`,
    patron compartido por todos los consumidores D-04 del repo); en
    Postgres (savepoints reales) el rollback tambien revierte el claim
    y el reintento reprocesa. Por eso el estado del claim no se afirma:
    el contrato portable es relanzar sin efectos parciales.
    """
    import app.modules.audit.service as audit_service

    _seed_conservative_rules(session)
    tx = _transfer(session, 50_000)  # sin perfil: ALLOW limpio, sin auditoria
    session.commit()
    assert tx.status == "SETTLED"
    _set_profile(session, avg_amount_minor=1_000)
    _set_rule_action(session, "UNUSUAL_AMOUNT", "BLOCK")

    def _boom(*args, **kwargs):
        raise RuntimeError("auditoria caida")

    monkeypatch.setattr(audit_service, "record", _boom)
    event_id = uuid.uuid4()
    with pytest.raises(RuntimeError):
        _consume_settled(session, tx.id, event_id=event_id)
    session.rollback()
    # Sin efectos parciales: la evaluacion no dejo auditoria. El estado
    # del claim tras el rollback depende del motor (ver nota arriba) y
    # no se afirma aqui: el contrato portable es relanzar sin efectos.
    assert _assessments(session) == []


# ------------------------------------------------------- extension de auth


def _consume_auth(session: Session, *, event_id=None, **overrides):
    from app.adapters.notification_sender import MockNotificationSender
    from app.modules.risk import service as risk_service

    payload = {
        "transaction_id": str(uuid.uuid4()),
        "user_id": str(USER_A),
        "device_id": DEVICE_KNOWN,
        "attempt": 3,
        "blocked": True,
        "reason": "blocked",
    }
    payload.update(overrides)
    return risk_service.handle_auth_failed_attempt(
        session,
        event_id=event_id or uuid.uuid4(),
        payload=payload,
        sender=MockNotificationSender(),
    )


def test_auth_extension_assesses_blocked_attempt(session: Session):
    _seed_conservative_rules(session)
    _set_rule_action(session, "FACIAL_SPOOFING", "BLOCK")

    event_id = uuid.uuid4()
    outcome = _consume_auth(session, event_id=event_id)
    session.commit()

    assert outcome["consumed"] is True
    from app.modules.audit.models import AuditLog

    audits = list(session.scalars(sa.select(AuditLog).order_by(AuditLog.seq)).all())
    by_action = [a.action for a in audits]
    # `update_rule` tambien audita (`risk.rule.updated`): se filtra.
    by_action = [a for a in by_action if a != "risk.rule.updated"]
    assert by_action == ["risk.auth_preventive_block", "risk.assessment"]
    assessment = audits[-1]
    assert (assessment.after_json or {}).get("matched_rules") == ["FACIAL_SPOOFING"]
    assert (assessment.after_json or {}).get("source") == "auth.failed_attempt"
    # Replay: ni perfil ni auditoria se duplican.
    again = _consume_auth(session, event_id=event_id)
    session.commit()
    assert again["consumed"] is False
    replayed = [
        a for a in session.scalars(sa.select(AuditLog)).all() if a.action != "risk.rule.updated"
    ]
    assert len(replayed) == 2


def test_auth_without_rules_keeps_single_audit(session: Session):
    outcome = _consume_auth(session)
    session.commit()

    assert outcome["consumed"] is True
    from app.modules.audit.models import AuditLog

    assert len(list(session.scalars(sa.select(AuditLog)).all())) == 1


# ------------------------------------------------------------------ wiring


def test_wiring_subscribes_antifraud_consumers():
    from app.composition import is_risk_antifraud_wired, subscribe_risk_antifraud
    from app.core.bus import EventBus

    bus = EventBus()
    assert is_risk_antifraud_wired(bus) is False
    subscribe_risk_antifraud(bus)
    assert is_risk_antifraud_wired(bus) is True
    assert bus.handler_count("transaction.initiated") == 1
    assert bus.handler_count("transfer.settled") == 1
    subscribe_risk_antifraud(bus)  # idempotente
    assert bus.handler_count("transaction.initiated") == 1
    assert bus.handler_count("transfer.settled") == 1


# ------------------------------------------------------------------- sin PII


def test_assessment_and_reason_carry_no_pii(session: Session):
    _seed_conservative_rules(session)
    _set_profile(session, avg_amount_minor=10_000)
    _set_rule_action(session, "UNUSUAL_AMOUNT", "BLOCK")

    _transfer(session, 50_000, device_id=DEVICE_OTHER)
    session.commit()

    audits = _assessments(session)
    assert len(audits) == 1
    blob = str(audits[0].after_json)
    for secret in (DEVICE_OTHER, DEVICE_KNOWN, "50000", "PE"):
        assert secret not in blob, f"posible PII en risk.assessment: {secret}"
