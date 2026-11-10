"""Punto de entrada de la aplicacion FastAPI.

Ensambla los routers de todos los modulos (monolito modular). No contiene logica de negocio.

Arranque (E2-T23): el `lifespan` cablea los ports productivos, suscribe el
consumidor `ledger.entry.posted` -> `movements_view` al bus y arranca el
worker periodico del outbox en un hilo daemon (no bloquea). El worker se
desactiva con `OUTBOX_WORKER_ENABLED=0` y su intervalo se configura con
`OUTBOX_POLL_INTERVAL_SECONDS` (regla de oro 6: fuera del codigo).

Jobs de cuotas (E3-T19, HU12 CA-03/CA-04): el `lifespan` tambien arranca el
worker periodico de debito automatico + recordatorios con el mismo mecanismo
(hilo daemon, parada por evento). Se desactiva con `CREDIT_LOAN_JOBS_ENABLED=0`
(los flags por job `CREDIT_LOANS_AUTO_DEBIT_ENABLED` /
`CREDIT_LOANS_REMINDERS_ENABLED` rigen cada pasada) y su intervalo se configura
con `CREDIT_LOANS_JOBS_INTERVAL_SECONDS` (defecto 3600 s).

Caducidad del QR (E4-T07, HU14 CA-02) + reconciliacion del cobro
(R-1, HU15): el `lifespan` arranca ademas el worker periodico de QR
(`wallet/jobs/qr_expiry.py` + `wallet/jobs/qr_reconcile.py`) con el
mismo mecanismo. Se desactiva con `WALLET_QR_JOBS_ENABLED=0` (los flags
por job `WALLET_QR_EXPIRY_ENABLED` / `WALLET_QR_RECONCILE_ENABLED`
rigen cada pasada) y su intervalo se configura con
`WALLET_QR_JOBS_INTERVAL_SECONDS` (defecto 60 s).

Recordatorios de servicios (E4-T18, HU16 CA-03): el `lifespan`
arranca ademas el worker periodico de servicios
(`wallet/jobs/service_reminders.py`) con el mismo mecanismo. Se
desactiva con `WALLET_SERVICE_JOBS_ENABLED=0` (el flag por job
`WALLET_SERVICE_REMINDERS_ENABLED` rige cada pasada) y su intervalo
se configura con `WALLET_SERVICE_JOBS_INTERVAL_SECONDS` (defecto
3600 s).
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.errors import AppError, app_error_handler
from app.core.health import router as health_router
from app.core.logging import configure_logging, request_id_var
from app.modules import iter_routers

logger = logging.getLogger(__name__)


def _validation_fields(errors: list[dict]) -> list[str]:
    """Resume `loc` + `type` sin valores crudos (sin PII en logs, regla 7)."""
    fields: list[str] = []
    for err in errors:
        loc = err.get("loc", ())
        err_type = err.get("type", "unknown")
        try:
            loc_str = ".".join(str(part) for part in loc)
        except Exception:  # noqa: BLE001 - el log nunca debe romper el 422
            loc_str = "?"
        fields.append(f"{loc_str}:{err_type}")
    return fields


def _sanitized_validation_errors(errors: list[dict]) -> list[dict]:
    """Sanea errores de Pydantic sin PII/secretos (E2-T31: el PIN en `input`).

    Pydantic incluye `input` (body crudo) y `ctx` (valores del validador) en
    cada error: un 422 los expondria en claro (p. ej. el `pin`). Solo se
    exponen `loc` + `type` + `msg` (suficientes para corregir el request).
    """
    sanitized: list[dict] = []
    for err in errors:
        if not isinstance(err, dict):
            continue
        try:
            loc = err.get("loc", ())
            try:
                loc_list = (
                    [str(part) for part in loc] if isinstance(loc, (list, tuple)) else [str(loc)]
                )
            except Exception:  # noqa: BLE001 - el 422 nunca debe romperse
                loc_list = ["?"]
            sanitized.append(
                {
                    "loc": loc_list,
                    "msg": str(err.get("msg", "")),
                    "type": str(err.get("type", "")),
                }
            )
        except Exception:  # noqa: BLE001 - un error roto no rompe el 422
            logger.debug("validation_error sanitario omitido")
            continue
    return sanitized


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 de esquema: log seguro + contrato `{"detail": [...]}` intacto."""
    try:
        raw_errors = exc.errors()
    except Exception:  # noqa: BLE001 - el log nunca debe romper el 422
        raw_errors = []
    fields = _validation_fields(list(raw_errors))
    logger.warning(
        "validation_error path=%s method=%s count=%d fields=%s",
        request.url.path,
        request.method,
        len(fields),
        ",".join(fields),
    )
    sanitized = _sanitized_validation_errors(list(raw_errors))
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(sanitized)})


def _outbox_worker_enabled() -> bool:
    return os.getenv("OUTBOX_WORKER_ENABLED", "1").strip().lower() not in ("0", "false", "no")


def _outbox_poll_interval_seconds() -> float:
    try:
        return max(0.1, float(os.getenv("OUTBOX_POLL_INTERVAL_SECONDS", "2.0")))
    except ValueError:
        return 2.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Cableado productivo E2-T23 + jobs de cuotas E3-T19 + jobs QR E4-T07/R-1.

    No llama a `wire_ports()`: la ruta productiva de saldos/proyeccion
    resuelve los adaptadores reales via `resolve_*` sin mutar globales
    (diseno E2-T01 opcion b); mutarlos aqui romperia el aislamiento.
    """
    from app.composition import (
        credit_loan_jobs_enabled,
        credit_loan_jobs_interval_seconds,
        start_credit_loan_jobs_worker,
        start_outbox_worker,
        start_wallet_qr_jobs_worker,
        start_wallet_service_jobs_worker,
        subscribe_accounts_movements,
        subscribe_channel_lock,
        subscribe_risk_security,
        subscribe_security_alerts,
        subscribe_unrecognized_reports,
        wallet_qr_jobs_enabled,
        wallet_qr_jobs_interval_seconds,
        wallet_service_jobs_enabled,
        wallet_service_jobs_interval_seconds,
    )

    subscribe_accounts_movements()
    subscribe_risk_security()
    subscribe_security_alerts()
    subscribe_unrecognized_reports()
    subscribe_channel_lock()
    stop_event = None
    if _outbox_worker_enabled():
        _thread, stop_event = start_outbox_worker(interval_seconds=_outbox_poll_interval_seconds())
    jobs_stop_event = None
    if credit_loan_jobs_enabled():
        _jobs_thread, jobs_stop_event = start_credit_loan_jobs_worker(
            interval_seconds=credit_loan_jobs_interval_seconds()
        )
        logger.info(
            "credit_loan_jobs_worker started interval_seconds=%s",
            credit_loan_jobs_interval_seconds(),
        )
    else:
        logger.info("credit_loan_jobs_worker disabled")
    qr_jobs_stop_event = None
    if wallet_qr_jobs_enabled():
        _qr_thread, qr_jobs_stop_event = start_wallet_qr_jobs_worker(
            interval_seconds=wallet_qr_jobs_interval_seconds()
        )
        logger.info(
            "wallet_qr_jobs_worker started interval_seconds=%s",
            wallet_qr_jobs_interval_seconds(),
        )
    else:
        logger.info("wallet_qr_jobs_worker disabled")
    service_jobs_stop_event = None
    if wallet_service_jobs_enabled():
        _service_thread, service_jobs_stop_event = start_wallet_service_jobs_worker(
            interval_seconds=wallet_service_jobs_interval_seconds()
        )
        logger.info(
            "wallet_service_jobs_worker started interval_seconds=%s",
            wallet_service_jobs_interval_seconds(),
        )
    else:
        logger.info("wallet_service_jobs_worker disabled")
    try:
        yield
    finally:
        if stop_event is not None:
            stop_event.set()
        if jobs_stop_event is not None:
            jobs_stop_event.set()
        if qr_jobs_stop_event is not None:
            qr_jobs_stop_event.set()
        if service_jobs_stop_event is not None:
            service_jobs_stop_event.set()


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
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.include_router(health_router)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        """Fija `request_id_var` por request (Q-T07 CA-01, gap real).

        El `ContextVar` existia en `app.core.logging` pero nadie lo asignaba y
        el log JSON salia siempre con `request_id: "-"`. El middleware respeta
        `X-Request-Id` (docs/05#3; si no llega, se genera como en `_request_id`
        de los routers) para el log y lo devuelve en la respuesta para
        correlacion log <-> request. No muta el scope ni los cuerpos de error
        (los `_request_id` de cada router y `AppError` siguen mandando en sus
        respuestas; con cabecera explicita todo converge al mismo id). Sin PII
        ni secretos (solo el id tecnico).
        """
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-Id"] = request_id
        return response

    for _name, router in iter_routers():
        app.include_router(router, prefix="/api/v1")

    return app


app = create_app()
