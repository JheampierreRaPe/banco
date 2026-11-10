"""Regresion CAS del poller `FUNDS_HELD` (defecto ALTA: doble `INTERBANK_SEND`).

Dos pollers concurrentes sobre la misma transaccion en `FUNDS_HELD` (estado
que deja `transfer_interbank` tras timeout) deben producir exactamente un
`INTERBANK_SEND`: el ganador reclama `FUNDS_HELD->POSTED` con CAS y asienta;
el perdedor retorna sin asentar.

Sin el fix (asentar primero + CAS despues tragando `ConcurrencyError` sin
revertir) ambos pollers intentan el `INSERT` del `INTERBANK_SEND` y el
asiento perdedor persiste: en SQLite la carrera de escritores concurrentes
falla (`database is locked` o doble `INTERBANK_SEND`); en Postgres persiste
el doble debito `2100-A->2200` y, tras un unico `SETTLE`, `2200` no cuadra
a cero. Con el fix (reclamar primero con `_claim_transition`, solo el
ganador asienta) hay exactamente un `INTERBANK_SEND`, `2200` neto 0 tras
settle y `1000-Banco` acreditado una sola vez.

Sin `threading.Lock` global: la coordinacion usa solo una `Barrier` en la
pasarela para forzar la ventana de carrera (ambos pollers cargan `FUNDS_HELD`
antes de reclamar), nunca un lock que serialice el avance.
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
    """Dos engines independientes sobre los mismos archivos SQLite.

    `WAL` + `busy_timeout`: permite escritores concurrentes reales sin el
    interbloqueo de upgrade (`RESERVED`->`EXCLUSIVE`) del modo journal por
    defecto — sin `Lock` global, la coordinacion sigue siendo solo la
    `Barrier` de la pasarela. Con el defecto, ambos INSERTs progresan y el
    doble `INTERBANK_SEND` persiste (fallo limpio en el conteo); con el fix
    solo el ganador escribe.
    """
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


class _BarrierSendGateway(MockInterbankGateway):
    """Sincroniza a ambos pollers en `send_transfer` (barrera, no lock)."""

    def __init__(self, barrier: threading.Barrier, **kwargs):
        super().__init__(**kwargs)
        self._barrier = barrier

    def send_transfer(self, *, reference, amount_minor, currency, bank_code="", cci_hash=""):
        # Ambos hilos ya cargaron FUNDS_HELD; se alinean aqui antes de
        # reclamar el CAS, forzando la ventana de carrera del defecto.
        self._barrier.wait(timeout=30)
        return super().send_transfer(
            reference=reference,
            amount_minor=amount_minor,
            currency=currency,
            bank_code=bank_code,
            cci_hash=cci_hash,
        )


def _is_busy(exc: BaseException) -> bool:
    return "database is locked" in str(exc).lower()


def _setup_funds_held_tx(engine, amount_minor=10_000, fee_minor=200):
    """Cuenta fondeada + tx interbancaria en `FUNDS_HELD` (commiteada)."""
    from app.modules.accounts import repository as accounts_repo
    from app.modules.transactions.service import interbank as saga

    setup = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        src = accounts_repo.create_account(
            setup, user_id=USER_A, account_number="00330007771", type="AHORRO", currency="PEN"
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
            idempotency_key=f"e2t09-send-{uuid.uuid4()}",
            external_ref=f"IB-SND-{uuid.uuid4().hex[:8].upper()}",
            gateway=_InitialTimeoutGateway(),
            holder_provider=_holder(),
            balance_port=_real_port(),
        )
        assert tx.status == "FUNDS_HELD", tx.status
        seeded.commit()
        return src_id, tx.id, amount_minor
    finally:
        seeded.close()


def test_concurrent_funds_held_poll_single_send(tmp_path):
    """Dos pollers reales sobre `FUNDS_HELD`: exactamente un `INTERBANK_SEND`.

    Falla sin el fix (doble `INTERBANK_SEND` + `2200` descuadrado tras el
    settle unico); pasa con el fix (CAS reclamado antes de asentar).
    """
    from app.modules.transactions.service import interbank as saga

    e1, e2 = _make_file_engines(tmp_path)
    try:
        _, tx_id, amount = _setup_funds_held_tx(e1)
        barrier = threading.Barrier(2)
        outcomes: dict[str, str] = {}

        def _worker(name: str, engine):
            import time

            session = Session(bind=engine, autoflush=False, expire_on_commit=False)
            try:
                # Reintento del paso completo ante contienda SQLite: emula el
                # bloqueo de Postgres (`FOR UPDATE` espera). El poller es
                # idempotente (CAS + replay), asi que reejecutar no duplica.
                last: Exception | None = None
                for i in range(15):
                    try:
                        gw = _BarrierSendGateway(barrier, default_send="accept") if i == 0 else None
                        # Solo la primera pasada usa la barrera (forzar la
                        # ventana); los reintentos van sin barrera (el ganador
                        # ya commiteo, el perdedor es replay).
                        if gw is None:
                            from app.adapters.interbank_gateway import MockInterbankGateway

                            gw = MockInterbankGateway(
                                default_send="accept", default_status="pending"
                            )
                        out = saga.poll_interbank(
                            session, tx_id, gateway=gw, balance_port=_real_port()
                        )
                        session.commit()
                        outcomes[name] = out.status
                        break
                    except Exception as exc:  # noqa: BLE001 - reintento BUSY, resto falla
                        session.rollback()
                        if _is_busy(exc):
                            last = exc
                            time.sleep(min(0.05 * (i + 1), 0.3))
                            continue
                        outcomes[name] = f"ERROR:{type(exc).__name__}:{exc}"
                        break
                else:
                    outcomes[name] = f"ERROR:BusyRetryExhausted:{last}"
            except Exception as exc:  # noqa: BLE001 - se reporta como fallo del hilo
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001, S110 - best-effort
                    pass
                outcomes[name] = f"ERROR:{type(exc).__name__}:{exc}"
            finally:
                session.close()

        threads = [
            threading.Thread(target=_worker, args=("t0", e1)),
            threading.Thread(target=_worker, args=("t1", e2)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not any(v.startswith("ERROR") for v in outcomes.values()), outcomes
        assert sorted(outcomes.values()) == ["POSTED", "POSTED"], outcomes

        check = Session(bind=e1, autoflush=False, expire_on_commit=False)
        try:
            from app.modules.transactions import repository as tx_repo

            final = tx_repo.get_transaction(check, tx_id)
            assert final.status == "POSTED"
            sends = [e for e in _tx_entries(check, tx_id) if e.entry_type == "INTERBANK_SEND"]
            assert len(sends) == 1, "doble polling FUNDS_HELD no duplica INTERBANK_SEND"
        finally:
            check.close()

        # Un unico settle posterior debe cuadrar 2200 a cero y acreditar
        # 1000-Banco una sola vez (sin creacion de dinero).
        settle = Session(bind=e1, autoflush=False, expire_on_commit=False)
        try:
            gw = MockInterbankGateway(default_status="confirmed")
            out = saga.poll_interbank(settle, tx_id, gateway=gw, balance_port=_real_port())
            settle.commit()
            assert out.status == "SETTLED"
        finally:
            settle.close()

        verify = Session(bind=e1, autoflush=False, expire_on_commit=False)
        try:
            sends = [e for e in _tx_entries(verify, tx_id) if e.entry_type == "INTERBANK_SEND"]
            assert len(sends) == 1, "settle no duplica INTERBANK_SEND"
            assert _transit_net(verify) == 0, "2200-Transito cuadra a cero"
            assert (
                _bank_credited_total(verify, tx_id) == amount
            ), "1000-Banco acreditado una sola vez (sin creacion de dinero)"
        finally:
            verify.close()
    finally:
        e1.dispose()
        e2.dispose()
