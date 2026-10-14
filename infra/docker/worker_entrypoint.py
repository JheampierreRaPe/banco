"""Entrypoint del worker demo (Q-T06, jobs de cuotas E3-T19, jobs QR E4-T07/R-1,
jobs de servicios E4-T18).

Este proceso:
1. valida conectividad con Postgres y Redis al arrancar;
2. arranca el worker periodico de cuotas (debito automatico +
   recordatorios, HU12 CA-03/CA-04) en un hilo daemon con el mismo
   mecanismo que el `start_outbox_worker` del backend (una pasada por
   ciclo con sesion/`commit` propios via
   `app.composition.run_credit_loan_jobs_once`; los errores del ciclo
   no matan el hilo); inerte si `CREDIT_LOAN_JOBS_ENABLED=0`,
3. arranca el worker periodico de QR (caducidad HU14 CA-02 +
   reconciliacion del cobro R-1, HU15) con el mismo mecanismo (una
   pasada por ciclo via `app.composition.run_wallet_qr_jobs_once`,
   reconciliacion antes que caducidad); inerte si
   `WALLET_QR_JOBS_ENABLED=0`, y
4. arranca el worker periodico de servicios (recordatorios HU16
   CA-03) con el mismo mecanismo (una pasada por ciclo via
   `app.composition.run_wallet_service_jobs_once`); inerte si
   `WALLET_SERVICE_JOBS_ENABLED=0`, y
5. expone /health en WORKER_HEALTH_PORT (defecto 8100) para el healthcheck.

Sin PII ni secretos en logs (solo estado del worker e intervalo).
"""
from __future__ import annotations

import logging
import os
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from sqlalchemy import create_engine, text

logger = logging.getLogger("worker")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            body = b'{"status":"ok","module":"worker"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return


def _redis_reachable(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or "redis"
    port = parsed.port or 6379
    with socket.create_connection((host, port), timeout=5):
        return True


def main() -> None:
    database_url = os.getenv(
        "DATABASE_URL", "postgresql+psycopg://banca:banca@postgres:5432/banca"
    )
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")

    engine = create_engine(database_url)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    print("worker: postgres OK")

    _redis_reachable(redis_url)
    print("worker: redis OK")

    _thread, jobs_stop = maybe_start_credit_loan_jobs()
    _qr_thread, qr_jobs_stop = maybe_start_wallet_qr_jobs()
    _service_thread, service_jobs_stop = maybe_start_wallet_service_jobs()

    port = int(os.getenv("WORKER_HEALTH_PORT", "8100"))
    httpd = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"worker: listo, /health en el puerto {port}")
    try:
        httpd.serve_forever()
    finally:
        if jobs_stop is not None:
            jobs_stop.set()
        if qr_jobs_stop is not None:
            qr_jobs_stop.set()
        if service_jobs_stop is not None:
            service_jobs_stop.set()


def maybe_start_credit_loan_jobs(
    *,
    interval_seconds: float | None = None,
    stop_event=None,
):
    """Arranca el worker periodico de cuotas si el flag lo habilita (E3-T19).

    Mismo mecanismo que `app.composition.start_credit_loan_jobs_worker`
    (hilo daemon + parada por evento; cada ciclo es una pasada con
    sesion/`commit` propios y los errores del ciclo no matan el hilo).
    Retorna `(thread, stop_event)`; con `CREDIT_LOAN_JOBS_ENABLED=0`
    retorna `(None, None)` sin ejecutar nada (jobs inertes).
    `interval_seconds`/`stop_event` solo existen para inyeccion en
    pruebas. Sin PII ni secretos en logs.
    """
    from app.composition import (
        credit_loan_jobs_enabled,
        credit_loan_jobs_interval_seconds,
        start_credit_loan_jobs_worker,
    )

    if not credit_loan_jobs_enabled():
        logger.info("worker credit_loan_jobs disabled")
        return None, None
    if interval_seconds is None:
        interval_seconds = credit_loan_jobs_interval_seconds()
    thread, stop = start_credit_loan_jobs_worker(
        interval_seconds=interval_seconds,
        stop_event=stop_event,
    )
    logger.info("worker credit_loan_jobs started interval_seconds=%s", interval_seconds)
    return thread, stop


def maybe_start_wallet_qr_jobs(
    *,
    interval_seconds: float | None = None,
    stop_event=None,
):
    """Arranca el worker periodico de QR si el flag lo habilita (E4-T07 + R-1).

    Mismo mecanismo que `app.composition.start_wallet_qr_jobs_worker`
    (hilo daemon + parada por evento; cada ciclo es una pasada con
    sesion/`commit` propios y los errores del ciclo no matan el hilo:
    reconciliacion del cobro y luego caducidad). Retorna `(thread,
    stop_event)`; con `WALLET_QR_JOBS_ENABLED=0` retorna `(None, None)`
    sin ejecutar nada (jobs inertes). `interval_seconds`/`stop_event`
    solo existen para inyeccion en pruebas. Sin PII ni secretos en logs.
    """
    from app.composition import (
        start_wallet_qr_jobs_worker,
        wallet_qr_jobs_enabled,
        wallet_qr_jobs_interval_seconds,
    )

    if not wallet_qr_jobs_enabled():
        logger.info("worker wallet_qr_jobs disabled")
        return None, None
    if interval_seconds is None:
        interval_seconds = wallet_qr_jobs_interval_seconds()
    thread, stop = start_wallet_qr_jobs_worker(
        interval_seconds=interval_seconds,
        stop_event=stop_event,
    )
    logger.info("worker wallet_qr_jobs started interval_seconds=%s", interval_seconds)
    return thread, stop


def maybe_start_wallet_service_jobs(
    *,
    interval_seconds: float | None = None,
    stop_event=None,
):
    """Arranca el worker periodico de servicios si el flag lo habilita (E4-T18).

    Mismo mecanismo que `app.composition.start_wallet_service_jobs_worker`
    (hilo daemon + parada por evento; cada ciclo es una pasada con
    sesion/`commit` propios y los errores del ciclo no matan el hilo:
    recordatorios de vencimiento HU16 CA-03). Retorna `(thread,
    stop_event)`; con `WALLET_SERVICE_JOBS_ENABLED=0` retorna `(None,
    None)` sin ejecutar nada (jobs inertes). `interval_seconds`/
    `stop_event` solo existen para inyeccion en pruebas. Sin PII ni
    secretos en logs.
    """
    from app.composition import (
        start_wallet_service_jobs_worker,
        wallet_service_jobs_enabled,
        wallet_service_jobs_interval_seconds,
    )

    if not wallet_service_jobs_enabled():
        logger.info("worker wallet_service_jobs disabled")
        return None, None
    if interval_seconds is None:
        interval_seconds = wallet_service_jobs_interval_seconds()
    thread, stop = start_wallet_service_jobs_worker(
        interval_seconds=interval_seconds,
        stop_event=stop_event,
    )
    logger.info("worker wallet_service_jobs started interval_seconds=%s", interval_seconds)
    return thread, stop


if __name__ == "__main__":
    main()