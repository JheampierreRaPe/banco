"""Esquemas del perfil autenticado minimo (E1-T44, HU01/HU02).

`GET /me` (Bearer) -> devuelve **solo** el nombre del titular
(`first_name`/`last_name` y, si aplica, `business_name`/razon social) para
que el cliente componga el saludo y las iniciales del avatar. Envoltorio
`docs/05#4` (`{"data", "meta"}`). Los errores de negocio usan problem+json
via `AppError` en el router (`NOT_AUTHENTICATED` generico sin filtrar,
`NOT_FOUND` neutro).

Jamas se exponen documento, correo, telefono, estado, roles ni hashes: el
cuerpo solo trae nombre/razon social. Sin PII/secretos en logs ni en
respuestas.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProfileData(BaseModel):
    """Nombre del titular (RUC juridica: nombres `""` + razon social)."""

    first_name: str = Field(description='Nombre del titular (`""` en RUC de persona juridica).')
    last_name: str = Field(description='Apellido del titular (`""` en RUC de persona juridica).')
    business_name: str | None = Field(
        default=None,
        description="Razon social; solo RUC de persona juridica (`null` en el resto).",
    )


class ProfileResponse(BaseModel):
    data: ProfileData
    meta: dict = Field(default_factory=dict)


__all__ = ["ProfileData", "ProfileResponse"]
