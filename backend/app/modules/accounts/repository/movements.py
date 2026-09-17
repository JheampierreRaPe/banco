"""Proyeccion de movimientos (`accounts.movements_view`, E2-T04, HU05 CA-03).

Fuente: `docs/03b-diccionario-de-datos.md#5.4-movements_view-vista-materializada`
(columnas exactas), `docs/03c-modelo-er.md#14-fuente-de-verdad-y-proyecciones`
(origen `ledger.postings`, la proyeccion refleja), `docs/04#2` (partida doble:
cada fila refleja un posting con su `direction`/`amount_minor` originales),
`docs/modules/README.md#accounts` (consume `ledger.entry.posted`, prohibido
escribir asientos o leer `ledger` por FK).

Decision documentada (vista materializada vs tabla):
- `03b#5.4` la llama "vista materializada", pero una `MATERIALIZED VIEW`
  nativa exigiria `SELECT ... FROM ledger.postings` (JOIN entre modulos,
  prohibido por la regla de oro 4 y `docs/16#7`). Se implementa como **tabla
  de proyeccion** con el nombre y las columnas exactas de `03b#5.4`,
  mantenida solo por evento/fachada: el "refresh" es `rebuild_entry_movements`
  (re-derivacion desde los postings que entrega el evento, nunca lectura
  directa a tablas `ledger`). Equivalente funcional, portable SQLite/Postgres.

Mapeo cuenta elegido (documentado):
1. Via primaria `posting["account_ref"]` (REF logica que el ledger informa
   por posting, ver `ledger/repository/entries.py`: `Posting.account_ref`).
2. Fallback por `posting["ledger_account_id"]` buscando en las REFs propias
   (`accounts.ledger_account_id` / `accounts.ledger_hold_account_id`):
   cubre postings sin `account_ref` (p.ej. `2000-*`/`2100-*`).
3. Sin dueno (cuentas del sistema, p.ej. `4000-Comisiones`): se omite, no
   pertenece a ningun cliente. Nunca se importa ni se lee `ledger`.

Idempotencia (doble capa, mismo patron que E2-T01 `handle_ledger_entry_posted`):
- `processed_events` via `shared.try_mark_processed` (import perezoso),
  consumidor `"accounts-movements"`: reproceso con el mismo `event_id` -> `False`.
- UQ natural = PK compuesta
  (`journal_entry_id`, `account_id`, `direction`, `amount_minor`, `currency`):
  re-entrega con distinto `event_id` no duplica (check previo + savepoint).

Gana el origen (`03c#14`): `rebuild_entry_movements` borra las filas de la
entrada y las re-deriva desde los postings entregados (tras borrado o
divergencia manual, el refresh restaura lo del origen).

Convencion: `flush` sin `commit` (quien llama decide). Dinero entero en
centimos (`BIGINT`); nunca `float`. Sin publicar eventos (regla de oro 8).
`DELETE` solo sobre la propia proyeccion en `rebuild_entry_movements`
(`postings`/`audit` siguen intactos: `03b#16.7` no aplica a esta tabla).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.db import Base
from app.modules.accounts.models import SCHEMA, Account

#: Consumidor para `processed_events` (inbox `shared`, `03b#2.3`).
MOVEMENTS_CONSUMER = "accounts-movements"

MOVEMENT_DIRECTIONS = ("DEBIT", "CREDIT")


class MovementView(Base):
    """Fila de `accounts.movements_view`: un posting proyectado por cuenta.

    Columnas exactas de `03b#5.4`. PK compuesta = UQ natural (idempotencia).
    `account_id` es la unica FK fisica (autocontenida en `accounts`);
    `journal_entry_id` y `transaction_id` son REFs logicas sin constraint
    (regla de oro 4: sin FK entre schemas).
    """

    __tablename__ = "movements_view"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{SCHEMA}.accounts.id"],
            name="fk_movements_account",
        ),
        sa.CheckConstraint(
            "direction IN ('DEBIT', 'CREDIT')",
            name="ck_movements_direction",
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_movements_amount_positive"),
        {"schema": SCHEMA},
    )

    journal_entry_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    direction: Mapped[str] = mapped_column(sa.String(6), primary_key=True)
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger(), primary_key=True)
    currency: Mapped[str] = mapped_column(sa.String(3), primary_key=True)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    description: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    value_date: Mapped[date | None] = mapped_column(sa.Date(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


sa.Index(
    "ix_movements_account_created",
    MovementView.account_id,
    MovementView.created_at.desc(),
)


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def _coerce_optional_uuid(value: uuid.UUID | str | None, field: str) -> uuid.UUID | None:
    if value is None:
        return None
    return _coerce_uuid(value, field)


def _validate_currency(currency: str) -> str:
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        raise ValueError(f"currency debe ser ISO-4217 de 3 letras, recibido: {currency!r}")
    if currency != currency.upper():
        raise ValueError(f"currency debe ser mayusculas, recibido: {currency!r}")
    return currency


def _validate_minor(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} debe ser int (centimos), sin float; recibido: {value!r}")
    if value <= 0:
        raise ValueError(f"{field} debe ser > 0, recibido: {value!r}")
    return value


def _validate_direction(direction: str) -> str:
    if direction not in MOVEMENT_DIRECTIONS:
        raise ValueError(f"direction debe ser DEBIT/CREDIT, recibido: {direction!r}")
    return direction


def _coerce_value_date(value: date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, date):
        raise TypeError(f"value_date debe ser DATE, recibido: {value!r}")
    return value


def resolve_movement_account_id(session: Session, posting: dict) -> uuid.UUID | None:
    """Resuelve la cuenta de cliente de un posting (`None` si es del sistema).

    1. `account_ref` (REF que informa el ledger por posting).
    2. Fallback: `ledger_account_id` contra las REFs propias
       (`ledger_account_id` / `ledger_hold_account_id`).
    Solo lee tablas propias (`accounts`); nunca `ledger`.
    """
    if not isinstance(posting, dict):
        raise TypeError(f"posting debe ser dict, recibido: {posting!r}")
    ref = posting.get("account_ref")
    if ref is not None:
        aid = _coerce_uuid(ref, "account_ref")
        return aid if session.get(Account, aid) is not None else None
    ledger_id = posting.get("ledger_account_id")
    if ledger_id is None:
        return None
    lid = _coerce_uuid(ledger_id, "ledger_account_id")
    stmt = sa.select(Account.id).where(
        sa.or_(
            Account.ledger_account_id == lid,
            Account.ledger_hold_account_id == lid,
        )
    )
    return session.scalar(stmt)


def _insert_movement(
    session: Session,
    *,
    entry_id: uuid.UUID,
    account_id: uuid.UUID,
    direction: str,
    amount_minor: int,
    currency: str,
    transaction_id: uuid.UUID | None,
    description: str | None,
    value_date: date | None,
) -> bool:
    """Inserta una fila si no existe (UQ natural). `True` si inserto."""
    exists = session.scalar(
        sa.select(sa.literal(1)).where(
            MovementView.journal_entry_id == entry_id,
            MovementView.account_id == account_id,
            MovementView.direction == direction,
            MovementView.amount_minor == amount_minor,
            MovementView.currency == currency,
        )
    )
    if exists is not None:
        return False
    try:
        with session.begin_nested():
            session.add(
                MovementView(
                    journal_entry_id=entry_id,
                    account_id=account_id,
                    direction=direction,
                    amount_minor=amount_minor,
                    currency=currency,
                    transaction_id=transaction_id,
                    description=description,
                    value_date=value_date,
                )
            )
            session.flush()
    except IntegrityError:
        return False
    return True


def _derive_movements(
    session: Session,
    *,
    entry_id: uuid.UUID,
    postings: list[dict],
    transaction_id: uuid.UUID | None,
    description: str | None,
    value_date: date | None,
) -> int:
    """Deriva filas desde los postings del origen. Retorna nº de insertadas."""
    inserted = 0
    for posting in postings or []:
        account_id = resolve_movement_account_id(session, posting)
        if account_id is None:
            continue
        if _insert_movement(
            session,
            entry_id=entry_id,
            account_id=account_id,
            direction=_validate_direction(posting.get("direction")),
            amount_minor=_validate_minor(posting.get("amount_minor"), "amount_minor"),
            currency=_validate_currency(posting.get("currency")),
            transaction_id=transaction_id,
            description=description,
            value_date=value_date,
        ):
            inserted += 1
    session.flush()
    return inserted


def handle_movement_event(
    session: Session,
    *,
    event_id: uuid.UUID | str,
    entry_id: uuid.UUID | str,
    postings: list[dict],
    transaction_id: uuid.UUID | str | None = None,
    description: str | None = None,
    value_date: date | datetime | None = None,
) -> bool:
    """Consumidor `ledger.entry.posted` -> proyeccion `movements_view`.

    Idempotente: registra en `shared.processed_events`
    (`try_mark_processed`, consumidor `"accounts-movements"`). `True` si es
    el primer consumo (deriva filas); `False` si ya estaba registrado
    (duplicado ignorado, sin re-aplicar). Misma sesion, `flush` sin `commit`.

    Cada posting: `{"ledger_account_id", "direction", "amount_minor",
    "currency", "account_ref" | None}` (forma de `ledger/repository/entries.py`,
    consumida por evento/fachada: aqui solo se reciben dicts, sin importar
    modelos `ledger`). `transaction_id`/`description`/`value_date` son
    datos del asiento que informa el evento.
    """
    from app.modules.shared.repository.outbox import try_mark_processed

    if not try_mark_processed(session, event_id=event_id, consumer=MOVEMENTS_CONSUMER):
        return False
    eid = _coerce_uuid(entry_id, "entry_id")
    tx_id = _coerce_optional_uuid(transaction_id, "transaction_id")
    if description is not None and not isinstance(description, str):
        raise ValueError(f"description debe ser str, recibido: {description!r}")
    _derive_movements(
        session,
        entry_id=eid,
        postings=postings,
        transaction_id=tx_id,
        description=description,
        value_date=_coerce_value_date(value_date),
    )
    return True


def rebuild_entry_movements(
    session: Session,
    *,
    entry_id: uuid.UUID | str,
    postings: list[dict],
    transaction_id: uuid.UUID | str | None = None,
    description: str | None = None,
    value_date: date | datetime | None = None,
) -> int:
    """Re-deriva la proyeccion de un asiento desde el origen (gana el origen).

    Borra las filas de `movements_view` de `entry_id` y las reconstruye desde
    los `postings` entregados por evento/fachada. No toca `processed_events`
    (es reparacion, no consumo). Retorna nº de filas reconstruidas.
    `flush` sin `commit`.
    """
    eid = _coerce_uuid(entry_id, "entry_id")
    tx_id = _coerce_optional_uuid(transaction_id, "transaction_id")
    if description is not None and not isinstance(description, str):
        raise ValueError(f"description debe ser str, recibido: {description!r}")
    session.execute(sa.delete(MovementView).where(MovementView.journal_entry_id == eid))
    session.flush()
    return _derive_movements(
        session,
        entry_id=eid,
        postings=postings,
        transaction_id=tx_id,
        description=description,
        value_date=_coerce_value_date(value_date),
    )


def list_movements(
    session: Session, account_id: uuid.UUID | str, *, limit: int = 100, offset: int = 0
) -> list[MovementView]:
    """Lista movimientos de una cuenta (recientes primero). Solo lectura."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit debe ser int >= 1, recibido: {limit!r}")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError(f"offset debe ser int >= 0, recibido: {offset!r}")
    aid = _coerce_uuid(account_id, "account_id")
    stmt = (
        sa.select(MovementView)
        .where(MovementView.account_id == aid)
        .order_by(
            MovementView.created_at.desc(),
            MovementView.journal_entry_id,
            MovementView.direction,
            MovementView.amount_minor,
            MovementView.currency,
        )
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt).all())


__all__ = [
    "MOVEMENTS_CONSUMER",
    "MOVEMENT_DIRECTIONS",
    "MovementView",
    "handle_movement_event",
    "list_movements",
    "rebuild_entry_movements",
    "resolve_movement_account_id",
]
