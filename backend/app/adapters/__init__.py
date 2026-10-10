"""Registro de adaptadores externos (docs/02#9, E2-T08).

Cada adaptador expone interfaz comun + implementacion `mock` seleccionable
por entorno. Importacion perezosa: este paquete no importa `httpx` al
cargarse; los modulos se resuelven bajo demanda via `__getattr__`.
"""

from __future__ import annotations

_LAZY = {
    "holder_lookup": "app.adapters.holder_lookup",
    "credit_bureau": "app.adapters.credit_bureau",
}

__all__ = ["create_credit_bureau_provider", "create_holder_lookup_provider"]


def __getattr__(name: str):
    if name == "create_holder_lookup_provider":
        from app.adapters.holder_lookup import create_holder_lookup_provider as factory

        return factory
    if name == "create_credit_bureau_provider":
        from app.adapters.credit_bureau import create_credit_bureau_provider as factory

        return factory
    raise AttributeError(f"modulo {__name__!r} sin atributo {name!r}")
