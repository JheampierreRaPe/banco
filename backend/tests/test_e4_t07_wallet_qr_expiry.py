"""Job y validacion on-read de caducidad del QR (E4-T07, HU14 CA-02).

- Job `run_qr_expiry_once`: los `ACTIVE` con `expires_at <= now`
  (borde exacto incluido) pasan a `EXPIRED`; los vigentes quedan
  intactos; `PAID`/`EXPIRED`/`CANCELLED` y `STATIC` (`expires_at
  NULL`) nunca se reprocesan; dos pasadas no duplican efectos.
- On-read `is_charge_valid` / `is_charge_expired`: un vencido es
  invalido aunque el job no haya corrido (doble defensa para
  `E4-T08` y el pago `HU15`); no mueve dinero.
- Wiring (patron `E3-T19`/M-2): el `lifespan` arranca el worker con
  el flag encendido y no con el apagado; `worker_entrypoint`
  ejecuta pasadas con flags on y queda inerte con flags off;
  `/health` intacto (cubierto por `test_e3_t19b`, aqui no se duplica).

SQLite en memoria + schemas ATTACH (sin red); `now` inyectable. Sin
PII/secretos en logs ni asserts.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.modules.audit.models as _audit
import app.modules.shared.models as _shared
import app.modules.wallet.models as _wallet
from app.core.db import Base
from app.modules.shared.config import parameters as params
from app.modules.wallet.domain import qr as qr_domain
from app.modules.wallet.jobs import qr_expiry as qr_expiry_job
from app.modules.wallet.repository import qr as qr_repo
from app.modules.wallet.service import qr as qr_service

assert _audit is not None and _shared is not None and _wallet is not None

SECRET = "test-qr-expiry-secret"
RUC_PLAIN = "20123456789"

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)

TABLES = [
    "wallet.merchant_accounts",
    "wallet.qr_charges",
    "shared.outbox",
    "audit.audit_log",
]

SCHEMAS = ("wallet", "shared", "config", "audit")


def _seed_param(session: Session, key: str, value: str, module: str = "wallet") -> None:
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, :module)"
        ),
        {"id": uuid.uuid4().hex, "key": key, "value": value, "module": module},
    )
    session.flush()


@pytest.fixture()
def env():
    """QR con caducidad en SQLite: comercios demo + TTL 300 s."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

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
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        _seed_param(session, params.WALLET_QR_TTL_SECONDS_KEY, "300")
        merchants = qr_repo.ensure_demo_merchants(session)
        session.commit()
        yield {"session": session, "engine": engine, "merchants": merchants}
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def _create(session, merchant, *, now, mode="DYNAMIC", amount_minor=1_000):
    return qr_service.create_qr_charge(
        session,
        merchant_id=merchant.id,
        amount_minor=amount_minor,
        currency="PEN",
        concept="Cobro QR",
        mode=mode,
        now=now,
        secret=SECRET,
    )


# ------------------------------------------------------- job: camino feliz


def test_job_expires_overdue_and_keeps_valid(env):
    session = env["session"]
    merchant = env["merchants"][0]
    overdue = _create(session, merchant, now=NOW - timedelta(seconds=600))
    valid = _create(session, merchant, now=NOW)
    session.commit()

    result = qr_expiry_job.run_qr_expiry_once(session, now=NOW)

    assert result["expired"] == [overdue["charge_id"]]
    assert result["skipped"] == []
    assert datetime.fromisoformat(result["as_of"]) == NOW
    assert qr_repo.get_qr_charge(session, overdue["charge_id"]).status == "EXPIRED"
    assert qr_repo.get_qr_charge(session, valid["charge_id"]).status == "ACTIVE"


def test_job_expiry_exact_boundary(env):
    session = env["session"]
    merchant = env["merchants"][0]
    # `expires_at` cae exactamente en `now`: ya expiro (`moment >= limit`).
    edge = _create(session, merchant, now=NOW - timedelta(seconds=300))
    row = qr_repo.get_qr_charge(session, edge["charge_id"])
    assert row is not None and row.expires_at is not None
    stored = row.expires_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=UTC)
    assert stored == NOW

    result = qr_expiry_job.run_qr_expiry_once(session, now=NOW)

    assert result["expired"] == [edge["charge_id"]]
    assert qr_repo.get_qr_charge(session, edge["charge_id"]).status == "EXPIRED"


def test_job_preserves_terminal_states(env):
    session = env["session"]
    merchant = env["merchants"][0]
    paid = _create(session, merchant, now=NOW - timedelta(seconds=900))
    cancelled = _create(session, merchant, now=NOW - timedelta(seconds=900))
    already = _create(session, merchant, now=NOW - timedelta(seconds=900))
    paid_row = qr_repo.get_qr_charge(session, paid["charge_id"])
    cancelled_row = qr_repo.get_qr_charge(session, cancelled["charge_id"])
    expired_row = qr_repo.get_qr_charge(session, already["charge_id"])
    assert paid_row is not None and cancelled_row is not None and expired_row is not None
    qr_repo.mark_paid(session, paid_row, paid_at=NOW - timedelta(seconds=800))
    qr_repo.mark_cancelled(session, cancelled_row)
    qr_repo.mark_expired(session, expired_row)
    session.commit()

    result = qr_expiry_job.run_qr_expiry_once(session, now=NOW)

    assert result["expired"] == []
    assert result["skipped"] == []
    assert qr_repo.get_qr_charge(session, paid["charge_id"]).status == "PAID"
    assert qr_repo.get_qr_charge(session, cancelled["charge_id"]).status == "CANCELLED"
    assert qr_repo.get_qr_charge(session, already["charge_id"]).status == "EXPIRED"


def test_job_never_expires_static_without_expiry(env):
    session = env["session"]
    merchant = env["merchants"][1]
    view = _create(session, merchant, now=NOW - timedelta(days=30), mode="STATIC")
    assert view["expires_at"] is None
    session.commit()

    result = qr_expiry_job.run_qr_expiry_once(session, now=NOW + timedelta(days=365))

    assert result["expired"] == []
    assert qr_repo.get_qr_charge(session, view["charge_id"]).status == "ACTIVE"


def test_job_idempotent(env):
    session = env["session"]
    merchant = env["merchants"][0]
    overdue = _create(session, merchant, now=NOW - timedelta(seconds=600))
    session.commit()

    first = qr_expiry_job.run_qr_expiry_once(session, now=NOW)
    second = qr_expiry_job.run_qr_expiry_once(session, now=NOW)

    assert first["expired"] == [overdue["charge_id"]]
    assert second == {"expired": [], "skipped": [], "as_of": first["as_of"]}
    assert qr_repo.get_qr_charge(session, overdue["charge_id"]).status == "EXPIRED"


def test_job_rejects_bad_limit(env):
    session = env["session"]
    for bad in (0, -1, True, "100"):
        with pytest.raises(ValueError):
            qr_expiry_job.run_qr_expiry_once(session, now=NOW, limit=bad)
    session.rollback()


def test_list_expired_charges_selector(env):
    session = env["session"]
    merchant = env["merchants"][0]
    overdue = _create(session, merchant, now=NOW - timedelta(seconds=600))
    _create(session, merchant, now=NOW)
    session.flush()

    found = qr_repo.list_expired_charges(session, NOW)

    assert [str(row.id) for row in found] == [overdue["charge_id"]]
    with pytest.raises(ValueError):
        qr_repo.list_expired_charges(session, NOW, limit=0)
    session.rollback()


# ------------------------------------------------------- on-read


def test_on_read_validity(env):
    session = env["session"]
    merchant = env["merchants"][0]
    valid = _create(session, merchant, now=NOW)
    overdue = _create(session, merchant, now=NOW - timedelta(seconds=600))
    edge = _create(session, merchant, now=NOW - timedelta(seconds=300))
    valid_row = qr_repo.get_qr_charge(session, valid["charge_id"])
    overdue_row = qr_repo.get_qr_charge(session, overdue["charge_id"])
    edge_row = qr_repo.get_qr_charge(session, edge["charge_id"])
    assert valid_row is not None and overdue_row is not None and edge_row is not None

    # Vigente: valido; vencido o borde exacto: invalido sin esperar al job.
    assert qr_service.is_charge_valid(valid_row, NOW) is True
    assert qr_service.is_charge_valid(overdue_row, NOW) is False
    assert qr_service.is_charge_valid(edge_row, NOW) is False
    assert qr_service.is_charge_expired(edge_row, NOW - timedelta(seconds=1)) is False
    assert qr_service.is_charge_expired(edge_row, NOW) is True
    # Acepta dicts (puerta para `E4-T08`/`HU15`).
    assert qr_service.is_charge_valid({"status": "ACTIVE", "expires_at": None}, NOW) is True
    assert (
        qr_service.is_charge_valid(
            {"status": "ACTIVE", "expires_at": NOW - timedelta(seconds=1)}, NOW
        )
        is False
    )
    session.rollback()


def test_on_read_rejects_non_active(env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _create(session, merchant, now=NOW)
    row = qr_repo.get_qr_charge(session, view["charge_id"])
    assert row is not None
    for status in ("PAID", "EXPIRED", "CANCELLED"):
        row.status = status
        assert qr_service.is_charge_valid(row, NOW) is False
    assert qr_service.is_charge_valid(None, NOW) is False
    assert qr_service.is_charge_valid({"status": "UNKNOWN", "expires_at": None}, NOW) is False
    session.rollback()


def test_expired_charge_cannot_be_used_after_job(env):
    session = env["session"]
    merchant = env["merchants"][0]
    overdue = _create(session, merchant, now=NOW - timedelta(seconds=600))
    session.commit()

    assert (
        qr_service.is_charge_valid(qr_repo.get_qr_charge(session, overdue["charge_id"]), NOW)
        is False
    )
    qr_expiry_job.run_qr_expiry_once(session, now=NOW)
    row = qr_repo.get_qr_charge(session, overdue["charge_id"])
    assert row is not None and row.status == "EXPIRED"
    assert qr_service.is_charge_valid(row, NOW) is False


def test_job_logs_without_pii(env, caplog):
    session = env["session"]
    merchant = env["merchants"][0]
    _create(session, merchant, now=NOW - timedelta(seconds=600))
    assert qr_domain.hash_tax_id(RUC_PLAIN)  # el RUC solo existe hasheado
    session.commit()
    with caplog.at_level(logging.INFO, logger="app.modules.wallet"):
        result = qr_expiry_job.run_qr_expiry_once(session, now=NOW)
    assert len(result["expired"]) == 1
    assert SECRET not in caplog.text
    assert RUC_PLAIN not in caplog.text
    assert merchant.business_name not in caplog.text


# ------------------------------------------------------- runner (composition)


def test_runner_respects_flags(env, monkeypatch):
    from app import composition

    engine = env["engine"]
    merchant_id = env["merchants"][0].id
    factory_calls: list[str] = []

    def _factory():
        factory_calls.append("open")
        return Session(bind=engine, autoflush=False, expire_on_commit=False)

    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_EXPIRY_ENABLED", "1")
    monkeypatch.setenv("WALLET_QR_RECONCILE_ENABLED", "1")
    skipped = composition.run_wallet_qr_jobs_once(session_factory=_factory, now=NOW)
    assert skipped == {
        "qr_reconcile": {"skipped": "disabled"},
        "qr_expiry": {"skipped": "disabled"},
    }
    assert factory_calls == ["open"]

    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "1")
    monkeypatch.setenv("WALLET_QR_EXPIRY_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_RECONCILE_ENABLED", "0")
    skipped = composition.run_wallet_qr_jobs_once(session_factory=_factory, now=NOW)
    assert skipped == {
        "qr_reconcile": {"skipped": "disabled"},
        "qr_expiry": {"skipped": "disabled"},
    }

    monkeypatch.setenv("WALLET_QR_EXPIRY_ENABLED", "1")
    seed = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        view = qr_service.create_qr_charge(
            seed,
            merchant_id=merchant_id,
            amount_minor=1_000,
            currency="PEN",
            now=NOW - timedelta(seconds=600),
            secret=SECRET,
        )
        seed.commit()
    finally:
        seed.close()
    ran = composition.run_wallet_qr_jobs_once(session_factory=_factory, now=NOW)
    assert ran["qr_expiry"]["expired"] == [view["charge_id"]]

    check = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        assert qr_repo.get_qr_charge(check, view["charge_id"]).status == "EXPIRED"
    finally:
        check.close()


def test_runner_interval_defaults_and_parsing(monkeypatch):
    from app import composition

    monkeypatch.delenv("WALLET_QR_JOBS_INTERVAL_SECONDS", raising=False)
    assert composition.wallet_qr_jobs_interval_seconds() == 60.0
    monkeypatch.setenv("WALLET_QR_JOBS_INTERVAL_SECONDS", "15")
    assert composition.wallet_qr_jobs_interval_seconds() == 15.0
    monkeypatch.setenv("WALLET_QR_JOBS_INTERVAL_SECONDS", "no-numerico")
    assert composition.wallet_qr_jobs_interval_seconds() == 60.0
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    assert composition.wallet_qr_jobs_enabled() is False
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "1")
    assert composition.wallet_qr_jobs_enabled() is True


# ------------------------------------------------------- wiring runtime


def test_lifespan_starts_wallet_qr_jobs_worker_when_enabled(monkeypatch):
    from app import composition
    from app.main import app

    calls: list[dict] = []
    stop_events: list[threading.Event] = []

    def _fake_start_qr(*, interval_seconds=None, stop_event=None, **kwargs):
        event = stop_event or threading.Event()
        stop_events.append(event)
        calls.append({"interval_seconds": interval_seconds, "kwargs": kwargs})
        return object(), event

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "1")
    monkeypatch.setenv("WALLET_QR_JOBS_INTERVAL_SECONDS", "45")
    monkeypatch.setattr(composition, "start_wallet_qr_jobs_worker", _fake_start_qr)

    from fastapi.testclient import TestClient

    with TestClient(app):
        pass

    assert len(calls) == 1
    assert calls[0]["interval_seconds"] == 45.0
    assert calls[0]["kwargs"] == {}
    assert len(stop_events) == 1 and stop_events[0].is_set()


def test_lifespan_does_not_start_wallet_qr_jobs_when_disabled(monkeypatch):
    from app import composition
    from app.main import app

    def _fake_start_qr(*args, **kwargs):
        raise AssertionError("jobs inertes con WALLET_QR_JOBS_ENABLED=0")

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    monkeypatch.setattr(composition, "start_wallet_qr_jobs_worker", _fake_start_qr)

    from fastapi.testclient import TestClient

    with TestClient(app):
        pass


def _load_entrypoint():
    import importlib.machinery
    import importlib.util
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "infra"
        / "docker"
        / "worker_entrypoint.py"
    )
    loader = importlib.machinery.SourceFileLoader("worker_entrypoint", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _wait_until(predicate, timeout_seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_entrypoint_runs_qr_pass_when_enabled(monkeypatch):
    from app import composition

    entrypoint = _load_entrypoint()
    ran = threading.Event()
    calls: list[dict] = []

    def _fake_run_once(**kwargs):
        calls.append(kwargs)
        ran.set()
        return {"qr_expiry": {"expired": [], "skipped": [], "as_of": NOW.isoformat()}}

    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "1")
    monkeypatch.setattr(composition, "run_wallet_qr_jobs_once", _fake_run_once)

    thread, stop = entrypoint.maybe_start_wallet_qr_jobs(interval_seconds=0.05)
    try:
        assert thread is not None and stop is not None
        assert getattr(thread, "daemon", False) is True
        assert _wait_until(ran.is_set), "el worker no ejecuto ninguna pasada"
        assert calls
    finally:
        stop.set()
        thread.join(timeout=5.0)


def test_entrypoint_qr_inert_when_disabled(monkeypatch):
    from app import composition

    entrypoint = _load_entrypoint()

    def _fake_run_once(**kwargs):  # pragma: no cover - no debe ejecutarse
        raise AssertionError("jobs inertes con WALLET_QR_JOBS_ENABLED=0")

    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    monkeypatch.setattr(composition, "run_wallet_qr_jobs_once", _fake_run_once)

    thread, stop = entrypoint.maybe_start_wallet_qr_jobs(interval_seconds=0.05)
    assert (thread, stop) == (None, None)
    time.sleep(0.2)
