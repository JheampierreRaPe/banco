"""Regresion CAS atomico del motor interbancario (defecto ALTA: doble liquidacion).

Dos pollers concurrentes sobre la misma transaccion `POSTED` deben producir
exactamente un `SETTLED`: un solo `INTERBANK_SETTLE`, `2200-Transito` neto 0
y `1000-Banco` acreditado una sola vez (sin creacion de dinero).

Sin el fix (`transition_transaction` comparando `expected_version` en memoria
y `_claim_transition` devolviendo `True` por rebote) ambos pollers liquidan.
Con el fix (compare-and-swap `UPDATE ... WHERE version=:esperada` + `rowcount`
y `_claim_transition` `True` solo si este llamador gano) el perdedor es replay.

Sin `threading.Lock` global envolviendo la operacion: la coordinacion usa solo
una `Barrier` en la pasarela para forzar la ventana de carrera (ambos pollers
cargan `POSTED` antes de liquidar), nunca un lock que serialice el settle.
"""

from __future__ import annotations

import threading
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.adapters.holder_lookup import MockHolderLookupProvider, mock_holder_name_for
from app.adapters.interbank_gateway import MockInterbankGateway
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
    """Total acreditado a `1000-Banco` para la transaccion (debe ser 1x monto)."""
    from app.modules.ledger.repository import get_by_code

    bank = get_by_code(session, "1000")
    assert bank is not None
    total = 0
    for entry in _tx_entries(session, tx_id):
        for row in _postings(session, entry.id):
            if str(row.ledger_account_id) == str(bank.id) and row.direction == "CREDIT":
                total += int(row.amount_minor)
    return total


def _setup_posted_tx(engine, amount_minor=10_000, fee_minor=200):
    """Cuenta fondeada + transaccion interbancaria en `POSTED` (commiteada)."""
    from app.modules.accounts import repository as accounts_repo
    from app.modules.transactions.service import interbank as saga

    setup = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        src = accounts_repo.create_account(
            setup, user_id=USER_A, account_number="00330009991", type="AHORRO", currency="PEN"
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
            idempotency_key=f"e2t09-cas-{uuid.uuid4()}",
            external_ref=f"IB-CAS-{uuid.uuid4().hex[:8].upper()}",
            gateway=MockInterbankGateway(default_send="accept"),
            holder_provider=_holder(),
            balance_port=_real_port(),
        )
        assert tx.status == "POSTED"
        seeded.commit()
        return src_id, tx.id, amount_minor
    finally:
        seeded.close()


class _BarrierGateway(MockInterbankGateway):
    """Pasarela que sincroniza a ambos pollers en `get_status` (barrera, no lock)."""

    def __init__(self, barrier: threading.Barrier, **kwargs):
        super().__init__(**kwargs)
        self._barrier = barrier

    def get_status(self, *, reference: str) -> str:
        # Ambos hilos ya cargaron POSTED; se alinean aqui antes de liquidar.
        self._barrier.wait(timeout=30)
        return super().get_status(reference=reference)


def _make_file_engines(tmp_path):
    """Dos engines independientes sobre los mismos archivos SQLite.

    A diferencia del `StaticPool` compartido (una sola conexion: sin
    aislamiento real), aqui cada engine/sesion tiene su conexion propia:
    la sesion perdedora conserva su snapshot obsoleto (`version` vieja en
    su identity map) y solo el CAS en BD la desenmascara. Determinista.
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
            cur.close()

        event.listen(engine, "connect", _attach)
        return engine

    e1, e2 = _mk(), _mk()
    Base.metadata.create_all(e1, tables=[Base.metadata.tables[k] for k in TABLES])
    return e1, e2


def test_cas_same_version_two_attempts_one_wins(tmp_path):
    """Dos intentos con la misma `expected_version`: uno gana, otro `ConcurrencyError`.

    Sesiones verdaderamente independientes (engines/archivos separados): el
    perdedor opera sobre su snapshot obsoleto y solo el CAS en BD
    (`UPDATE ... WHERE version`) lo detecta. Sin el fix ambos ganan.
    """
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.domain.state_machine import ConcurrencyError

    e1, e2 = _make_file_engines(tmp_path)
    try:
        _, tx_id, _ = _setup_posted_tx(e1)
        s1 = Session(bind=e1, autoflush=False, expire_on_commit=False)
        s2 = Session(bind=e2, autoflush=False, expire_on_commit=False)
        try:
            # Cada poller carga SU snapshot y lo mantiene vivo (referencia
            # fuerte en su frame, como `poll_interbank`/`_settle_confirmed`):
            # el identity map es de refs debiles, sin ella el objeto se
            # recolecta y la sesion re-lee fresco (ocultaria el bug).
            loser_snapshot = tx_repo.get_transaction(s2, tx_id)
            assert int(tx_repo.get_transaction(s1, tx_id).version) == int(loser_snapshot.version)
            stale_version = int(loser_snapshot.version)
            won = tx_repo.transition_transaction(
                s1, tx_id, "SETTLED", reason="poller A", expected_version=stale_version
            )
            s1.commit()
            assert won.status == "SETTLED"
            assert int(loser_snapshot.version) == stale_version, "snapshot obsoleto retenido"
            with pytest.raises(ConcurrencyError):
                tx_repo.transition_transaction(
                    s2, tx_id, "SETTLED", reason="poller B", expected_version=stale_version
                )
            s2.rollback()
        finally:
            s1.close()
            s2.close()
    finally:
        e1.dispose()
        e2.dispose()


def test_concurrent_double_poll_single_settle_no_double_entry(tmp_path):
    """Dos pollers reales (hilos, sin Lock) liquidan: exactamente un SETTLE.

    Cada hilo usa su propio engine/conexion sobre los mismos archivos
    (independencia real: el `StaticPool` compartido serializa a nivel de
    driver pysqlite y rompe el commit ajeno, artefacto del harness, no del
    producto). La barrera en la pasarela fuerza la ventana de carrera.
    """
    from app.modules.transactions.service import interbank as saga

    e1, e2 = _make_file_engines(tmp_path)
    try:
        _, tx_id, amount = _setup_posted_tx(e1)
        barrier = threading.Barrier(2)
        outcomes: dict[str, str] = {}

        def _worker(name: str, engine):
            session = Session(bind=engine, autoflush=False, expire_on_commit=False)
            try:
                gw = _BarrierGateway(barrier, default_status="confirmed")
                out = saga.poll_interbank(session, tx_id, gateway=gw, balance_port=_real_port())
                session.commit()
                outcomes[name] = out.status
            except Exception as exc:  # noqa: BLE001 - se reporta como fallo del hilo
                session.rollback()
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
        assert sorted(outcomes.values()) == ["SETTLED", "SETTLED"], outcomes

        check = Session(bind=e1, autoflush=False, expire_on_commit=False)
        try:
            from app.modules.transactions import repository as tx_repo

            final = tx_repo.get_transaction(check, tx_id)
            assert final.status == "SETTLED"
            settles = [e for e in _tx_entries(check, tx_id) if e.entry_type == "INTERBANK_SETTLE"]
            assert len(settles) == 1, "doble polling no duplica la liquidacion"
            assert _transit_net(check) == 0, "2200-Transito cuadra a cero"
            assert (
                _bank_credited_total(check, tx_id) == amount
            ), "1000-Banco acreditado una sola vez (sin creacion de dinero)"
        finally:
            check.close()
    finally:
        e1.dispose()
        e2.dispose()
