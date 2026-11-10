"""Job de recordatorios de vencimiento de servicios (E4-T18, HU16 CA-03).

- Camino feliz: suscripcion con vencimiento derivado en la ventana
  (`hoy + 3`) recibe un aviso `service_reminder`; fuera de ventana no
  hay aviso.
- Idempotencia: dos pasadas el mismo dia -> un solo aviso (marca en
  `processed_events`, consumidor `wallet-service-reminders`).
- Parametro: `set_parameter` de `wallet.service_reminder_days_before`
  cambia el resultado sin redespliegue; el `notify_days_before` de la
  fila adelanta la ventana (minimo entre ambos).
- Wiring: arranque/no-arranque en `lifespan` y `worker` segun flags;
  `/health` del worker intacto; intervalo por entorno.
- Sin PII en el `data` de la notificacion ni en logs.
- Estructural: `alembic heads` = 1 (`0043_wallet_service_reminder`,
  archivo `0043_wallet_service_reminder_param.py`,
  `down_revision="0042_wallet_services"`) y `downgrade` reversible.

SQLite en memoria + schemas ATTACH (sin red, sin `float`).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import date, timedelta
from http.server import HTTPServer

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.modules.notifications.models as _notifications
import app.modules.shared.models as _shared
import app.modules.wallet.models as _wallet
from app.adapters.notification_sender import MockNotificationSender
from app.core.db import Base
from app.modules.shared.config import parameters as params
from app.modules.wallet.jobs.service_reminders import (
    REMINDER_TEMPLATE_CODE,
    REMINDERS_CONSUMER,
    next_due_date,
    run_service_reminders_once,
    subscription_due_day,
)
from app.modules.wallet.repository import services as services_repo

assert _notifications is not None and _shared is not None and _wallet is not None

USER = uuid.uuid4()
LUZ_ID = "c1111111-1111-4111-8111-111111111111"
AGUA_ID = "c2222222-2222-4222-8222-222222222222"
TODAY = date(2026, 10, 13)

TABLES = [
    "wallet.billers",
    "wallet.service_payments",
    "wallet.topups",
    "wallet.service_subscriptions",
    "shared.processed_events",
    "notifications.notification_templates",
    "notifications.notifications",
]

SCHEMAS = ("wallet", "shared", "notifications", "config")


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
def db():
    """SQLite con `billers` demo + `config.parameters` + tablas de avisos."""
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
        services_repo.ensure_demo_billers(session)
        session.commit()
        yield engine, session
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001, S110 - limpieza best-effort
            pass
        engine.dispose()


def _make_sub_with_due(session, today, window, *, notify=3, biller_id=LUZ_ID, tag="SUP"):
    """Crea suscripciones hasta que una vence en `today + window` (derivado)."""
    for attempt in range(500):
        row, _ = services_repo.create_subscription(
            session,
            user_id=USER,
            biller_id=biller_id,
            supply_ref=f"{tag}-{attempt:04d}-{uuid.uuid4().hex[:6]}",
            notify_days_before=notify,
        )
        session.flush()
        if next_due_date(row.id, today) == today + timedelta(days=window):
            session.commit()
            return row
        session.rollback()
    raise AssertionError(f"sin suscripcion con vencimiento en +{window} tras 500 intentos")


def _make_sub_outside_window(session, today, window, **kwargs):
    """Crea una suscripcion cuyo vencimiento NO cae en `today + window`."""
    for attempt in range(500):
        row, _ = services_repo.create_subscription(
            session,
            user_id=USER,
            biller_id=kwargs.get("biller_id", AGUA_ID),
            supply_ref=f"OUT-{attempt:04d}-{uuid.uuid4().hex[:6]}",
            notify_days_before=kwargs.get("notify", 3),
        )
        session.flush()
        if next_due_date(row.id, today) != today + timedelta(days=window):
            session.commit()
            return row
        session.rollback()
    raise AssertionError("sin suscripcion fuera de ventana tras 500 intentos")


def _notifications_rows(session):
    return list(session.scalars(sa.select(_notifications.Notification)).all())


# ------------------------------------------------------- derivacion pura


def test_due_derivation_deterministic_and_bounded():
    assert subscription_due_day(uuid.UUID(int=0)) == 1
    assert subscription_due_day(uuid.UUID(int=27)) == 28
    assert subscription_due_day(uuid.UUID(int=28)) == 1
    sid = uuid.uuid4()
    assert subscription_due_day(sid) == subscription_due_day(sid)
    assert 1 <= subscription_due_day(sid) <= 28
    # Dia 1 con hoy 13: la proxima ocurrencia es el mes siguiente.
    assert next_due_date(uuid.UUID(int=0), TODAY) == date(2026, 11, 1)
    for _ in range(50):
        due = next_due_date(uuid.uuid4(), TODAY)
        assert due >= TODAY


# ------------------------------------------------------- camino feliz


def test_window_exact_notifies_only_inside(db):
    _, session = db
    sender = MockNotificationSender()
    sub_in = _make_sub_with_due(session, TODAY, 3)
    sub_out = _make_sub_outside_window(session, TODAY, 3)

    result = run_service_reminders_once(session, now=TODAY, sender=sender)

    assert result["days_before"] == 3
    assert result["notified"] == [str(sub_in.id)]
    assert str(sub_out.id) in result["skipped"]
    assert result["failed"] == []
    rows = _notifications_rows(session)
    assert len(rows) == 1
    assert rows[0].template_code == REMINDER_TEMPLATE_CODE
    assert rows[0].channel == "push"


def test_notification_data_without_pii(db):
    _, session = db
    supply = "10293847"
    # Ventana arbitraria 1..30: `notify=30` para que el minimo con el
    # global sea el global sembrado.
    row = None
    window = 0
    for attempt in range(500):
        candidate, _ = services_repo.create_subscription(
            session,
            user_id=USER,
            biller_id=LUZ_ID,
            supply_ref=f"{supply}-{attempt:04d}",
            notify_days_before=30,
        )
        session.flush()
        due_tmp = next_due_date(candidate.id, TODAY)
        window_tmp = (due_tmp - TODAY).days
        if 1 <= window_tmp <= 30:
            row, window = candidate, window_tmp
            break
        session.rollback()
    assert row is not None
    session.commit()
    due = next_due_date(row.id, TODAY)
    assert (due - TODAY).days == window
    params_key = params.WALLET_SERVICE_REMINDER_DAYS_BEFORE_KEY
    _seed_param(session, params_key, str(window))
    session.commit()

    run_service_reminders_once(session, now=TODAY, sender=MockNotificationSender())

    rows = _notifications_rows(session)
    assert len(rows) == 1
    payload = dict(rows[0].payload_json or {})
    data = dict(payload.get("data") or {})
    assert set(data) == {"service_ref", "due_on"}
    assert data["service_ref"] == str(row.id)[:8]
    assert data["due_on"] == due.isoformat()
    blob = json.dumps(payload, default=str)
    assert supply not in blob


def test_inactive_subscription_never_notified(db):
    _, session = db
    # Ventana global amplia para que el vencimiento derivado caiga dentro.
    _seed_param(session, params.WALLET_SERVICE_REMINDER_DAYS_BEFORE_KEY, "28")
    session.commit()
    row, _ = services_repo.create_subscription(
        session, user_id=USER, biller_id=LUZ_ID, supply_ref="BAJA-001", notify_days_before=3
    )
    row.status = "INACTIVE"
    session.flush()
    session.commit()

    result = run_service_reminders_once(session, now=TODAY, sender=MockNotificationSender())

    assert result["notified"] == []
    assert _notifications_rows(session) == []


# ------------------------------------------------------- idempotencia


def test_rerun_same_day_does_not_duplicate(db):
    _, session = db
    sub_in = _make_sub_with_due(session, TODAY, 3)
    sender = MockNotificationSender()

    first = run_service_reminders_once(session, now=TODAY, sender=sender)
    second = run_service_reminders_once(session, now=TODAY, sender=sender)

    assert first["notified"] == [str(sub_in.id)]
    assert second["notified"] == []
    assert str(sub_in.id) in second["skipped"]
    assert len(_notifications_rows(session)) == 1
    assert second["failed"] == []


def test_sender_failure_does_not_break_pass_nor_duplicate(db, monkeypatch):
    """Fallo de envio: best-effort (item `failed`, sin romper ni duplicar).

    La marca se confirma al reclamarla (semantica de
    `shared.try_mark_processed`, patron `E3-T19`): el reintento del
    mismo dia se omite (sin duplicar el aviso).
    """
    from app.modules.wallet.service import services as services_mod

    _, session = db
    sub_in = _make_sub_with_due(session, TODAY, 3)
    state = {"down": True}
    real_notify = services_mod.notify_service_reminder

    def _flaky(*args, **kwargs):
        if state["down"]:
            raise RuntimeError("proveedor caido")
        return real_notify(*args, **kwargs)

    monkeypatch.setattr(services_mod, "notify_service_reminder", _flaky)

    failed = run_service_reminders_once(session, now=TODAY, sender=MockNotificationSender())
    assert failed["notified"] == []
    assert len(failed["failed"]) == 1
    assert failed["failed"][0]["subscription_id"] == str(sub_in.id)
    assert _notifications_rows(session) == []

    # Reintento el mismo dia: no duplica el aviso.
    state["down"] = False
    retry = run_service_reminders_once(session, now=TODAY, sender=MockNotificationSender())
    assert retry["notified"] == []
    assert str(sub_in.id) in retry["skipped"]
    assert len(_notifications_rows(session)) == 0


# ------------------------------------------------------- parametro y ventana


def test_set_parameter_changes_window_without_redeploy(db):
    _, session = db
    key = params.WALLET_SERVICE_REMINDER_DAYS_BEFORE_KEY
    _seed_param(session, key, "3")
    session.commit()
    sub = _make_sub_with_due(session, TODAY, 5, notify=5)
    sender = MockNotificationSender()

    before = run_service_reminders_once(session, now=TODAY, sender=sender)
    assert before["notified"] == []
    assert str(sub.id) in before["skipped"]

    params.set_parameter(session, key=key, value=5)
    session.commit()
    assert params.get_wallet_service_reminder_days_before(session) == 5

    after = run_service_reminders_once(session, now=TODAY, sender=sender)
    assert after["days_before"] == 5
    assert after["notified"] == [str(sub.id)]


def test_per_subscription_notify_advances_window(db):
    _, session = db
    # `notify_days_before=2` adelanta el aviso aunque el global sea 3.
    sub = _make_sub_with_due(session, TODAY, 2, notify=2)

    result = run_service_reminders_once(session, now=TODAY, sender=MockNotificationSender())

    assert result["days_before"] == 3
    assert result["notified"] == [str(sub.id)]


def test_invalid_window_values_fall_back_to_default(db):
    _, session = db
    assert params.get_wallet_service_reminder_days_before(session) == 3
    with pytest.raises(ValueError):
        params.set_parameter(session, key=params.WALLET_SERVICE_REMINDER_DAYS_BEFORE_KEY, value=0)


def test_no_pii_in_logs(db, caplog):
    _, session = db
    secret = "SUP-SECRETO-999"
    # `notify=30` + ventana = global sembrado (no vacuo: el aviso sale).
    row = None
    window = 0
    for attempt in range(500):
        candidate, _ = services_repo.create_subscription(
            session,
            user_id=USER,
            biller_id=LUZ_ID,
            supply_ref=f"{secret}-{attempt:04d}",
            notify_days_before=30,
        )
        session.flush()
        window_tmp = (next_due_date(candidate.id, TODAY) - TODAY).days
        if 1 <= window_tmp <= 30:
            row, window = candidate, window_tmp
            break
        session.rollback()
    assert row is not None
    session.commit()
    _seed_param(session, params.WALLET_SERVICE_REMINDER_DAYS_BEFORE_KEY, str(window))
    session.commit()

    with caplog.at_level(logging.INFO, logger="app.modules.wallet.jobs.service_reminders"):
        result = run_service_reminders_once(session, now=TODAY, sender=MockNotificationSender())

    assert result["notified"] == [str(row.id)]
    assert secret not in caplog.text


# ------------------------------------------------------- wiring (patron M-2)


def test_runner_flags_and_interval(monkeypatch):
    from app import composition

    monkeypatch.delenv("WALLET_SERVICE_JOBS_INTERVAL_SECONDS", raising=False)
    assert composition.wallet_service_jobs_interval_seconds() == 3600.0
    monkeypatch.setenv("WALLET_SERVICE_JOBS_INTERVAL_SECONDS", "120")
    assert composition.wallet_service_jobs_interval_seconds() == 120.0
    monkeypatch.setenv("WALLET_SERVICE_JOBS_INTERVAL_SECONDS", "no-numerico")
    assert composition.wallet_service_jobs_interval_seconds() == 3600.0
    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "0")
    assert composition.wallet_service_jobs_enabled() is False
    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "1")
    assert composition.wallet_service_jobs_enabled() is True
    monkeypatch.setenv("WALLET_SERVICE_REMINDERS_ENABLED", "0")
    assert composition.wallet_service_reminders_enabled() is False
    monkeypatch.setenv("WALLET_SERVICE_REMINDERS_ENABLED", "1")
    assert composition.wallet_service_reminders_enabled() is True


def test_runner_skipped_when_disabled(db, monkeypatch):
    from app import composition

    engine, _ = db
    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "0")

    result = composition.run_wallet_service_jobs_once(
        session_factory=lambda: Session(bind=engine), now=TODAY
    )

    assert result == {"reminders": {"skipped": "disabled"}}


def test_runner_executes_reminders_when_enabled(db, monkeypatch):
    from app import composition

    engine, session = db
    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "1")
    monkeypatch.setenv("WALLET_SERVICE_REMINDERS_ENABLED", "1")
    sub_in = _make_sub_with_due(session, TODAY, 3)

    result = composition.run_wallet_service_jobs_once(
        session_factory=lambda: Session(bind=engine),
        now=TODAY,
        sender=MockNotificationSender(),
    )

    assert result["reminders"]["notified"] == [str(sub_in.id)]


def test_lifespan_starts_service_jobs_worker_when_enabled(monkeypatch):
    import threading

    from app import composition
    from app.main import app

    calls: list[dict] = []
    stop_events: list[threading.Event] = []

    def _fake_start_service(*, interval_seconds=None, stop_event=None, **kwargs):
        evt = stop_event or threading.Event()
        stop_events.append(evt)
        calls.append({"interval_seconds": interval_seconds, "kwargs": kwargs})
        return object(), evt

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "1")
    monkeypatch.setenv("WALLET_SERVICE_JOBS_INTERVAL_SECONDS", "120")
    monkeypatch.setattr(composition, "start_wallet_service_jobs_worker", _fake_start_service)

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"

    assert len(calls) == 1
    assert calls[0]["interval_seconds"] == 120.0
    assert calls[0]["kwargs"] == {}
    assert len(stop_events) == 1 and stop_events[0].is_set()


def test_lifespan_does_not_start_service_jobs_when_disabled(monkeypatch):
    from app import composition
    from app.main import app

    def _fake_start_service(*args, **kwargs):
        raise AssertionError("jobs inertes con WALLET_SERVICE_JOBS_ENABLED=0")

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "0")
    monkeypatch.setattr(composition, "start_wallet_service_jobs_worker", _fake_start_service)

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"


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


def test_entrypoint_runs_service_pass_when_enabled(monkeypatch):
    from app import composition

    entrypoint = _load_entrypoint()
    ran = threading.Event()
    calls: list[dict] = []

    def _fake_run_once(**kwargs):
        calls.append(kwargs)
        ran.set()
        return {
            "reminders": {"notified": [], "skipped": [], "failed": [], "as_of": TODAY.isoformat()}
        }

    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "1")
    monkeypatch.setattr(composition, "run_wallet_service_jobs_once", _fake_run_once)

    thread, stop = entrypoint.maybe_start_wallet_service_jobs(interval_seconds=0.05)
    try:
        assert thread is not None and stop is not None
        assert getattr(thread, "daemon", False) is True
        assert _wait_until(ran.is_set), "el worker no ejecuto ninguna pasada"
        assert calls
    finally:
        stop.set()
        thread.join(timeout=5.0)


def test_entrypoint_service_inert_when_disabled(monkeypatch):
    from app import composition

    entrypoint = _load_entrypoint()

    def _fake_run_once(**kwargs):  # pragma: no cover - no debe ejecutarse
        raise AssertionError("jobs inertes con WALLET_SERVICE_JOBS_ENABLED=0")

    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "0")
    monkeypatch.setattr(composition, "run_wallet_service_jobs_once", _fake_run_once)

    thread, stop = entrypoint.maybe_start_wallet_service_jobs(interval_seconds=0.05)
    assert (thread, stop) == (None, None)
    time.sleep(0.2)


def test_entrypoint_healthcheck_intact():
    import urllib.request

    entrypoint = _load_entrypoint()
    httpd = HTTPServer(("127.0.0.1", 0), entrypoint.HealthHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
            assert resp.status == 200
            assert json.loads(resp.read().decode("utf-8")) == {
                "status": "ok",
                "module": "worker",
            }
    finally:
        httpd.shutdown()
        thread.join(timeout=5.0)
        httpd.server_close()


# ------------------------------------------------------- estructural


def test_consumer_and_template_constants():
    assert REMINDERS_CONSUMER == "wallet-service-reminders"
    assert len(REMINDERS_CONSUMER) <= 60
    assert REMINDER_TEMPLATE_CODE == "service_reminder"


def test_migration_chain_has_single_head_0043():
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend_dir = Path(__file__).resolve().parent.parent
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    revisions = {rev.revision: rev.down_revision for rev in script.walk_revisions()}
    heads = [rev for rev in revisions if rev not in revisions.values()]
    # Desviacion documentada del brief (ver docstring de la migracion y
    # `docs/18`): el `revision` se acorta a 30 caracteres porque el
    # mandado (34) excede `alembic_version.version_num VARCHAR(32)` y
    # romperia `upgrade head` en Postgres; el archivo conserva el
    # nombre mandado `0043_wallet_service_reminder_param.py`.
    assert revisions.get("0043_wallet_service_reminder") == "0042_wallet_services"
    assert "0043_wallet_service_reminder_param" not in revisions
    # E6-T12 (2026-10-13): la cabeza avanza a 0044_notifications_preferences
    # (preferencias por canal + claves de seguridad, tras 0043). Pulido de
    # correos (2026-10-15): la cabeza avanza a
    # 0045_notification_email_content (textos de semillas, tras 0044).
    assert revisions.get("0046_services_settlement") == "0045_notification_email_content"
    assert revisions.get("0045_notification_email_content") == "0044_notifications_preferences"
    assert revisions.get("0044_notifications_preferences") == "0043_wallet_service_reminder"
    # E4-T21 (2026-10-15): la cabeza avanza a 0047_wallet_personal_qr
    # (QR personal estatico, tras 0046).
    assert revisions.get("0047_wallet_personal_qr") == "0046_services_settlement"
    assert heads == ["0047_wallet_personal_qr"], f"heads inesperados: {heads}"
    content = (
        backend_dir / "migrations" / "versions" / "0043_wallet_service_reminder_param.py"
    ).read_text(encoding="utf-8")
    assert "def upgrade" in content and "def downgrade" in content
    assert len("0043_wallet_service_reminder") <= 32
    assert "wallet.service_reminder_days_before" in content
