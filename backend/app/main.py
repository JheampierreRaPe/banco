"""Punto de entrada de la aplicacion FastAPI.

Ensambla los routers de todos los modulos (monolito modular). No contiene logica de negocio.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.errors import AppError, app_error_handler
from app.core.health import router as health_router
from app.core.logging import configure_logging
from app.modules import iter_routers


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Banca Online Integral - API (MVP academico, dinero simulado).",
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
