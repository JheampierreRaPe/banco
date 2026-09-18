"""Repositorio de cuentas y proyeccion de saldos (`accounts`, E2-T01, HU05).

Capa de datos sin endpoints: solo SQLAlchemy sobre el schema propio
(`accounts`, `account_balances`, `daily_balance_snapshots`).
No accede a tablas de otros modulos: `user_id`, `ledger_account_id` y
`ledger_hold_account_id` son UUID logicos (REF, sin FK fisica).
No calcula saldos por fuera del ledger: la proyeccion refleja, no inventa
(`docs/03c-modelo-er.md#14`, `docs/modules/README.md#accounts`).

Convencion: las funciones hacen `flush` y no `commit`; quien llama decide
la transaccion (permite rollback en pruebas y atomicidad con `outbox`).
Dinero entero en centimos (`BIGINT`); nunca `float`.
No publica eventos aqui (regla de oro 8).

Bloqueo pesimista (`02#8`): `lock_and_get` emite `SELECT ... FOR UPDATE`
sobre la fila de saldo. Aqui se bloquea una sola fila; para operaciones
multi-cuenta el llamante debe bloquear en orden estable de `account_id`
(ver `lock_many_in_order`) para evitar deadlocks.

Adaptadores fase 3 (cierran seams pendientes de otros modulos):
- `AccountsBalanceAdapter` satisface `BalancePort` de `transactions`
  (`transactions/service`: `lock_and_get` / `apply_delta`). Cableado: ese
  modulo lo importa (top-level seguro: este archivo no importa a
  `transactions`/`ledger` a nivel top, solo de forma perezosa dentro de
  `create_account` y del consumidor) o lo inyecta como `balance_port`.
- `AccountsLedgerProjectionAdapter` satisface `AccountProjectionPort` de
  `ledger` (`ledger/repository/balances.py`: `apply_projection`). Cableado:
  se pasa como `account_projection` a `post_entry_and_update_balances` /
  `reverse_entry_and_update_balances`.
- `handle_ledger_entry_posted` es el stub del consumidor
  `ledger.entry.posted`: registra idempotencia en `processed_events` via
  `shared` (`try_mark_processed`, import perezoso) y aplica la proyeccion;
  el cableado al bus queda documentado aqui hasta que el worker exista.

Invariante (`03c#15.1`): `available_minor + held_minor` coincide con el
saldo economico del ledger. Las subcuentas de cliente son de pasivo por
construccion (`ledger.ensure_customer_accounts` las crea con
`type="liability"`): el efecto economico de un posting es el delta firmado
negado (DEBIT resta, CREDIT suma). Reparto de canales (sin doble conteo):
`BalancePort.apply_delta` (motor, sincronico) es dueno de `available`;
`AccountProjectionPort.apply_projection` (ledger, via consumidor
`ledger.entry.posted`) es duena de `held` y omite las patas de disponible
y las cuentas del sistema (p.ej. `4000`). El ledger es la fuente de verdad
ante cualquier diferencia.
"""

from __future__ import annotations

import uuid
from datetime import date

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.accounts.models import (
    ACCOUNT_STATUSES,
    ACCOUNT_TYPES,
    Account,
    AccountBalance,
    DailyBalanceSnapshot,
)

#: Consumidor para `processed_events` (inbox `shared`, `03b#2.3`).
ACCOUNTS_CONSUMER = "accounts"


class VersionConflictError(ValueError):
    """Conflicto de version concurrente en `account_balances`."""


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def _validate_currency(currency: str) -> str:
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        raise ValueError(f"currency debe ser ISO-4217 de 3 letras, recibido: {currency!r}")
    if currency != currency.upper():
        raise ValueError(f"currency debe ser mayusculas, recibido: {currency!r}")
    return currency


def _validate_minor(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} debe ser int (centimos), sin float; recibido: {value!r}")
    return value


def create_account(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    account_number: str,
    type: str = "AHORRO",
    currency: str = "PEN",
    status: str = "ACTIVE",
) -> Account:
    """Crea la cuenta con balance inicial en cero y subcuentas ledger.

    Todo en la misma sesion (`flush`, sin `commit`): inserta `accounts`,
    inserta `account_balances` en cero y asegura las subcuentas
    `2000-<cuenta>`/`2100-<cuenta>` via la fachada
    `ledger.service.ensure_customer_accounts` (import perezoso: `accounts`
    no se deja importar de forma ciclica), guardando sus ids logicos.
    """
    uid = _coerce_uuid(user_id, "user_id")
    if not isinstance(account_number, str) or not account_number.strip():
        raise ValueError("account_number es obligatorio")
    number = account_number.strip()
    if len(number) > 20:
        raise ValueError("account_number supera 20 caracteres")
    if type not in ACCOUNT_TYPES:
        raise ValueError(f"type debe ser uno de {ACCOUNT_TYPES}, recibido: {type!r}")
    cur = _validate_currency(currency)
    if status not in ACCOUNT_STATUSES:
        raise ValueError(f"status debe ser uno de {ACCOUNT_STATUSES}, recibido: {status!r}")
    if session.scalar(sa.select(Account.id).where(Account.account_number == number)) is not None:
        raise ValueError(f"account_number duplicado: {number!r}")

    account = Account(
        user_id=uid,
        account_number=number,
        type=type,
        currency=cur,
        status=status,
    )
    session.add(account)
    session.flush()
    session.add(
        AccountBalance(
            account_id=account.id,
            currency=cur,
            available_minor=0,
            held_minor=0,
            version=0,
        )
    )
    session.flush()

    # Fachada ledger (perezosa para no crear ciclo de imports a nivel top).
    from app.modules.ledger.service import ensure_customer_accounts

    available, hold = ensure_customer_accounts(session, account.id, cur)
    account.ledger_account_id = available.id
    account.ledger_hold_account_id = hold.id
    session.flush()
    return account


def get_account(session: Session, account_id: uuid.UUID | str) -> Account | None:
    """Lee una cuenta por id (`None` si no existe)."""
    return session.get(Account, _coerce_uuid(account_id, "account_id"))


def get_by_number(session: Session, account_number: str) -> Account | None:
    """Lee una cuenta por `account_number` (`None` si no existe)."""
    stmt = sa.select(Account).where(Account.account_number == account_number)
    return session.scalars(stmt).first()


def list_by_user(session: Session, user_id: uuid.UUID | str) -> list[Account]:
    """Lista las cuentas de un usuario en orden de creacion."""
    uid = _coerce_uuid(user_id, "user_id")
    stmt = sa.select(Account).where(Account.user_id == uid).order_by(Account.created_at, Account.id)
    return list(session.scalars(stmt).all())


def get_balance(session: Session, account_id: uuid.UUID | str) -> AccountBalance | None:
    """Lee la proyeccion de una cuenta (`None` si no existe)."""
    return session.get(AccountBalance, _coerce_uuid(account_id, "account_id"))


def lock_and_get(session: Session, account_id: uuid.UUID | str) -> AccountBalance:
    """Bloquea (pesimista) y retorna la fila de saldo de una cuenta.

    `SELECT ... FOR UPDATE` sobre una sola fila. Para operaciones con
    varias cuentas, usar `lock_many_in_order` (orden estable de IDs).
    En SQLite (pruebas sin Postgres) no hay `FOR UPDATE`: se lee sin
    bloqueo (el ORM/tests usan este mismo camino).
    """
    aid = _coerce_uuid(account_id, "account_id")
    stmt = sa.select(AccountBalance).where(AccountBalance.account_id == aid)
    bind = session.get_bind()
    dialect = getattr(bind, "dialect", None)
    if dialect is not None and dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    row = session.scalars(stmt).first()
    if row is None:
        raise LookupError(f"saldo inexistente para la cuenta: {aid}")
    return row


def lock_many_in_order(
    session: Session, account_ids: list[uuid.UUID | str]
) -> dict[uuid.UUID, AccountBalance]:
    """Bloquea varias filas de saldo en orden estable (`str` del id).

    Orden total y determinista: dos transacciones concurrentes que
    bloqueen el mismo conjunto lo hacen en el mismo orden (sin deadlock).
    """
    aids = sorted({_coerce_uuid(a, "account_id") for a in account_ids}, key=str)
    return {aid: lock_and_get(session, aid) for aid in aids}


def apply_delta(
    session: Session,
    account_id: uuid.UUID | str,
    *,
    available_delta: int = 0,
    held_delta: int = 0,
    currency: str,
    expected_version: int | None = None,
) -> AccountBalance:
    """Aplica deltas enteros con optimistic locking (`flush`, sin `commit`).

    Verifica `expected_version` (si se da) e incrementa `version` en 1.
    Rechaza el intento si algun resultante quedaria negativo (antes de
    mutar: no deja residuo). La moneda debe coincidir con la fila.
    """
    avail_d = _validate_minor(available_delta, "available_delta")
    held_d = _validate_minor(held_delta, "held_delta")
    cur = _validate_currency(currency)
    row = lock_and_get(session, account_id)
    if row.currency != cur:
        raise ValueError(
            f"moneda de la proyeccion {row.currency!r} != {currency!r} " f"para {row.account_id}"
        )
    if expected_version is not None and row.version != expected_version:
        raise VersionConflictError(
            f"conflicto de version en {row.account_id}: "
            f"esperada {expected_version}, actual {row.version}"
        )
    new_available = int(row.available_minor) + int(avail_d)
    new_held = int(row.held_minor) + int(held_d)
    if new_available < 0:
        raise ValueError(f"available_minor quedaria negativo ({row.available_minor} + {avail_d})")
    if new_held < 0:
        raise ValueError(f"held_minor quedaria negativo ({row.held_minor} + {held_d})")
    row.available_minor = new_available
    row.held_minor = new_held
    row.version = int(row.version) + 1
    session.flush()
    return row


def record_snapshot(
    session: Session, account_id: uuid.UUID | str, snapshot_date: date
) -> DailyBalanceSnapshot:
    """Registra (o actualiza) el corte diario con los saldos actuales."""
    if not isinstance(snapshot_date, date):
        raise TypeError(f"snapshot_date debe ser date, recibido: {snapshot_date!r}")
    aid = _coerce_uuid(account_id, "account_id")
    row = get_balance(session, aid)
    if row is None:
        raise LookupError(f"saldo inexistente para la cuenta: {aid}")
    snap = session.get(DailyBalanceSnapshot, (aid, snapshot_date))
    if snap is None:
        snap = DailyBalanceSnapshot(
            account_id=aid,
            snapshot_date=snapshot_date,
            available_minor=int(row.available_minor),
            held_minor=int(row.held_minor),
        )
        session.add(snap)
    else:
        snap.available_minor = int(row.available_minor)
        snap.held_minor = int(row.held_minor)
    session.flush()
    return snap


def get_snapshot(
    session: Session, account_id: uuid.UUID | str, snapshot_date: date
) -> DailyBalanceSnapshot | None:
    """Lee un corte diario (`None` si no existe)."""
    aid = _coerce_uuid(account_id, "account_id")
    return session.get(DailyBalanceSnapshot, (aid, snapshot_date))


def list_snapshots(session: Session, account_id: uuid.UUID | str) -> list[DailyBalanceSnapshot]:
    """Lista los cortes de una cuenta en orden cronologico."""
    aid = _coerce_uuid(account_id, "account_id")
    stmt = (
        sa.select(DailyBalanceSnapshot)
        .where(DailyBalanceSnapshot.account_id == aid)
        .order_by(DailyBalanceSnapshot.snapshot_date)
    )
    return list(session.scalars(stmt).all())


# ------------------------------------------------- Adaptadores fase 3


class AccountsBalanceAdapter:
    """Adaptador real de `BalancePort` (`transactions`, E2-T01).

    - `lock_and_get`: bloqueo pesimista de una sola fila (ver nota del
      modulo sobre orden estable multi-fila).
    - `apply_delta`: via rapida del motor sobre `available_minor`
      (negativo debita, positivo acredita); `held` se sincroniza por la
      proyeccion del ledger, fuente de verdad (`03c#15.1`).
    """

    def lock_and_get(self, session: Session, account_id: uuid.UUID) -> AccountBalance:
        """Bloquea (pesimista) y retorna el saldo de la cuenta."""
        return lock_and_get(session, account_id)

    def apply_delta(
        self, session: Session, account_id: uuid.UUID, delta_minor: int, currency: str
    ) -> None:
        """Aplica un delta entero sobre `available` (negativo debita)."""
        apply_delta(
            session,
            account_id,
            available_delta=delta_minor,
            held_delta=0,
            currency=currency,
        )


ACCOUNTS_BALANCE_PORT = AccountsBalanceAdapter()


class AccountsLedgerProjectionAdapter:
    """Adaptador real de `AccountProjectionPort` (`ledger`, E2-T01).

    Mapea cada `ledger_account_id` a su cuenta logica y refleja solo las
    patas de retenido (`ledger_hold_account_id`, `2100-*`) en `held_minor`
    con el delta firmado NEGADO (subcuentas de pasivo: el efecto economico
    de DEBIT/CREDIT es el opuesto del signo de `ledger.signed_delta`).

    Omite silenciosamente (sin mutar):
    - las patas de disponible (`ledger_account_id`, `2000-*`): son duenas
      de `BalancePort` en el motor; aplicarlas aqui contaria dos veces;
    - las cuentas del sistema sin dueno (p.ej. `4000`): no pertenecen a
      ningun cliente y el ledger ya actualizo su propia proyeccion.
    """

    def apply_projection(
        self,
        session: Session,
        ledger_account_id: uuid.UUID,
        signed_delta_minor: int,
        currency: str,
    ) -> None:
        """Refleja un delta firmado del ledger en `held` (o lo omite)."""
        delta = _validate_minor(signed_delta_minor, "signed_delta_minor")
        cur = _validate_currency(currency)
        lid = _coerce_uuid(ledger_account_id, "ledger_account_id")
        stmt = sa.select(Account).where(Account.ledger_hold_account_id == lid)
        account = session.scalars(stmt).first()
        if account is None:
            return
        apply_delta(
            session,
            account.id,
            available_delta=0,
            held_delta=-int(delta),
            currency=cur,
        )
        return


ACCOUNTS_LEDGER_PROJECTION = AccountsLedgerProjectionAdapter()


def handle_ledger_entry_posted(
    session: Session,
    *,
    event_id: uuid.UUID | str,
    entry_id: uuid.UUID | str,
    postings: list[dict],
) -> bool:
    """Stub del consumidor `ledger.entry.posted` (idempotente).

    Registra el consumo en `shared.processed_events` via
    `try_mark_processed` (import perezoso: `shared` es infraestructura).
    `True` si es el primer consumo (aplica la proyeccion posting por
    posting con `AccountsLedgerProjectionAdapter`); `False` si ya estaba
    registrado (duplicado ignorado, sin re-aplicar).

    Cada posting: `{"ledger_account_id", "signed_delta_minor", "currency"}`.
    Hook de cableado: el worker/bus que consuma `ledger.entry.posted`
    debe llamar aqui con el `event_id` del outbox; misma sesion, `flush`
    sin `commit`. No duplica logica de E5-T05 (solo reutiliza su inbox).
    """
    from app.modules.shared.repository.outbox import try_mark_processed

    if not try_mark_processed(session, event_id=event_id, consumer=ACCOUNTS_CONSUMER):
        return False
    _ = _coerce_uuid(entry_id, "entry_id")  # valida el formato, sin FK fisica
    adapter = AccountsLedgerProjectionAdapter()
    for posting in postings or []:
        adapter.apply_projection(
            session,
            posting["ledger_account_id"],
            posting["signed_delta_minor"],
            posting["currency"],
        )
    session.flush()
    return True


__all__ = [
    "ACCOUNTS_BALANCE_PORT",
    "ACCOUNTS_CONSUMER",
    "ACCOUNTS_LEDGER_PROJECTION",
    "AccountsBalanceAdapter",
    "AccountsLedgerProjectionAdapter",
    "VersionConflictError",
    "apply_delta",
    "create_account",
    "get_account",
    "get_balance",
    "get_by_number",
    "get_snapshot",
    "handle_ledger_entry_posted",
    "list_by_user",
    "list_snapshots",
    "lock_and_get",
    "lock_many_in_order",
    "record_snapshot",
]


# ------------------------------------------------- Proyeccion E2-T04
from app.modules.accounts.repository.movements import (
    MOVEMENTS_CONSUMER,
    MovementView,
    handle_movement_event,
    list_movements,
    rebuild_entry_movements,
    resolve_movement_account_id,
)

__all__ += [
    "MOVEMENTS_CONSUMER",
    "MovementView",
    "handle_movement_event",
    "list_movements",
    "rebuild_entry_movements",
    "resolve_movement_account_id",
]
