"""Registro de modulos del monolito modular.

`main.py` usa `iter_routers()` para ensamblar los routers de cada bounded context.
Cada modulo expone `app.modules.<modulo>.api.router`.
"""

import importlib
from collections.abc import Iterator

from fastapi import APIRouter

# Orden segun docs/02-arquitectura.md seccion 4.
MODULES: tuple[str, ...] = (
    "identity",
    "accounts",
    "transactions",
    "ledger",
    "credits",
    "wallet",
    "fx",
    "risk",
    "reconciliation",
    "notifications",
    "audit",
    "admin",
)


def iter_routers() -> Iterator[tuple[str, APIRouter]]:
    for name in MODULES:
        module = importlib.import_module(f"app.modules.{name}.api")
        yield name, module.router
