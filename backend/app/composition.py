"""Capa de composicion: cableado definitivo fase 3, opcion b (E2-T01, H3 fase 4).

Problema: `transactions.service.execute_transfer` usaba `_DefaultBalancePort`
(`NotImplementedError("E2-T01 pendiente")`) y `ledger.repository.balances`
usaba `DefaultAccountProjection` (stub) cuando nadie inyectaba el adaptador.
Los adaptadores reales (`AccountsBalanceAdapter`,
`AccountsLedgerProjectionAdapter` en `accounts/repository/`) existian pero
nada los conectaba en la ruta productiva.

Solucion (opcion b): los puntos de uso resuelven el port real via
`resolve_balance_port()` / `resolve_account_projection()` (imports perezosos
a `accounts.repository`). Los stubs `_Default*` quedan como ultima red de
seguridad documentada (el `fallback`), pero la ruta productiva nunca los
alcanza porque `accounts` siempre esta disponible en el monolito.

Sin ciclos de importacion: este modulo no importa nada de `app` a nivel
top (solo stdlib + `typing`); los adaptadores se importan de forma perezosa
dentro de cada funcion. Los consumidores (`transactions/service`,
`ledger/repository/balances`) tambien importan este modulo de forma perezosa
dentro de sus funciones, nunca a nivel top.
"""

from __future__ import annotations

from typing import Any


def get_balance_port() -> Any:
    """Retorna el adaptador real de saldos (dueño de `available`)."""
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT

    return ACCOUNTS_BALANCE_PORT


def get_account_projection_port() -> Any:
    """Retorna el adaptador real de proyeccion ledger -> `held`."""
    from app.modules.accounts.repository import ACCOUNTS_LEDGER_PROJECTION

    return ACCOUNTS_LEDGER_PROJECTION


def get_default_ports() -> tuple[Any, Any]:
    """Retorna `(balance_port, account_projection)` reales (sin mutar nada)."""
    return (get_balance_port(), get_account_projection_port())


def resolve_balance_port(explicit: Any | None, fallback: Any | None = None) -> Any:
    """Resuelve el `BalancePort` productivo: explicito > real > fallback.

    Nunca lanza `NotImplementedError` por si mismo: si el adaptador real no
    estuviera disponible, retorna el `fallback` (stub de seguridad).
    """
    if explicit is not None:
        return explicit
    try:
        return get_balance_port()
    except ImportError:
        if fallback is not None:
            return fallback
        from app.modules.transactions.service import DEFAULT_BALANCE_PORT

        return DEFAULT_BALANCE_PORT


def resolve_account_projection(explicit: Any | None, fallback: Any | None = None) -> Any:
    """Resuelve el `AccountProjectionPort` productivo: explicito > real."""
    if explicit is not None:
        return explicit
    try:
        return get_account_projection_port()
    except ImportError:
        if fallback is not None:
            return fallback
        from app.modules.ledger.repository.balances import (
            DEFAULT_ACCOUNT_PROJECTION,
        )

        return DEFAULT_ACCOUNT_PROJECTION


def wire_ports() -> dict[str, Any]:
    """Cableado explicito opcional: parchea los defaults globales al real.

    Idempotente. La ruta productiva ya no depende de esto (los puntos de uso
    resuelven via `resolve_*`), pero se mantiene para el arranque / validador
    H3 que espera un `wire_ports()` llamable. Retorna los ports cableados.
    """
    import app.modules.ledger.repository.balances as ledger_balances
    import app.modules.transactions.service as tx_service
    from app.modules.accounts.repository import (
        ACCOUNTS_BALANCE_PORT,
        ACCOUNTS_LEDGER_PROJECTION,
    )

    tx_service.DEFAULT_BALANCE_PORT = ACCOUNTS_BALANCE_PORT
    ledger_balances.DEFAULT_ACCOUNT_PROJECTION = ACCOUNTS_LEDGER_PROJECTION
    try:
        import app.modules.ledger.repository as ledger_repo

        ledger_repo.DEFAULT_ACCOUNT_PROJECTION = ACCOUNTS_LEDGER_PROJECTION
    except (ImportError, AttributeError):
        pass
    return {
        "balance_port": ACCOUNTS_BALANCE_PORT,
        "account_projection": ACCOUNTS_LEDGER_PROJECTION,
    }


def is_wired() -> bool:
    """`True` si los defaults globales ya apuntan a los adaptadores reales."""
    try:
        import app.modules.ledger.repository.balances as ledger_balances
        import app.modules.transactions.service as tx_service

        tx_ok = type(tx_service.DEFAULT_BALANCE_PORT).__name__ == ("AccountsBalanceAdapter")
        lx_ok = (
            type(ledger_balances.DEFAULT_ACCOUNT_PROJECTION).__name__
            == "AccountsLedgerProjectionAdapter"
        )
        return bool(tx_ok and lx_ok)
    except ImportError:
        return False


__all__ = [
    "get_account_projection_port",
    "get_balance_port",
    "get_default_ports",
    "is_wired",
    "resolve_account_projection",
    "resolve_balance_port",
    "wire_ports",
]
