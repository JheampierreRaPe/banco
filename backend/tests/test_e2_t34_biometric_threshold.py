"""Umbral biometrico por operacion, no por acumulado del dia (E2-T34, HU08).

Corrige `evaluate_gate` (`transactions/service/biometric.py`): el umbral
mira el monto de la operacion (`total = amount_minor + fee_minor`) y no el
acumulado del dia. El limite diario SIGUE siendo acumulado
(`acumulado + total > daily_limit` -> `REJECTED`/`DAILY_LIMIT_EXCEEDED`).
El acumulador deja de inflarse con fondos devueltos (`REVERSED`).
(Nota E2-T36: `PENDING_AUTHORIZATION` vuelve a contar —ver
`test_accumulator_counts_pending_excludes_reversed_rejected_failed`—.)

Sobre el motor y el ledger con adaptadores reales
(`ACCOUNTS_BALANCE_PORT`, fachada real `shared.config`, E2-T18): dinero de
juguete (UUIDs sinteticos, sin PII), SQLite en memoria + schemas ATTACH,
sin red, sin `float`.

Mapeo brief -> test:
- monto pequeno con acumulado alto => sin biometria (`SETTLED`)
  (CA-01) ............................ `test_small_amount_with_high_accumulated_skips_biometry`
- `total > threshold` => `PENDING_AUTHORIZATION` con acumulado 0;
  `total == threshold` => `SETTLED`; la comision cuenta en el total
  (CA-02) ............................ `test_total_over_threshold_requires_biometry_from_zero`,
                                       `test_threshold_boundary_and_fee_count_in_total`
- limite diario acumulado intacto: `acumulado + total > daily_limit`
  => `REJECTED`/`DAILY_LIMIT_EXCEEDED` sin hold; la rechazada no acumula
  (CA-03) ............................ `test_daily_limit_still_accumulated`
- acumulador sano: `REVERSED`/`REJECTED`/`FAILED` no cuentan;
  `PENDING_AUTHORIZATION` (E2-T36: las pendientes con hold si comprometen
  el dia) y `FUNDS_HELD`/`POSTED`/`SETTLED`/`CONCILIATED` si
  (CA-04) ............................ `test_accumulator_counts_pending_excludes_reversed_rejected_failed`
  (adaptado en E2-T36: antes excluia la pendiente, semantica derogada)
- paridad propia / tercero / interbancaria con el mismo gate
  (CA-05) ............................ `test_parity_across_routes`
- formula del gate a nivel de decision (CA-01/CA-02/CA-03)
  ................................... `test_evaluate_gate_decision_formula`
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
DEVICE_A = "dev-e2t34-threshold"

THRESHOLD_KEY = "transfer.biometric_threshold_minor"
DAILY_KEY = "transfer.daily_limit_minor"
DAILY_STANDARD_KEY = "transfer.daily_limit_STANDARD_minor"
MAX_ATTEMPTS_KEY = "auth.max_failed_attempts"

# Umbral del fixture: la banda se lee por operacion.
THRESHOLD = 2_000

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

    import app.modules.accounts.models as _a  # registro de tablas
    import app.modules.ledger.models as _l
    import app.modules.shared.models as _s
    import app.modules.transactions.models as _t

    assert _a is not None and _l is not None and _s is not None and _t is not None
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
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE config.parameters (id TEXT PRIMARY KEY, "
                "key TEXT NOT NULL UNIQUE, value_json TEXT NOT NULL, "
                "module TEXT NOT NULL, description TEXT, updated_by TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
        )
    return engine


def _seed_param(session: Session, key: str, value: str, module: str = "transactions") -> None:
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, :module)"
        ),
        {"id": uuid.uuid4().hex, "key": key, "value": value, "module": module},
    )
    session.flush()


def _set_param(session: Session, key: str, value: int) -> None:
    session.execute(
        sa.text("UPDATE config.parameters SET value_json = :value WHERE key = :key"),
        {"value": str(value), "key": key},
    )
    session.flush()


@pytest.fixture(autouse=True)
def _clean_holder_cache():
    from app.modules.transactions import service as svc

    svc.clear_holder_cache()
    yield
    svc.clear_holder_cache()


@pytest.fixture()
def t34_session():
    """Sesion SQLite aislada con cuentas fondeadas y umbral 2000 / limite 500000."""
    from app.modules.accounts import repository as accounts_repo

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        a1 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="00990001111", type="AHORRO", currency="PEN"
        )
        a2 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="00990002222", type="CORRIENTE", currency="PEN"
        )
        b1 = accounts_repo.create_account(
            session, user_id=USER_B, account_number="00880003333", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a1.id, available_delta=100_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a2.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, b1.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        _seed_param(session, THRESHOLD_KEY, str(THRESHOLD))
        _seed_param(session, DAILY_KEY, "500000")
        _seed_param(session, DAILY_STANDARD_KEY, "500000")
        _seed_param(session, MAX_ATTEMPTS_KEY, "5", module="identity")
        session.commit()
        yield {"session": session, "a1": a1.id, "a2": a2.id, "b1": b1.id}
    finally:
        session.close()
        engine.dispose()


def _real_port():
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT

    return ACCOUNTS_BALANCE_PORT


def _third_party(session, a1, b1, amount, **overrides):
    from app.modules.transactions import service as svc

    params = {
        "initiator_user_id": USER_A,
        "source_account_id": a1,
        "target_account_id": b1,
        "amount_minor": amount,
        "currency": "PEN",
        "fee_minor": 0,
        "idempotency_key": f"e2t34-{uuid.uuid4()}",
        "device_id": DEVICE_A,
        "balance_port": _real_port(),
    }
    params.update(overrides)
    return svc.transfer_third_party(session, **params)


def _own(session, a1, a2, amount, **overrides):
    from app.modules.transactions import service as svc

    params = {
        "initiator_user_id": USER_A,
        "source_account_id": a1,
        "target_account_id": a2,
        "amount_minor": amount,
        "currency": "PEN",
        "idempotency_key": f"e2t34-own-{uuid.uuid4()}",
        "device_id": DEVICE_A,
        "balance_port": _real_port(),
    }
    params.update(overrides)
    return svc.transfer_own(session, **params)


CCI_OK = "00200100200100200101"
BANK_CODE = "BCP01"


def _interbank(session, a1, gateway, amount, **overrides):
    from app.adapters.holder_lookup import MockHolderLookupProvider, mock_holder_name_for
    from app.modules.transactions.service import interbank as saga

    params = {
        "initiator_user_id": USER_A,
        "source_account_id": a1,
        "amount_minor": amount,
        "currency": "PEN",
        "cci": CCI_OK,
        "holder_name": mock_holder_name_for(CCI_OK),
        "bank_code": BANK_CODE,
        "fee_minor": 200,
        "idempotency_key": f"e2t34-ib-{uuid.uuid4()}",
        "device_id": DEVICE_A,
        "gateway": gateway,
        "holder_provider": MockHolderLookupProvider(mode="success"),
        "balance_port": _real_port(),
    }
    params.update(overrides)
    return saga.transfer_interbank(session, **params)


def _confirm_gateway():
    from app.adapters.interbank_gateway import MockInterbankGateway

    return MockInterbankGateway(default_send="confirm")


def _holds(session, tx_id):
    from app.modules.transactions import repository as tx_repo

    return list(tx_repo.list_holds_by_transaction(session, tx_id))


# ------------------------------------------------- CA-01: umbral por operacion
def test_small_amount_with_high_accumulated_skips_biometry(t34_session):
    """CA-01: monto bajo no exige biometria aunque el acumulado supere el umbral."""
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = t34_session["session"], t34_session["a1"], t34_session["b1"]
    for _ in range(3):
        first = _third_party(session, a1, b1, 1_000)
        session.commit()
        assert first.status == "SETTLED"
    accumulated = bio_svc.daily_accumulated_minor(session, USER_A, exclude_tx_id=None)
    assert accumulated == 3_000
    assert accumulated > THRESHOLD  # con la semantica derogada esto pediria biometria
    small = _third_party(session, a1, b1, 1_000)
    session.commit()
    assert small.status == "SETTLED"
    assert bio_svc.get_block(small) is None


# --------------------------------------- CA-02: total > umbral exige biometria
def test_total_over_threshold_requires_biometry_from_zero(t34_session):
    """CA-02: `total > threshold` exige biometria con acumulado 0."""
    session, a1, b1 = t34_session["session"], t34_session["a1"], t34_session["b1"]
    tx = _third_party(session, a1, b1, THRESHOLD + 3_000)
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    holds = _holds(session, tx.id)
    assert len(holds) == 1 and holds[0].status == "ACTIVE"


def test_threshold_boundary_and_fee_count_in_total(t34_session):
    """CA-02: `total == threshold` liquida; la comision cuenta en el total."""
    session, a1, b1 = t34_session["session"], t34_session["a1"], t34_session["b1"]
    exact = _third_party(session, a1, b1, THRESHOLD)
    session.commit()
    assert exact.status == "SETTLED"
    # 1900 + 200 de comision = 2100 > 2000: la comision empuja sobre el umbral.
    with_fee = _third_party(session, a1, b1, THRESHOLD - 100, fee_minor=200)
    session.commit()
    assert with_fee.status == "PENDING_AUTHORIZATION"


# -------------------------------------- CA-03: limite diario sigue acumulado
def test_daily_limit_still_accumulated(t34_session):
    """CA-03: `acumulado + total > daily_limit` rechaza sin hold; la rechazada no acumula."""
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = t34_session["session"], t34_session["a1"], t34_session["b1"]
    _set_param(session, THRESHOLD_KEY, 1_000_000)  # aisla el limite del umbral
    _set_param(session, DAILY_KEY, 10_000)
    session.commit()
    first = _third_party(session, a1, b1, 6_000)
    session.commit()
    assert first.status == "SETTLED"
    second = _third_party(session, a1, b1, 5_000)  # 6000 + 5000 > 10000
    session.commit()
    assert second.status == "REJECTED"
    assert second.failure_reason == bio_svc.DAILY_LIMIT_EXCEEDED
    assert _holds(session, second.id) == []
    third = _third_party(session, a1, b1, 3_000)  # 6000 + 3000 <= 10000
    session.commit()
    assert third.status == "SETTLED"


# ------------------------------------------------ CA-04: acumulador sano
def _seed_status(session, target: str, amount: int):
    """Crea una tx del titular hoy y la lleva a `target` por transiciones validas."""
    from app.modules.transactions import repository as tx_repo

    tx = tx_repo.create_transaction(
        session,
        type="THIRD_PARTY_TRANSFER",
        amount_minor=amount,
        currency="PEN",
        status="INITIATED",
        idempotency_key=f"e2t34-seed-{uuid.uuid4()}",
        initiator_user_id=USER_A,
    )
    path = {
        "REJECTED": ["VALIDATED", "REJECTED"],
        "PENDING_AUTHORIZATION": ["VALIDATED", "PENDING_AUTHORIZATION"],
        "FAILED": ["VALIDATED", "AUTHORIZED", "FUNDS_HELD", "FAILED"],
        "FUNDS_HELD": ["VALIDATED", "AUTHORIZED", "FUNDS_HELD"],
        "POSTED": ["VALIDATED", "AUTHORIZED", "POSTED"],
        "SETTLED": ["VALIDATED", "AUTHORIZED", "POSTED", "SETTLED"],
        "CONCILIATED": ["VALIDATED", "AUTHORIZED", "POSTED", "SETTLED", "CONCILIATED"],
        "REVERSED": ["VALIDATED", "AUTHORIZED", "POSTED", "REVERSED"],
    }[target]
    for status in path:
        tx = tx_repo.transition_transaction(session, tx.id, status)
    session.commit()
    return tx


def test_accumulator_counts_pending_excludes_reversed_rejected_failed(t34_session):
    """CA-04 (E2-T36, adapta la semantica derogada de E2-T34): la pendiente SI acumula.

    `EXCLUDED_ACCUMULATED_STATUSES` pasa a `("REJECTED", "FAILED",
    "REVERSED")`: `PENDING_AUTHORIZATION` cuenta (retiene fondos con
    hold) y `REVERSED`/`REJECTED`/`FAILED` siguen sin contar.
    """
    from app.modules.transactions.service import biometric as bio_svc

    session = t34_session["session"]
    assert set(bio_svc.EXCLUDED_ACCUMULATED_STATUSES) == {
        "REJECTED",
        "FAILED",
        "REVERSED",
    }
    _seed_status(session, "SETTLED", 5_000)
    _seed_status(session, "FUNDS_HELD", 11_000)
    _seed_status(session, "CONCILIATED", 13_000)
    _seed_status(session, "PENDING_AUTHORIZATION", 7_000)
    _seed_status(session, "REVERSED", 6_000)
    _seed_status(session, "REJECTED", 9_000)
    _seed_status(session, "FAILED", 8_000)
    accumulated = bio_svc.daily_accumulated_minor(session, USER_A)
    assert accumulated == 5_000 + 11_000 + 13_000 + 7_000


# ------------------------------------------------- CA-05: paridad de rutas
def test_parity_across_routes(t34_session):
    """CA-05: propia, tercero e interbancaria comparten el mismo gate por operacion."""
    session, a1, a2, b1 = (
        t34_session["session"],
        t34_session["a1"],
        t34_session["a2"],
        t34_session["b1"],
    )
    gw = _confirm_gateway()
    for tx in (
        _own(session, a1, a2, 1_000),
        _third_party(session, a1, b1, 1_000),
        _interbank(session, a1, gw, 1_000),  # total 1200 <= 2000
    ):
        session.commit()
        assert tx.status == "SETTLED"
    for tx in (
        _own(session, a1, a2, 5_000),
        _third_party(session, a1, b1, 5_000),
        _interbank(session, a1, gw, 5_000),
    ):
        session.commit()
        assert tx.status == "PENDING_AUTHORIZATION"


# --------------------------------- Formula del gate a nivel de decision
def test_evaluate_gate_decision_formula(t34_session):
    """CA-01/CA-02/CA-03: `requires = (not exceeded) and (total > threshold)`."""
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = t34_session["session"], t34_session["a1"], t34_session["b1"]
    for _ in range(3):
        tx = _third_party(session, a1, b1, 1_000)
        session.commit()
        assert tx.status == "SETTLED"
    small = bio_svc.evaluate_gate(session, user_id=USER_A, amount_minor=1_000)
    assert small.accumulated_minor == 3_000
    assert small.total_minor == 1_000
    assert small.limit_exceeded is False
    assert small.requires_biometry is False
    big = bio_svc.evaluate_gate(session, user_id=USER_A, amount_minor=5_000)
    assert big.accumulated_minor == 3_000
    assert big.requires_biometry is True
    capped = bio_svc.evaluate_gate(
        session,
        user_id=USER_A,
        amount_minor=5_000,
        daily_limit_reader=lambda _s, _p: 7_000,  # 3000 + 5000 > 7000
    )
    assert capped.limit_exceeded is True
    assert capped.requires_biometry is False
    # Dinero entero: el gate nunca usa `float`.
    assert isinstance(small.total_minor, int) and isinstance(small.accumulated_minor, int)
