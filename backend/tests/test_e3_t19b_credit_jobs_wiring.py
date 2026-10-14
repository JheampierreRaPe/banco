"""Cableado runtime de los jobs de cuotas (fix M-2, E3-T19 HU12 CA-03/CA-04).

`run_credit_loan_jobs_once` / `start_credit_loan_jobs_worker` existian en
`app.composition` pero nada los registraba: ni el `lifespan` del backend ni
el `worker_entrypoint` del contenedor `worker` los arrancaban, asi que el
debito automatico y los recordatorios no corrian en el runtime real.

Regresion (falla sin el fix, pasa con el):
- `lifespan` arranca el worker de cuotas con el intervalo de entorno cuando
  `CREDIT_LOAN_JOBS_ENABLED=1` y no lo arranca con `=0` (mismo mecanismo
  que `start_outbox_worker`: hilo daemon + parada por evento).
- `worker_entrypoint.maybe_start_credit_loan_jobs` ejecuta una pasada con
  flags on y queda inerte (sin pasadas) con flags off.
- El `/health` del worker sigue intacto.

Sin Postgres/Redis: los starters/runner reales se sustituyen por dobles
(`monkeypatch`); ningun test abre red ni BD. Sin PII en logs ni asserts.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import pathlib
import threading
import time
import urllib.request
from http.server import HTTPServer

from fastapi.testclient import TestClient

ENTRYPOINT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "infra"
    / "docker"
    / "worker_entrypoint.py"
)


def _load_entrypoint():
    loader = importlib.machinery.SourceFileLoader("worker_entrypoint", str(ENTRYPOINT_PATH))
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


# ------------------------------------------------------- lifespan (backend)


def test_lifespan_starts_credit_loan_jobs_worker_when_enabled(monkeypatch):
    from app import composition
    from app.main import app

    calls: list[dict] = []
    outbox_calls: list[dict] = []
    stop_events: list[threading.Event] = []

    def _fake_start_jobs(*, interval_seconds=None, stop_event=None, **kwargs):
        event = stop_event or threading.Event()
        stop_events.append(event)
        calls.append({"interval_seconds": interval_seconds, "kwargs": kwargs})
        return object(), event

    def _fake_start_outbox(*args, **kwargs):
        outbox_calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("el outbox debe quedar apagado en este test")

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "1")
    monkeypatch.setenv("CREDIT_LOANS_JOBS_INTERVAL_SECONDS", "45")
    monkeypatch.setattr(composition, "start_credit_loan_jobs_worker", _fake_start_jobs)
    monkeypatch.setattr(composition, "start_outbox_worker", _fake_start_outbox)

    with TestClient(app):
        pass

    assert len(calls) == 1
    assert calls[0]["interval_seconds"] == 45.0
    assert calls[0]["kwargs"] == {}
    assert len(stop_events) == 1 and stop_events[0].is_set()


def test_lifespan_does_not_start_credit_loan_jobs_when_disabled(monkeypatch):
    from app import composition
    from app.main import app

    def _fake_start_jobs(*args, **kwargs):
        raise AssertionError("jobs inertes con CREDIT_LOAN_JOBS_ENABLED=0")

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setattr(composition, "start_credit_loan_jobs_worker", _fake_start_jobs)

    with TestClient(app):
        pass


# ------------------------------------------------------- worker (contenedor)


def test_entrypoint_runs_pass_when_enabled(monkeypatch):
    from app import composition

    entrypoint = _load_entrypoint()
    ran = threading.Event()
    calls: list[dict] = []

    def _fake_run_once(**kwargs):
        calls.append(kwargs)
        ran.set()
        return {"auto_debit": {"skipped": "disabled"}, "reminders": {"skipped": "disabled"}}

    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "1")
    monkeypatch.setattr(composition, "run_credit_loan_jobs_once", _fake_run_once)

    thread, stop = entrypoint.maybe_start_credit_loan_jobs(interval_seconds=0.05)
    try:
        assert thread is not None and stop is not None
        assert getattr(thread, "daemon", False) is True
        assert _wait_until(ran.is_set), "el worker no ejecuto ninguna pasada"
        assert calls
    finally:
        stop.set()
        thread.join(timeout=5.0)


def test_entrypoint_inert_when_disabled(monkeypatch):
    from app import composition

    entrypoint = _load_entrypoint()

    def _fake_run_once(**kwargs):  # pragma: no cover - no debe ejecutarse
        raise AssertionError("jobs inertes con CREDIT_LOAN_JOBS_ENABLED=0")

    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setattr(composition, "run_credit_loan_jobs_once", _fake_run_once)

    thread, stop = entrypoint.maybe_start_credit_loan_jobs(interval_seconds=0.05)
    assert (thread, stop) == (None, None)
    time.sleep(0.2)


def test_entrypoint_healthcheck_intact():
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
