"""Consumidor `auth.failed_attempt` -> `risk` + notificacion (E2-T20, HU08).

Contrato D-04 (mismo que `accounts.events`, E2-T23): el bus
`app.core.bus` entrega **solo el `entry`** (registro de
`shared.outbox`, sin sesion). `on_auth_failed_attempt(entry)` abre
**su propia sesion** con `commit` propio, deriva
`event_id = entry.id`, mapea `entry.payload` y delega en
`risk.service.handle_auth_failed_attempt` (idempotente por
`processed_events`, consumidor `"risk-auth-failures"`).

Sin imports a modelos ajenos: solo dicts del payload por
evento/fachada (regla de oro 4). Sin PII ni secretos en logs. No mueve
dinero (regla de oro 1): solo registra severidad, audita y notifica.
Ante fallo se revierte y se re-lanza para que el worker aplique
reintento con backoff (diseno E5-T05).

Extension E6-T03 (HU20): el perfil de comportamiento se alimenta con
`on_profile_transaction_initiated` / `on_profile_transfer_settled` /
`on_profile_login_succeeded` / `on_profile_auth_failed_attempt`
(consumidor `"risk-profile"`, idempotente por `processed_events`):
monto promedio (media incremental entera), horas/paises habituales y
ultimo dispositivo. El `score` lo sigue poseyendo `E2-T20`/`E6-T14`.
`auth.login_succeeded` no existe en `app.core.events` (lo emite
`identity`); aqui se referencia por su literal (identico al canonico
de `identity.service.device_login.LOGIN_SUCCEEDED_EVENT`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: `auth.login_succeeded` (literal canonico de `identity`; sin PII).
LOGIN_SUCCEEDED_EVENT = "auth.login_succeeded"


def on_auth_failed_attempt(
    entry: Any,
    *,
    session_factory: Callable[[], Any] | None = None,
    sender: Any | None = None,
) -> bool:
    """Handler del bus para `auth.failed_attempt` (D-04).

    Abre su propia sesion (`session_factory` o `SessionLocal`), deriva
    `event_id = entry.id`, mapea `entry.payload` y delega en el servicio.
    `commit` propio si registra; `rollback` y re-lanza ante fallo (el
    worker reintenta). Retorna `True` en el primer consumo, `False` ante
    duplicado ignorado. `sender` se inyecta (tests sin red).
    """
    from app.modules.risk.service import handle_auth_failed_attempt

    if session_factory is None:
        from app.core.db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    payload = dict(entry.payload or {})
    session = session_factory()
    try:
        outcome = handle_auth_failed_attempt(
            session,
            event_id=entry.id,
            payload=payload,
            sender=sender,
        )
        session.commit()
        return bool(outcome.get("consumed"))
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


def on_unrecognized_operation_reported(
    entry: Any,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Handler del bus para `security.operation_reported` (D-04, E6-T14).

    Abre su propia sesion (`session_factory` o `SessionLocal`), deriva
    `event_id = entry.id`, mapea `entry.payload` y delega en
    `risk.service.handle_unrecognized_operation_report` (idempotente por
    `processed_events`, consumidor `"risk-unrecognized-reports"`).
    `commit` propio si escala; `rollback` y re-lanza ante fallo (el
    worker reintenta). Retorna `True` en el primer consumo, `False` ante
    duplicado ignorado. Sin PII ni secretos en logs. No mueve dinero
    (regla de oro 1): solo registra severidad, audita y emite
    `fraud.alert.raised`. Nunca escribe `risk.alerts` (HU20, D-16).
    """
    from app.modules.risk.service import handle_unrecognized_operation_report

    if session_factory is None:
        from app.core.db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    payload = dict(entry.payload or {})
    session = session_factory()
    try:
        outcome = handle_unrecognized_operation_report(
            session,
            event_id=entry.id,
            payload=payload,
        )
        session.commit()
        return bool(outcome.get("consumed"))
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


__all__ = [
    "LOGIN_SUCCEEDED_EVENT",
    "on_auth_failed_attempt",
    "on_profile_auth_failed_attempt",
    "on_profile_login_succeeded",
    "on_profile_transaction_initiated",
    "on_profile_transfer_settled",
    "on_transaction_initiated",
    "on_transfer_settled",
    "on_unrecognized_operation_reported",
]


def on_transaction_initiated(
    entry: Any,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Handler del bus para `transaction.initiated` (D-04, E6-T02 corte 2).

    Abre su propia sesion (`session_factory` o `SessionLocal`), deriva
    `event_id = entry.id`, mapea `entry.payload` y delega en
    `risk.service.handle_transaction_initiated` (idempotente por
    `processed_events`, consumidor `"risk-tx-initiated"`). `commit`
    propio si audita; `rollback` y re-lanza ante fallo (el worker
    reintenta). Retorna `True` en el primer consumo, `False` ante
    duplicado ignorado. Sin PII ni secretos en logs. No mueve dinero
    (regla de oro 1): solo evalua y audita (`risk.assessment`).
    """
    from app.modules.risk.service import handle_transaction_initiated

    if session_factory is None:
        from app.core.db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    payload = dict(entry.payload or {})
    session = session_factory()
    try:
        outcome = handle_transaction_initiated(
            session,
            event_id=entry.id,
            payload=payload,
        )
        session.commit()
        return bool(outcome.get("consumed"))
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


def on_transfer_settled(
    entry: Any,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Handler del bus para `transfer.settled` (D-04, E6-T02 corte 2).

    Mismo contrato que `on_transaction_initiated` (consumidor
    `"risk-tx-settled"`): reevalua tras liquidar y audita
    `risk.assessment` solo con hallazgo. `commit` propio; `rollback` y
    re-lanza ante fallo. Sin PII; no mueve dinero.
    """
    from app.modules.risk.service import handle_transfer_settled

    if session_factory is None:
        from app.core.db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    payload = dict(entry.payload or {})
    session = session_factory()
    try:
        outcome = handle_transfer_settled(
            session,
            event_id=entry.id,
            payload=payload,
        )
        session.commit()
        return bool(outcome.get("consumed"))
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


def _run_profile_handler(
    entry: Any,
    handler_name: str,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Ejecuta un handler del perfil con sesion/`commit` propios (D-04).

    Delega en `risk.service.handle_profile_*` (idempotentes por
    `processed_events`, consumidor `"risk-profile"`). `commit` propio
    si alimenta; `rollback` y re-lanza ante fallo (el worker
    reintenta). Retorna `True` en el primer consumo, `False` ante
    duplicado ignorado. Sin PII ni secretos en logs. No mueve dinero
    (regla de oro 1): solo alimenta habitos del perfil.
    """
    from app.modules.risk import service as risk_service

    if session_factory is None:
        from app.core.db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    handle = getattr(risk_service, handler_name)
    payload = dict(entry.payload or {})
    session = session_factory()
    try:
        outcome = handle(
            session,
            event_id=entry.id,
            payload=payload,
            at=getattr(entry, "created_at", None),
        )
        session.commit()
        return bool(outcome.get("consumed"))
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


def on_profile_transaction_initiated(
    entry: Any,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Handler del bus para `transaction.initiated` (D-04, E6-T03).

    Alimenta el perfil (monto/moneda/hora/pais/dispositivo) via
    `risk.service.handle_profile_transaction` (consumidor
    `"risk-profile"`). Delega el `at` (hora UTC) en el instante del
    `entry`; la hora fina la aporta la operacion leida por fachada.
    """
    from app.modules.risk import service as risk_service

    if session_factory is None:
        from app.core.db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    payload = dict(entry.payload or {})
    session = session_factory()
    try:
        outcome = risk_service.handle_profile_transaction(
            session,
            event_id=entry.id,
            payload=payload,
            source="transaction.initiated",
        )
        session.commit()
        return bool(outcome.get("consumed"))
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


def on_profile_transfer_settled(
    entry: Any,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Handler del bus para `transfer.settled` (D-04, E6-T03).

    Mismo contrato que `on_profile_transaction_initiated`
    (consumidor `"risk-profile"`): re-alimenta tras liquidar (la
    ventana del perfil ya incluye la operacion).
    """
    from app.modules.risk import service as risk_service

    if session_factory is None:
        from app.core.db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    payload = dict(entry.payload or {})
    session = session_factory()
    try:
        outcome = risk_service.handle_profile_transaction(
            session,
            event_id=entry.id,
            payload=payload,
            source="transfer.settled",
        )
        session.commit()
        return bool(outcome.get("consumed"))
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


def on_profile_login_succeeded(
    entry: Any,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Handler del bus para `auth.login_succeeded` (D-04, E6-T03).

    Alimenta hora/dispositivo/pais (si el payload los trae) via
    `risk.service.handle_profile_login` (consumidor `"risk-profile"`).
    """
    return _run_profile_handler(entry, "handle_profile_login", session_factory=session_factory)


def on_profile_auth_failed_attempt(
    entry: Any,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Handler del bus para `auth.failed_attempt` (D-04, E6-T03).

    Complementa hora/dispositivo via
    `risk.service.handle_profile_failed_attempt` (consumidor
    `"risk-profile"`): NO toca `score` ni notifica (eso lo posee
    `E2-T20`; aqui solo habitos).
    """
    return _run_profile_handler(
        entry, "handle_profile_failed_attempt", session_factory=session_factory
    )
