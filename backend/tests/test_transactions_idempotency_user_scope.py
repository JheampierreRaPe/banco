"""Regresion: la idempotencia del motor esta acotada al titular.

Defecto: `execute_transfer` buscaba por `idempotency_key` globalmente y un
segundo usuario reutilizando la clave recibia la transaccion ajena (fuga de
UUIDs/montos), sin debito propio y con `transaction.initiated` duplicado.

Cubre:
- B reutiliza la clave de A -> NO recibe la tx de A, se debita/registra lo
  suyo y no se emite evento nuevo sobre la tx ajena.
- Replay del mismo usuario con la misma clave -> su tx (1 solo debito).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()

TABLES = [
    "accounts.accounts",
    "accounts.account_balances",
    "accounts.daily_balance_snapshots",
    "ledger.ledger_accounts",
    "ledger.journal_entries",
    "ledger.postings",
    "ledger.ledger_balances",
    "shared.outbox",
    "shared.processed_events",
    "shared.idempotency_keys",
    "transactions.transactions",
    "transactions.transaction_status_history",
    "transactions.holds",
]


def _make_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401 (registro)
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401
    import app.modules.transactions.models as _t  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "ledger", "shared", "transactions", "config"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    return engine


def _avail(session, account_id) -> int:
    from app.modules.accounts import repository as accounts_repo

    return int(accounts_repo.get_balance(session, account_id).available_minor)


def _count(session, key: str) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[key]))


def _outbox_count(session, tx_id) -> int:
    from app.modules.shared.models import OutboxEntry

    return session.scalar(
        sa.select(sa.func.count()).select_from(OutboxEntry).where(OutboxEntry.aggregate_id == tx_id)
    )


def _real_port():
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT

    return ACCOUNTS_BALANCE_PORT


def test_idempotency_key_is_scoped_to_initiator():
    """B con la clave de A procesa lo suyo (sin fuga, con debito y eventos propios)."""
    from app.modules.accounts import repository as accounts_repo
    from app.modules.transactions import service as svc
    from app.modules.transactions.models import Transaction

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        a1 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="09110001111", type="AHORRO", currency="PEN"
        ).id
        a2 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="09110002222", type="AHORRO", currency="PEN"
        ).id
        b1 = accounts_repo.create_account(
            session, user_id=USER_B, account_number="09220003333", type="AHORRO", currency="PEN"
        ).id
        b2 = accounts_repo.create_account(
            session, user_id=USER_B, account_number="09220004444", type="AHORRO", currency="PEN"
        ).id
        accounts_repo.apply_delta(session, a1, available_delta=10_000, held_delta=0, currency="PEN")
        accounts_repo.apply_delta(session, b1, available_delta=10_000, held_delta=0, currency="PEN")
        session.commit()

        key = f"scope-{uuid.uuid4()}"
        tx_a = svc.transfer_own(
            session,
            initiator_user_id=USER_A,
            source_account_id=a1,
            target_account_id=a2,
            amount_minor=1_000,
            currency="PEN",
            idempotency_key=key,
            balance_port=_real_port(),
        )
        assert tx_a.status == "SETTLED"
        assert tx_a.initiator_user_id == USER_A
        outbox_a_before = _outbox_count(session, tx_a.id)

        # B reutiliza la misma clave: NO debe ver ni recibir la tx de A.
        tx_b = svc.transfer_own(
            session,
            initiator_user_id=USER_B,
            source_account_id=b1,
            target_account_id=b2,
            amount_minor=2_000,
            currency="PEN",
            idempotency_key=key,
            balance_port=_real_port(),
        )
        assert tx_b.id != tx_a.id, "fuga: B recibio la transaccion de A"
        assert tx_b.initiator_user_id == USER_B
        assert tx_b.status == "SETTLED"
        assert tx_b.source_account_id == b1
        assert tx_b.target_account_id == b2
        assert int(tx_b.amount_minor) == 2_000
        # Sin fuga de datos: B no referencia cuentas ni montos de A.
        assert tx_b.source_account_id != tx_a.source_account_id
        assert tx_b.target_account_id != tx_a.target_account_id

        # B si se debita/registra correctamente (no queda "SETTLED" fantasma).
        assert _avail(session, b1) == 8_000
        assert _avail(session, b2) == 2_000
        assert _avail(session, a1) == 9_000
        assert _count(session, "transactions.transactions") == 2

        # No se emite evento ajeno: el outbox de la tx de A queda intacto.
        assert _outbox_count(session, tx_a.id) == outbox_a_before
        # La tx de B tiene sus propios eventos.
        assert _outbox_count(session, tx_b.id) >= 1

        # El repositorio acota por titular en ambos sentidos.
        from app.modules.transactions import repository as repo

        assert repo.get_by_idempotency_key(session, key, initiator_user_id=USER_A).id == tx_a.id
        assert repo.get_by_idempotency_key(session, key, initiator_user_id=USER_B).id == tx_b.id
        assert (
            session.scalars(sa.select(Transaction).where(Transaction.id == tx_a.id)).one().id
            == tx_a.id
        )

        # Replay del mismo usuario sigue funcionando: 1 solo debito.
        tx_a2 = svc.transfer_own(
            session,
            initiator_user_id=USER_A,
            source_account_id=a1,
            target_account_id=a2,
            amount_minor=1_000,
            currency="PEN",
            idempotency_key=key,
            balance_port=_real_port(),
        )
        assert tx_a2.id == tx_a.id
        assert _avail(session, a1) == 9_000
        assert _count(session, "transactions.transactions") == 2
    finally:
        session.close()
        engine.dispose()
