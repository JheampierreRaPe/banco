"""Cableado definitivo fase 3, opcion b (E2-T01, H3 fase 4).

Demuestra que la ruta productiva nunca alcanza el `NotImplementedError`:
el motor resuelve el port real sin inyeccion manual (via `app.composition`).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base


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


def _new_account(session, **kwargs):
    from app.modules.accounts import repository as repo

    params = {
        "user_id": uuid.uuid4(),
        "account_number": f"009-{uuid.uuid4().hex[:8]}",
        "type": "AHORRO",
        "currency": "PEN",
    }
    params.update(kwargs)
    return repo.create_account(session, **params)


def test_import_app_main_has_no_cycle():
    import app.main  # noqa: F401 (no debe lanzar por ciclos)
    from app.composition import get_default_ports, wire_ports

    assert callable(get_default_ports) and callable(wire_ports)


def test_default_ports_are_real_adapters():
    from app.composition import get_default_ports, resolve_account_projection, resolve_balance_port

    balance, projection = get_default_ports()
    assert type(balance).__name__ == "AccountsBalanceAdapter"
    assert type(projection).__name__ == "AccountsLedgerProjectionAdapter"
    # Sin inyeccion manual: el resolver retorna el real, nunca el stub.
    assert (
        resolve_balance_port(None) is balance
        or type(resolve_balance_port(None)).__name__ == "AccountsBalanceAdapter"
    )
    assert type(resolve_account_projection(None)).__name__ == ("AccountsLedgerProjectionAdapter")
    # El explicito siempre gana.
    sentinel = object()
    assert resolve_balance_port(sentinel) is sentinel
    assert resolve_account_projection(sentinel) is sentinel


def test_wire_ports_patches_globals_and_restores(sqlite_session: Session):
    import app.modules.ledger.repository.balances as ledger_balances
    import app.modules.transactions.service as tx_service
    from app.composition import wire_ports

    prev_tx = tx_service.DEFAULT_BALANCE_PORT
    prev_lx = ledger_balances.DEFAULT_ACCOUNT_PROJECTION
    try:
        wired = wire_ports()
        assert type(wired["balance_port"]).__name__ == "AccountsBalanceAdapter"
        assert type(wired["account_projection"]).__name__ == ("AccountsLedgerProjectionAdapter")
        assert type(tx_service.DEFAULT_BALANCE_PORT).__name__ == ("AccountsBalanceAdapter")
        assert type(ledger_balances.DEFAULT_ACCOUNT_PROJECTION).__name__ == (
            "AccountsLedgerProjectionAdapter"
        )
    finally:
        tx_service.DEFAULT_BALANCE_PORT = prev_tx
        ledger_balances.DEFAULT_ACCOUNT_PROJECTION = prev_lx


def test_motor_resolves_real_port_without_manual_injection(sqlite_session: Session):
    """`execute_transfer` sin `balance_port` liquida (no llega al stub)."""
    from app.modules.accounts import repository as repo
    from app.modules.transactions.service import execute_transfer

    source = _new_account(sqlite_session)
    target = _new_account(sqlite_session)
    repo.apply_delta(sqlite_session, source.id, available_delta=50_000, currency="PEN")

    tx = execute_transfer(
        sqlite_session,
        source_account_id=source.id,
        target_account_id=target.id,
        amount_minor=10_000,
        currency="PEN",
    )
    assert tx.status == "SETTLED"
    assert repo.get_balance(sqlite_session, source.id).available_minor == 40_000
    assert repo.get_balance(sqlite_session, target.id).available_minor == 10_000


def test_ledger_default_projection_applies_held(sqlite_session: Session):
    """`post_entry_and_update_balances` sin `account_projection` refleja held."""
    from app.modules.accounts import repository as repo
    from app.modules.ledger.repository import post_entry_and_update_balances

    source = _new_account(sqlite_session)
    repo.apply_delta(sqlite_session, source.id, available_delta=20_000, currency="PEN")
    repo.apply_delta(sqlite_session, source.id, available_delta=-8_000, currency="PEN")

    post_entry_and_update_balances(
        sqlite_session,
        entry_type="TRANSFER_HOLD",
        postings=[
            {
                "ledger_account_id": source.ledger_account_id,
                "direction": "DEBIT",
                "amount_minor": 8_000,
                "currency": "PEN",
                "account_ref": source.id,
            },
            {
                "ledger_account_id": source.ledger_hold_account_id,
                "direction": "CREDIT",
                "amount_minor": 8_000,
                "currency": "PEN",
                "account_ref": source.id,
            },
        ],
    )
    row = repo.get_balance(sqlite_session, source.id)
    assert (row.available_minor, row.held_minor) == (12_000, 8_000)
