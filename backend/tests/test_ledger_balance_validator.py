"""Validador de cuadre contable (E5-T11, HU18, CA-01/CA-03).

- Parte A (sin BD): `validate_balanced` como pieza explicita del agregado:
  asiento que cuadra, descuadre por 1 centimo rechazado, multi-moneda
  (cada moneda cuadra por separado; montos de distintas monedas no se
  mezclan), validacion no desactivable (sin flags/bypass en la API) y
  dinero entero en centimos (nunca `float`, sin redondeos).
- Parte B (SQLite en memoria + schema ATTACH): `post_entry` (repositorio
  y fachada `service`) acepta el asiento que cuadra y rechaza el
  descuadre por 1 centimo sin filas residuales; multi-moneda cuadra.
- Parte C (Postgres `db_session`): humo de integracion; se omite si no hay BD.
"""

from __future__ import annotations

import inspect
import re
import uuid
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base


def _valid_postings(avail_id, hold_id, amount=10_000, currency="PEN"):
    return [
        {
            "ledger_account_id": avail_id,
            "direction": "DEBIT",
            "amount_minor": amount,
            "currency": currency,
        },
        {
            "ledger_account_id": hold_id,
            "direction": "CREDIT",
            "amount_minor": amount,
            "currency": currency,
        },
    ]


# ---------------------------------------------------------------- Parte A: dominio puro
def test_validate_balanced_accepts_balanced_entry():
    from app.modules.ledger.domain.entries import (
        validate_balanced,
        validate_postings,
    )

    a, b = uuid.uuid4(), uuid.uuid4()
    items = validate_postings(_valid_postings(a, b))
    assert validate_balanced(items) == items


def test_validate_balanced_rejects_off_by_one_cent():
    from app.modules.ledger.domain.entries import validate_postings

    a, b = uuid.uuid4(), uuid.uuid4()
    postings = _valid_postings(a, b)
    postings[1]["amount_minor"] = 9_999  # descuadre por 1 centimo
    with pytest.raises(ValueError, match="descuadrado"):
        validate_postings(postings)


def test_validate_balanced_multi_currency():
    from app.modules.ledger.domain.entries import validate_postings

    a, b = uuid.uuid4(), uuid.uuid4()
    # Cada asiento cuadra en su moneda (patron FX 04#7.7: dos asientos ligados).
    pen = validate_postings(_valid_postings(a, b, 38_000, "PEN"))
    assert len(pen) == 2
    usd = validate_postings(_valid_postings(a, b, 10_000, "USD"))
    assert len(usd) == 2
    # Montos de distintas monedas no se mezclan: DEBIT PEN vs CREDIT USD descuadra.
    mixed = [
        {
            "ledger_account_id": a,
            "direction": "DEBIT",
            "amount_minor": 10_000,
            "currency": "PEN",
        },
        {
            "ledger_account_id": b,
            "direction": "CREDIT",
            "amount_minor": 10_000,
            "currency": "USD",
        },
    ]
    with pytest.raises(ValueError, match="descuadrado"):
        validate_postings(mixed)


def test_validation_cannot_be_disabled():
    """Sin flags/bypass: ninguna firma expone `skip_*`, `validate=False`,
    tolerancias ni redondeos; el repositorio no publica eventos ni usa float."""
    import app.modules.ledger.domain.entries as domain
    import app.modules.ledger.repository.entries as repo_entries
    import app.modules.ledger.service as service

    assert callable(domain.validate_balanced)
    for fn in (
        domain.validate_balanced,
        domain.validate_postings,
        repo_entries.post_entry,
        repo_entries.reverse_entry,
        service.post_entry,
    ):
        params = inspect.signature(fn).parameters
        for forbidden in (
            "skip_validation",
            "skip",
            "bypass",
            "validate",
            "tolerance",
            "allow_unbalanced",
        ):
            assert forbidden not in params, (
                f"{fn.__qualname__} expone flag {forbidden}"
            )
    for path in (
        Path(domain.__file__),
        Path(repo_entries.__file__),
        Path(service.__file__),
    ):
        content = path.read_text(encoding="utf-8")
        assert "skip_validation" not in content
        assert "allow_unbalanced" not in content
        assert "float(" not in content
        assert "round(" not in content
    repo_content = Path(repo_entries.__file__).read_text(encoding="utf-8")
    assert "publish(" not in repo_content
    assert "outbox" not in repo_content


# ---------------------------------------------------------------- Parte B: SQLite
@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schema `ledger` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.ledger.models as m  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS ledger")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["ledger.journal_entries"],
            Base.metadata.tables["ledger.postings"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_accounts(session):
    from app.modules.ledger import repository as repo

    return repo.ensure_customer_accounts(session, uuid.uuid4(), "PEN")


def _count(session, key):
    return session.scalar(
        sa.select(sa.func.count()).select_from(Base.metadata.tables[key])
    )


def test_post_balanced_entry(sqlite_session: Session):
    from app.modules.ledger import repository as repo

    avail, hold = _seed_accounts(sqlite_session)
    entry = repo.post_entry(
        sqlite_session,
        entry_type="OWN_TRANSFER",
        postings=_valid_postings(avail.id, hold.id),
        transaction_id=uuid.uuid4(),
        description="cuadre E5-T11",
        value_date=date(2026, 9, 17),
    )
    assert entry.status == "POSTED"
    rows = repo.list_postings(sqlite_session, entry.id)
    debit = sum(r.amount_minor for r in rows if r.direction == "DEBIT")
    credit = sum(r.amount_minor for r in rows if r.direction == "CREDIT")
    assert debit == credit == 10_000


def test_post_off_by_one_cent_rejected_without_residue(sqlite_session: Session):
    from app.modules.ledger import repository as repo

    avail, hold = _seed_accounts(sqlite_session)
    postings = _valid_postings(avail.id, hold.id)
    postings[1]["amount_minor"] = 9_999
    before_entries, before_postings = (
        _count(sqlite_session, "ledger.journal_entries"),
        _count(sqlite_session, "ledger.postings"),
    )
    with pytest.raises(ValueError, match="descuadrado"):
        repo.post_entry(
            sqlite_session, entry_type="OWN_TRANSFER", postings=postings
        )
    sqlite_session.rollback()
    assert _count(sqlite_session, "ledger.journal_entries") == before_entries
    assert _count(sqlite_session, "ledger.postings") == before_postings


def test_post_multi_currency_each_balanced(sqlite_session: Session):
    from app.modules.ledger import repository as repo

    avail, hold = _seed_accounts(sqlite_session)
    fx_id = uuid.uuid4()
    pen = repo.post_entry(
        sqlite_session,
        entry_type="FX_BUY",
        postings=_valid_postings(avail.id, hold.id, 38_000, "PEN"),
        transaction_id=fx_id,
    )
    usd = repo.post_entry(
        sqlite_session,
        entry_type="FX_BUY",
        postings=_valid_postings(avail.id, hold.id, 10_000, "USD"),
        transaction_id=fx_id,
    )
    assert pen.id != usd.id
    for entry, amount, currency in ((pen, 38_000, "PEN"), (usd, 10_000, "USD")):
        rows = repo.list_postings(sqlite_session, entry.id)
        assert {r.currency for r in rows} == {currency}
        assert sum(r.amount_minor for r in rows if r.direction == "DEBIT") == amount
        assert sum(r.amount_minor for r in rows if r.direction == "CREDIT") == amount
    # Mezclar monedas en un solo asiento se rechaza.
    with pytest.raises(ValueError, match="descuadrado"):
        repo.post_entry(
            sqlite_session,
            entry_type="FX_BUY",
            postings=[
                {
                    "ledger_account_id": avail.id,
                    "direction": "DEBIT",
                    "amount_minor": 10_000,
                    "currency": "PEN",
                },
                {
                    "ledger_account_id": hold.id,
                    "direction": "CREDIT",
                    "amount_minor": 10_000,
                    "currency": "USD",
                },
            ],
        )


def test_service_post_entry_enforces_balance(sqlite_session: Session):
    from app.modules.ledger import service

    avail, hold = _seed_accounts(sqlite_session)
    entry = service.post_entry(
        sqlite_session,
        entry_type="OWN_TRANSFER",
        postings=_valid_postings(avail.id, hold.id),
    )
    assert entry.status == "POSTED"
    bad = _valid_postings(avail.id, hold.id)
    bad[1]["amount_minor"] = 9_999
    with pytest.raises(ValueError, match="descuadrado"):
        service.post_entry(
            sqlite_session, entry_type="OWN_TRANSFER", postings=bad
        )


# ---------------------------------------------------------------- Parte C: Postgres
def test_integration_postgres_balance_validator(db_session: Session):
    from app.modules.ledger import repository as repo

    avail, hold = repo.ensure_customer_accounts(db_session, uuid.uuid4(), "PEN")
    entry = repo.post_entry(
        db_session,
        entry_type="OWN_TRANSFER",
        postings=_valid_postings(avail.id, hold.id),
        transaction_id=uuid.uuid4(),
        description="humo E5-T11",
    )
    assert entry.id is not None
    assert re.fullmatch(r"[0-9a-f]{64}", entry.hash)
    bad = _valid_postings(avail.id, hold.id)
    bad[1]["amount_minor"] = 9_999
    with pytest.raises(ValueError, match="descuadrado"):
        repo.post_entry(
            db_session, entry_type="OWN_TRANSFER", postings=bad
        )
