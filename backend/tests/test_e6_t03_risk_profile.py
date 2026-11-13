"""Perfil de comportamiento alimentado por historial (E6-T03, HU20).

Sobre SQLite en memoria + schemas ATTACH, sin red, sin `float`, sin PII:
dinero de juguete (UUIDs sinteticos).

Mapeo brief -> test:
- dominio (media incremental, redondeo, horas/paises, topes) `test_domain_*`
- consumidores (cada evento, replay, rollback) .............. `test_feed_*`
- ventana configurable `risk.profile_window_days` .......... `test_window_*`
- endpoint (existente/inexistente/RBAC) ..................... `test_endpoint_*`
- integracion con el motor E6-T02 .......................... `test_engine_*`
- cableado del bus + flags ................................. `test_wiring_*`
- handlers D-04 (sesion/commit propios, rollback) ........... `test_handler_*`
- sin PII / sin movimiento de dinero ........................ `test_no_*`
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


def _set_param(session: Session, key: str, value: str) -> None:
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, :module)"
        ),
        {"id": uuid.uuid4().hex, "key": key, "value": value, "module": "risk"},
    )
    session.flush()


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


def test_feed_initiated_updates_profile_without_moving_money(session: Session):
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

    assert outcome["consumed"] is True and outcome["source"] == "transaction.initiated"
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


def test_feed_settled_reads_payload_user_and_country(session: Session):
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
        },
        source="transfer.settled",
    )
    session.commit()

    assert outcome["consumed"] is True and outcome["source"] == "transfer.settled"
    row = _profile(session)
    assert row.avg_amount_minor == 20_000
    assert row.usual_countries == ["CL"]
    assert row.last_device_id == DEVICE_B


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


def test_feed_missing_transaction_marks_processed(session: Session):
    from app.modules.risk import service as risk_service

    event_id = uuid.uuid4()
    outcome = risk_service.handle_profile_transaction(
        session, event_id=event_id, payload={"transaction_id": str(uuid.uuid4())}
    )
    session.commit()
    assert outcome == {"consumed": True, "reason": "missing-transaction"}
    again = risk_service.handle_profile_transaction(
        session, event_id=event_id, payload={"transaction_id": str(uuid.uuid4())}
    )
    session.commit()
    assert again == {"consumed": False, "reason": "duplicate"}
    assert _profile(session) is None, "sin operacion no se crea perfil"


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
    assert row.usual_hours == [15] and row.usual_countries == ["PE"]
    assert row.last_device_id == DEVICE_B and row.score == 0


def test_feed_login_without_identifiers_marks_processed(session: Session):
    from app.modules.risk import service as risk_service

    outcome = risk_service.handle_profile_login(
        session, event_id=uuid.uuid4(), payload={"device_id": DEVICE_A}
    )
    session.commit()
    assert outcome == {"consumed": True, "reason": "missing-user-id"}


def test_feed_failed_attempt_complements_without_severity(session: Session):
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
    assert row.usual_hours == [3] and row.last_device_id == DEVICE_B


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


# -------------------------------------------------------------------- ventana


def test_profile_window_days_defaults_and_reads_config(session: Session):
    from app.modules.risk import service as risk_service

    assert risk_service.profile_window_days(session) == 90, "default del brief"
    _set_param(session, risk_service.PROFILE_WINDOW_KEY, "1")
    session.commit()
    assert risk_service.profile_window_days(session) == 1, "configurable sin codigo"
    _set_param(session, "other.key", "x")
    session.execute(
        sa.text("UPDATE config.parameters SET value_json = :v WHERE key = :k"),
        {"v": "no-entero", "k": risk_service.PROFILE_WINDOW_KEY},
    )
    session.commit()
    assert risk_service.profile_window_days(session) == 90, "invalido -> fallback"


def test_window_bounds_the_average(session: Session):
    from app.modules.risk import service as risk_service

    old = _transfer(session, 10_000)
    session.commit()
    old.created_at = datetime.now(UTC) - timedelta(days=5)
    session.commit()
    new = _transfer(session, 20_000)
    session.commit()

    # La vieja funda el promedio (10_000); la ventana de 1 dia la
    # excluye del conteo, asi que la nueva lo reemplaza (ops = 0).
    risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(old.id)}
    )
    session.commit()
    assert _profile(session).avg_amount_minor == 10_000
    _set_param(session, risk_service.PROFILE_WINDOW_KEY, "1")
    session.commit()
    outcome = risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(new.id)}
    )
    session.commit()
    assert outcome["avg_amount_minor"] == 20_000, "ventana de 1 dia excluye la vieja"


def test_default_window_covers_both_operations(session: Session):
    from app.modules.risk import service as risk_service

    first = _transfer(session, 10_000)
    session.commit()
    new = _transfer(session, 20_000)
    session.commit()

    # Cada operacion alimenta una vez y en orden: la primera funda el
    # promedio (10_000) y la segunda lo promedia ((10000+20000)/2).
    risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(first.id)}
    )
    session.commit()
    outcome = risk_service.handle_profile_transaction(
        session, event_id=uuid.uuid4(), payload={"transaction_id": str(new.id)}
    )
    session.commit()
    assert outcome["avg_amount_minor"] == 15_000, "(10000+20000)/2 con ventana de 90 dias"


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
        "usual_countries",
        "usual_hours",
        "last_device_id",
        "score",
        "updated_at",
    }
    assert data["user_id"] == str(holder)
    assert data["avg_amount_minor"] == 25_000
    assert data["usual_countries"] == ["PE", "CL"]
    assert data["usual_hours"] == [8, 20]
    assert data["last_device_id"] == DEVICE_A
    assert data["score"] == 3 and data["updated_at"] is not None
    blob = resp.text
    for secret in ("Jheampierre", "0011", "RENEC"):
        assert secret not in blob


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


def test_wiring_subscribes_all_profile_consumers():
    from app.composition import is_risk_profile_wired, subscribe_risk_profile
    from app.core.bus import EventBus
    from app.modules.risk.events import LOGIN_SUCCEEDED_EVENT

    bus = EventBus()
    assert is_risk_profile_wired(bus) is False
    handlers = subscribe_risk_profile(bus)
    assert len(handlers) == 4
    assert is_risk_profile_wired(bus) is True
    assert bus.handler_count("transaction.initiated") == 1
    assert bus.handler_count("transfer.settled") == 1
    assert bus.handler_count(LOGIN_SUCCEEDED_EVENT) == 1
    assert bus.handler_count("auth.failed_attempt") == 1
    subscribe_risk_profile(bus)  # idempotente
    assert bus.handler_count("transaction.initiated") == 1
    assert bus.handler_count("auth.failed_attempt") == 1


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
