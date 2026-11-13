"""Regresion PR #24 (E6-T02 corte 2): arranque + `failure_code` HTTP.

- DEFECTO 1 (ALTA): `subscribe_risk_antifraud()` existia y su test unitario
  pasaba, pero el `lifespan` de `app.main` nunca lo llamaba: el bus global
  no registraba `on_transaction_initiated`/`on_transfer_settled` y los
  eventos del outbox se marcaban `PUBLISHED` sin entregarse.
  `test_lifespan_subscribes_antifraud_consumers` ejecuta el arranque real
  (`TestClient`, que corre el `lifespan`) con el bus global limpio y
  verifica que quedan suscritos los consumidores de `transaction.initiated`
  y `transfer.settled`. Falla sin el fix (bus sin handlers), pasa con el.
- DEFECTO 2 (ALTA/MEDIA): `_rejected_failure_code` solo reconocia
  `DAILY_LIMIT_EXCEEDED`/`BIOMETRIC_PREVENTIVE_BLOCK`: un bloqueo
  antifraude por HTTP se reportaba como `INSUFFICIENT_FUNDS`.
  `test_http_risk_block_reports_risk_blocked` fuerza una regla
  `UNUSUAL_AMOUNT=BLOCK` y hace `POST /transfers/third-party` a nivel API:
  verifica `201` + `data.status=REJECTED` +
  `data.failure_code=RISK_BLOCKED` sin mover saldos ni crear asientos.
  Falla sin el fix (`INSUFFICIENT_FUNDS`), pasa con el.

Sobre SQLite en memoria + schemas ATTACH, sin red, sin `float`, sin PII:
dinero de juguete (UUIDs sinteticos).
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()

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
    "shared.idempotency_keys",
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
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    return engine


def _disable_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apaga todos los workers del `lifespan` (el test solo mira el cableado)."""
    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "0")
    monkeypatch.setenv("RECONCILIATION_INGEST_ENABLED", "0")


# ------------------------------------------------- DEFECTO 1: arranque


def test_lifespan_subscribes_antifraud_consumers(monkeypatch):
    """El arranque suscribe la reevaluacion antifraude al bus global."""
    from app.composition import is_risk_antifraud_wired
    from app.core.bus import get_default_bus
    from app.main import app

    _disable_workers(monkeypatch)

    bus = get_default_bus()
    saved = {event_type: list(handlers) for event_type, handlers in bus._handlers.items()}
    bus.clear()
    try:
        assert is_risk_antifraud_wired() is False
        with TestClient(app):
            pass
        assert is_risk_antifraud_wired() is True
        assert bus.handler_count("transaction.initiated") >= 1
        assert bus.handler_count("transfer.settled") >= 1
    finally:
        bus.clear()
        for event_type, handlers in saved.items():
            for handler in handlers:
                bus.subscribe(event_type, handler)


# ------------------------------------------------- DEFECTO 2: HTTP


@pytest.fixture()
def svc_session():
    """Sesion SQLite aislada con cuentas fondeadas + reglas conservadoras."""
    from app.modules.accounts import repository as accounts_repo
    from app.modules.risk import service as risk_service

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        a1 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="00330001111", type="AHORRO", currency="PEN"
        )
        b1 = accounts_repo.create_account(
            session, user_id=USER_B, account_number="00440003333", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a1.id, available_delta=200_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, b1.id, available_delta=5_000, held_delta=0, currency="PEN"
        )
        for code, name, priority, params in [
            ("FACIAL_SPOOFING", "Suplantacion", 10, {"score_bump": 25}),
            ("ATYPICAL_GEO", "Geo atipica", 20, {"mode": "country_mismatch", "score_bump": 10}),
            ("UNUSUAL_AMOUNT", "Monto inusual", 30, {"factor": 3.0, "min_amount_minor": 10000}),
            ("VELOCITY", "Velocidad", 40, {"max_operations": 10, "window_seconds": 600}),
            ("NEW_DEVICE", "Dispositivo nuevo", 50, {"score_bump": 5}),
        ]:
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
        yield {"session": session, "a1": a1.id, "b1": b1.id}
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def http_client(svc_session, monkeypatch):
    """TestClient con BD de `svc_session` y auth falsa como USER_A."""
    from app.core.db import get_db
    from app.main import app
    from app.modules.transactions.api import get_current_user_id

    _disable_workers(monkeypatch)
    session = svc_session["session"]

    def _override_db():
        yield session

    def _override_user():
        return USER_A

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = _override_user
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)


def _avail(session, account_id) -> int:
    from app.modules.accounts import repository as accounts_repo

    return int(accounts_repo.get_balance(session, account_id).available_minor)


def test_http_risk_block_reports_risk_blocked(http_client, svc_session):
    """Un `BLOCK` antifraude por HTTP responde `RISK_BLOCKED` (no fondos)."""
    from app.modules.risk import service as risk_service

    session = svc_session["session"]
    a1, b1 = svc_session["a1"], svc_session["b1"]

    risk_service.update_behavior(
        session,
        USER_A,
        avg_amount_minor=10_000,
        usual_countries=["PE"],
        last_device_id="dev-known-pr24",
    )
    session.commit()
    risk_service.update_rule(session, "UNUSUAL_AMOUNT", actor=USER_A, action="BLOCK")
    session.commit()

    before_a1, before_b1 = _avail(session, a1), _avail(session, b1)

    body = {
        "source_account_id": str(a1),
        "target_account_id": str(b1),
        "amount_minor": 50_000,
        "currency": "PEN",
    }
    response = http_client.post(
        "/api/v1/transfers/third-party",
        json=body,
        headers={"Idempotency-Key": f"pr24-risk-block-{uuid.uuid4()}"},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["status"] == "REJECTED"
    assert data["failure_code"] == "RISK_BLOCKED"

    session.expire_all()
    assert _avail(session, a1) == before_a1  # saldos intactos
    assert _avail(session, b1) == before_b1
    from app.modules.ledger.models import JournalEntry

    assert session.scalar(sa.select(sa.func.count()).select_from(JournalEntry)) == 0
