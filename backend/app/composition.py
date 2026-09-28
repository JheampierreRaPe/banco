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


# ------------------------------------------------- Cableado E2-T23
# Consumidor `ledger.entry.posted` -> `movements_view` en produccion.
# `handle_ledger_entry_posted` (retenidos) NO se suscribe: `held` lo
# sincroniza `AccountsLedgerProjectionAdapter` en la transaccion del
# asiento; suscribirlo sin reconciliar duplicaria el efecto.


def subscribe_accounts_movements(bus: Any | None = None) -> Any:
    """Suscribe `on_ledger_entry_posted` al bus (E2-T23, HU05).

    Idempotente: si el handler ya esta registrado no lo duplica.
    `bus` por defecto es el bus global (`app.core.bus`). Retorna el
    handler registrado (el arranque real lo usa; los tests lo verifican).
    """
    from app.core import events as domain_events
    from app.modules.accounts.events import on_ledger_entry_posted

    if bus is None:
        from app.core.bus import get_default_bus as _default_bus

        bus = _default_bus()
    handlers = list(getattr(bus, "_handlers", {}).get(domain_events.LEDGER_ENTRY_POSTED, []))
    if on_ledger_entry_posted not in handlers:
        bus.subscribe(domain_events.LEDGER_ENTRY_POSTED, on_ledger_entry_posted)
    return on_ledger_entry_posted


def is_movements_wired(bus: Any | None = None) -> bool:
    """`True` si el consumidor de movimientos ya esta suscrito al bus."""
    from app.core import events as domain_events
    from app.modules.accounts.events import on_ledger_entry_posted

    if bus is None:
        from app.core.bus import get_default_bus as _default_bus

        bus = _default_bus()
    try:
        return bus.handler_count(domain_events.LEDGER_ENTRY_POSTED) > 0 and (
            on_ledger_entry_posted in bus._handlers.get(domain_events.LEDGER_ENTRY_POSTED, [])
        )
    except AttributeError:
        return False


# ------------------------------------------------- Cableado E2-T20
# Consumidor `auth.failed_attempt` -> `risk` (severidad) + notificacion.
# Solo registra/sube severidad, audita y notifica: nunca mueve dinero.


def subscribe_risk_security(bus: Any | None = None) -> Any:
    """Suscribe `on_auth_failed_attempt` al bus (E2-T20, HU08 CA-04/CA-02).

    Idempotente: si el handler ya esta registrado no lo duplica.
    `bus` por defecto es el bus global (`app.core.bus`). Retorna el
    handler registrado (el arranque real lo usa; los tests lo verifican).
    """
    from app.core import events as domain_events
    from app.modules.risk.events import on_auth_failed_attempt

    if bus is None:
        from app.core.bus import get_default_bus as _default_bus

        bus = _default_bus()
    handlers = list(getattr(bus, "_handlers", {}).get(domain_events.AUTH_FAILED_ATTEMPT, []))
    if on_auth_failed_attempt not in handlers:
        bus.subscribe(domain_events.AUTH_FAILED_ATTEMPT, on_auth_failed_attempt)
    return on_auth_failed_attempt


def is_risk_security_wired(bus: Any | None = None) -> bool:
    """`True` si el consumidor de seguridad ya esta suscrito al bus."""
    from app.core import events as domain_events
    from app.modules.risk.events import on_auth_failed_attempt

    if bus is None:
        from app.core.bus import get_default_bus as _default_bus

        bus = _default_bus()
    try:
        return bus.handler_count(domain_events.AUTH_FAILED_ATTEMPT) > 0 and (
            on_auth_failed_attempt in bus._handlers.get(domain_events.AUTH_FAILED_ATTEMPT, [])
        )
    except AttributeError:
        return False


def run_outbox_once(
    *,
    session_factory: Any | None = None,
    bus: Any | None = None,
    limit: int = 100,
    max_attempts: int | None = None,
    backoff_base_seconds: int | None = None,
) -> dict[str, int]:
    """Una pasada del worker del outbox con sesion/`commit` propios (E2-T23).

    Abre su propia sesion (`session_factory` o `SessionLocal`), publica
    pendientes via `publish_pending` y hace `commit` (o `rollback` ante
    fallo y re-lanza). Sin bloquear el hilo del llamante mas que la pasada.
    `max_attempts`/`backoff_base_seconds` son configurables (regla de oro 6:
    el arranque los lee de entorno); `None` usa los defaults de `shared`.
    """
    if session_factory is None:
        from app.core.db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    from app.modules.shared import repository as shared_repo
    from app.modules.shared.jobs.publisher import publish_pending

    if max_attempts is None:
        max_attempts = shared_repo.DEFAULT_MAX_ATTEMPTS
    if backoff_base_seconds is None:
        backoff_base_seconds = shared_repo.DEFAULT_BACKOFF_BASE_SECONDS
    session = session_factory()
    try:
        result = publish_pending(
            session,
            bus=bus,
            limit=limit,
            max_attempts=max_attempts,
            backoff_base_seconds=backoff_base_seconds,
        )
        session.commit()
        return result
    except Exception:
        try:
            session.rollback()
        except Exception:  # noqa: BLE001, S110 - limpieza best-effort
            pass
        raise
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001, S110 - limpieza best-effort
            pass


def start_outbox_worker(
    *,
    session_factory: Any | None = None,
    bus: Any | None = None,
    interval_seconds: float = 2.0,
    stop_event: Any | None = None,
    **run_kwargs: Any,
) -> tuple[Any, Any]:
    """Arranca el worker periodico del outbox en un hilo daemon (E2-T23).

    Cada ciclo es `run_outbox_once` (sesion/`commit` propios); los errores
    del ciclo se tragan (best-effort) para no matar el hilo. Retorna
    `(thread, stop_event)`; fijar `stop_event` detiene el ciclo. No bloquea
    al llamante. `interval_seconds` lo fija el arranque desde entorno.
    """
    import threading as _threading

    if stop_event is None:
        stop_event = _threading.Event()

    def _loop() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                run_outbox_once(
                    session_factory=session_factory,
                    bus=bus,
                    **run_kwargs,
                )
            except Exception:  # noqa: BLE001, S112 - el worker nunca muere
                continue

    thread = _threading.Thread(
        name="outbox-publisher",
        target=_loop,
        daemon=True,
    )
    thread.start()
    return thread, stop_event


__all__ = [
    "get_account_projection_port",
    "get_balance_port",
    "get_default_ports",
    "is_movements_wired",
    "is_risk_security_wired",
    "is_wired",
    "resolve_account_projection",
    "resolve_balance_port",
    "run_outbox_once",
    "start_outbox_worker",
    "subscribe_accounts_movements",
    "subscribe_risk_security",
    "wire_ports",
]
