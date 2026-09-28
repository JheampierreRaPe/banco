"""Esquemas Pydantic del modulo `admin` (fachadas de administracion)."""

from app.modules.admin.schemas.parameters import (
    ParameterItem,
    ParametersListResponse,
    ParameterUpdated,
    ParameterUpdateRequest,
    ParameterUpdateResponse,
)

__all__ = [
    "ParameterItem",
    "ParameterUpdateRequest",
    "ParameterUpdateResponse",
    "ParameterUpdated",
    "ParametersListResponse",
]
