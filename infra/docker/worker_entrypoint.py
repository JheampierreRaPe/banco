"""Entrypoint del worker demo (Q-T06).

Mientras no exista el consumidor real de colas, este proceso:
1. valida conectividad con Postgres y Redis al arrancar, y
2. expone /health en WORKER_HEALTH_PORT (defecto 8100) para el healthcheck.
No consume ni publica eventos: es solo el esqueleto de la demo.
"""
from __future__ import annotations

import os
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from sqlalchemy import create_engine, text


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

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - firma del handler
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

    port = int(os.getenv("WORKER_HEALTH_PORT", "8100"))
    httpd = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"worker: listo, /health en el puerto {port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()