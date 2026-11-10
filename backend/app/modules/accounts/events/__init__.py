"""Consumidor `ledger.entry.posted` -> `movements_view` (E2-T23, HU05 CA-03).

Contrato D-04: el bus `app.core.bus` entrega **solo el `entry`** (registro
de `shared.outbox`, sin sesion). `on_ledger_entry_posted(entry)` abre **su
propia sesion** con `commit` propio, deriva `event_id = entry.id`, mapea
`entry.payload` (`entry_id`, `entry_type`, `transaction_id`, `description`,
`value_date`, `postings`) y llama a `handle_movement_event(...)` (idempotente por
`processed_events`, consumidor `"accounts-movements"`).

Sin imports a modelos `ledger`: solo dicts del payload por evento/fachada
(regla de oro 4). Sin PII ni secretos en logs. Ante fallo se revierte y se
re-lanza para que el worker aplique reintento con backoff (diseno E5-T05).

`handle_ledger_entry_posted` (retenidos) NO se suscribe aqui: `held` lo
sincroniza hoy `AccountsLedgerProjectionAdapter` en la misma transaccion
del asiento; suscribirlo al bus sin reconciliacion previa aplicaria dos
veces el efecto. Solo `movements_view` (tabla de lectura) se alimenta por
este canal.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any


def _parse_value_date(value: date | datetime | str | None) -> date | None:
    """`value_date` del payload (ISO) a `date` (pasa `date`/`None`)."""
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return date.fromisoformat(text)
    raise TypeError(f"value_date debe ser DATE/ISO, recibido: {value!r}")


def on_ledger_entry_posted(entry: Any, *, session_factory: Callable[[], Any] | None = None) -> bool:
    """Handler del bus para `ledger.entry.posted` (D-04).

    Abre su propia sesion (`session_factory` o `SessionLocal`), deriva
    `event_id = entry.id`, mapea `entry.payload` y delega en
    `handle_movement_event`. `commit` propio si proyecta; `rollback` y
    re-lanza ante fallo (el worker reintenta). Retorna lo del consumidor
    (`True` primer consumo, `False` duplicado ignorado).
    """
    from app.modules.accounts.repository.movements import handle_movement_event

    if session_factory is None:
        from app.core.db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    payload = dict(entry.payload or {})
    session = session_factory()
    try:
        consumed = handle_movement_event(
            session,
            event_id=entry.id,
            entry_id=payload.get("entry_id", str(entry.id)),
            postings=list(payload.get("postings") or []),
            transaction_id=payload.get("transaction_id"),
            description=payload.get("description"),
            entry_type=payload.get("entry_type"),
            value_date=_parse_value_date(payload.get("value_date")),
        )
        session.commit()
        return consumed
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


__all__ = ["on_ledger_entry_posted"]
