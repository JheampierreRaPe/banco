"""Errores de aplicacion y formato de respuesta unico (estilo problem+json simplificado).

Ver docs/05-contratos-api.md.
"""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Error de negocio con codigo estable para el cliente."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def error_payload(
    code: str, message: str, details: dict | None = None, request_id: str | None = None
) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id,
        }
    }


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(
            exc.code, exc.message, exc.details, request.headers.get("x-request-id")
        ),
    )
