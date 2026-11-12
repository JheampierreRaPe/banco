"""Fachada `accounts.service.open_customer_account` (E7-T02 corte 1, HU24).

Cubre: apertura `POCKET`/`MULTICURRENCY` (cuenta + balance 0 + subcuentas
ledger `2000`/`2100`), `currency`/`type` invalidos, `user_id` invalido,
unicidad del numero generado, formato E2-T28 del numero (`001` + DV) y que
la apertura no mueve dinero (sin asientos ni posteos, saldos en cero).
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base


def test_check_digit_matches_e2_t28_reference():
    """El DV congelado en `accounts` coincide con el canonico de `identity`."""
    from app.modules.accounts import service as accounts_service
    from app.modules.identity.service import compute_check_digit

    for first13 in ("0010000000001", "0011234567890", "0019999999999", "0010000000000"):
        assert accounts_service.compute_account_check_digit(first13) == compute_check_digit(first13)
    with pytest.raises(ValueError):
        accounts_service.compute_account_check_digit("00123")


@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schemas `accounts`/`ledger`/`shared` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401
    import app.modules.transactions.models as _t  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "ledger", "shared", "transactions"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["accounts.accounts"],
            Base.metadata.tables["accounts.account_balances"],
            Base.metadata.tables["accounts.daily_balance_snapshots"],
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["ledger.journal_entries"],
            Base.metadata.tables["ledger.postings"],
            Base.metadata.tables["ledger.ledger_balances"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["shared.processed_events"],
            Base.metadata.tables["transactions.transactions"],
            Base.metadata.tables["transactions.transaction_status_history"],
            Base.metadata.tables["transactions.holds"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _count(session: Session, table: str) -> int:
    return int(session.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[table])))


def test_open_pocket_pen_creates_account_zero_balance_and_subaccounts(sqlite_session: Session):
    from app.modules.accounts import repository as repo
    from app.modules.accounts import service as accounts_service

    user_id = uuid.uuid4()
    result = accounts_service.open_customer_account(sqlite_session, user_id, currency="PEN")

    assert set(result) == {"account_id", "account_number", "currency", "type", "status"}
    assert result["currency"] == "PEN"
    assert result["type"] == "POCKET"
    assert result["status"] == "ACTIVE"
    assert isinstance(result["account_id"], uuid.UUID)

    from app.modules.identity.service import verify_account_number

    assert verify_account_number(result["account_number"]) is True

    account = repo.get_account(sqlite_session, result["account_id"])
    assert account is not None
    assert account.user_id == user_id
    assert account.account_number == result["account_number"]
    assert account.type == "POCKET"
    assert account.currency == "PEN"
    assert account.status == "ACTIVE"
    assert account.ledger_account_id is not None
    assert account.ledger_hold_account_id is not None
    assert account.ledger_account_id != account.ledger_hold_account_id

    balance = repo.get_balance(sqlite_session, result["account_id"])
    assert (balance.available_minor, balance.held_minor, balance.version) == (0, 0, 0)
    assert balance.currency == "PEN"

    assert repo.get_by_number(sqlite_session, result["account_number"]).id == result["account_id"]

    from app.modules.ledger import repository as ledger_repo

    codes = {a.code for a in ledger_repo.get_by_owner(sqlite_session, result["account_id"])}
    assert codes == {f"2000-{result['account_id']}", f"2100-{result['account_id']}"}


@pytest.mark.parametrize("currency", ["PEN", "USD", "EUR"])
@pytest.mark.parametrize("account_type", ["POCKET", "MULTICURRENCY"])
def test_open_supported_currencies_and_types(
    sqlite_session: Session, currency: str, account_type: str
):
    from app.modules.accounts import service as accounts_service

    result = accounts_service.open_customer_account(
        sqlite_session, uuid.uuid4(), currency=currency, type=account_type
    )
    assert (result["currency"], result["type"], result["status"]) == (
        currency,
        account_type,
        "ACTIVE",
    )


@pytest.mark.parametrize("bad_type", ["AHORRO", "CORRIENTE", "WALLET", "", "pocket", None, 123])
def test_open_rejects_invalid_type(sqlite_session: Session, bad_type: object):
    from app.modules.accounts import service as accounts_service

    with pytest.raises(ValueError):
        accounts_service.open_customer_account(
            sqlite_session, uuid.uuid4(), currency="PEN", type=bad_type
        )
    assert _count(sqlite_session, "accounts.accounts") == 0


@pytest.mark.parametrize("bad_currency", ["JPY", "pen", "PE", "PENS", "", None, 123])
def test_open_rejects_invalid_currency(sqlite_session: Session, bad_currency: object):
    from app.modules.accounts import service as accounts_service

    with pytest.raises(ValueError):
        accounts_service.open_customer_account(sqlite_session, uuid.uuid4(), currency=bad_currency)
    assert _count(sqlite_session, "accounts.accounts") == 0


@pytest.mark.parametrize("bad_user", ["no-es-uuid", "", None, 123])
def test_open_rejects_invalid_user_id(sqlite_session: Session, bad_user: object):
    from app.modules.accounts import service as accounts_service

    with pytest.raises(ValueError):
        accounts_service.open_customer_account(sqlite_session, bad_user, currency="PEN")
    assert _count(sqlite_session, "accounts.accounts") == 0


def test_open_moves_no_money(sqlite_session: Session):
    """La apertura no crea asientos/posteos y deja saldos en cero."""
    from app.modules.accounts import service as accounts_service

    accounts_service.open_customer_account(sqlite_session, uuid.uuid4(), currency="USD")
    accounts_service.open_customer_account(
        sqlite_session, uuid.uuid4(), currency="EUR", type="MULTICURRENCY"
    )

    assert _count(sqlite_session, "ledger.journal_entries") == 0
    assert _count(sqlite_session, "ledger.postings") == 0
    from app.modules.accounts.models import AccountBalance

    balances = sqlite_session.scalars(sa.select(AccountBalance)).all()
    assert len(balances) == 2
    assert {(b.available_minor, b.held_minor) for b in balances} == {(0, 0)}


def test_open_generates_unique_numbers(sqlite_session: Session):
    from app.modules.accounts import service as accounts_service

    first = accounts_service.open_customer_account(sqlite_session, uuid.uuid4(), currency="PEN")
    second = accounts_service.open_customer_account(sqlite_session, uuid.uuid4(), currency="PEN")
    assert first["account_number"] != second["account_number"]
    assert first["account_id"] != second["account_id"]
