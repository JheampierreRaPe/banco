"""Job de liberacion de holds vencidos (E5-T06, HU17 CA-02).

Selecciona holds `ACTIVE` con `expires_at <= now`, los lleva a estado
terminal, mueve su transaccion a estado terminal y devuelve los fondos
retenidos al disponible con un asiento compensatorio nuevo
(`2100-X -> 2000-X`, `docs/04#7.3`). Idempotente: reprocesar no cambia
nada ni duplica historial/outbox/asientos.

Frontera (reglas de oro 1/2/3/4/8):
- Sin SQL directo: seleccion via `list_active_holds` + filtro Python y
  cambio de estado via `update_hold_status` (E5-T02). Sin consultas crudas
  ni UPDATE/DELETE manuales sobre `holds`/`transactions`.
- Ledger solo por su fachada (`ensure_customer_accounts` + `post_entry`,
  mismo patron de importacion que E5-T03): nunca crea/edita postings a
  mano ni revierte asientos existentes (regla de oro 2: solo se crea el
  compensatorio NUEVO, los asientos previos quedan intactos).
- Dinero entero en centimos; nunca `float`.
- Eventos solo por `outbox` con import perezoso (igual que E5-T03);
  best-effort: si el modulo/tabla no existe se continua sin publicar.

Decision documentada (estado terminal):
- Hold: `ACTIVE -> EXPIRED` (terminal E5-T02, via `update_hold_status`).
  `EXPIRED` (y no `RELEASED`) porque es el estado que el repositorio
  reserva para vencimiento (`expire_due_holds`); `RELEASED` queda para
  liberacion explicita pre-vencimiento (ruta de fallo E5-T03).
- Transaccion: `FUNDS_HELD/POSTED -> FAILED` (terminal `04#3`: "un FAILED
  siempre libera holds"). `FAILED` (y no `REJECTED`) porque el hold ya
  retuvo fondos: `REJECTED` es solo negocio pre-retencion y la maquina
  (E5-T01) no permite `POSTED -> REJECTED`. Si la tx ya esta en terminal
  (o en un estado no transitable a FAILED) se deja intacta: el hold queda
  terminal de todos modos (nunca huerfano) y el job no falla.
- Compensatorio: por cada hold vencido se asienta `HOLD_RELEASE`
  (`DEBIT 2100-X / CREDIT 2000-X` por `hold.amount_minor`) via la fachada
  `ledger`. Se asienta aunque la tx ya estuviera en terminal: un hold
  `ACTIVE` implica fondos aun retenidos en `2100`, y sin este asiento el
  dinero quedaria congelado para siempre (`04#7.3`).

Decision documentada (orden de operaciones y fallo del compensatorio):
- Orden estado-primero (como el job original): `ACTIVE -> EXPIRED`, luego
  tx `-> FAILED` best-effort, y despues el compensatorio, todo en la
  MISMA sesion para atomicidad. Se eligio estado-primero (y no
  asiento-primero) para no cambiar el orden historico del job y porque la
  atomicidad la da la sesion compartida: si el compensatorio falla, se
  hace `raise` con contexto y el llamante revierte TODO (rollback total,
  igual que E5-T03 ante fallo tecnico). Nunca quedan etiquetas sin
  fondos ni fondos sin etiquetas: o todo el hold expira con su asiento,
  o nada persiste. El `raise` ademas aborta el resto del lote; como el
  job es idempotente, el reintento retoma donde quedo.

Gestion de transaccion (decision documentada):
- Solo `flush`, nunca `commit`: sigue la convencion de E5-T02/E5-T03.
  El llamante (scheduler o test) decide `commit`/`rollback`. Procesa en
  lote con `limit` dentro de la misma sesion.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core import events as domain_events
from app.modules.ledger import service as ledger_service
from app.modules.transactions import repository as tx_repository
from app.modules.transactions.models import Hold, HoldStatus

#: Estados de transaccion desde los que la expiracion lleva a FAILED
#: (maquina E5-T01: solo FUNDS_HELD/POSTED transitan a FAILED).
_FAILED_FROM = frozenset({"FUNDS_HELD", "POSTED"})

#: Concepto contable del compensatorio de vencimiento (`04#7.3`).
HOLD_RELEASE_ENTRY_TYPE = "HOLD_RELEASE"

DEFAULT_LIMIT = 100


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_naive(moment: datetime) -> datetime:
    """Normaliza a naive para comparar (SQLite pierde el tz)."""
    if moment.tzinfo is not None:
        return moment.replace(tzinfo=None)
    return moment


def _hold_release_description(hold: Hold) -> str:
    """Descripcion del compensatorio; siempre incluye el `hold.id`.

    El `hold.id` en la descripcion es la clave de idempotencia legible:
    permite detectar un compensatorio ya asentado para ese hold.
    """
    return f"hold release {hold.id} (tx {hold.transaction_id})"


def _hold_release_exists(session: Session, hold: Hold) -> bool:
    """Indica si ya existe el compensatorio de este hold (solo lectura).

    Guarda de idempotencia en profundidad: delega en la fachada `ledger`
    (`find_hold_release`), que busca un asiento
    `entry_type="HOLD_RELEASE"` con el mismo `transaction_id` y el
    `hold.id` en la descripcion. Este modulo nunca toca las tablas de
    `ledger` directamente (regla de oro 4); la escritura del asiento
    siempre va por `ledger_service.post_entry`. La primera linea de
    defensa sigue siendo el filtro `ACTIVE`: un hold ya procesado nunca
    llega hasta aqui.
    """
    return (
        ledger_service.find_hold_release(
            session, transaction_id=hold.transaction_id, hold_id=hold.id
        )
        is not None
    )


def _post_hold_release(session: Session, hold: Hold) -> None:
    """Asienta el compensatorio `2100-X -> 2000-X` via la fachada ledger.

    Idempotente: si ya existe el compensatorio de este hold no duplica.
    Solo `flush` (lo hace `post_entry`); el `commit` lo decide el llamante.
    """
    if _hold_release_exists(session, hold):
        return
    available, retained = ledger_service.ensure_customer_accounts(
        session, hold.account_id, hold.currency
    )
    ledger_service.post_entry(
        session,
        entry_type=HOLD_RELEASE_ENTRY_TYPE,
        transaction_id=hold.transaction_id,
        description=_hold_release_description(hold),
        postings=[
            {
                "ledger_account_id": retained.id,
                "direction": "DEBIT",
                "amount_minor": hold.amount_minor,
                "currency": hold.currency,
                "account_ref": hold.account_id,
            },
            {
                "ledger_account_id": available.id,
                "direction": "CREDIT",
                "amount_minor": hold.amount_minor,
                "currency": hold.currency,
                "account_ref": hold.account_id,
            },
        ],
    )


def _publish_outbox(
    session: Session, *, event_type: str, hold: Hold, tx_status: str, payload: dict[str, Any]
) -> bool:
    """Publica en outbox con import perezoso; `False` si no disponible.

    Best-effort: cualquier fallo (modulo E5-T05 ausente, tabla shared no
    creada en SQLite) se traga para no abortar la liberacion.
    """
    try:
        from app.core.outbox import record as outbox_record
    except ImportError:
        # TODO(E5-T05): modulo outbox aun en curso; se continua sin publicar.
        return False
    try:
        outbox_record(
            session,
            aggregate_type="transaction",
            aggregate_id=hold.transaction_id,
            event_type=event_type,
            payload=payload,
        )
    except Exception:  # noqa: BLE001 - best-effort: no aborta la liberacion
        return False
    return True


def release_expired_holds(
    session: Session, *, now: datetime | None = None, limit: int = DEFAULT_LIMIT
) -> list[Hold]:
    """Libera los holds vencidos, falla sus transacciones y devuelve fondos.

    - Seleccion: `ACTIVE` con `expires_at <= now` (coherente con
      `expire_due_holds` de E5-T02; el brief usa `<`, equivalente salvo
      el instante exacto), ordenados por `expires_at`, hasta `limit`.
      Los holds sin `expires_at` nunca se tocan.
    - Por hold: `ACTIVE -> EXPIRED` via repositorio + `FUNDS_HELD/POSTED
      -> FAILED` best-effort via repositorio (historial incluido) +
      compensatorio `HOLD_RELEASE` (`2100-X -> 2000-X`) via la fachada
      `ledger` (idempotente: no duplica si ya existe).
    - Si el compensatorio falla, se hace `raise` con contexto: el hold YA
      quedo terminal en la sesion, pero el llamante revierte todo
      (rollback total), asi que ningun estado parcial persiste.
    - Solo `flush`, nunca `commit` (lo decide el llamante).
    - Retorna los holds procesados en esta corrida (``[]`` si nada vence:
      reproceso seguro).
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError(f"limit debe ser entero > 0, recibido: {limit!r}")
    moment = now or _utcnow()
    cutoff = _as_naive(moment)

    actives = tx_repository.list_active_holds(session)
    due = sorted(
        (h for h in actives if h.expires_at is not None and _as_naive(h.expires_at) <= cutoff),
        key=lambda h: _as_naive(h.expires_at),  # type: ignore[arg-type]
    )[:limit]

    processed: list[Hold] = []
    for hold in due:
        # Marca terminal via E5-T02 (valida ACTIVE -> EXPIRED; si otro
        # proceso ya lo movio, se omite el hold: idempotencia ante carrera).
        try:
            tx_repository.update_hold_status(session, hold.id, HoldStatus.EXPIRED.value)
        except (KeyError, ValueError):
            continue
        processed.append(hold)

        tx = tx_repository.get_transaction(session, hold.transaction_id)
        tx_failed_now = False
        if tx is not None and tx.status in _FAILED_FROM:
            try:
                tx_repository.transition_transaction(
                    session,
                    tx.id,
                    "FAILED",
                    reason=f"hold expirado: {hold.id}",
                )
            except Exception:  # noqa: BLE001, S110 - best-effort: el hold ya quedo terminal
                pass
            else:
                tx_failed_now = True
        # Si la tx no existe o ya estaba en terminal/otro estado, se deja
        # intacta: el hold queda terminal de todos modos (nunca huerfano).

        # Compensatorio contable (misma sesion: atomicidad con los estados).
        # Si falla, raise con contexto: el llamante revierte todo y ningun
        # estado parcial persiste (ver decision documentada arriba).
        try:
            _post_hold_release(session, hold)
        except Exception as exc:
            raise RuntimeError(
                "E5-T06: no se pudo asentar el compensatorio HOLD_RELEASE "
                f"del hold {hold.id} (tx {hold.transaction_id}): {exc}"
            ) from exc

        if not tx_failed_now:
            continue  # sin transicion a FAILED no se publica evento
        _publish_outbox(
            session,
            event_type=domain_events.FUNDS_RELEASED,
            hold=hold,
            tx_status="FAILED",
            payload={
                "transaction_id": str(tx.id),
                "hold_id": str(hold.id),
                "account_id": str(hold.account_id),
                "amount_minor": hold.amount_minor,
                "currency": hold.currency,
                "status": "FAILED",
            },
        )
    if processed:
        session.flush()
    return processed


__all__ = ["DEFAULT_LIMIT", "HOLD_RELEASE_ENTRY_TYPE", "release_expired_holds"]
