"""Cierre contable diario y control de consistencia (E5-T13, HU18 CA-04).

Job programado fuera del horario de operacion (`04#9`):
1. Suma debitos y creditos del dia por moneda.
2. Si `debitos == creditos` -> candidato a `daily_closings.balanced = true`.
3. Si no cuadra -> no cierra (`closed_at=None`), retorna alerta estructurada.
4. Verifica que `ledger_balances` coincida con `postings` reutilizando
   `check_projection_consistency` (E5-T12); ante desviacion tampoco cierra.
5. Idempotente: UQ (`closing_date`, `currency`); la segunda corrida del
   mismo dia+moneda retorna la fila existente sin duplicar.

Eleccion de fecha (documentada): el dia se recorta por
`journal_entries.value_date` (fecha valor contable del asiento), NO por
`postings.created_at` (marca operativa). El cierre es contable: un asiento
registrado hoy con fecha valor de ayer pertenece al cierre de ayer.

Convencion de transaccion: `flush` sin `commit`; quien llama (el
scheduler/llamante del job) hace `commit`. Justificacion: misma convencion
que E5-T10/E5-T12 (permite atomicidad futura con el buzon transaccional y
rollback en pruebas). El job nunca publica eventos directamente
(regla de oro 8): la alerta se RETORNA como dato estructurado para que el
llamante decida el canal (buzon/notificaciones); nunca UPDATE/DELETE sobre
postings ni balances (regla de oro 2). Dinero entero en centimos; nunca
`float`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.ledger.domain.entries import validate_currency
from app.modules.ledger.models import DailyClosing, JournalEntry, Posting
from app.modules.ledger.repository.balances import check_projection_consistency

DEFAULT_CURRENCY = "PEN"


def get_closing(
    session: Session, closing_date: date, currency: str
) -> DailyClosing | None:
    """Retorna el cierre de un dia+moneda (`None` si aun no existe)."""
    stmt = sa.select(DailyClosing).where(
        DailyClosing.closing_date == closing_date,
        DailyClosing.currency == currency,
    )
    return session.scalars(stmt).first()


def _day_totals(
    session: Session, closing_date: date, currency: str
) -> tuple[int, int, int]:
    """Suma (debitos, creditos, conteo) del dia por `value_date` y moneda.

    Igualdad exacta de enteros en centimos (sin tolerancias ni redondeos):
    el cuadre es `debits == credits` (`04#2`, `03b#7.5`).
    """
    debits = sa.func.coalesce(
        sa.func.sum(
            sa.case((Posting.direction == "DEBIT", Posting.amount_minor), else_=0)
        ),
        0,
    ).label("debits")
    credits = sa.func.coalesce(
        sa.func.sum(
            sa.case((Posting.direction == "CREDIT", Posting.amount_minor), else_=0)
        ),
        0,
    ).label("credits")
    count = sa.func.count().label("count")
    stmt = (
        sa.select(debits, credits, count)
        .select_from(Posting)
        .join(JournalEntry, JournalEntry.id == Posting.journal_entry_id)
        .where(
            JournalEntry.value_date == closing_date,
            Posting.currency == currency,
        )
    )
    row = session.execute(stmt).one()
    return int(row.debits), int(row.credits), int(row.count)


def run_daily_closing(
    session: Session,
    *,
    closing_date: date | None = None,
    currency: str = DEFAULT_CURRENCY,
    closed_by: uuid.UUID | str | None = None,
) -> tuple[DailyClosing, dict | None]:
    """Ejecuta el cierre del dia y retorna `(closing, alert)`.

    - `closing`: fila `DailyClosing` (nueva o la existente si la corrida se
      repite: idempotente por UQ dia+moneda, sin duplicar).
    - `alert`: `None` si cerro limpio; dict estructurado
      (`type`/`severity`/`closing_date`/`currency`/totales/`projection_diffs`)
      si descuadro o la proyeccion desvio. No se publica nada: el llamante
      decide el canal.

    `balanced` = (`total_debits == total_credits`) en enteros; `closed_at`
    solo se fija cuando ademas `check_projection_consistency` vuelve vacio.
    Si no cuadra: `balanced=false`, `closed_at=None`, sin tocar postings
    ni balances. Solo lectura + un INSERT; `flush` sin `commit`.
    """
    cur = validate_currency(currency)
    day = date.today() if closing_date is None else closing_date
    if not isinstance(day, date):
        raise ValueError(f"closing_date debe ser DATE, recibido: {closing_date!r}")

    existing = get_closing(session, day, cur)
    if existing is not None:
        return existing, None

    debits, credits, count = _day_totals(session, day, cur)
    balanced = debits == credits
    diffs = check_projection_consistency(session)
    closed = balanced and not diffs

    if isinstance(closed_by, str):
        try:
            closed_by = uuid.UUID(closed_by)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError(
                f"closed_by debe ser UUID, recibido: {closed_by!r}"
            ) from exc

    closing = DailyClosing(
        id=uuid.uuid4(),
        closing_date=day,
        currency=cur,
        total_debits_minor=debits,
        total_credits_minor=credits,
        balanced=balanced,
        postings_count=count,
        closed_at=datetime.now(timezone.utc) if closed else None,
        closed_by=closed_by,
    )
    session.add(closing)
    session.flush()

    alert: dict | None = None
    if not closed:
        if not balanced:
            alert_type = "daily_closing.unbalanced"
            severity = "CRITICAL"
        else:
            alert_type = "daily_closing.projection_mismatch"
            severity = "HIGH"
        alert = {
            "type": alert_type,
            "severity": severity,
            "closing_date": day.isoformat(),
            "currency": cur,
            "total_debits_minor": debits,
            "total_credits_minor": credits,
            "postings_count": count,
            "projection_diffs": diffs,
        }
    return closing, alert


__all__ = ["DEFAULT_CURRENCY", "get_closing", "run_daily_closing"]
