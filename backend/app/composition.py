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


# ------------------------------------------------- Cableado E6-T14
# Cadena "no reconozco" (HU22): `notifications` emite
# `security.operation_reported` -> `risk` escala (severidad + auditoria
# + `fraud.alert.raised`) -> `identity` bloquea (revoca sesiones).
# Cada consumidor es D-04 (sesion/`commit` propios, idempotente por
# `processed_events`); aqui solo se suscriben al bus, sin logica.


def subscribe_unrecognized_reports(bus: Any | None = None) -> Any:
    """Suscribe `on_unrecognized_operation_reported` al bus (E6-T14).

    Idempotente: si el handler ya esta registrado no lo duplica.
    `bus` por defecto es el bus global (`app.core.bus`). Retorna el
    handler registrado.
    """
    from app.core import events as domain_events
    from app.modules.risk.events import on_unrecognized_operation_reported

    if bus is None:
        from app.core.bus import get_default_bus as _default_bus

        bus = _default_bus()
    handlers = list(
        getattr(bus, "_handlers", {}).get(domain_events.SECURITY_OPERATION_REPORTED, [])
    )
    if on_unrecognized_operation_reported not in handlers:
        bus.subscribe(domain_events.SECURITY_OPERATION_REPORTED, on_unrecognized_operation_reported)
    return on_unrecognized_operation_reported


def is_unrecognized_reports_wired(bus: Any | None = None) -> bool:
    """`True` si el consumidor del reporte ya esta suscrito al bus."""
    from app.core import events as domain_events
    from app.modules.risk.events import on_unrecognized_operation_reported

    if bus is None:
        from app.core.bus import get_default_bus as _default_bus

        bus = _default_bus()
    try:
        return bus.handler_count(domain_events.SECURITY_OPERATION_REPORTED) > 0 and (
            on_unrecognized_operation_reported
            in bus._handlers.get(domain_events.SECURITY_OPERATION_REPORTED, [])
        )
    except AttributeError:
        return False


def subscribe_channel_lock(bus: Any | None = None) -> Any:
    """Suscribe `on_fraud_alert_raised` al bus (E6-T14, bloqueo).

    Idempotente: si el handler ya esta registrado no lo duplica.
    `bus` por defecto es el bus global (`app.core.bus`). Retorna el
    handler registrado.
    """
    from app.core import events as domain_events
    from app.modules.identity.events import on_fraud_alert_raised

    if bus is None:
        from app.core.bus import get_default_bus as _default_bus

        bus = _default_bus()
    handlers = list(getattr(bus, "_handlers", {}).get(domain_events.FRAUD_ALERT_RAISED, []))
    if on_fraud_alert_raised not in handlers:
        bus.subscribe(domain_events.FRAUD_ALERT_RAISED, on_fraud_alert_raised)
    return on_fraud_alert_raised


def is_channel_lock_wired(bus: Any | None = None) -> bool:
    """`True` si el consumidor de bloqueo ya esta suscrito al bus."""
    from app.core import events as domain_events
    from app.modules.identity.events import on_fraud_alert_raised

    if bus is None:
        from app.core.bus import get_default_bus as _default_bus

        bus = _default_bus()
    try:
        return bus.handler_count(domain_events.FRAUD_ALERT_RAISED) > 0 and (
            on_fraud_alert_raised in bus._handlers.get(domain_events.FRAUD_ALERT_RAISED, [])
        )
    except AttributeError:
        return False


# ------------------------------------------------- Cableado A1 (HU22 CA-01)
# Alertas de seguridad multicanal E2E: `notifications` consume los eventos
# de negocio sensibles ya emitidos por `outbox` y el login (solo correo
# de respaldo: el push lo emite `risk`/E2-T20, sin duplicar). Cada
# consumidor es D-04 (sesion/`commit` propios, idempotente por
# `processed_events`); aqui solo se suscriben al bus, sin logica, mas el
# registro del resolutor del correo del titular (A3, fachada
# `holder_contact`, D-03: `composition` SI puede importar `identity`;
# `notifications/service` no, por frontera).


def wire_security_email_resolver() -> Any:
    """Registra el resolutor del correo de respaldo (A3, HU22).

    Idempotente. Import perezoso a `identity.service.holder_contact`
    (D-03, mismo patron que la copia certificada de HU11 en `credits`).
    Retorna el resolutor registrado (o `None` si `identity` no esta
    disponible: rige el fallback `str(user_id)` del servicio).
    """
    try:
        from app.modules.identity.service import holder_contact as _holder_contact
    except ImportError:
        return None
    try:
        from app.modules.notifications import service as _notifications_service
    except ImportError:  # pragma: no cover - el modulo existe en el repo
        return None
    _notifications_service.set_security_email_resolver(_holder_contact.get_holder_email)
    return _holder_contact.get_holder_email


def subscribe_security_alerts(bus: Any | None = None) -> Any:
    """Suscribe las alertas de seguridad multicanal al bus (A1, HU22).

    Idempotente: no duplica handlers ya registrados. Suscribe
    `notifications.events.on_auth_failed_attempt` (solo correo) a
    `auth.failed_attempt` y `on_sensitive_business_event` (push +
    correo) a cada evento de `SENSITIVE_BUSINESS_EVENTS`, y registra el
    resolutor del correo (A3). `bus` por defecto es el bus global.
    Retorna el handler de negocio registrado.
    """
    from app.core import events as domain_events
    from app.modules.notifications import events as notifications_events

    if bus is None:
        from app.core.bus import get_default_bus as _default_bus

        bus = _default_bus()
    wire_security_email_resolver()
    login_handlers = list(getattr(bus, "_handlers", {}).get(domain_events.AUTH_FAILED_ATTEMPT, []))
    if notifications_events.on_auth_failed_attempt not in login_handlers:
        bus.subscribe(
            domain_events.AUTH_FAILED_ATTEMPT, notifications_events.on_auth_failed_attempt
        )
    for event_type in notifications_events.SENSITIVE_BUSINESS_EVENTS:
        handlers = list(getattr(bus, "_handlers", {}).get(event_type, []))
        if notifications_events.on_sensitive_business_event not in handlers:
            bus.subscribe(event_type, notifications_events.on_sensitive_business_event)
    return notifications_events.on_sensitive_business_event


def is_security_alerts_wired(bus: Any | None = None) -> bool:
    """`True` si las alertas de seguridad estan suscritas (A1)."""
    from app.core import events as domain_events
    from app.modules.notifications import events as notifications_events

    if bus is None:
        from app.core.bus import get_default_bus as _default_bus

        bus = _default_bus()
    try:
        handlers = getattr(bus, "_handlers", {})
        if notifications_events.on_auth_failed_attempt not in handlers.get(
            domain_events.AUTH_FAILED_ATTEMPT, []
        ):
            return False
        return all(
            notifications_events.on_sensitive_business_event in handlers.get(event_type, [])
            for event_type in notifications_events.SENSITIVE_BUSINESS_EVENTS
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


# ------------------------------------------------- Jobs E3-T19 (HU12 CA-03/CA-04)
# Debito automatico + recordatorios de cuota (`credits/jobs`). Cada job
# gestiona su propio `commit` por item (idempotentes y re-ejecutables);
# aqui solo se abre/cierra la sesion y se leen los flags de entorno
# (regla de oro 6: encendido y periodicidad fuera del codigo). No se
# toca `liveness`/`lifespan`: el despliegue registra
# `start_credit_loan_jobs_worker` o invoca `run_credit_loan_jobs_once`
# desde su programador (cron); los tests llaman a los `run_*_once`
# directamente con su sesion.


def credit_loan_jobs_enabled() -> bool:
    """Interruptor general de los jobs de cuotas (`1` por defecto)."""
    import os as _os

    return (_os.getenv("CREDIT_LOAN_JOBS_ENABLED", "1").strip().lower() or "1") not in (
        "0",
        "false",
        "no",
    )


def credit_loan_auto_debit_enabled() -> bool:
    """Interruptor del debito automatico (`1` por defecto)."""
    import os as _os

    return (_os.getenv("CREDIT_LOANS_AUTO_DEBIT_ENABLED", "1").strip().lower() or "1") not in (
        "0",
        "false",
        "no",
    )


def credit_loan_reminders_enabled() -> bool:
    """Interruptor de los recordatorios (`1` por defecto)."""
    import os as _os

    return (_os.getenv("CREDIT_LOANS_REMINDERS_ENABLED", "1").strip().lower() or "1") not in (
        "0",
        "false",
        "no",
    )


def credit_loan_jobs_interval_seconds() -> float:
    """Periodicidad del worker en segundos (env, defecto 3600 s)."""
    import os as _os

    try:
        return max(1.0, float(_os.getenv("CREDIT_LOANS_JOBS_INTERVAL_SECONDS", "3600")))
    except ValueError:
        return 3600.0


def run_credit_loan_jobs_once(
    *,
    session_factory: Any | None = None,
    today: Any | None = None,
    auto_debit: bool | None = None,
    reminders: bool | None = None,
    limit: int = 100,
    sender: Any | None = None,
) -> dict[str, Any]:
    """Una pasada de los jobs de cuotas con sesion propia (E3-T19).

    Ejecuta el debito automatico y/o los recordatorios segun los
    flags (parametro explicito o env `CREDIT_LOANS_*_ENABLED`); cada
    job confirma por item con su propio `commit`. Retorna
    `{"auto_debit": {...} | {"skipped": ...}, "reminders": {...} |
    {"skipped": ...}}`. Ante un fallo fuera de los items se revierte
    y re-lanza (los items ya confirmados quedan; el reintento los
    omite por idempotencia).
    """
    from app.modules.credits.jobs.auto_debit import run_auto_debit_once
    from app.modules.credits.jobs.installment_reminders import run_installment_reminders_once

    if session_factory is None:
        from app.core.db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    run_debit = credit_loan_auto_debit_enabled() if auto_debit is None else bool(auto_debit)
    run_remind = credit_loan_reminders_enabled() if reminders is None else bool(reminders)
    if not credit_loan_jobs_enabled():
        run_debit = False
        run_remind = False
    session = session_factory()
    try:
        result: dict[str, Any] = {}
        result["auto_debit"] = (
            run_auto_debit_once(session, today=today, limit=limit, sender=sender)
            if run_debit
            else {"skipped": "disabled"}
        )
        result["reminders"] = (
            run_installment_reminders_once(session, today=today, limit=limit, sender=sender)
            if run_remind
            else {"skipped": "disabled"}
        )
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


def start_credit_loan_jobs_worker(
    *,
    session_factory: Any | None = None,
    interval_seconds: float | None = None,
    stop_event: Any | None = None,
    **run_kwargs: Any,
) -> tuple[Any, Any]:
    """Arranca el worker periodico de los jobs de cuotas en hilo daemon (E3-T19).

    Cada ciclo es `run_credit_loan_jobs_once` (sesion/`commit`
    propios); los errores del ciclo se tragan (best-effort) para no
    matar el hilo. Retorna `(thread, stop_event)`; fijar `stop_event`
    detiene el ciclo. No bloquea al llamante. `interval_seconds` lo
    fija el arranque desde entorno (`credit_loan_jobs_interval_seconds`).
    """
    import threading as _threading

    if interval_seconds is None:
        interval_seconds = credit_loan_jobs_interval_seconds()
    if stop_event is None:
        stop_event = _threading.Event()

    def _loop() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                run_credit_loan_jobs_once(
                    session_factory=session_factory,
                    **run_kwargs,
                )
            except Exception:  # noqa: BLE001, S112 - el worker nunca muere
                continue

    thread = _threading.Thread(
        name="credit-loan-jobs",
        target=_loop,
        daemon=True,
    )
    thread.start()
    return thread, stop_event


# ------------------------------------------------- Jobs E4-T07 (HU14 CA-02) + R-1 (HU15)
# Caducidad del QR (`wallet/jobs/qr_expiry.py`) + reconciliacion del
# cobro (`wallet/jobs/qr_reconcile.py`). Cada job gestiona su propio
# `commit` por item (idempotentes y re-ejecutables); aqui solo se
# abre/cierra la sesion y se leen los flags de entorno (regla de oro 6:
# encendido y periodicidad fuera del codigo). Ninguno mueve dinero (la
# caducidad solo toca `status` de `qr_charges`; la reconciliacion solo
# cierra cobros cuya operacion del motor ya liquido). El despliegue
# registra `start_wallet_qr_jobs_worker` o invoca
# `run_wallet_qr_jobs_once` desde su programador (cron); los tests
# llaman a los `run_*_once` directamente con su sesion.


def wallet_qr_jobs_enabled() -> bool:
    """Interruptor general de los jobs de QR (`1` por defecto)."""
    import os as _os

    return (_os.getenv("WALLET_QR_JOBS_ENABLED", "1").strip().lower() or "1") not in (
        "0",
        "false",
        "no",
    )


def wallet_qr_expiry_enabled() -> bool:
    """Interruptor de la caducidad (`1` por defecto)."""
    import os as _os

    return (_os.getenv("WALLET_QR_EXPIRY_ENABLED", "1").strip().lower() or "1") not in (
        "0",
        "false",
        "no",
    )


def wallet_qr_reconcile_enabled() -> bool:
    """Interruptor de la reconciliacion del cobro (R-1, `1` por defecto)."""
    import os as _os

    return (_os.getenv("WALLET_QR_RECONCILE_ENABLED", "1").strip().lower() or "1") not in (
        "0",
        "false",
        "no",
    )


def wallet_qr_jobs_interval_seconds() -> float:
    """Periodicidad del worker en segundos (env, defecto 60 s).

    Barrido corto frente al TTL (`wallet.qr_ttl_seconds`, 300 s) para
    que un vencido quede `EXPIRED` sin esperar al siguiente ciclo
    (la defensa on-read `is_charge_valid` cubre el intermedio).
    """
    import os as _os

    try:
        return max(1.0, float(_os.getenv("WALLET_QR_JOBS_INTERVAL_SECONDS", "60")))
    except ValueError:
        return 60.0


def run_wallet_qr_jobs_once(
    *,
    session_factory: Any | None = None,
    now: Any | None = None,
    limit: int = 100,
    expiry: bool | None = None,
    reconcile: bool | None = None,
) -> dict[str, Any]:
    """Una pasada de los jobs de QR con sesion propia (E4-T07 + R-1).

    Ejecuta la caducidad y/o la reconciliacion segun los flags
    (parametro explicito o env `WALLET_QR_EXPIRY_ENABLED` /
    `WALLET_QR_RECONCILE_ENABLED`); cada job confirma por item con su
    propio `commit`. La reconciliacion corre **antes** que la caducidad:
    un cobro autorizado (`Transaction` `SETTLED`) se cierra a `PAID`
    antes de que el vencimiento pueda pasarlo a `EXPIRED` sin aviso
    (R-1). Retorna `{"qr_reconcile": {...} | {"skipped": "disabled"},
    "qr_expiry": {...} | {"skipped": "disabled"}}`. Ante un fallo fuera
    de los items se revierte y re-lanza (los items ya confirmados
    quedan; el reintento los omite por idempotencia).
    """
    from app.modules.wallet.jobs.qr_expiry import run_qr_expiry_once
    from app.modules.wallet.jobs.qr_reconcile import run_qr_reconcile_once

    if session_factory is None:
        from app.core.db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    run_expiry = wallet_qr_expiry_enabled() if expiry is None else bool(expiry)
    run_reconcile = wallet_qr_reconcile_enabled() if reconcile is None else bool(reconcile)
    if not wallet_qr_jobs_enabled():
        run_expiry = False
        run_reconcile = False
    session = session_factory()
    try:
        result: dict[str, Any] = {}
        result["qr_reconcile"] = (
            run_qr_reconcile_once(session, limit=limit)
            if run_reconcile
            else {"skipped": "disabled"}
        )
        result["qr_expiry"] = (
            run_qr_expiry_once(session, now=now, limit=limit)
            if run_expiry
            else {"skipped": "disabled"}
        )
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


def start_wallet_qr_jobs_worker(
    *,
    session_factory: Any | None = None,
    interval_seconds: float | None = None,
    stop_event: Any | None = None,
    **run_kwargs: Any,
) -> tuple[Any, Any]:
    """Arranca el worker periodico de los jobs de QR en hilo daemon (E4-T07 + R-1).

    Cada ciclo es `run_wallet_qr_jobs_once` (sesion/`commit`
    propios: reconciliacion del cobro y luego caducidad); los errores
    del ciclo se tragan (best-effort) para no matar el hilo. Retorna
    `(thread, stop_event)`; fijar `stop_event` detiene el ciclo. No
    bloquea al llamante. `interval_seconds` lo fija el arranque desde
    entorno (`wallet_qr_jobs_interval_seconds`).
    """
    import threading as _threading

    if interval_seconds is None:
        interval_seconds = wallet_qr_jobs_interval_seconds()
    if stop_event is None:
        stop_event = _threading.Event()

    def _loop() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                run_wallet_qr_jobs_once(
                    session_factory=session_factory,
                    **run_kwargs,
                )
            except Exception:  # noqa: BLE001, S112 - el worker nunca muere
                continue

    thread = _threading.Thread(
        name="wallet-qr-jobs",
        target=_loop,
        daemon=True,
    )
    thread.start()
    return thread, stop_event


# ------------------------------------------------- Jobs E4-T18 (HU16 CA-03)
# Recordatorio de vencimiento de servicios (`wallet/jobs/
# service_reminders.py`). El job gestiona su propio `commit` por item
# (idempotente y re-ejecutable); aqui solo se abre/cierra la sesion y
# se leen los flags de entorno (regla de oro 6: encendido y
# periodicidad fuera del codigo). No mueve dinero (solo un push
# best-effort por la fachada `notifications.send`, sin PII). El
# despliegue registra `start_wallet_service_jobs_worker` o invoca
# `run_wallet_service_jobs_once` desde su programador (cron); los
# tests llaman a `run_service_reminders_once` directamente con su
# sesion.


def wallet_service_jobs_enabled() -> bool:
    """Interruptor general de los jobs de servicios (`1` por defecto)."""
    import os as _os

    return (_os.getenv("WALLET_SERVICE_JOBS_ENABLED", "1").strip().lower() or "1") not in (
        "0",
        "false",
        "no",
    )


def wallet_service_reminders_enabled() -> bool:
    """Interruptor de los recordatorios (`1` por defecto)."""
    import os as _os

    return (_os.getenv("WALLET_SERVICE_REMINDERS_ENABLED", "1").strip().lower() or "1") not in (
        "0",
        "false",
        "no",
    )


def wallet_service_jobs_interval_seconds() -> float:
    """Periodicidad del worker en segundos (env, defecto 3600 s)."""
    import os as _os

    try:
        return max(1.0, float(_os.getenv("WALLET_SERVICE_JOBS_INTERVAL_SECONDS", "3600")))
    except ValueError:
        return 3600.0


def run_wallet_service_jobs_once(
    *,
    session_factory: Any | None = None,
    now: Any | None = None,
    limit: int = 100,
    reminders: bool | None = None,
    sender: Any | None = None,
) -> dict[str, Any]:
    """Una pasada de los jobs de servicios con sesion propia (E4-T18).

    Ejecuta los recordatorios segun el flag (parametro explicito o
    env `WALLET_SERVICE_REMINDERS_ENABLED`); el job confirma por item
    con su propio `commit`. Retorna `{"reminders": {...} |
    {"skipped": "disabled"}}`. Ante un fallo fuera de los items se
    revierte y re-lanza (los items ya confirmados quedan; el reintento
    los omite por idempotencia).
    """
    from app.modules.wallet.jobs.service_reminders import run_service_reminders_once

    if session_factory is None:
        from app.core.db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    run_remind = wallet_service_reminders_enabled() if reminders is None else bool(reminders)
    if not wallet_service_jobs_enabled():
        run_remind = False
    session = session_factory()
    try:
        result: dict[str, Any] = {}
        result["reminders"] = (
            run_service_reminders_once(session, now=now, limit=limit, sender=sender)
            if run_remind
            else {"skipped": "disabled"}
        )
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


def start_wallet_service_jobs_worker(
    *,
    session_factory: Any | None = None,
    interval_seconds: float | None = None,
    stop_event: Any | None = None,
    **run_kwargs: Any,
) -> tuple[Any, Any]:
    """Arranca el worker periodico de los jobs de servicios en hilo daemon (E4-T18).

    Cada ciclo es `run_wallet_service_jobs_once` (sesion/`commit`
    propios); los errores del ciclo se tragan (best-effort) para no
    matar el hilo. Retorna `(thread, stop_event)`; fijar `stop_event`
    detiene el ciclo. No bloquea al llamante. `interval_seconds` lo
    fija el arranque desde entorno (`wallet_service_jobs_interval_seconds`).
    """
    import threading as _threading

    if interval_seconds is None:
        interval_seconds = wallet_service_jobs_interval_seconds()
    if stop_event is None:
        stop_event = _threading.Event()

    def _loop() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                run_wallet_service_jobs_once(
                    session_factory=session_factory,
                    **run_kwargs,
                )
            except Exception:  # noqa: BLE001, S112 - el worker nunca muere
                continue

    thread = _threading.Thread(
        name="wallet-service-jobs",
        target=_loop,
        daemon=True,
    )
    thread.start()
    return thread, stop_event


__all__ = [
    "credit_loan_auto_debit_enabled",
    "credit_loan_jobs_enabled",
    "credit_loan_jobs_interval_seconds",
    "credit_loan_reminders_enabled",
    "get_account_projection_port",
    "get_balance_port",
    "get_default_ports",
    "is_channel_lock_wired",
    "is_movements_wired",
    "is_risk_security_wired",
    "is_security_alerts_wired",
    "is_unrecognized_reports_wired",
    "is_wired",
    "resolve_account_projection",
    "resolve_balance_port",
    "run_credit_loan_jobs_once",
    "run_outbox_once",
    "run_wallet_qr_jobs_once",
    "run_wallet_service_jobs_once",
    "start_credit_loan_jobs_worker",
    "start_outbox_worker",
    "start_wallet_qr_jobs_worker",
    "start_wallet_service_jobs_worker",
    "subscribe_accounts_movements",
    "subscribe_channel_lock",
    "subscribe_risk_security",
    "subscribe_security_alerts",
    "subscribe_unrecognized_reports",
    "wallet_qr_expiry_enabled",
    "wallet_qr_jobs_enabled",
    "wallet_qr_jobs_interval_seconds",
    "wallet_qr_reconcile_enabled",
    "wallet_service_jobs_enabled",
    "wallet_service_jobs_interval_seconds",
    "wallet_service_reminders_enabled",
    "wire_ports",
    "wire_security_email_resolver",
]
