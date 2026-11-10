"""Pago de servicio y recarga por el motor (E4-T17, HU16 CA-02/CA-04).

- Camino feliz: pago de servicio -> `service_payments.PAID` con
  `receipt_data` (comprobante con `receipt_no` oficial `OP-…`,
  convencion `E2-T10`), asiento cuadrado `docs/04#7.10` + evento
  `service.paid`; recarga -> `topups PAID` + `transaction_id` +
  evento `topup.completed` (CA-02).
- Limite/refuerzo: sobre el base -> `PENDING_AUTHORIZATION` (reto) y
  tras autorizar con PIN (+ `daily_limit_reader` de billetera, G75)
  el replay finaliza a `PAID`; sobre el diario/sin fondos ->
  `REJECTED` sin tocar saldos.
- Idempotencia: misma clave -> un solo movimiento; cuerpo distinto
  -> `CONFLICT`; clave en vuelo -> `CONFLICT`.
- Errores: `biller`/operador/vinculo ajeno -> `404` neutro;
  suministro/telefono invalidos -> `422`; proveedor caido/timeout ->
  `503`/`502`; sin liquidacion configurada -> `422` fail-closed.
- Comprobante: estructura con numero oficial; sin PII (suministro/
  telefono solo enmascarados; nunca en logs ni eventos).
- Frontera: `wallet` no escribe `transactions`/`ledger`/`accounts`
  (solo fachadas `transactions.service`/`accounts.service`).
- Estructural: `alembic heads` = 1 (`0042_wallet_services`).

SQLite en memoria + schemas ATTACH con el motor real; HTTP real
(TestClient) con auth falsa. Sin red.
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.modules.accounts.models as _accounts
import app.modules.audit.models as _audit
import app.modules.ledger.models as _ledger
import app.modules.notifications.models as _notifications
import app.modules.shared.models as _shared
import app.modules.transactions.models as _transactions
import app.modules.wallet.models as _wallet
from app.adapters.service_provider import MockServiceProviderGateway
from app.core.db import Base, get_db
from app.main import app
from app.modules.shared.config import parameters as params
from app.modules.transactions.service import biometric as biometric_service
from app.modules.wallet import service as wallet_service
from app.modules.wallet.api import get_current_user_id
from app.modules.wallet.repository import services as services_repo

assert _accounts is not None and _audit is not None and _ledger is not None
assert _notifications is not None and _shared is not None
assert _transactions is not None and _wallet is not None

USER = uuid.uuid4()
OTHER = uuid.uuid4()
SETTLEMENT_USER = uuid.uuid4()

PAYER_NUMBER = "00110001111111"
PAYER_LOW_NUMBER = "00110002222222"
OTHER_NUMBER = "00110003333333"
SETTLEMENT_NUMBER = "00990001111111"

FUNDED = 1_000_000
FUNDED_LOW = 3_000

EXPRESS_BASE = 20_000
DAILY = 500_000

LUZ_ID = "c1111111-1111-4111-8111-111111111111"
SOL_ID = "c5555555-5555-4555-8555-555555555555"
LUNA_ID = "c6666666-6666-4666-8666-666666666666"
SOL_NAME = "Recarga Movil Sol Demo"
LUNA_NAME = "Recarga Movil Luna Demo"
SUPPLY = "10293847"
PHONE = "999888777"
LUNA_PHONE = "922598475"

TABLES = [
    "wallet.wallets",
    "wallet.wallet_links",
    "wallet.billers",
    "wallet.service_payments",
    "wallet.topups",
    "wallet.service_subscriptions",
    "accounts.accounts",
    "accounts.account_balances",
    "ledger.ledger_accounts",
    "ledger.journal_entries",
    "ledger.postings",
    "ledger.ledger_balances",
    "transactions.transactions",
    "transactions.transaction_status_history",
    "transactions.holds",
    "shared.outbox",
    "shared.processed_events",
    "shared.idempotency_keys",
    "audit.audit_log",
]

SCHEMAS = (
    "wallet",
    "accounts",
    "ledger",
    "transactions",
    "shared",
    "audit",
    "config",
)


def _seed_param(session: Session, key: str, value: str, module: str = "wallet") -> None:
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, :module)"
        ),
        {"id": uuid.uuid4().hex, "key": key, "value": value, "module": module},
    )
    session.flush()


@pytest.fixture()
def env(monkeypatch):
    """Motor real en SQLite: pagador + liquidacion + vinculos + billers."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from app.modules.accounts import repository as accounts_repo

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("SERVICE_PROVIDER", "mock")
    monkeypatch.setenv("SERVICE_PROVIDER_MOCK_MODE", "success")
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in SCHEMAS:
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE config.parameters (id TEXT PRIMARY KEY, "
                "key TEXT NOT NULL UNIQUE, value_json TEXT NOT NULL, "
                "module TEXT NOT NULL, description TEXT, updated_by TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
        )
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        _seed_param(session, params.WALLET_EXPRESS_LIMIT_MINOR_KEY, str(EXPRESS_BASE))
        _seed_param(session, params.WALLET_DAILY_LIMIT_MINOR_KEY, str(DAILY))
        services_repo.ensure_demo_billers(session)
        payer = accounts_repo.create_account(
            session, user_id=USER, account_number=PAYER_NUMBER, type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, payer.id, available_delta=FUNDED, held_delta=0, currency="PEN"
        )
        payer_low = accounts_repo.create_account(
            session,
            user_id=USER,
            account_number=PAYER_LOW_NUMBER,
            type="AHORRO",
            currency="PEN",
        )
        accounts_repo.apply_delta(
            session, payer_low.id, available_delta=FUNDED_LOW, held_delta=0, currency="PEN"
        )
        other_acct = accounts_repo.create_account(
            session, user_id=OTHER, account_number=OTHER_NUMBER, type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, other_acct.id, available_delta=FUNDED, held_delta=0, currency="PEN"
        )
        settlement = accounts_repo.create_account(
            session,
            user_id=SETTLEMENT_USER,
            account_number=SETTLEMENT_NUMBER,
            type="AHORRO",
            currency="PEN",
        )
        link = wallet_service.link_source(session, USER, type="ACCOUNT", account_id=payer.id)
        low_link = wallet_service.link_source(
            session, USER, type="ACCOUNT", account_id=payer_low.id
        )
        other_link = wallet_service.link_source(
            session, OTHER, type="ACCOUNT", account_id=other_acct.id
        )
        session.commit()
        monkeypatch.setenv(wallet_service.SETTLEMENT_ACCOUNT_ENV_VAR, str(settlement.id))
        yield {
            "session": session,
            "payer": payer.id,
            "payer_low": payer_low.id,
            "settlement": settlement.id,
            "link": link["id"],
            "low_link": low_link["id"],
            "other_link": other_link["id"],
        }
        session.rollback()
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def api_client(env, monkeypatch):
    """TestClient con BD SQLite y auth falsa como el titular."""
    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    session = env["session"]

    def _override_db():
        yield session

    def _override_user():
        return USER

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = _override_user
    try:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)


def _act_as(user_id: uuid.UUID):
    """Cambia el usuario autenticado del TestClient (aislamiento)."""
    app.dependency_overrides[get_current_user_id] = lambda: user_id


# ------------------------------------------------------- helpers comunes


def _balances(session: Session, *account_ids: uuid.UUID) -> dict[str, tuple[int, int]]:
    from app.modules.accounts.models import AccountBalance

    out = {}
    for aid in account_ids:
        row = session.scalars(
            sa.select(AccountBalance).where(AccountBalance.account_id == aid)
        ).one()
        out[str(aid)] = (int(row.available_minor), int(row.held_minor))
    return out


def _tx_count(session: Session) -> int:
    return int(session.execute(sa.text("SELECT COUNT(*) FROM transactions.transactions")).scalar())


def _entry_count(session: Session) -> int:
    return int(session.execute(sa.text("SELECT COUNT(*) FROM ledger.journal_entries")).scalar())


def _payment_count(session: Session) -> int:
    return int(session.execute(sa.text("SELECT COUNT(*) FROM wallet.service_payments")).scalar())


def _topup_count(session: Session) -> int:
    return int(session.execute(sa.text("SELECT COUNT(*) FROM wallet.topups")).scalar())


def _balanced_postings(session: Session, tx_id: uuid.UUID) -> bool:
    from app.modules.ledger.models import JournalEntry, Posting

    entries = session.scalars(
        sa.select(JournalEntry).where(JournalEntry.transaction_id == tx_id)
    ).all()
    assert entries, "el pago debe dejar asiento(s) en el ledger"
    for entry in entries:
        postings = session.scalars(
            sa.select(Posting).where(Posting.journal_entry_id == entry.id)
        ).all()
        debit = sum(int(p.amount_minor) for p in postings if p.direction == "DEBIT")
        credit = sum(int(p.amount_minor) for p in postings if p.direction == "CREDIT")
        assert debit > 0 and debit == credit, f"asiento descuadrado: {entry.id}"
        assert all(isinstance(p.amount_minor, int) for p in postings)
    return True


def _service_events(session: Session, payment_id: uuid.UUID) -> list:
    from app.modules.shared.models import OutboxEntry

    return session.scalars(
        sa.select(OutboxEntry).where(
            OutboxEntry.event_type == "service.paid",
            OutboxEntry.aggregate_id == payment_id,
        )
    ).all()


def _topup_events(session: Session, topup_id: uuid.UUID) -> list:
    from app.modules.shared.models import OutboxEntry

    return session.scalars(
        sa.select(OutboxEntry).where(
            OutboxEntry.event_type == "topup.completed",
            OutboxEntry.aggregate_id == topup_id,
        )
    ).all()


def _pay_kwargs(env, **overrides):
    base = {
        "biller_id": uuid.UUID(LUZ_ID),
        "supply_ref": SUPPLY,
        "amount_minor": 5_000,
        "currency": "PEN",
        "link_id": env["link"],
        "idempotency_key": "e4-t17-pay-1",
    }
    base.update(overrides)
    return base


def _topup_kwargs(env, **overrides):
    base = {
        "operator": SOL_NAME,
        "phone": PHONE,
        "amount_minor": 3_000,
        "currency": "PEN",
        "link_id": env["link"],
        "idempotency_key": "e4-t17-topup-1",
    }
    base.update(overrides)
    return base


# ------------------------------------------------------- camino feliz


def test_pay_service_happy_path_settles_with_receipt_and_square_entry(env):
    session = env["session"]
    payment, tx = wallet_service.pay_service(session, USER, **_pay_kwargs(env))
    session.commit()
    assert payment.status == "PAID"
    assert tx.status == "SETTLED"
    assert str(payment.transaction_id) == str(tx.id)
    assert int(payment.amount_minor) == 5_000
    assert isinstance(payment.amount_minor, int)
    assert payment.currency == "PEN"
    receipt = payment.receipt_data
    assert set(receipt) == {
        "receipt_no",
        "transaction_id",
        "concept",
        "supply_masked",
        "amount_minor",
        "currency",
        "paid_at",
        "status",
    }
    assert receipt["receipt_no"] == wallet_service.build_receipt_no(tx.id)
    assert receipt["transaction_id"] == str(tx.id)
    assert receipt["concept"] == "Luz del Hogar Demo"
    assert SUPPLY not in json.dumps(receipt)
    assert receipt["status"] == "PAID"
    events = _service_events(session, payment.id)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["service_payment_id"] == str(payment.id)
    assert payload["biller_id"] == LUZ_ID
    assert payload["transaction_id"] == str(tx.id)
    assert payload["amount_minor"] == 5_000
    assert SUPPLY not in json.dumps(payload)
    assert _balanced_postings(session, tx.id)
    balances = _balances(session, env["payer"], env["settlement"])
    assert balances[str(env["payer"])] == (FUNDED - 5_000, 0)
    assert balances[str(env["settlement"])] == (5_000, 0)
    assert _tx_count(session) == 1
    assert _payment_count(session) == 1


def test_topup_happy_path_moves_money_and_emits_event(env):
    session = env["session"]
    topup, tx = wallet_service.pay_topup(session, USER, **_topup_kwargs(env))
    session.commit()
    assert topup.status == "PAID"
    assert tx.status == "SETTLED"
    assert str(topup.transaction_id) == str(tx.id)
    assert topup.operator == SOL_NAME
    assert topup.phone == PHONE
    assert isinstance(topup.amount_minor, int)
    events = _topup_events(session, topup.id)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["topup_id"] == str(topup.id)
    assert payload["operator"] == SOL_NAME
    assert payload["transaction_id"] == str(tx.id)
    assert payload["amount_minor"] == 3_000
    assert PHONE not in json.dumps(payload)
    receipt = wallet_service.to_topup_receipt(topup)
    assert receipt is not None
    assert receipt["receipt_no"] == wallet_service.build_receipt_no(tx.id)
    assert receipt["concept"] == f"Recarga {SOL_NAME}"
    assert PHONE not in json.dumps(receipt)
    assert _balanced_postings(session, tx.id)
    balances = _balances(session, env["payer"], env["settlement"])
    assert balances[str(env["payer"])] == (FUNDED - 3_000, 0)
    assert balances[str(env["settlement"])] == (3_000, 0)
    assert _tx_count(session) == 1
    assert _topup_count(session) == 1


def test_recarga_happy_path_with_seeded_settlement_account(env, monkeypatch):
    """Regresion BUG HU16 (critico): la recarga liquida con la cuenta de
    liquidacion demo determinista (UUID fijo de la migracion
    `0046_services_settlement`) inyectada por la env
    `WALLET_SERVICES_SETTLEMENT_ACCOUNT_ID`.

    Antes del fix, en runtime (docker) faltaba la env y no habia cuenta
    sembrada: TODA recarga/pago respondia `422 VALIDATION_ERROR` ("algun
    campo es invalido") sin mover dinero. Ahora la recarga se crea y el
    motor mueve el dinero (asiento cuadrado) hacia la cuenta de liquidacion.
    """
    from tests.factories import (
        SETTLEMENT_ACCOUNT_ID,
        SETTLEMENT_ACCOUNT_NUMBER,
        SETTLEMENT_OWNER_ID,
    )

    session = env["session"]
    settlement_id = uuid.UUID(SETTLEMENT_ACCOUNT_ID)
    session.add(
        _accounts.Account(
            id=settlement_id,
            user_id=uuid.UUID(SETTLEMENT_OWNER_ID),
            account_number=SETTLEMENT_ACCOUNT_NUMBER,
            type="CORRIENTE",
            currency="PEN",
            status="ACTIVE",
        )
    )
    session.add(
        _accounts.AccountBalance(
            account_id=settlement_id,
            currency="PEN",
            available_minor=0,
            held_minor=0,
            version=0,
        )
    )
    session.commit()
    monkeypatch.setenv(wallet_service.SETTLEMENT_ACCOUNT_ENV_VAR, SETTLEMENT_ACCOUNT_ID)

    before = _balances(session, env["payer"])
    topup, tx = wallet_service.pay_topup(
        session,
        USER,
        **_topup_kwargs(
            env,
            operator=LUNA_NAME,
            phone=LUNA_PHONE,
            amount_minor=2_000,
            idempotency_key="hu16-bugfix-topup-1",
        ),
    )
    session.commit()
    assert topup.status == "PAID"
    assert tx.status == "SETTLED"
    assert topup.operator == LUNA_NAME
    assert topup.phone == LUNA_PHONE
    receipt = wallet_service.to_topup_receipt(topup)
    assert receipt is not None
    assert receipt["concept"] == f"Recarga {LUNA_NAME}"
    assert LUNA_PHONE not in json.dumps(receipt)
    assert _balanced_postings(session, tx.id)
    balances = _balances(session, env["payer"], settlement_id)
    assert balances[str(env["payer"])] == (before[str(env["payer"])][0] - 2_000, 0)
    assert balances[str(settlement_id)] == (2_000, 0)
    assert _topup_count(session) == 1


def test_receipt_no_follows_official_format_and_is_deterministic():
    first = wallet_service.build_receipt_no(uuid.uuid4())
    assert first.startswith("OP-")
    assert len(first) == 15
    assert first[3:] == first[3:].upper()
    int(first[3:], 16)
    tx_id = uuid.uuid4()
    assert wallet_service.build_receipt_no(tx_id) == wallet_service.build_receipt_no(tx_id)
    assert wallet_service.build_receipt_no(uuid.uuid4()) != wallet_service.build_receipt_no(
        uuid.uuid4()
    )


# ------------------------------------------------------- limite / refuerzo


def test_over_base_pending_then_authorize_finalizes(env):
    session = env["session"]
    amount = EXPRESS_BASE + 1
    payment, tx = wallet_service.pay_service(
        session, USER, **_pay_kwargs(env, amount_minor=amount, idempotency_key="e4-t17-pend-1")
    )
    session.commit()
    assert payment.status == "PENDING"
    assert tx.status == "PENDING_AUTHORIZATION"
    assert payment.receipt_data is None
    assert _service_events(session, payment.id) == []

    settled = biometric_service.authorize_transaction_with_pin(
        session,
        str(tx.id),
        pin="1234",
        pin_verifier=lambda _s, _u, _p: _p == "1234",
        daily_limit_reader=wallet_service.wallet_daily_limit_reader,
    )
    session.commit()
    assert settled.status == "SETTLED"

    again, _same_tx = wallet_service.pay_service(
        session, USER, **_pay_kwargs(env, amount_minor=amount, idempotency_key="e4-t17-pend-1")
    )
    session.commit()
    assert str(again.id) == str(payment.id)
    assert again.status == "PAID"
    assert again.receipt_data is not None
    assert again.receipt_data["receipt_no"] == wallet_service.build_receipt_no(tx.id)
    assert len(_service_events(session, payment.id)) == 1
    balances = _balances(session, env["payer"], env["settlement"])
    assert balances[str(env["payer"])] == (FUNDED - amount, 0)
    assert balances[str(env["settlement"])] == (amount, 0)

    final = wallet_service.finalize_service_payment(
        session, payment_id=payment.id, transaction=settled
    )
    session.commit()
    assert str(final.id) == str(payment.id)
    assert len(_service_events(session, payment.id)) == 1


def test_topup_over_base_pending_then_authorize_finalizes(env):
    session = env["session"]
    amount = EXPRESS_BASE + 1
    topup, tx = wallet_service.pay_topup(
        session, USER, **_topup_kwargs(env, amount_minor=amount, idempotency_key="e4-t17-tpend-1")
    )
    session.commit()
    assert topup.status == "PENDING"
    assert tx.status == "PENDING_AUTHORIZATION"
    assert wallet_service.to_topup_receipt(topup) is None

    settled = biometric_service.authorize_transaction_with_pin(
        session,
        str(tx.id),
        pin="1234",
        pin_verifier=lambda _s, _u, _p: _p == "1234",
        daily_limit_reader=wallet_service.wallet_daily_limit_reader,
    )
    session.commit()
    assert settled.status == "SETTLED"

    again, _ = wallet_service.pay_topup(
        session, USER, **_topup_kwargs(env, amount_minor=amount, idempotency_key="e4-t17-tpend-1")
    )
    session.commit()
    assert str(again.id) == str(topup.id)
    assert again.status == "PAID"
    assert len(_topup_events(session, topup.id)) == 1
    assert wallet_service.to_topup_receipt(again) is not None


def test_over_daily_rejected_without_touching_balances(env):
    session = env["session"]
    before = _balances(session, env["payer"], env["settlement"])
    before_entries = _entry_count(session)
    payment, tx = wallet_service.pay_service(
        session, USER, **_pay_kwargs(env, amount_minor=DAILY + 1, idempotency_key="e4-t17-daily-1")
    )
    session.commit()
    assert payment.status == "REJECTED"
    assert tx.status == "REJECTED"
    assert payment.receipt_data is None
    assert _service_events(session, payment.id) == []
    assert _balances(session, env["payer"], env["settlement"]) == before
    assert _entry_count(session) == before_entries


def test_insufficient_funds_rejected_without_touching_balances(env):
    session = env["session"]
    before = _balances(session, env["payer_low"], env["settlement"])
    payment, tx = wallet_service.pay_topup(
        session,
        USER,
        **_topup_kwargs(
            env,
            amount_minor=FUNDED_LOW + 1_000,
            link_id=env["low_link"],
            idempotency_key="e4-t17-nofunds-1",
        ),
    )
    session.commit()
    assert payment.status == "REJECTED"
    assert tx.status == "REJECTED"
    assert _topup_events(session, payment.id) == []
    assert _balances(session, env["payer_low"], env["settlement"]) == before


def test_retry_after_rejected_settles_with_new_key(env):
    session = env["session"]
    rejected, _ = wallet_service.pay_topup(
        session,
        USER,
        **_topup_kwargs(
            env,
            amount_minor=FUNDED_LOW + 1_000,
            link_id=env["low_link"],
            idempotency_key="e4-t17-retry-1",
        ),
    )
    session.commit()
    assert rejected.status == "REJECTED"
    ok, tx = wallet_service.pay_topup(
        session, USER, **_topup_kwargs(env, idempotency_key="e4-t17-retry-2")
    )
    session.commit()
    assert ok.status == "PAID"
    assert tx.status == "SETTLED"
    assert _topup_count(session) == 2


# ------------------------------------------------------- idempotencia


def test_same_key_replays_single_movement(env):
    session = env["session"]
    first, first_tx = wallet_service.pay_service(session, USER, **_pay_kwargs(env))
    session.commit()
    second, second_tx = wallet_service.pay_service(session, USER, **_pay_kwargs(env))
    session.commit()
    assert str(second.id) == str(first.id)
    assert str(second_tx.id) == str(first_tx.id)
    assert _tx_count(session) == 1
    assert _payment_count(session) == 1
    assert len(_service_events(session, first.id)) == 1
    balances = _balances(session, env["payer"])
    assert balances[str(env["payer"])] == (FUNDED - 5_000, 0)


def test_same_key_different_body_is_conflict(env):
    session = env["session"]
    wallet_service.pay_service(session, USER, **_pay_kwargs(env))
    session.commit()
    with pytest.raises(wallet_service.ServicePaymentConflictError):
        wallet_service.pay_service(session, USER, **_pay_kwargs(env, amount_minor=5_001))
    session.rollback()
    assert _tx_count(session) == 1
    assert _payment_count(session) == 1


def test_inflight_key_is_conflict(env):
    """Clave reclamada sin snapshot (carrera en vuelo) -> `CONFLICT`."""
    from app.modules.shared.repository.idempotency import (
        resolve_ttl_seconds,
        try_claim,
    )

    session = env["session"]
    row, created = try_claim(
        session,
        key="e4-t17-inflight-1",
        user_id=USER,
        endpoint=wallet_service.SERVICE_PAYMENT_IDEMPOTENCY_ENDPOINT,
        method="POST",
        request_hash="0" * 64,
        ttl_seconds=resolve_ttl_seconds(),
    )
    assert created
    session.commit()
    with pytest.raises(wallet_service.ServicePaymentConflictError):
        wallet_service.pay_service(
            session, USER, **_pay_kwargs(env, idempotency_key="e4-t17-inflight-1")
        )
    session.rollback()
    assert _tx_count(session) == 0
    assert _payment_count(session) == 0
    assert row is not None


def test_topup_same_key_replays_single_movement(env):
    session = env["session"]
    first, _ = wallet_service.pay_topup(session, USER, **_topup_kwargs(env))
    session.commit()
    second, _ = wallet_service.pay_topup(session, USER, **_topup_kwargs(env))
    session.commit()
    assert str(second.id) == str(first.id)
    assert _tx_count(session) == 1
    assert _topup_count(session) == 1


def test_replay_payment_same_key_survives_provider_outage_and_missing_settlement(env, monkeypatch):
    """H1: misma clave + mismo cuerpo = replay aunque el proveedor este
    caido o la cuenta de liquidacion ya no este configurada (sin
    volver a cobrar ni fallar; clave nueva sigue validando)."""
    session = env["session"]
    first, first_tx = wallet_service.pay_service(
        session, USER, **_pay_kwargs(env, idempotency_key="e4-t17-h1-pay-1")
    )
    session.commit()
    assert first.status == "PAID"

    second, second_tx = wallet_service.pay_service(
        session,
        USER,
        **_pay_kwargs(
            env,
            idempotency_key="e4-t17-h1-pay-1",
            gateway=MockServiceProviderGateway(mode="down"),
        ),
    )
    session.commit()
    assert str(second.id) == str(first.id)
    assert str(second_tx.id) == str(first_tx.id)
    assert second.status == "PAID"

    monkeypatch.delenv(wallet_service.SETTLEMENT_ACCOUNT_ENV_VAR, raising=False)
    third, third_tx = wallet_service.pay_service(
        session, USER, **_pay_kwargs(env, idempotency_key="e4-t17-h1-pay-1")
    )
    session.commit()
    assert str(third.id) == str(first.id)
    assert str(third_tx.id) == str(first_tx.id)
    assert _tx_count(session) == 1
    assert _payment_count(session) == 1
    assert len(_service_events(session, first.id)) == 1
    assert _balances(session, env["payer"])[str(env["payer"])] == (FUNDED - 5_000, 0)

    with pytest.raises(wallet_service.ServiceProviderDownError):
        wallet_service.pay_service(
            session,
            USER,
            **_pay_kwargs(
                env,
                idempotency_key="e4-t17-h1-pay-new",
                gateway=MockServiceProviderGateway(mode="down"),
            ),
        )
    session.rollback()
    with pytest.raises(wallet_service.ServicePaymentError):
        wallet_service.pay_service(
            session, USER, **_pay_kwargs(env, idempotency_key="e4-t17-h1-pay-new2")
        )
    session.rollback()
    assert _tx_count(session) == 1
    assert _payment_count(session) == 1


def test_replay_topup_same_key_survives_provider_outage_and_missing_settlement(env, monkeypatch):
    """H1 (recarga): misma clave + mismo cuerpo = replay aunque el
    proveedor falle (timeout) o la cuenta de liquidacion ya no este
    configurada; clave nueva sigue validando fail-closed."""
    session = env["session"]
    first, first_tx = wallet_service.pay_topup(
        session, USER, **_topup_kwargs(env, idempotency_key="e4-t17-h1-topup-1")
    )
    session.commit()
    assert first.status == "PAID"

    second, second_tx = wallet_service.pay_topup(
        session,
        USER,
        **_topup_kwargs(
            env,
            idempotency_key="e4-t17-h1-topup-1",
            gateway=MockServiceProviderGateway(mode="timeout"),
        ),
    )
    session.commit()
    assert str(second.id) == str(first.id)
    assert str(second_tx.id) == str(first_tx.id)
    assert second.status == "PAID"

    monkeypatch.delenv(wallet_service.SETTLEMENT_ACCOUNT_ENV_VAR, raising=False)
    third, third_tx = wallet_service.pay_topup(
        session, USER, **_topup_kwargs(env, idempotency_key="e4-t17-h1-topup-1")
    )
    session.commit()
    assert str(third.id) == str(first.id)
    assert str(third_tx.id) == str(first_tx.id)
    assert _tx_count(session) == 1
    assert _topup_count(session) == 1
    assert len(_topup_events(session, first.id)) == 1

    with pytest.raises(wallet_service.ServiceProviderGatewayTimeoutError):
        wallet_service.pay_topup(
            session,
            USER,
            **_topup_kwargs(
                env,
                idempotency_key="e4-t17-h1-topup-new",
                gateway=MockServiceProviderGateway(mode="timeout"),
            ),
        )
    session.rollback()
    with pytest.raises(wallet_service.ServicePaymentError):
        wallet_service.pay_topup(
            session, USER, **_topup_kwargs(env, idempotency_key="e4-t17-h1-topup-new2")
        )
    session.rollback()
    assert _tx_count(session) == 1
    assert _topup_count(session) == 1


# ------------------------------------------------------- errores


def test_unknown_biller_is_404_neutral(env):
    session = env["session"]
    before_entries = _entry_count(session)
    with pytest.raises(wallet_service.ServiceBillerNotFoundError):
        wallet_service.pay_service(session, USER, **_pay_kwargs(env, biller_id=uuid.uuid4()))
    session.rollback()
    assert _tx_count(session) == 0
    assert _entry_count(session) == before_entries


def test_disabled_biller_is_404_neutral(env):
    session = env["session"]
    row = services_repo.get_biller(session, LUZ_ID)
    assert row is not None
    row.enabled = False
    session.commit()
    with pytest.raises(wallet_service.ServiceBillerNotFoundError):
        wallet_service.pay_service(session, USER, **_pay_kwargs(env))
    session.rollback()


def test_invalid_supply_is_422_without_moving_funds(env):
    session = env["session"]
    before = _balances(session, env["payer"])
    with pytest.raises(wallet_service.ServicePaymentError):
        wallet_service.pay_service(session, USER, **_pay_kwargs(env, supply_ref="!!"))
    session.rollback()
    assert _tx_count(session) == 0
    assert _payment_count(session) == 0
    assert _balances(session, env["payer"]) == before


def test_rejected_supply_is_422(env):
    session = env["session"]
    gateway = MockServiceProviderGateway(rejected=[SUPPLY])
    with pytest.raises(wallet_service.ServicePaymentError):
        wallet_service.pay_service(session, USER, **_pay_kwargs(env), gateway=gateway)
    session.rollback()
    assert _tx_count(session) == 0


def test_provider_down_is_503_and_timeout_is_502(env):
    session = env["session"]
    with pytest.raises(wallet_service.ServiceProviderDownError):
        wallet_service.pay_service(
            session,
            USER,
            **_pay_kwargs(env),
            gateway=MockServiceProviderGateway(mode="down"),
        )
    session.rollback()
    with pytest.raises(wallet_service.ServiceProviderGatewayTimeoutError):
        wallet_service.pay_service(
            session,
            USER,
            **_pay_kwargs(env, idempotency_key="e4-t17-down-2"),
            gateway=MockServiceProviderGateway(mode="timeout"),
        )
    session.rollback()
    assert _tx_count(session) == 0


def test_unknown_operator_is_404_neutral(env):
    session = env["session"]
    with pytest.raises(wallet_service.TopupOperatorNotFoundError):
        wallet_service.pay_topup(session, USER, **_topup_kwargs(env, operator="Operador Fantasma"))
    session.rollback()
    with pytest.raises(wallet_service.TopupOperatorNotFoundError):
        # Un biller que no es de recarga no opera recargas.
        wallet_service.pay_topup(
            session,
            USER,
            **_topup_kwargs(env, operator="Luz del Hogar Demo", idempotency_key="e4-t17-op-2"),
        )
    session.rollback()
    assert _tx_count(session) == 0


def test_operator_lookup_is_case_insensitive(env):
    session = env["session"]
    topup, tx = wallet_service.pay_topup(
        session, USER, **_topup_kwargs(env, operator="  recarga movil sol demo  ")
    )
    session.commit()
    assert topup.status == "PAID"
    assert tx.status == "SETTLED"


def test_invalid_phone_is_422(env):
    session = env["session"]
    with pytest.raises(wallet_service.ServicePaymentError):
        wallet_service.pay_topup(session, USER, **_topup_kwargs(env, phone="abc"))
    session.rollback()
    assert _tx_count(session) == 0


def test_foreign_link_is_404_neutral(env):
    session = env["session"]
    with pytest.raises(Exception) as exc:
        wallet_service.pay_service(session, USER, **_pay_kwargs(env, link_id=env["other_link"]))
    assert "inexistente" in str(exc.value)
    session.rollback()
    assert _tx_count(session) == 0


def test_card_link_is_not_debitable(env):
    session = env["session"]
    view = wallet_service.link_source(
        session, USER, type="CARD", card={"token_ref": "tok-e4t17-1", "last4": "1234"}
    )
    session.commit()
    with pytest.raises(wallet_service.ExpressValidationError):
        wallet_service.pay_service(session, USER, **_pay_kwargs(env, link_id=view["id"]))
    session.rollback()
    assert _tx_count(session) == 0


def test_settlement_unconfigured_is_fail_closed(env, monkeypatch):
    session = env["session"]
    monkeypatch.delenv(wallet_service.SETTLEMENT_ACCOUNT_ENV_VAR, raising=False)
    before = _balances(session, env["payer"])
    with pytest.raises(wallet_service.ServicePaymentError):
        wallet_service.pay_service(session, USER, **_pay_kwargs(env))
    session.rollback()
    assert _tx_count(session) == 0
    assert _balances(session, env["payer"]) == before


def test_settlement_unconfigured_uses_dedicated_code(env, monkeypatch):
    """El fallo de liquidacion es un subtipo dedicado con `code`
    `SETTLEMENT_UNAVAILABLE` (no `VALIDATION_ERROR`), sigue siendo
    fail-closed y no mueve dinero."""
    session = env["session"]
    monkeypatch.delenv(wallet_service.SETTLEMENT_ACCOUNT_ENV_VAR, raising=False)
    with pytest.raises(wallet_service.ServiceSettlementUnavailableError) as exc:
        wallet_service.resolve_settlement_account(session, "PEN")
    assert exc.value.code == "SETTLEMENT_UNAVAILABLE"
    assert isinstance(exc.value, wallet_service.ServicePaymentError)
    session.rollback()
    assert _tx_count(session) == 0


def test_missing_key_is_rejected(env):
    session = env["session"]
    with pytest.raises(wallet_service.ServicePaymentError):
        wallet_service.pay_service(session, USER, **_pay_kwargs(env, idempotency_key=""))
    session.rollback()


# ------------------------------------------------------- sin PII / frontera


def test_supply_and_phone_never_leak_in_logs_or_events(env, caplog):
    session = env["session"]
    with caplog.at_level(logging.INFO, logger="app.modules.wallet.service.services"):
        wallet_service.pay_service(session, USER, **_pay_kwargs(env))
        wallet_service.pay_topup(
            session, USER, **_topup_kwargs(env, idempotency_key="e4-t17-nopii-2")
        )
    session.commit()
    for record in caplog.records:
        assert SUPPLY not in record.getMessage()
        assert PHONE not in record.getMessage()


def test_module_boundary_only_facades():
    import pathlib

    for name in ("service/services.py", "repository/services.py", "api/services.py"):
        source = (
            pathlib.Path(__file__).resolve().parents[1] / "app" / "modules" / "wallet" / name
        ).read_text(encoding="utf-8")
        assert "transactions.repository" not in source
        assert "transactions.models" not in source
        assert "ledger.repository" not in source
        assert "ledger.models" not in source
        assert "accounts.models" not in source
        assert "accounts.repository" not in source
        assert "float(" not in source


# ------------------------------------------------------- API


def _post_payment(client, env, key="api-pay-1", **overrides):
    body = {
        "biller_id": LUZ_ID,
        "supply_ref": SUPPLY,
        "amount_minor": 5_000,
        "currency": "PEN",
        "link_id": str(env["link"]),
    }
    body.update(overrides)
    return client.post("/api/v1/services/payments", json=body, headers={"Idempotency-Key": key})


def _post_topup(client, env, key="api-topup-1", **overrides):
    body = {
        "operator": SOL_NAME,
        "phone": PHONE,
        "amount_minor": 3_000,
        "currency": "PEN",
        "link_id": str(env["link"]),
    }
    body.update(overrides)
    return client.post("/api/v1/topups", json=body, headers={"Idempotency-Key": key})


def test_api_pay_service_happy_path_returns_receipt(api_client, env):
    resp = _post_payment(api_client, env)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    data = body["data"]
    assert set(data) == {
        "payment_id",
        "transaction_id",
        "status",
        "amount_minor",
        "currency",
        "receipt",
        "failure_code",
        "authorization",
    }
    assert data["status"] == "SETTLED"
    assert data["amount_minor"] == 5_000
    assert data["failure_code"] is None
    assert data["authorization"] is None
    receipt = data["receipt"]
    assert receipt["receipt_no"].startswith("OP-")
    assert receipt["concept"] == "Luz del Hogar Demo"
    assert SUPPLY not in json.dumps(receipt)
    assert body["meta"]["replayed"] is False
    assert "request_id" in body["meta"]


def test_api_pay_service_replay_returns_same_payment(api_client, env):
    first = _post_payment(api_client, env, key="api-pay-replay-1")
    assert first.status_code == 201, first.text
    second = _post_payment(api_client, env, key="api-pay-replay-1")
    assert second.status_code == 201, second.text
    assert second.json()["data"]["payment_id"] == first.json()["data"]["payment_id"]
    assert second.json()["meta"]["replayed"] is True
    session = env["session"]
    assert _tx_count(session) == 1
    assert _payment_count(session) == 1


def test_api_pay_service_conflict_on_different_body(api_client, env):
    first = _post_payment(api_client, env, key="api-pay-conflict-1")
    assert first.status_code == 201, first.text
    second = _post_payment(api_client, env, key="api-pay-conflict-1", amount_minor=5_001)
    assert second.status_code == 409, second.text


def test_api_pay_service_requires_key(api_client, env):
    resp = api_client.post(
        "/api/v1/services/payments",
        json={
            "biller_id": LUZ_ID,
            "supply_ref": SUPPLY,
            "amount_minor": 5_000,
            "currency": "PEN",
            "link_id": str(env["link"]),
        },
    )
    assert resp.status_code == 422, resp.text


def test_api_pay_service_unknown_biller_is_404(api_client, env):
    resp = _post_payment(api_client, env, key="api-pay-404-1", biller_id=str(uuid.uuid4()))
    assert resp.status_code == 404, resp.text
    assert SUPPLY not in resp.text


def test_api_pay_service_invalid_supply_is_422(api_client, env):
    resp = _post_payment(api_client, env, key="api-pay-422-1", supply_ref="!!")
    assert resp.status_code == 422, resp.text


def test_api_pay_service_foreign_link_is_404(api_client, env):
    resp = _post_payment(api_client, env, key="api-pay-404-2", link_id=str(env["other_link"]))
    assert resp.status_code == 404, resp.text


def test_api_requires_auth(api_client, env):
    app.dependency_overrides.pop(get_current_user_id, None)
    try:
        resp = api_client.post(
            "/api/v1/services/payments",
            json={
                "biller_id": LUZ_ID,
                "supply_ref": SUPPLY,
                "amount_minor": 5_000,
                "currency": "PEN",
                "link_id": str(env["link"]),
            },
            headers={"Authorization": "Bearer malo", "Idempotency-Key": "api-pay-401-1"},
        )
        assert resp.status_code == 401, resp.text
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: USER


def test_api_topup_happy_path_returns_receipt(api_client, env):
    resp = _post_topup(api_client, env)
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "SETTLED"
    assert data["receipt"]["receipt_no"].startswith("OP-")
    assert data["receipt"]["concept"] == f"Recarga {SOL_NAME}"
    assert PHONE not in json.dumps(data["receipt"])
    assert resp.json()["meta"]["replayed"] is False


def test_api_topup_pending_then_authorize_finalizes_by_replay(api_client, env):
    amount = EXPRESS_BASE + 1
    first = _post_topup(api_client, env, key="api-topup-final-1", amount_minor=amount)
    assert first.status_code == 201, first.text
    data = first.json()["data"]
    assert data["status"] == "PENDING_AUTHORIZATION"
    assert data["receipt"] is None
    assert data["authorization"]["method"] == "BIOMETRIC"
    assert data["authorization"]["nonce"]
    assert first.json()["meta"]["replayed"] is False

    session = env["session"]
    settled = biometric_service.authorize_transaction_with_pin(
        session,
        data["transaction_id"],
        pin="1234",
        pin_verifier=lambda _s, _u, _p: _p == "1234",
        daily_limit_reader=wallet_service.wallet_daily_limit_reader,
    )
    session.commit()
    assert settled.status == "SETTLED"

    second = _post_topup(api_client, env, key="api-topup-final-1", amount_minor=amount)
    assert second.status_code == 201, second.text
    again = second.json()["data"]
    assert again["status"] == "SETTLED"
    assert again["topup_id"] == data["topup_id"]
    assert again["receipt"]["receipt_no"].startswith("OP-")
    assert second.json()["meta"]["replayed"] is True


def test_api_topup_over_daily_rejected_without_moving_funds(api_client, env):
    session = env["session"]
    before = _balances(session, env["payer"])
    resp = _post_topup(api_client, env, key="api-topup-daily-1", amount_minor=DAILY + 1)
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "REJECTED"
    assert data["failure_code"] == "DAILY_LIMIT_EXCEEDED"
    assert data["receipt"] is None
    assert _balances(session, env["payer"]) == before


def test_api_topup_unknown_operator_is_404(api_client, env):
    resp = _post_topup(api_client, env, key="api-topup-404-1", operator="Fantasma")
    assert resp.status_code == 404, resp.text


def test_api_topup_requires_key(api_client, env):
    resp = api_client.post(
        "/api/v1/topups",
        json={
            "operator": SOL_NAME,
            "phone": PHONE,
            "amount_minor": 3_000,
            "currency": "PEN",
            "link_id": str(env["link"]),
        },
    )
    assert resp.status_code == 422, resp.text


def test_api_topup_without_settlement_is_503_positive_code(api_client, env, monkeypatch):
    """Sin cuenta de liquidacion la recarga falla fail-closed con un codigo
    propio (`503 SETTLEMENT_UNAVAILABLE`, no `422 VALIDATION_ERROR`): el
    frontend muestra un mensaje honesto, no "algun campo es invalido". El
    mensaje no expone detalles internos."""
    monkeypatch.delenv(wallet_service.SETTLEMENT_ACCOUNT_ENV_VAR, raising=False)
    resp = _post_topup(
        api_client,
        env,
        key="hu16-bugfix-api-1",
        operator=LUNA_NAME,
        phone=LUNA_PHONE,
        amount_minor=2_000,
    )
    assert resp.status_code == 503, resp.text
    error = resp.json()["error"]
    assert error["code"] == "SETTLEMENT_UNAVAILABLE"
    assert "liquidacion" not in error["message"].lower()
    session = env["session"]
    assert _tx_count(session) == 0
    assert _topup_count(session) == 0


def test_api_topup_replay_and_conflict(api_client, env):
    first = _post_topup(api_client, env, key="api-topup-replay-1")
    assert first.status_code == 201, first.text
    second = _post_topup(api_client, env, key="api-topup-replay-1")
    assert second.json()["meta"]["replayed"] is True
    third = _post_topup(api_client, env, key="api-topup-replay-1", amount_minor=3_001)
    assert third.status_code == 409, third.text


def test_openapi_lists_service_payment_routes():
    spec = app.openapi()
    assert "/api/v1/services/payments" in spec["paths"]
    assert "/api/v1/topups" in spec["paths"]
    assert "post" in spec["paths"]["/api/v1/services/payments"]
    assert "post" in spec["paths"]["/api/v1/topups"]


# ------------------------------------------------------- migracion 0042


def test_migration_chain_has_single_head_0042():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from tests.db_utils import BACKEND_DIR

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.config_file_name = None
    script = ScriptDirectory.from_config(cfg)
    revisions = {rev.revision: rev.down_revision for rev in script.walk_revisions()}
    assert revisions.get("0042_wallet_services") == "0041_wallet_qr_payment_guard"
    assert revisions.get("0046_services_settlement") == "0045_notification_email_content"
    assert revisions.get("0045_notification_email_content") == "0044_notifications_preferences"
    # E4-T21 (2026-10-15): la cabeza avanza a 0047_wallet_personal_qr
    # (QR personal estatico, tras 0046).
    assert revisions.get("0047_wallet_personal_qr") == "0046_services_settlement"
    heads = [rev for rev in script.get_heads() if rev not in ()]
    assert heads == ["0047_wallet_personal_qr"], f"heads inesperados: {heads}"


def test_migration_0046_seeds_demo_settlement_account():
    """La migracion `0046_services_settlement` siembra la MISMA cuenta
    determinista que usa la env/factories (PEN, `accounts.accounts` +
    `accounts.account_balances`), idempotente y reversible."""
    from tests.db_utils import BACKEND_DIR
    from tests.factories import SETTLEMENT_ACCOUNT_ID

    content = (BACKEND_DIR / "migrations" / "versions" / "0046_services_settlement.py").read_text(
        encoding="utf-8"
    )
    assert len("0046_services_settlement") <= 32  # limite de alembic_version
    assert f'SETTLEMENT_ACCOUNT_ID = "{SETTLEMENT_ACCOUNT_ID}"' in content
    assert "accounts.accounts" in content
    assert "accounts.account_balances" in content
    assert "def upgrade" in content and "def downgrade" in content
