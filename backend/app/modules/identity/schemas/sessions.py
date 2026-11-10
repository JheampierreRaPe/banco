"""Esquemas de gestion de sesiones (E1-T15, HU03 CA-04).

`POST /auth/refresh {refresh_token}` -> rota el refresh (emite uno nuevo
e invalida el anterior). `POST /auth/logout {refresh_token}` -> revoca la
sesion. Envoltorio `docs/05#4` (`{"data", "meta"}`). Los errores de negocio
usan problem+json via `AppError` en el router (`INVALID_REFRESH`,
`REFRESH_EXPIRED`, `SESSION_INACTIVE`, `REFRESH_REUSED`); los errores de
esquema usan el 422 estandar de FastAPI (`{"detail": [...]}`).

El refresh viaja como texto libre (`1..256`): el esquema no valida su
forma interna para no crear un oraculo 422-vs-401 (el servicio responde
generico ante token desconocido). El refresh jamas se refleja en logs.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RefreshRequest(BaseModel):
    """Entrada de `POST /auth/refresh` (refresh vigente a rotar)."""

    refresh_token: str = Field(min_length=1, max_length=256, description="Refresh opaco vigente.")


class RefreshData(BaseModel):
    access_token: str
    refresh_token: str = Field(description="Refresh nuevo; el anterior queda revocado.")
    token_type: str = Field(default="Bearer")
    session_id: str
    expires_in: int = Field(ge=0, description="Vigencia del refresh nuevo en segundos.")


class RefreshResponse(BaseModel):
    data: RefreshData
    meta: dict = Field(default_factory=dict)


class LogoutRequest(BaseModel):
    """Entrada de `POST /auth/logout` (sesion a revocar)."""

    refresh_token: str = Field(
        min_length=1, max_length=256, description="Refresh de la sesion a cerrar."
    )


class LogoutData(BaseModel):
    revoked: bool = Field(description="True si esta llamada revoco la sesion.")
    session_id: str | None = Field(
        default=None, description="Sesion afectada (None si el refresh era desconocido)."
    )


class LogoutResponse(BaseModel):
    data: LogoutData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "LogoutData",
    "LogoutRequest",
    "LogoutResponse",
    "RefreshData",
    "RefreshRequest",
    "RefreshResponse",
]
