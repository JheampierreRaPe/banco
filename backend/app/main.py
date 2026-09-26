"""Punto de entrada de la aplicacion FastAPI.

Ensambla los routers de todos los modulos (monolito modular). No contiene logica de negocio.

Arranque (E2-T23): el `lifespan` cablea los ports productivos, suscribe el
consumidor `ledger.entry.posted` -> `movements_view` al bus y arranca el
worker periodico del outbox en un hilo daemon (no bloquea). El worker se
desactiva con `OUTBOX_WORKER_ENABLED=0` y su intervalo se configura con
`OUTBOX_POLL_INTERVAL_SECONDS` (regla de oro 6: fuera del codigo).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.errors import AppError, app_error_handler
from app.core.health import router as health_router
from app.core.logging import configure_logging
from app.modules import iter_routers


def _outbox_worker_enabled() -> bool:
    return os.getenv("OUTBOX_WORKER_ENABLED", "1").strip().lower() not in ("0", "false", "no")


def _outbox_poll_interval_seconds() -> float:
    try:
        return max(0.1, float(os.getenv("OUTBOX_POLL_INTERVAL_SECONDS", "2.0")))
    except ValueError:
        return 2.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Cableado productivo E2-T23: suscripcion + worker del outbox.

    No llama a `wire_ports()`: la ruta productiva de saldos/proyeccion
    resuelve los adaptadores reales via `resolve_*` sin mutar globales
    (diseno E2-T01 opcion b); mutarlos aqui romperia el aislamiento.
    """
    from app.composition import start_outbox_worker, subscribe_accounts_movements

    subscribe_accounts_movements()
    stop_event = None
    if _outbox_worker_enabled():
        _thread, stop_event = start_outbox_worker(interval_seconds=_outbox_poll_interval_seconds())
    try:
        yield
    finally:
        if stop_event is not None:
            stop_event.set()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Banca Online Integral - API (MVP academico, dinero simulado).",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(health_router)

    for _name, router in iter_routers():
        app.include_router(router, prefix="/api/v1")

    return app


app = create_app()
