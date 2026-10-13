"""Regresion accept-vs-timeout de la saga interbancaria (defecto ALTA residual).

Dos pollers concurrentes sobre `FUNDS_HELD` con desenlace mixto (uno
`ACCEPTED`, otro `GatewayTimeoutError` justo despues del commit del primero):
sin el fix, el perdedor (timeout) escribe `meta` desde su snapshot obsoleto
via `_set_meta` (UPDATE por PK sin WHERE version, last-writer-wins) y pisa
`meta.entries`, perdiendo `send_entry`; un poller confirmado posterior
re-postea `INTERBANK_SEND` -> doble liquidacion de un paso (`2200` neto
-10000). Con el fix (exclusion por transaccion + meta CAS con relectura
fresca y merge), hay exactamente un `INTERBANK_SEND`, `2200` neto 0 y
`1000-Banco` acreditado 1x.

Sin `threading.Lock` global: la coordinacion usa solo una `Barrier` en la
pasarela (ambos pollers cargan `FUNDS_HELD` antes de avanzar) mas un `Event`
para ordenar el timeout justo despues del commit del ganador; nunca un lock
que serialice el avance.
"""

from __future__ import annotations

import threading
import uuid

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.adapters.holder_lookup import MockHolderLookupProvider, mock_holder_name_for
from app.adapters.interbank_gateway import (
    GatewayTimeoutError,
    MockInterbankGateway,
)
from app.core.db import Base

USER_A = uuid.uuid4()
CCI_OK = "00200100200100200101"
BANK_CODE = "BCP01"

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


def _holder():
    return MockHolderLookupProvider(mode="success")


def _real_port():
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT

    return ACCOUNTS_BALANCE_PORT


def _tx_entries(session, tx_id):
    from app.modules.ledger.models import JournalEntry

    return list(
        session.scalars(sa.select(JournalEntry).where(JournalEntry.transaction_id == tx_id)).all()
    )


def _postings(session, entry_id):
    from app.modules.ledger import service as ledger_service

    return ledger_service.list_postings(session, entry_id)


def _transit_net(session) -> int:
    from app.modules.ledger.models import JournalEntry
    from app.modules.ledger.repository import get_by_code

    transit = get_by_code(session, "2200")
    assert transit is not None
    total = 0
    for entry in session.scalars(sa.select(JournalEntry)).all():
        for row in _postings(session, entry.id):
            if str(row.ledger_account_id) == str(transit.id):
                total += row.amount_minor if row.direction == "DEBIT" else -row.amount_minor
    return total


def _bank_credited_total(session, tx_id) -> int:
    from app.modules.ledger.repository import get_by_code

    bank = get_by_code(session, "1000")
    assert bank is not None
    total = 0
    for entry in _tx_entries(session, tx_id):
        for row in _postings(session, entry.id):
            if str(row.ledger_account_id) == str(bank.id) and row.direction == "CREDIT":
                total += int(row.amount_minor)
    return total


def _make_file_engines(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    import app.modules.accounts.models as _a  # noqa: F401 (registro de tablas)
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401
    import app.modules.transactions.models as _t  # noqa: F401

    paths = {
        schema: str(tmp_path / f"{schema}.db")
        for schema in ("accounts", "ledger", "shared", "transactions", "config")
    }

    def _mk():
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False, "timeout": 30},
            poolclass=NullPool,
        )

        def _attach(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            for schema, path in paths.items():
                cur.execute(f"ATTACH DATABASE '{path}' AS {schema}")
            for schema in paths:
                cur.execute(f"PRAGMA {schema}.journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        event.listen(engine, "connect", _attach)
        return engine

    e1, e2 = _mk(), _mk()
    Base.metadata.create_all(e1, tables=[Base.metadata.tables[k] for k in TABLES])
    return e1, e2


class _InitialTimeoutGateway(MockInterbankGateway):
    """Primer envio cae (timeout): deja la tx en `FUNDS_HELD` reintentable."""

    def send_transfer(self, **kwargs):
        raise GatewayTimeoutError("timeout inicial (setup FUNDS_HELD)")


class _AcceptGateway(MockInterbankGateway):
    """Gana la carrera: sincroniza en barrera y retorna `ACCEPTED`."""

    def __init__(self, barrier: threading.Barrier, **kwargs):
        super().__init__(**kwargs)
        self._barrier = barrier

    def send_transfer(self, *, reference, amount_minor, currency, bank_code="", cci_hash=""):
        # Ambos hilos ya cargaron FUNDS_HELD; se alinean aqui para forzar
        # la ventana (el snapshot del perdedor queda obsoleto).
        self._barrier.wait(timeout=30)
        return super().send_transfer(
            reference=reference,
            amount_minor=amount_minor,
            currency=currency,
            bank_code=bank_code,
            cci_hash=cci_hash,
        )


class _TimeoutAfterCommitGateway(MockInterbankGateway):
    """Pierde la carrera: espera el commit del ganador y luego cae (timeout).

    El `bump` posterior corre sobre el snapshot obsoleto (cargado antes del
    commit ajeno): sin el fix pisa `meta.entries` y pierde `send_entry`.
    """

    def __init__(self, barrier: threading.Barrier, committed: threading.Event, **kwargs):
        super().__init__(**kwargs)
        self._barrier = barrier
        self._committed = committed

    def send_transfer(self, *, reference, amount_minor, currency, bank_code="", cci_hash=""):
        self._barrier.wait(timeout=30)
        assert self._committed.wait(timeout=30), "el ganador debio commitear primero"
        raise GatewayTimeoutError("timeout tras el commit del ganador")


def _setup_funds_held_tx(engine, amount_minor=10_000, fee_minor=200):
    """Cuenta fondeada + tx interbancaria en `FUNDS_HELD` (commiteada)."""
    from app.modules.accounts import repository as accounts_repo
    from app.modules.transactions.service import interbank as saga

    setup = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        src = accounts_repo.create_account(
            setup, user_id=USER_A, account_number="00330007772", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            setup, src.id, available_delta=100_000, held_delta=0, currency="PEN"
        )
        setup.commit()
        src_id = src.id
    finally:
        setup.close()
    seeded = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        tx = saga.transfer_interbank(
            seeded,
            initiator_user_id=USER_A,
            source_account_id=src_id,
            amount_minor=amount_minor,
            currency="PEN",
            cci=CCI_OK,
            holder_name=mock_holder_name_for(CCI_OK),
            bank_code=BANK_CODE,
            fee_minor=fee_minor,
            idempotency_key=f"e2t09-accept-timeout-{uuid.uuid4()}",
            external_ref=f"IB-AT-{uuid.uuid4().hex[:8].upper()}",
            gateway=_InitialTimeoutGateway(),
            holder_provider=_holder(),
            balance_port=_real_port(),
        )
        assert tx.status == "FUNDS_HELD", tx.status
        seeded.commit()
        return src_id, tx.id, amount_minor
    finally:
        seeded.close()


def test_concurrent_accept_vs_timeout_single_send(tmp_path):
    """Accept-vs-timeout concurrente: un solo `INTERBANK_SEND`, `2200` neto 0.

    Falla sin el fix (el `bump` del timeout pisa `meta.entries`, el settle
    posterior re-postea `INTERBANK_SEND`: 2 sends, `2200` neto -10000);
    pasa con el fix (exclusion por tx + meta CAS con merge: el perdedor
    conserva `send_entry`).
    """
    from app.modules.transactions.service import interbank as saga

    e1, e2 = _make_file_engines(tmp_path)
    try:
        _, tx_id, amount = _setup_funds_held_tx(e1)
        barrier = threading.Barrier(2)
        committed = threading.Event()
        outcomes: dict[str, str] = {}

        def _worker_accept():
            session = Session(bind=e1, autoflush=False, expire_on_commit=False)
            try:
                gw = _AcceptGateway(barrier, default_send="accept")
                out = saga.poll_interbank(session, tx_id, gateway=gw, balance_port=_real_port())
                session.commit()
                committed.set()
                outcomes["accept"] = out.status
            except Exception as exc:  # noqa: BLE001 - se reporta como fallo del hilo
                session.rollback()
                committed.set()
                outcomes["accept"] = f"ERROR:{type(exc).__name__}:{exc}"
            finally:
                session.close()

        def _worker_timeout():
            session = Session(bind=e2, autoflush=False, expire_on_commit=False)
            try:
                gw = _TimeoutAfterCommitGateway(barrier, committed)
                out = saga.poll_interbank(session, tx_id, gateway=gw, balance_port=_real_port())
                session.commit()
                outcomes["timeout"] = out.status
            except Exception as exc:  # noqa: BLE001 - se reporta como fallo del hilo
                session.rollback()
                outcomes["timeout"] = f"ERROR:{type(exc).__name__}:{exc}"
            finally:
                session.close()

        threads = [
            threading.Thread(target=_worker_accept),
            threading.Thread(target=_worker_timeout),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not any(v.startswith("ERROR") for v in outcomes.values()), outcomes
        # El ganador avanza a POSTED. El perdedor (timeout) sin el fix retorna
        # su snapshot obsoleto FUNDS_HELD; con el fix relee fresco y retorna
        # POSTED. Se acepta ambos aqui: el defecto se verifica abajo (meta
        # pisado -> doble SEND tras el settle).
        assert outcomes.get("accept") == "POSTED", outcomes
        assert outcomes.get("timeout") in ("POSTED", "FUNDS_HELD"), outcomes

        check = Session(bind=e1, autoflush=False, expire_on_commit=False)
        try:
            from app.modules.transactions import repository as tx_repo

            final = tx_repo.get_transaction(check, tx_id)
            assert final.status == "POSTED", final.status
            block = dict((final.meta or {}).get("interbank") or {})
            entries = dict(block.get("entries") or {})
            assert "send_entry" in entries, "meta.entries perdio send_entry (pisado por el timeout)"
            sends = [e for e in _tx_entries(check, tx_id) if e.entry_type == "INTERBANK_SEND"]
            assert len(sends) == 1, "accept-vs-timeout no duplica INTERBANK_SEND aun"
        finally:
            check.close()

        # Un unico settle confirmado posterior no debe re-postear el envio.
        settle = Session(bind=e1, autoflush=False, expire_on_commit=False)
        try:
            gw = MockInterbankGateway(default_status="confirmed")
            out = saga.poll_interbank(settle, tx_id, gateway=gw, balance_port=_real_port())
            settle.commit()
            assert out.status == "SETTLED", out.status
        finally:
            settle.close()

        verify = Session(bind=e1, autoflush=False, expire_on_commit=False)
        try:
            sends = [e for e in _tx_entries(verify, tx_id) if e.entry_type == "INTERBANK_SEND"]
            assert len(sends) == 1, "settle no re-postea INTERBANK_SEND"
            assert _transit_net(verify) == 0, "2200-Transito cuadra a cero"
            assert (
                _bank_credited_total(verify, tx_id) == amount
            ), "1000-Banco acreditado una sola vez (sin creacion de dinero)"
        finally:
            verify.close()
    finally:
        e1.dispose()
        e2.dispose()
