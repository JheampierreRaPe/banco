"""Primitivas de seguridad (JWT). La logica de negocio se implementa en las tareas."""

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.config import get_settings


def create_access_token(subject: str, roles: list[str] | None = None, **extra: Any) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "roles": roles or [],
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
        **extra,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
