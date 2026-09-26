"""Esquemas del consentimiento biometrico post-login (E1-T39, HU02/HU03).

`POST /auth/biometric/consent {enabled}` -> fija
`identity.credentials.biometric_enabled` del usuario del JWT y devuelve el
flag vigente. Envoltorio `docs/05#4` (`{"data", "meta"}`). Los errores de
negocio usan problem+json via `AppError` en el router (`NOT_AUTHENTICATED`
generico sin filtrar, `NOT_FOUND` neutro); los errores de esquema usan el
422 estandar de FastAPI (`{"detail": [...]}`).

Endpoint post-sesion: exige `Authorization: Bearer <jwt>` cuyo `sub` es el
`user_id` (`core.security.decode_token`, 05#2). Sin PII/secretos en logs ni
en respuestas; el cuerpo nunca refleja tokens ni claves.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class BiometricConsentRequest(BaseModel):
    """Entrada de `POST /auth/biometric/consent` (enable/revoke)."""

    enabled: bool = Field(description="Activa (`true`) o revoca (`false`) el acceso biometrico.")


class BiometricConsentData(BaseModel):
    biometric_enabled: bool = Field(description="Consentimiento vigente del usuario.")


class BiometricConsentResponse(BaseModel):
    data: BiometricConsentData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "BiometricConsentData",
    "BiometricConsentRequest",
    "BiometricConsentResponse",
]
