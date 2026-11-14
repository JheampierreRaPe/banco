"""Perfil de comportamiento alimentado por historial (E6-T03, HU20, fixes PR #26).

Sobre SQLite en memoria + schemas ATTACH, sin red, sin `float`, sin PII:
dinero de juguete (UUIDs sinteticos).

Mapeo brief -> test:
- dominio (media incremental, redondeo, horas, topes, `0` no diluye) `test_domain_*`
- consumidores (solo `settled` exitoso, replay, reintento, rollback) .. `test_feed_*`
- ventana real por fachada (fria, N=3, expiracion, excluidos) ......... `test_window_*`
- multi-moneda (mapa + motor por moneda) .............................. `test_currency_*`
- endpoint (forma, listas `[]`, auditoria, RBAC) ...................... `test_endpoint_*`
- integracion con el motor E6-T02 .................................... `test_engine_*`
- cableado del bus + orden motor->perfil + flags ..................... `test_wiring_*`
- handlers D-04 (sesion/commit propios, rollback) .................... `test_handler_*`
- contratos de frontera (espejos verificados N2/N3) ................. `test_profile_*`
- end-to-end outbox/bus -> perfil -> GET ............................. `test_e2e_*`
- sin PII / sin movimiento de dinero ................................. `test_no_*`
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
DEVICE_A = "dev-perfil-a"
DEVICE_B = "dev-perfil-b"

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

SCHEMAS = (
    "accounts",
    "ledger",
    "shared",
    "transactions",
    "risk",
    "audit",
    "notifications",
    "config",
)


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
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE config.parameters (id TEXT PRIMARY KEY, "
                "key TEXT NOT NULL UNIQUE, value_json TEXT NOT NULL, "
                "module TEXT NOT NULL, description TEXT, updated_by TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
        )
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
        accounts_repo.apply_delta(s, a1.id, available_delta=500_000, held_delta=0, currency="PEN")
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


def _transfer(session: Session, amount_minor: int, **kwargs):
    from app.modules.transactions import service as tx_service

    params = {
        "initiator_user_id": USER_A,
        "source_account_id": session.info["a1"],
        "target_account_id": session.info["b1"],
        "amount_minor": amount_minor,
        "currency": "PEN",
        "idempotency_key": f"e6t03-{uuid.uuid4()}",
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


def _profile(session: Session, user_id=USER_A):
    from app.modules.risk import repository as risk_repo

    return risk_repo.get_by_user(session, user_id)


# ------------------------------------------------------------------- dominio


def test_domain_first_observation_founds_average():
    from app.modules.risk.domain.profile import next_profile

    update = next_profile(None, {"amount_minor": 10_000}, operations=0)
    assert update.avg_amount_minor == 10_000
    assert update.operations == 1
    assert update.usual_countries is None and update.usual_hours is None
    assert update.last_device_id is None


def test_domain_incremental_mean_without_float():
    from app.modules.risk.domain.profile import next_profile

    current = {"avg_amount_minor": 10_000, "usual_countries": [], "usual_hours": []}
    first = next_profile(current, {"amount_minor": 10_000}, operations=2)
    assert first.avg_amount_minor == 10_000  # (20000+10000)/3 exacto
    assert first.operations == 3
    second = next_profile(
        {"avg_amount_minor": first.avg_amount_minor}, {"amount_minor": 16_000}, operations=3
    )
    assert second.avg_amount_minor == 11_500  # (30000+16000)/4 exacto
    assert isinstance(second.avg_amount_minor, int)


def test_domain_rounding_is_half_up_with_example():
    from app.modules.risk.domain.profile import next_profile

    # 5/2 = 2.5 -> 3 con ROUND_HALF_UP (con HALF_EVEN daria 2).
    update = next_profile({"avg_amount_minor": 2}, {"amount_minor": 3}, operations=1)
    assert update.avg_amount_minor == 3
    # Enteros grandes exactos (un `float` perderia precision aqui).
    big = next_profile({"avg_amount_minor": 10**18}, {"amount_minor": 10**18 + 1}, operations=1)
    assert big.avg_amount_minor == 10**18 + 1  # (2e18+1)/2 = ...000.5 -> +1


def test_domain_habits_merge_and_top5_recency():
    from app.modules.risk.domain.profile import next_profile

    current = {"usual_countries": ["PE"], "usual_hours": [8]}
    update = next_profile(current, {"country": " cl ", "hour": 20, "device_id": DEVICE_A})
    assert update.usual_countries == ["PE", "CL"]
    assert update.usual_hours == [8, 20]
    assert update.last_device_id == DEVICE_A

    crowded = {"usual_countries": ["A", "B", "C", "D", "E"], "usual_hours": [0, 1, 2, 3, 4]}
    capped = next_profile(crowded, {"country": "F", "hour": 5})
    assert capped.usual_countries == ["B", "C", "D", "E", "F"], "top 5: gana lo reciente"
    assert capped.usual_hours == [1, 2, 3, 4, 5]
    # Repetir un valor no lo duplica (lo refresca al final).
    refresh = next_profile(
        {"usual_countries": ["A", "B"], "usual_hours": [8, 20]},
        {"country": "a", "hour": 8},
    )
    assert refresh.usual_countries == ["B", "A"]
    assert refresh.usual_hours == [20, 8]


def test_domain_zero_amount_does_not_dilute():
    """B3: `amount_minor=0` no aporta (conserva el promedio)."""
    from app.modules.risk.domain.profile import next_profile

    update = next_profile({"avg_amount_minor": 10_000}, {"amount_minor": 0}, operations=1)
    assert update.avg_amount_minor is None, "el 0 no refunde el promedio"
    assert update.operations is None


def test_domain_malformed_observation_is_ignored():
    from app.modules.risk.domain.profile import next_profile

    current = {
        "avg_amount_minor": 7_000,
        "usual_countries": ["PE"],
        "usual_hours": [8],
        "last_device_id": DEVICE_A,
    }
    for bad in (
        {"amount_minor": -5},
        {"amount_minor": True},
        {"amount_minor": "100"},
        {"amount_minor": 10.5},
        {"hour": 24},
        {"hour": -1},
        {"hour": "8"},
        {"country": ""},
        {"country": "TOOLONGCODE"},
        {"device_id": ""},
        {"device_id": "   "},
        {"device_id": "x" * 129},
    ):
        update = next_profile(current, bad, operations=4)
        assert update.avg_amount_minor is None, bad
        assert update.usual_countries is None, bad
        assert update.usual_hours is None, bad
        assert update.last_device_id is None, bad
    # La entrada nunca lanza por forma (None/dict vacio/tipo raro).
    assert next_profile(current, None, operations=4).avg_amount_minor is None
    assert next_profile(current, {}, operations=4).avg_amount_minor is None
    assert next_profile(current, {"amount_minor": 100}, operations="x").avg_amount_minor == 100


def test_domain_device_last_wins_and_score_untouched():
    from app.modules.risk.domain.profile import next_profile

    update = next_profile({"last_device_id": DEVICE_A}, {"device_id": DEVICE_B})
    assert update.last_device_id == DEVICE_B
    assert "score" not in update.to_dict(), "el dominio no conoce severidad (E2-T20/E6-T14)"
    kept = next_profile({"last_device_id": DEVICE_A}, {"hour": 9})
    assert kept.last_device_id is None, "sin dispositivo nuevo se conserva"


# --------------------------------------------------------------- consumidores


def test_feed_settled_feeds_profile_without_moving_money(session: Session):
    from app.modules.risk import service as risk_service

    tx = _transfer(session, 12_000, device_id=DEVICE_A, metadata={"device_id": DEVICE_A})
    session.commit()
    assert tx.status == "SETTLED"
    # El motor movio el dinero; el consumidor solo debe observar.
    before_balances = _balances(session)
    before_entries = _journal_count(session)

    outcome = risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(tx.id)}
    )
    session.commit()

    assert outcome["consumed"] is True and outcome["source"] == "transfer.settled"
    assert outcome["avg_amount_minor"] == 12_000
    assert set(outcome) == {
        "consumed",
        "source",
        "avg_amount_minor",
        "usual_countries",
        "usual_hours",
        "last_device_id",
    }, "contrato sin PII: solo habitos"
    row = _profile(session)
    assert row.avg_amount_minor == 12_000
    assert row.usual_hours == [tx.created_at.hour]
    assert row.last_device_id == DEVICE_A
    assert row.score == 0, "alimentar no toca severidad"
    assert _balances(session) == before_balances, "alimentar no mueve dinero"
    assert _journal_count(session) == before_entries, "alimentar no crea asientos"


def test_feed_ignores_client_country(session: Session):
    """A4: el `country` del payload no se persiste (el cliente no elige su pais)."""
    from app.modules.risk import service as risk_service

    tx = _transfer(session, 20_000)
    session.commit()

    outcome = risk_service.handle_profile_transaction(
        session,
        event_id=uuid.uuid4(),
        payload={
            "transaction_id": str(tx.id),
            "user_id": str(USER_A),
            "device_id": DEVICE_B,
            "country": "cl",
            "country_code": "CL",
        },
        source="transfer.settled",
    )
    session.commit()

    assert outcome["consumed"] is True
    row = _profile(session)
    assert row.avg_amount_minor == 20_000
    assert list(row.usual_countries or []) == [], "pais del cliente: no alimenta"
    assert row.last_device_id == DEVICE_B


def test_feed_double_event_same_operation_does_not_duplicate(session: Session):
    """A1: `initiated` + `settled` de la misma operacion alimentan una sola vez."""
    from app.modules.risk import service as risk_service

    tx = _transfer(session, 10_000, device_id=DEVICE_A, metadata={"device_id": DEVICE_A})
    session.commit()

    first = risk_service.handle_profile_transaction(
        session,
        event_id=uuid.uuid4(),
        payload={"transaction_id": str(tx.id)},
        source="transfer.settled",
    )
    session.commit()
    # Re-alimentar la MISMA operacion con otro evento converge al mismo valor.
    second = risk_service.handle_profile_transaction(
        session,
        event_id=uuid.uuid4(),
        payload={"transaction_id": str(tx.id)},
        source="transfer.settled",
    )
    session.commit()

    assert first["consumed"] is True and second["consumed"] is True
    assert first["avg_amount_minor"] == second["avg_amount_minor"] == 10_000
    assert _profile(session).avg_amount_minor == 10_000, "re-alimentar no duplica"


def test_feed_replay_does_not_duplicate(session: Session):
    from app.modules.risk import service as risk_service

    tx = _transfer(session, 10_000, device_id=DEVICE_A, metadata={"device_id": DEVICE_A})
    session.commit()
    session.expire_all()
    hour = tx.created_at.hour

    event_id = uuid.uuid4()
    first = risk_service.handle_profile_transaction(
        session, event_id=event_id, payload={"transaction_id": str(tx.id)}
    )
    session.commit()
    # Segunda operacion distinta con el MISMO event_id es replay: no duplica.
    tx2 = _transfer(session, 90_000)
    session.commit()
    second = risk_service.handle_profile_transaction(
        session, event_id=event_id, payload={"transaction_id": str(tx2.id)}
    )
    session.commit()

    assert first["consumed"] is True
    assert second == {"consumed": False, "reason": "duplicate"}
    row = _profile(session)
    assert row.avg_amount_minor == 10_000, "el replay no re-promedia"
    assert row.usual_hours == [hour] and row.last_device_id == DEVICE_A


def test_feed_rejected_is_excluded(session: Session):
    """A2: una transferencia REJECTED (fondos insuficientes) no alimenta."""
    from app.modules.risk import service as risk_service
    from app.modules.transactions import service as tx_service

    rejected = tx_service.transfer_third_party(
        session,
        initiator_user_id=USER_B,
        source_account_id=session.info["b1"],
        target_account_id=session.info["a1"],
        amount_minor=90_000,  # B solo tiene 5_000
        currency="PEN",
        idempotency_key=f"e6t03-{uuid.uuid4()}",
    )
    session.commit()
    assert rejected.status == "REJECTED"

    outcome = risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(rejected.id)}
    )
    session.commit()

    assert outcome == {
        "consumed": True,
        "reason": "non-successful-state",
        "status": "REJECTED",
    }
    row = _profile(session, USER_B)
    assert row is None or int(row.avg_amount_minor or 0) == 0, "REJECTED no crea promedio"


def test_feed_pending_and_failed_are_excluded(session: Session):
    """A2/M1: `PENDING_*` y `FAILED` tampoco aportan monto/conteo."""
    from app.modules.risk import service as risk_service

    ok = _transfer(session, 10_000)
    session.commit()
    risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(ok.id)}
    )
    session.commit()
    assert _profile(session).avg_amount_minor == 10_000

    for status in ("PENDING_AUTHORIZATION", "FAILED"):
        pending = _transfer(session, 90_000)
        session.commit()
        pending.status = status
        session.commit()
        outcome = risk_service.handle_profile_transaction(
            session, event_id=uuid.uuid4(), payload={"transaction_id": str(pending.id)}
        )
        session.commit()
        assert outcome["consumed"] is True
        assert outcome["reason"] == "non-successful-state", status
    assert _profile(session).avg_amount_minor == 10_000, "pendientes/fallidas no cuentan"


def test_feed_missing_transaction_retries_without_claim(session: Session, engine):
    """M4 real: sin operacion se lanza reintentable y el outbox reintenta.

    Nivel servicio: `handle_profile_transaction` NO reclama y lanza
    `ProfileRetryableError` (el mismo evento puede reintentarse: no
    queda marcado como duplicado). Nivel outbox real: un evento
    `transfer.settled` con `transaction_id` inexistente pasa por
    `publish_pending` -> `published=0`, `failed=1`, el evento sigue
    `PENDING` (no `PUBLISHED`, con `attempts=1` y backoff) y sin claim;
    cuando la operacion existe, el reintento alimenta el perfil.
    """
    from functools import partial

    from app.core.bus import EventBus
    from app.modules.risk import events as risk_events
    from app.modules.risk import service as risk_service
    from app.modules.shared import repository as shared_repo
    from app.modules.shared.jobs.publisher import publish_pending
    from app.modules.shared.models import OutboxEntry

    event_id = uuid.uuid4()
    missing = str(uuid.uuid4())
    with pytest.raises(risk_service.ProfileRetryableError):
        risk_service.handle_profile_transaction(
            session, event_id=event_id, payload={"transaction_id": missing}
        )
    session.rollback()
    assert shared_repo.is_processed(session, event_id=event_id, consumer="risk-profile") is False
    with pytest.raises(risk_service.ProfileRetryableError):
        risk_service.handle_profile_transaction(
            session, event_id=event_id, payload={"transaction_id": missing}
        )
    session.rollback()
    assert _profile(session) is None, "sin operacion no se crea perfil"

    entry = shared_repo.insert_event(
        session,
        aggregate_type="transaction",
        aggregate_id=uuid.uuid4(),
        event_type="transfer.settled",
        payload={"transaction_id": missing},
    )
    session.commit()

    bus = EventBus()
    bus.subscribe(
        "transfer.settled",
        partial(
            risk_events.on_profile_transfer_settled,
            session_factory=lambda: Session(engine),
        ),
    )
    result = publish_pending(session, bus=bus, backoff_base_seconds=0)
    assert result == {"published": 0, "failed": 1, "skipped": 0}
    session.commit()

    check = Session(engine)
    try:
        fresh = check.get(OutboxEntry, entry.id)
        assert fresh is not None and fresh.status == "PENDING", "sigue pendiente, no PUBLISHED"
        assert fresh.published_at is None and int(fresh.attempts) == 1, "backoff real aplicado"
    finally:
        check.close()
    assert (
        shared_repo.is_processed(session, event_id=entry.id, consumer="risk-profile") is False
    ), "sin operacion no hay claim"

    # Cuando la operacion aparece (el evento suele llegar antes que el
    # `commit` visible), el MISMO entry fallido (mismo `event_id`) se
    # re-dispara y alimenta el perfil: la fila tardia se siembra con el
    # `transaction_id` que el entry ya referencia (sin eventos nuevos).
    from app.modules.transactions.models import Transaction

    late_tx = Transaction(
        id=uuid.UUID(missing),
        type="THIRD_PARTY_TRANSFER",
        status="SETTLED",
        initiator_user_id=USER_A,
        source_account_id=session.info["a1"],
        target_account_id=session.info["b1"],
        amount_minor=10_000,
        currency="PEN",
    )
    session.add(late_tx)
    session.commit()
    retry = publish_pending(session, bus=bus, backoff_base_seconds=0)
    assert retry == {
        "published": 1,
        "failed": 0,
        "skipped": 0,
    }, f"el mismo entry se publica: {retry}"
    session.commit()
    assert shared_repo.is_processed(session, event_id=entry.id, consumer="risk-profile") is True
    assert _profile(session) is not None and _profile(session).avg_amount_minor == 10_000
    check_retry = Session(engine)
    try:
        again = check_retry.get(OutboxEntry, entry.id)
        assert again is not None and again.status == "PUBLISHED"
        assert int(again.attempts) == 1, "un solo intento fallido antes del exito"
        assert (
            int(again.attempts) < risk_service.PROFILE_MISSING_TX_MAX_ATTEMPTS
        ), "el limite de reintentos no se alcanza"
    finally:
        check_retry.close()


def test_profile_success_states_mirror_window_states():
    """N2 (fixes PR #26): `PROFILE_SUCCESS_STATES` espeja `WINDOW_SUCCESS_STATES`.

    Frontera D-01: `risk` no importa `transactions.service` a nivel de
    modulo (evita el ciclo de imports), asi que la paridad se verifica
    aqui: falla si ambos conjuntos divergen.
    """
    from app.modules.risk import service as risk_service
    from app.modules.transactions import service as tx_service

    assert risk_service.PROFILE_SUCCESS_STATES == tx_service.WINDOW_SUCCESS_STATES


def test_profile_missing_tx_limit_tracks_outbox_default():
    """N3 (fixes PR #26): el limite de reintentos es el default del publisher.

    Falla sin el cableado (`PROFILE_MISSING_TX_MAX_ATTEMPTS` literal y
    sin `DEFAULT_MAX_ATTEMPTS` en el namespace del servicio) y si ambos
    valores divergen en el futuro.
    """
    from app.modules.risk import service as risk_service
    from app.modules.shared.repository import outbox as outbox_repo

    assert risk_service.DEFAULT_MAX_ATTEMPTS is outbox_repo.DEFAULT_MAX_ATTEMPTS
    assert risk_service.PROFILE_MISSING_TX_MAX_ATTEMPTS == outbox_repo.DEFAULT_MAX_ATTEMPTS


def test_feed_failure_rolls_back_and_reraises(session: Session, monkeypatch):
    """Fallo en el consumo: revierte efectos y relanza (D-04).

    Nota SQLite (quirk compartido con E6-T02): `try_mark_processed` y
    `get_or_create` confirman su savepoint al salir, asi que el
    `RELEASE` deja esas filas visibles tras el `rollback` del llamante;
    en Postgres (savepoints reales) el rollback tambien las revierte y
    el reintento reprocesa. Por eso el estado del claim y la existencia
    de la fila no se afirman: el contrato portable es relanzar sin
    dejar valores alimentados (promedio/habitos intactos).
    """
    from app.modules.risk import repository as risk_repo
    from app.modules.risk import service as risk_service

    tx = _transfer(session, 10_000)
    session.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("persistencia caida")

    monkeypatch.setattr(risk_repo, "update_behavior", _boom)
    with pytest.raises(RuntimeError):
        risk_service.handle_profile_transaction(
            session, event_id=uuid.uuid4(), payload={"transaction_id": str(tx.id)}
        )
    session.rollback()
    row = _profile(session)
    assert row is None or (
        row.avg_amount_minor == 0
        and not row.usual_countries
        and not row.usual_hours
        and row.last_device_id is None
    ), "sin valores alimentados tras el fallo"


def test_feed_login_updates_habits_not_average(session: Session):
    from app.modules.risk import service as risk_service

    risk_service.update_behavior(session, USER_A, avg_amount_minor=10_000, last_device_id=DEVICE_A)
    session.commit()

    at = datetime(2026, 11, 12, 15, 30, tzinfo=UTC)
    outcome = risk_service.handle_profile_login(
        session,
        event_id=uuid.uuid4(),
        payload={"user_id": str(USER_A), "device_id": DEVICE_B, "country": "pe"},
        at=at,
    )
    session.commit()

    assert outcome["consumed"] is True and outcome["source"] == "auth.login_succeeded"
    row = _profile(session)
    assert row.avg_amount_minor == 10_000, "el login no mueve el promedio"
    assert row.usual_hours == [15] and list(row.usual_countries or []) == []
    assert row.last_device_id == DEVICE_B and row.score == 0


def test_feed_login_without_identifiers_marks_processed(session: Session):
    from app.modules.risk import service as risk_service

    outcome = risk_service.handle_profile_login(
        session, event_id=uuid.uuid4(), payload={"device_id": DEVICE_A}
    )
    session.commit()
    assert outcome == {"consumed": True, "reason": "missing-user-id"}


def test_feed_failed_attempt_updates_hour_only(session: Session):
    """M3: el fallo complementa la hora pero NO el dispositivo ni el score."""
    from app.modules.risk import service as risk_service

    risk_service.update_behavior(
        session, USER_A, avg_amount_minor=10_000, score=7, last_device_id=DEVICE_A
    )
    session.commit()

    at = datetime(2026, 11, 12, 3, 5, tzinfo=UTC)
    outcome = risk_service.handle_profile_failed_attempt(
        session,
        event_id=uuid.uuid4(),
        payload={"user_id": str(USER_A), "device_id": DEVICE_B, "blocked": True},
        at=at,
    )
    session.commit()

    assert outcome["consumed"] is True and outcome["source"] == "auth.failed_attempt"
    assert outcome["score"] == 7
    row = _profile(session)
    assert row.score == 7, "la severidad la posee E2-T20: aqui no se duplica"
    assert row.avg_amount_minor == 10_000
    assert row.usual_hours == [3]
    assert row.last_device_id == DEVICE_A, "el fallo no es confiable: no fija dispositivo"


def test_login_and_failed_attempt_skip_window_query(session: Session, monkeypatch):
    """R4: login/fallo completan sin consultar `transactions` (no calculan la ventana)."""
    from app.modules.risk import service as risk_service

    def _boom(sess, uid):
        raise AssertionError("login/fallo no deben calcular la ventana (R4)")

    monkeypatch.setattr(risk_service, "_window_average", _boom)
    risk_service.update_behavior(session, USER_A, avg_amount_minor=10_000, last_device_id=DEVICE_A)
    session.commit()

    login = risk_service.handle_profile_login(
        session,
        event_id=uuid.uuid4(),
        payload={"user_id": str(USER_A), "device_id": DEVICE_B},
        at=datetime(2026, 11, 12, 15, 30, tzinfo=UTC),
    )
    session.commit()
    failed = risk_service.handle_profile_failed_attempt(
        session,
        event_id=uuid.uuid4(),
        payload={"user_id": str(USER_A), "device_id": "attacker-device", "blocked": True},
        at=datetime(2026, 11, 12, 3, 5, tzinfo=UTC),
    )
    session.commit()

    assert login["consumed"] is True and failed["consumed"] is True
    row = _profile(session)
    assert row.avg_amount_minor == 10_000, "sin monto no se recalcula"
    assert row.last_device_id == DEVICE_B, "el login si fija dispositivo; el fallo no lo pisa"


def test_reversal_recalculates_average_without_habits(session: Session):
    """R7: `transfer.reversed` recalcula el promedio sin tocar habitos ni score."""
    from app.modules.risk import service as risk_service

    first = _transfer(session, 10_000, device_id=DEVICE_A, metadata={"device_id": DEVICE_A})
    session.commit()
    risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(first.id)}
    )
    session.commit()
    second = _transfer(session, 30_000)
    session.commit()
    risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(second.id)}
    )
    session.commit()
    assert _profile(session).avg_amount_minor == 20_000
    hours_before = list(_profile(session).usual_hours or [])

    second.status = "REVERSED"
    session.commit()
    outcome = risk_service.handle_profile_reversal(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(second.id)}
    )
    session.commit()

    assert outcome["consumed"] is True and outcome["source"] == "transfer.reversed"
    assert outcome["avg_amount_minor"] == 10_000, "el reverso sale de la ventana"
    row = _profile(session)
    assert row.avg_amount_minor == 10_000
    assert row.last_device_id == DEVICE_A, "sin habitos: el dispositivo se conserva"
    assert list(row.usual_hours or []) == hours_before, "sin habitos: las horas se conservan"
    assert row.score == 0, "el reverso no toca severidad"

    replayed = risk_service.handle_profile_reversal(
        session,
        event_id=uuid.uuid4(),
        payload={"transaction_id": str(second.id)},
    )
    session.commit()
    assert replayed["consumed"] is True
    assert _profile(session).avg_amount_minor == 10_000, "re-alimentar converge al mismo valor"


def test_reversal_missing_transaction_retries_without_claim(session: Session):
    """R7: sin operacion, el reverso no se reclama y se reintenta (M4 real)."""
    from app.modules.risk import service as risk_service
    from app.modules.shared import repository as shared_repo

    event_id = uuid.uuid4()
    with pytest.raises(risk_service.ProfileRetryableError):
        risk_service.handle_profile_reversal(
            session, event_id=event_id, payload={"transaction_id": str(uuid.uuid4())}
        )
    session.rollback()
    assert shared_repo.is_processed(session, event_id=event_id, consumer="risk-profile") is False


def test_update_behavior_with_event_id_is_idempotent(session: Session):
    from app.modules.risk import service as risk_service

    tx = _transfer(session, 10_000)
    session.commit()
    payload = {"transaction_id": str(tx.id)}
    event_id = uuid.uuid4()

    first = risk_service.handle_profile_transaction(session, event_id=event_id, payload=payload)
    session.commit()
    # Llamada directa con el mismo event_id: el claim ya existe, no re-escribe.
    row_before = _profile(session)
    replayed = risk_service.update_behavior(
        session, USER_A, {"amount_minor": 99_000}, event_id=event_id
    )
    session.commit()

    assert first["consumed"] is True
    assert replayed.avg_amount_minor == row_before.avg_amount_minor == 10_000


def test_update_behavior_validates_before_claiming(session: Session):
    """B2: la observacion se valida ANTES de reclamar el evento."""
    from app.modules.risk import repository as risk_repo
    from app.modules.risk import service as risk_service
    from app.modules.shared import repository as shared_repo

    event_id = uuid.uuid4()
    with pytest.raises(risk_repo.RiskProfileValidationError):
        risk_service.update_behavior(session, USER_A, "no-es-observacion", event_id=event_id)
    session.rollback()
    assert shared_repo.is_processed(session, event_id=event_id, consumer="risk-profile") is False


def test_zero_amount_observation_preserves_average(session: Session):
    """B3: una observacion con `amount_minor=0` conserva el promedio."""
    from app.modules.risk import service as risk_service

    tx = _transfer(session, 10_000)
    session.commit()
    risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(tx.id)}
    )
    session.commit()
    assert _profile(session).avg_amount_minor == 10_000

    row = risk_service.update_behavior(session, USER_A, {"amount_minor": 0, "hour": 9})
    session.commit()
    assert row.avg_amount_minor == 10_000, "el 0 no refunde el promedio"
    assert row.usual_hours == [tx.created_at.hour, 9][-5:]


def test_legacy_update_refreshes_updated_at(session: Session):
    """B11: el modo legado tambien refresca `updated_at`."""
    from app.modules.risk import service as risk_service

    row = risk_service.update_behavior(session, USER_A, avg_amount_minor=5_000)
    session.commit()
    row.updated_at = datetime(2020, 1, 1, tzinfo=UTC)
    session.commit()

    updated = risk_service.update_behavior(session, USER_A, score=4)
    session.commit()
    seen = updated.updated_at
    if seen is not None and seen.tzinfo is not None:
        seen = seen.replace(tzinfo=None)
    assert seen is not None and seen > datetime(2020, 1, 1)
    assert updated.score == 4 and updated.avg_amount_minor == 5_000


# -------------------------------------------------------------------- ventana


def test_profile_window_is_code_constant():
    """B5: la ventana es constante de codigo (90); sin claves huesped."""
    from app.modules.risk import service as risk_service

    assert risk_service.PROFILE_WINDOW_DEFAULT_DAYS == 90
    assert risk_service.profile_window_days() == 90
    assert risk_service.profile_window_days(None) == 90


def test_window_recomputes_real_average(session: Session):
    """A3: 5x10k + 100k -> 25.000 (media real de la ventana, sin EWMA)."""
    from app.modules.risk import service as risk_service

    for _ in range(5):
        tx = _transfer(session, 10_000)
        session.commit()
        risk_service.handle_profile_transaction(
            session, event_id=uuid.uuid4(), payload={"transaction_id": str(tx.id)}
        )
        session.commit()
    big = _transfer(session, 100_000)
    session.commit()
    outcome = risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(big.id)}
    )
    session.commit()

    assert outcome["avg_amount_minor"] == 25_000, "(5*10000+100000)/6 con ventana real"
    assert _profile(session).avg_amount_minor == 25_000


def test_cold_start_uses_real_history(session: Session):
    """A3: sin alimentacion previa, el primer evento usa el historial real."""
    from app.modules.risk import service as risk_service

    txs = [_transfer(session, amount) for amount in (10_000, 20_000, 30_000)]
    session.commit()
    assert _profile(session) is None, "arranque en frio: sin perfil"

    outcome = risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(txs[-1].id)}
    )
    session.commit()

    assert outcome["avg_amount_minor"] == 20_000, "media real (10+20+30)/3, no 30k fundador"
    assert _profile(session).avg_amount_minor == 20_000


def test_window_expiry_excludes_old_operations(session: Session):
    """M1/M2: fuera de la ventana de 90 dias no cuenta."""
    from app.modules.risk import service as risk_service

    old = _transfer(session, 10_000)
    session.commit()
    old.created_at = datetime.now(UTC) - timedelta(days=100)
    session.commit()
    new = _transfer(session, 20_000)
    session.commit()

    outcome = risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(new.id)}
    )
    session.commit()
    assert outcome["avg_amount_minor"] == 20_000, "la vieja expiro de la ventana"


def test_window_stats_only_counts_successful(session: Session):
    """M1/M2: la fachada solo agrega estados exitosos dentro de la ventana."""
    from app.modules.risk import service as risk_service
    from app.modules.transactions.service import get_window_amount_stats

    ok = _transfer(session, 10_000)
    session.commit()
    bad = _transfer(session, 90_000)
    session.commit()
    bad.status = "REJECTED"
    session.commit()
    pending = _transfer(session, 40_000)
    session.commit()
    pending.status = "PENDING_AUTHORIZATION"
    session.commit()

    stats = get_window_amount_stats(
        session, USER_A, window_days=risk_service.PROFILE_WINDOW_DEFAULT_DAYS
    )
    assert stats == {"PEN": {"sum_minor": 10_000, "count": 1}}, f"real: {stats}"
    assert ok.status == "SETTLED"


def test_window_excludes_own_transfer(session: Session):
    """R3: `OWN_TRANSFER` no es gasto: mover fondos entre cuentas propias no altera el promedio.

    Una transferencia de 100k entre cuentas propias (`OWN_TRANSFER`
    `SETTLED`) se excluye de la ventana por defecto; una transferencia
    a tercero SI cuenta como gasto.
    """
    from app.modules.risk import service as risk_service
    from app.modules.transactions.service import get_window_amount_stats

    normal = _transfer(session, 10_000)
    session.commit()
    own = _transfer(session, 100_000)
    own.type = "OWN_TRANSFER"
    session.commit()
    third = _transfer(session, 30_000)
    session.commit()
    assert all(tx.status == "SETTLED" for tx in (normal, own, third))

    stats = get_window_amount_stats(
        session, USER_A, window_days=risk_service.PROFILE_WINDOW_DEFAULT_DAYS
    )
    assert stats == {"PEN": {"sum_minor": 40_000, "count": 2}}, f"real: {stats}"
    assert risk_service.profile_average_by_currency(session, USER_A) == {"PEN": 20_000}

    outcome = risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(third.id)}
    )
    session.commit()
    assert outcome["avg_amount_minor"] == 20_000, "la propia no distorsiona el promedio"


def test_window_excludes_loan_disbursement(session: Session):
    """Un desembolso `SETTLED` no contamina el promedio (solo el gasto cuenta).

    `LOAN_DISBURSEMENT` acredita capital (fijaria el promedio y
    suprimiria `UNUSUAL_AMOUNT`); la cuota `LOAN_INSTALLMENT` y la
    `INTERBANK_TRANSFER` SI son gasto y se incluyen. El filtro es
    opt-out (`exclude_types=()` lo desactiva).
    """
    from app.modules.risk import service as risk_service
    from app.modules.transactions.service import get_window_amount_stats

    normal = _transfer(session, 10_000)
    session.commit()
    big = _transfer(session, 90_000)
    big.type = "LOAN_DISBURSEMENT"
    session.commit()
    quota = _transfer(session, 20_000)
    quota.type = "LOAN_INSTALLMENT"
    session.commit()
    inter = _transfer(session, 30_000)
    inter.type = "INTERBANK_TRANSFER"
    session.commit()
    assert all(tx.status == "SETTLED" for tx in (normal, big, quota, inter))

    stats = get_window_amount_stats(
        session, USER_A, window_days=risk_service.PROFILE_WINDOW_DEFAULT_DAYS
    )
    assert stats == {"PEN": {"sum_minor": 60_000, "count": 3}}, f"real: {stats}"
    assert risk_service.profile_average_by_currency(session, USER_A) == {"PEN": 20_000}
    assert risk_service.profile_window_days() == 90

    stats_all = get_window_amount_stats(
        session,
        USER_A,
        window_days=risk_service.PROFILE_WINDOW_DEFAULT_DAYS,
        exclude_types=(),
    )
    assert stats_all == {"PEN": {"sum_minor": 150_000, "count": 4}}, f"real: {stats_all}"


# ------------------------------------------------------------- multi-moneda


def _usd_pair(session: Session):
    from app.modules.accounts import repository as accounts_repo

    c = accounts_repo.create_account(
        session, user_id=USER_A, account_number="00990001111", type="AHORRO", currency="USD"
    )
    d = accounts_repo.create_account(
        session, user_id=USER_B, account_number="00880003333", type="AHORRO", currency="USD"
    )
    accounts_repo.apply_delta(session, c.id, available_delta=500_000, held_delta=0, currency="USD")
    accounts_repo.apply_delta(session, d.id, available_delta=5_000, held_delta=0, currency="USD")
    session.commit()
    return c.id, d.id


def _transfer_usd(session: Session, amount_minor: int, source_id, target_id):
    from app.modules.transactions import service as tx_service

    return tx_service.transfer_third_party(
        session,
        initiator_user_id=USER_A,
        source_account_id=source_id,
        target_account_id=target_id,
        amount_minor=amount_minor,
        currency="USD",
        idempotency_key=f"e6t03-{uuid.uuid4()}",
    )


def test_currency_average_map_by_currency(session: Session):
    """M6: el servicio expone el promedio por moneda de la ventana."""
    from app.modules.risk import service as risk_service

    c, d = _usd_pair(session)
    for amount in (10_000, 10_000):
        tx = _transfer(session, amount)
        session.commit()
        risk_service.handle_profile_transaction(
            session, event_id=uuid.uuid4(), payload={"transaction_id": str(tx.id)}
        )
        session.commit()
    for amount in (100_000, 100_000):
        tx = _transfer_usd(session, amount, c, d)
        session.commit()
        risk_service.handle_profile_transaction(
            session, event_id=uuid.uuid4(), payload={"transaction_id": str(tx.id)}
        )
        session.commit()

    by_currency = risk_service.profile_average_by_currency(session, USER_A)
    assert by_currency == {"PEN": 10_000, "USD": 100_000}
    row = _profile(session)
    assert row.avg_amount_minor == 55_000, "agregado de compatibilidad (20+200)/4"


def test_engine_compares_same_currency(session: Session):
    """M6: `UNUSUAL_AMOUNT` compara contra el promedio de la misma moneda."""
    from app.modules.risk import service as risk_service

    _seed_conservative_rules(session)
    c, d = _usd_pair(session)
    for amount in (10_000, 10_000):
        tx = _transfer(session, amount)
        session.commit()
        risk_service.handle_profile_transaction(
            session, event_id=uuid.uuid4(), payload={"transaction_id": str(tx.id)}
        )
        session.commit()
    for amount in (100_000, 100_000):
        tx = _transfer_usd(session, amount, c, d)
        session.commit()
        risk_service.handle_profile_transaction(
            session, event_id=uuid.uuid4(), payload={"transaction_id": str(tx.id)}
        )
        session.commit()

    calm_usd = risk_service.evaluate_pre_transaction(
        session, user_id=USER_A, amount_minor=200_000, currency="USD"
    )
    assert calm_usd.action == "ALLOW", "200k vs promedio USD 100k (factor 3) no dispara"
    alert_pen = risk_service.evaluate_pre_transaction(
        session, user_id=USER_A, amount_minor=200_000, currency="PEN"
    )
    assert alert_pen.action == "ALERT" and alert_pen.matched_rules == ("UNUSUAL_AMOUNT",)
    spike_usd = risk_service.evaluate_pre_transaction(
        session, user_id=USER_A, amount_minor=400_000, currency="USD"
    )
    assert spike_usd.matched_rules == ("UNUSUAL_AMOUNT",), "400k > 3x100k USD si dispara"


# ------------------------------------------------------------- endpoint RBAC


@pytest.fixture()
def profile_db():
    """SQLite con `risk.risk_profiles` + `config.parameters` (endpoint)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.risk.models as _risk_models

    assert _risk_models is not None
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("risk", "config"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
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
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["risk.risk_profiles"]])
    s = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield engine, s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def profile_client(profile_db, monkeypatch):
    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "0")
    monkeypatch.setenv("RISK_PROFILE_ENABLED", "0")
    _, s = profile_db

    from app.core.db import get_db
    from app.main import app

    def _override():
        yield s

    app.dependency_overrides[get_db] = _override
    try:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def full_client(session, engine, monkeypatch):
    """TestClient contra la BD completa (perfil + transacciones + auditoria)."""
    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "0")
    monkeypatch.setenv("RISK_PROFILE_ENABLED", "0")

    from app.core.db import get_db
    from app.main import app

    def _override():
        s = Session(engine)
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    try:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _bearer(user_id: uuid.UUID, roles: list[str] | None = None) -> dict:
    from app.core.security import create_access_token

    token = create_access_token(subject=str(user_id), roles=roles)
    return {"Authorization": f"Bearer {token}"}


def _seed_profile(profile_db) -> uuid.UUID:
    from app.modules.risk import service as risk_service

    _, s = profile_db
    holder = uuid.uuid4()
    risk_service.update_behavior(
        s,
        holder,
        avg_amount_minor=25_000,
        usual_countries=["PE", "CL"],
        usual_hours=[8, 20],
        last_device_id=DEVICE_A,
        score=3,
    )
    s.commit()
    return holder


def test_endpoint_returns_minimum_profile_without_pii(profile_client, profile_db):
    holder = _seed_profile(profile_db)
    resp = profile_client.get(
        f"/api/v1/risk/profiles/{holder}", headers=_bearer(uuid.uuid4(), ["fraud_analyst"])
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"data", "meta"} and "request_id" in body["meta"]
    data = body["data"]
    assert set(data) == {
        "user_id",
        "avg_amount_minor",
        "avg_amount_by_currency",
        "usual_countries",
        "usual_hours",
        "last_device_id",
        "score",
        "updated_at",
    }
    assert data["user_id"] == str(holder)
    assert data["avg_amount_minor"] == 25_000
    assert data["avg_amount_by_currency"] == {}, "sin tabla de tx: degrada a {}"
    assert data["usual_countries"] == ["PE", "CL"]
    assert data["usual_hours"] == [8, 20]
    assert data["last_device_id"] == DEVICE_A
    assert data["score"] == 3 and data["updated_at"] is not None
    blob = resp.text
    for secret in ("Jheampierre", "0011", "RENEC"):
        assert secret not in blob


def test_endpoint_empty_lists_not_null(profile_client, profile_db):
    """B8: `[]` (nunca `null`) para listas vacias."""
    from app.modules.risk import service as risk_service

    _, s = profile_db
    holder = uuid.uuid4()
    risk_service.update_behavior(s, holder, avg_amount_minor=1_000)
    s.commit()

    resp = profile_client.get(
        f"/api/v1/risk/profiles/{holder}", headers=_bearer(uuid.uuid4(), ["admin"])
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["usual_countries"] == [] and data["usual_hours"] == []
    assert data["avg_amount_by_currency"] == {}


def test_endpoint_with_history_returns_by_currency(full_client, session):
    """M6: el GET expone `avg_amount_by_currency` desde la ventana real."""
    from app.modules.risk import service as risk_service

    tx = _transfer(session, 30_000, device_id=DEVICE_A, metadata={"device_id": DEVICE_A})
    session.commit()
    risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(tx.id)}
    )
    session.commit()

    resp = full_client.get(
        f"/api/v1/risk/profiles/{USER_A}", headers=_bearer(uuid.uuid4(), ["fraud_analyst"])
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["avg_amount_minor"] == 30_000
    assert data["avg_amount_by_currency"] == {"PEN": 30_000}


def test_endpoint_averages_share_live_origin_with_delayed_outbox(full_client, session):
    """R6: con outbox retrasado, escalar y mapa derivan del mismo origen en vivo.

    La segunda operacion esta liquidada pero el perfil aun no la
    alimento (el evento `transfer.settled` sigue en el outbox): el
    escalar almacenado esta obsoleto y el GET debe exponer el agregado
    recalculado, coherente con el mapa.
    """
    from app.modules.risk import service as risk_service

    first = _transfer(session, 10_000, device_id=DEVICE_A, metadata={"device_id": DEVICE_A})
    session.commit()
    risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(first.id)}
    )
    session.commit()
    assert _profile(session).avg_amount_minor == 10_000

    _transfer(session, 30_000)
    session.commit()
    assert _profile(session).avg_amount_minor == 10_000, "outbox retrasado: escalar obsoleto"

    resp = full_client.get(
        f"/api/v1/risk/profiles/{USER_A}", headers=_bearer(uuid.uuid4(), ["fraud_analyst"])
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["avg_amount_by_currency"] == {"PEN": 20_000}
    assert data["avg_amount_minor"] == 20_000, "R6: ambos del mismo origen en vivo"


def test_endpoint_audits_view_best_effort(full_client, session, engine):
    """B7: el GET audita `risk.profile.viewed` (sin PII)."""
    from app.modules.risk import service as risk_service

    tx = _transfer(session, 10_000)
    session.commit()
    risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(tx.id)}
    )
    session.commit()

    resp = full_client.get(
        f"/api/v1/risk/profiles/{USER_A}", headers=_bearer(uuid.uuid4(), ["fraud_analyst"])
    )
    assert resp.status_code == 200, resp.text

    check = Session(engine)
    try:
        from app.modules.audit.models import AuditLog

        rows = list(
            check.scalars(sa.select(AuditLog).where(AuditLog.action == "risk.profile.viewed")).all()
        )
    finally:
        check.close()
    assert len(rows) >= 1, "la lectura queda auditada"
    assert rows[-1].entity_id == USER_A
    assert DEVICE_A not in str(rows[-1].after_json or {}), "sin PII en la auditoria"


def test_endpoint_missing_or_malformed_is_not_found(profile_client):
    headers = _bearer(uuid.uuid4(), ["admin"])
    assert (
        profile_client.get(f"/api/v1/risk/profiles/{uuid.uuid4()}", headers=headers).status_code
        == 404
    )
    assert (
        profile_client.get("/api/v1/risk/profiles/no-es-uuid", headers=headers).status_code == 404
    )


def test_endpoint_requires_fraud_role(profile_client, profile_db):
    holder = _seed_profile(profile_db)
    assert profile_client.get(f"/api/v1/risk/profiles/{holder}").status_code == 401
    resp = profile_client.get(
        f"/api/v1/risk/profiles/{holder}", headers=_bearer(uuid.uuid4(), ["analyst"])
    )
    assert resp.status_code == 403


# ------------------------------------------------------- integracion E6-T02


def test_engine_sees_updated_profile(session: Session):
    from app.modules.risk import service as risk_service

    _seed_conservative_rules(session)
    first = _transfer(session, 10_000)
    session.commit()
    fresh = _transfer(session, 10_000)
    session.commit()
    # Se alimentan TODAS las operaciones de la ventana (cada evento
    # alimenta una vez): el promedio converge al medio real (10_000).
    # Un evento repetido no duplica (consumidor `risk-profile`).
    risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(first.id)}
    )
    session.commit()
    risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(fresh.id)}
    )
    session.commit()
    assert _profile(session).avg_amount_minor == 10_000

    calm = risk_service.evaluate_pre_transaction(
        session, user_id=USER_A, amount_minor=10_000, currency="PEN"
    )
    assert calm.action == "ALLOW", "monto habitual no dispara"
    alert = risk_service.evaluate_pre_transaction(
        session, user_id=USER_A, amount_minor=50_000, currency="PEN"
    )
    assert alert.action == "ALERT" and alert.matched_rules == ("UNUSUAL_AMOUNT",)
    assert alert.score == 15


# ------------------------------------------------------------------- cableado


def test_wiring_subscribes_profile_consumers_without_initiated():
    """A1/M5 + R7: el perfil NO se suscribe a `transaction.initiated` (4 handlers)."""
    from app.composition import is_risk_profile_wired, subscribe_risk_profile
    from app.core.bus import EventBus
    from app.modules.risk.events import LOGIN_SUCCEEDED_EVENT, TRANSFER_REVERSED_EVENT

    bus = EventBus()
    assert is_risk_profile_wired(bus) is False
    handlers = subscribe_risk_profile(bus)
    assert len(handlers) == 4
    assert is_risk_profile_wired(bus) is True
    assert bus.handler_count("transaction.initiated") == 0, "A1: sin doble conteo"
    assert bus.handler_count("transfer.settled") == 1
    assert bus.handler_count(LOGIN_SUCCEEDED_EVENT) == 1
    assert bus.handler_count("auth.failed_attempt") == 1
    assert bus.handler_count(TRANSFER_REVERSED_EVENT) == 1, "R7: el reverso recalcula"
    subscribe_risk_profile(bus)  # idempotente
    assert bus.handler_count("transfer.settled") == 1
    assert bus.handler_count("auth.failed_attempt") == 1
    assert bus.handler_count(TRANSFER_REVERSED_EVENT) == 1


def test_wiring_single_profile_handler_per_event_and_motor_first():
    """M5: un solo handler de perfil por evento; el motor va primero donde aplica."""
    from app.composition import subscribe_risk_antifraud, subscribe_risk_profile
    from app.core import events as domain_events
    from app.core.bus import EventBus
    from app.modules.risk.events import (
        on_profile_transfer_settled,
        on_transaction_initiated,
        on_transfer_settled,
    )

    bus = EventBus()
    subscribe_risk_antifraud(bus)  # el motor primero
    subscribe_risk_profile(bus)  # el perfil despues

    initiated = list(bus._handlers.get(domain_events.TRANSACTION_INITIATED, []))
    assert initiated == [on_transaction_initiated], "el perfil no escucha initiated"
    settled = list(bus._handlers.get(domain_events.TRANSFER_SETTLED, []))
    assert settled == [on_transfer_settled, on_profile_transfer_settled]
    profile_handlers = [h for h in settled if "profile" in getattr(h, "__name__", "")]
    assert profile_handlers == [on_profile_transfer_settled], "un solo handler de perfil"


def test_wiring_flag_gates_subscription(monkeypatch):
    from app.composition import risk_profile_enabled

    assert risk_profile_enabled() is True, "default encendido"
    monkeypatch.setenv("RISK_PROFILE_ENABLED", "0")
    assert risk_profile_enabled() is False
    monkeypatch.setenv("RISK_PROFILE_ENABLED", "false")
    assert risk_profile_enabled() is False


def test_handler_uses_own_session_with_commit(session, engine):
    from types import SimpleNamespace

    from app.modules.risk.events import on_profile_transfer_settled

    tx = _transfer(session, 10_000)
    session.commit()
    session.expire_all()

    created: list = []

    class _Tracking:
        def __init__(self, inner: Session) -> None:
            self._inner = inner
            self.commits = 0
            self.rollbacks = 0
            self.closes = 0

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def commit(self) -> None:
            self.commits += 1
            self._inner.commit()

        def rollback(self) -> None:
            self.rollbacks += 1
            self._inner.rollback()

        def close(self) -> None:
            self.closes += 1
            self._inner.close()

    def _factory():
        tracking = _Tracking(Session(engine))
        created.append(tracking)
        return tracking

    entry = SimpleNamespace(
        id=uuid.uuid4(),
        payload={"transaction_id": str(tx.id), "user_id": str(USER_A)},
        created_at=datetime.now(UTC),
    )
    assert on_profile_transfer_settled(entry, session_factory=_factory) is True
    assert len(created) == 1
    assert created[0].commits == 1 and created[0].rollbacks == 0 and created[0].closes == 1

    check = Session(engine)
    try:
        assert _profile(check).avg_amount_minor == 10_000
    finally:
        check.close()


def test_handler_reversed_uses_own_session_with_commit(session, engine):
    """R7: `on_profile_transfer_reversed` abre sesion propia con `commit` (D-04)."""
    from types import SimpleNamespace

    from app.modules.risk import service as risk_service
    from app.modules.risk.events import on_profile_transfer_reversed

    first = _transfer(session, 10_000, device_id=DEVICE_A, metadata={"device_id": DEVICE_A})
    session.commit()
    risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(first.id)}
    )
    session.commit()
    second = _transfer(session, 30_000)
    session.commit()
    risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(second.id)}
    )
    session.commit()
    second.status = "REVERSED"
    session.commit()
    session.expire_all()

    entry = SimpleNamespace(
        id=uuid.uuid4(),
        payload={"transaction_id": str(second.id), "user_id": str(USER_A)},
        created_at=datetime.now(UTC),
    )
    assert on_profile_transfer_reversed(entry, session_factory=lambda: Session(engine)) is True

    check = Session(engine)
    try:
        assert _profile(check).avg_amount_minor == 10_000, "el reverso sale de la ventana"
        assert _profile(check).last_device_id == DEVICE_A, "sin habitos"
    finally:
        check.close()


def test_handler_revierte_y_relanza_ante_fallo():
    from types import SimpleNamespace

    from app.modules.risk.events import on_profile_login_succeeded

    states: dict = {}

    class _Failing:
        def commit(self):
            states["commit"] = True

        def rollback(self):
            states["rollback"] = True

        def close(self):
            states["close"] = True

        def begin_nested(self):
            raise RuntimeError("BD caida")

    entry = SimpleNamespace(
        id=uuid.uuid4(),
        payload={"user_id": str(USER_A), "device_id": DEVICE_A},
        created_at=datetime.now(UTC),
    )
    with pytest.raises(RuntimeError):
        on_profile_login_succeeded(entry, session_factory=_Failing)
    assert states.get("rollback") is True and states.get("close") is True


# ------------------------------------------------------------- end-to-end


def test_e2e_outbox_bus_profile_get(session, engine, monkeypatch, full_client):
    """Bus/outbox -> perfil -> GET: `settled` alimenta, el GET lo expone y audita."""
    from functools import partial

    from app.composition import run_outbox_once
    from app.core.bus import EventBus
    from app.modules.risk import events as risk_events
    from app.modules.shared import repository as shared_repo

    tx = _transfer(session, 30_000, device_id=DEVICE_A, metadata={"device_id": DEVICE_A})
    session.commit()
    shared_repo.insert_event(
        session,
        aggregate_type="transactions",
        aggregate_id=tx.id,
        event_type="transfer.settled",
        payload={"transaction_id": str(tx.id)},
    )
    session.commit()

    bus = EventBus()

    def _factory():
        return Session(engine)

    bus.subscribe(
        "transfer.settled",
        partial(risk_events.on_profile_transfer_settled, session_factory=_factory),
    )
    result = run_outbox_once(session_factory=_factory, bus=bus)
    assert result["published"] >= 1 and result["failed"] == 0

    check = Session(engine)
    try:
        assert _profile(check).avg_amount_minor == 30_000
    finally:
        check.close()

    resp = full_client.get(
        f"/api/v1/risk/profiles/{USER_A}", headers=_bearer(uuid.uuid4(), ["fraud_analyst"])
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["avg_amount_minor"] == 30_000
    assert data["avg_amount_by_currency"] == {"PEN": 30_000}
    assert data["usual_hours"] == [tx.created_at.hour]
    assert data["last_device_id"] == DEVICE_A


# ------------------------------------------------------------- sin PII/dinero


def test_no_pii_in_outcomes(session: Session):
    from app.modules.risk import service as risk_service

    tx = _transfer(session, 10_000)
    session.commit()
    outcome = risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(tx.id)}
    )
    session.commit()
    blob = str(outcome)
    for secret in ("Jheampierre", "0011", "RENEC"):
        assert secret not in blob


def test_no_money_movement_on_rejected_feed(session: Session):
    from app.modules.risk import service as risk_service

    before_balances = _balances(session)
    tx = _transfer(session, 10_000)
    session.commit()
    after_transfer = _balances(session)
    assert after_transfer != before_balances, "el motor si movio dinero"

    risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(tx.id)}
    )
    session.commit()
    assert _balances(session) == after_transfer, "el perfil no mueve dinero"
