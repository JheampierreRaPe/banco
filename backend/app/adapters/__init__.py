"""Registro de adaptadores externos (docs/02#9, E2-T08).

Cada adaptador expone interfaz comun + implementacion `mock` seleccionable
por entorno. Importacion perezosa: este paquete no importa `httpx` al
cargarse; los modulos se resuelven bajo demanda via `__getattr__`.
"""

from __future__ import annotations

_LAZY = {
    "holder_lookup": "app.adapters.holder_lookup",
    "credit_bureau": "app.adapters.credit_bureau",
    "wallet_interop": "app.adapters.wallet_interop",
    "service_provider": "app.adapters.service_provider",
}

__all__ = [
    "create_credit_bureau_provider",
    "create_holder_lookup_provider",
    "create_service_provider",
    "create_wallet_interop_provider",
]


def __getattr__(name: str):
    if name == "create_holder_lookup_provider":
        from app.adapters.holder_lookup import create_holder_lookup_provider as factory

        return factory
    if name == "create_credit_bureau_provider":
        from app.adapters.credit_bureau import create_credit_bureau_provider as factory

        return factory
    if name == "create_wallet_interop_provider":
        from app.adapters.wallet_interop import create_wallet_interop_provider as factory

        return factory
    if name == "create_service_provider":
        from app.adapters.service_provider import create_service_provider as factory

        return factory
    raise AttributeError(f"modulo {__name__!r} sin atributo {name!r}")
