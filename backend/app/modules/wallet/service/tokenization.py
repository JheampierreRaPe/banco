"""Fachada del proveedor de tokenizacion (E4-T01; `docs/18` G67).

Seleccion por entorno (regla de oro 6, fuera del codigo):
`WALLET_TOKENIZATION_PROVIDER` (default `mock`). El MVP solo trae el
mock local determinista (decision del dueno); un valor desconocido se
rechaza en el arranque del caso de uso (fail-fast, sin secretos).
"""

from __future__ import annotations

import os

from app.modules.wallet.domain.tokenization import (
    MockWalletTokenizationProvider,
    WalletTokenizationProvider,
)

PROVIDER_ENV_VAR = "WALLET_TOKENIZATION_PROVIDER"
DEFAULT_PROVIDER = "mock"


def get_tokenization_provider(name: str | None = None) -> WalletTokenizationProvider:
    """Resuelve el proveedor vigente (desacoplado del caso de uso)."""
    provider = (
        (name or os.getenv(PROVIDER_ENV_VAR, DEFAULT_PROVIDER) or DEFAULT_PROVIDER).strip().lower()
    )
    if provider == "mock":
        return MockWalletTokenizationProvider()
    raise ValueError(f"{PROVIDER_ENV_VAR} desconocido: {provider!r} (vigente: 'mock')")


__all__ = ["DEFAULT_PROVIDER", "PROVIDER_ENV_VAR", "get_tokenization_provider"]
