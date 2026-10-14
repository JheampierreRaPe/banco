"""Pago express de billetera con regla de limite base (E4-T02, HU13).

- Parte A (sin BD): regla pura `decide_express` (feliz, base exacto,
  refuerzo, diario, entradas invalidas, sin `float`).
- Parte B (SQLite en memoria + schemas ATTACH, motor real): feliz bajo
  el limite (una `Transaction`, disponible baja, asiento cuadrado,
  eventos del motor, auditoria), base exacto sin reto, refuerzo sobre
  el base (`PENDING_AUTHORIZATION` + PIN -> `SETTLED` con hold
  reutilizado; PIN malo no liquida), diario (`REJECTED` +
  `DAILY_LIMIT_EXCEEDED` sin tocar saldos), insuficiente (`REJECTED`
  sin tocar saldos), idempotencia (replay unico + `CONFLICT`),
  vinculo tarjeta/ajeno/inexistente (sin `Transaction`), parametros
  (`set_parameter` cambia la decision sin redespliegue + fallback),
  sin PII en logs ni en `outbox`.
- Parte C (migraciones): `0038_wallet_express_params` encadena tras
  `0037_wallet` y (con `0039_wallet_qr` de `E4-T06`) deja un solo head.

Sin endpoint publico (decision G66: servicio interno para HU15/HU16);
sin `float`; sin red.
"""

from __future__ import annotations

import logging
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.modules.accounts.models as _accounts
import app.modules.audit.models as _audit
import app.modules.ledger.models as _ledger
import app.modules.shared.models as _shared
import app.modules.transactions.models as _transactions
import app.modules.wallet.models as _wallet
from app.core.db import Base
from app.modules.shared.config import parameters as params
from app.modules.wallet import service as wallet_service
from app.modules.wallet.domain import limits as express_limits
from app.modules.wallet.service import express as express_service

assert _accounts is not None and _audit is not None and _ledger is not None
assert _shared is not None and _transactions is not None and _wallet is not None

USER = uuid.uuid4()
MERCHANT_USER = uuid.uuid4()
OTHER = uuid.uuid4()

PAYER_NUMBER = "00110001111111"
PAYER_LOW_NUMBER = "00110002222222"
PAYEE_NUMBER = "00220003333333"

FUNDED = 100_000
FUNDED_LOW = 3_000

VISA_PAN = "4111111111111111"

EXPRESS_BASE = 20_000
DAILY = 500_000

TABLES = [
    "wallet.wallets",
    "wallet.wallet_links",
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

SCHEMAS = ("wallet", "accounts", "ledger", "transactions", "shared", "audit", "config")


# ------------------------------------------------------- Parte A: regla pura


def test_decide_express_below_base():
    assert (
        express_limits.decide_express(
            5_000, 0, express_limit_minor=EXPRESS_BASE, daily_limit_minor=DAILY
        )
        == express_limits.EXPRESS
    )


def test_decide_express_exact_base_is_express():
    assert (
        express_limits.decide_express(
            EXPRESS_BASE, 0, express_limit_minor=EXPRESS_BASE, daily_limit_minor=DAILY
        )
        == express_limits.EXPRESS
    )


def test_decide_express_over_base_requires_reinforced():
    assert (
        express_limits.decide_express(
            EXPRESS_BASE + 1, 0, express_limit_minor=EXPRESS_BASE, daily_limit_minor=DAILY
        )
        == express_limits.REQUIRES_REINFORCED
    )


def test_decide_daily_exceeded_wins_over_base():
    assert (
        express_limits.decide_express(
            1_000,
            DAILY,
            express_limit_minor=EXPRESS_BASE,
            daily_limit_minor=DAILY,
        )
        == express_limits.LIMIT_EXCEEDED
    )
    # Igual al limite aun cabe (el motor rechaza solo con `>`).
    assert (
        express_limits.decide_express(
            1_000,
            DAILY - 1_000,
            express_limit_minor=EXPRESS_BASE,
            daily_limit_minor=DAILY,
        )
        == express_limits.EXPRESS
    )


def test_decide_express_rejects_non_int():
    for bad in (True, 5.0, "5000", None):
        with pytest.raises((TypeError, ValueError)):
            express_limits.decide_express(
                bad, 0, express_limit_minor=EXPRESS_BASE, daily_limit_minor=DAILY
            )
    with pytest.raises((TypeError, ValueError)):
        express_limits.decide_express(
            0, 0, express_limit_minor=EXPRESS_BASE, daily_limit_minor=DAILY
        )
    with pytest.raises((TypeError, ValueError)):
        express_limits.decide_express(
            1_000, -1, express_limit_minor=EXPRESS_BASE, daily_limit_minor=DAILY
        )


def test_decide_express_deterministic():
    kwargs = {"express_limit_minor": EXPRESS_BASE, "daily_limit_minor": DAILY}
    assert express_limits.decide_express(7_500, 1_000, **kwargs) == express_limits.decide_express(
        7_500, 1_000, **kwargs
    )


# ------------------------------------------------------- Parte B: servicio


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
    """Motor real en SQLite: cuentas fondeadas + vinculo + parametros."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from app.modules.accounts import repository as accounts_repo

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
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
        payee = accounts_repo.create_account(
            session,
            user_id=MERCHANT_USER,
            account_number=PAYEE_NUMBER,
            type="AHORRO",
            currency="PEN",
        )
        link = wallet_service.link_source(session, USER, type="ACCOUNT", account_id=payer.id)
        low_link = wallet_service.link_source(
            session, USER, type="ACCOUNT", account_id=payer_low.id
        )
        session.commit()
        yield {
            "session": session,
            "payer": payer.id,
            "payer_low": payer_low.id,
            "payee": payee.id,
            "link": link["id"],
            "low_link": low_link["id"],
        }
        session.rollback()
    finally:
        session.close()
        engine.dispose()


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
    return True


def test_express_happy_path_settles_without_challenge(env):
    session = env["session"]
    tx = express_service.pay_express(
        session,
        initiator_user_id=USER,
        link_id=env["link"],
        target_account_id=env["payee"],
        amount_minor=5_000,
        currency="PEN",
        idempotency_key="exp-happy-1",
    )
    session.commit()
    assert tx.status == "SETTLED"
    assert tx.type == "THIRD_PARTY_TRANSFER"
    assert (tx.meta or {}).get("wallet_link_id") == str(env["link"])
    assert "biometric" not in (tx.meta or {})
    balances = _balances(session, env["payer"], env["payee"])
    assert balances[str(env["payer"])] == (FUNDED - 5_000, 0)
    assert balances[str(env["payee"])] == (5_000, 0)
    assert _tx_count(session) == 1
    assert _balanced_postings(session, tx.id)


def test_express_exact_base_limit_settles_without_challenge(env):
    session = env["session"]
    tx = express_service.pay_express(
        session,
        initiator_user_id=USER,
        link_id=env["link"],
        target_account_id=env["payee"],
        amount_minor=EXPRESS_BASE,
        currency="PEN",
        idempotency_key="exp-exact-1",
    )
    session.commit()
    assert tx.status == "SETTLED"
    assert "biometric" not in (tx.meta or {})
    balances = _balances(session, env["payer"], env["payee"])
    assert balances[str(env["payer"])] == (FUNDED - EXPRESS_BASE, 0)


def test_express_over_base_requires_pin_and_settles_with_single_hold(env):
    from app.modules.transactions.service import biometric as biometric_service

    session = env["session"]
    amount = EXPRESS_BASE + 1
    tx = express_service.pay_express(
        session,
        initiator_user_id=USER,
        link_id=env["link"],
        target_account_id=env["payee"],
        amount_minor=amount,
        currency="PEN",
        idempotency_key="exp-reinforced-1",
    )
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    block = biometric_service.get_block(tx)
    assert isinstance(block, dict) and block.get("nonce")

    mid = _balances(session, env["payer"], env["payee"])
    assert mid[str(env["payer"])][0] == FUNDED - amount
    pending_holds = session.scalars(
        sa.select(_transactions.Hold).where(_transactions.Hold.transaction_id == tx.id)
    ).all()
    assert len(pending_holds) == 1
    assert pending_holds[0].status == "ACTIVE"
    assert int(pending_holds[0].amount_minor) == amount

    with pytest.raises(biometric_service.NotAuthorizedError):
        biometric_service.authorize_transaction_with_pin(
            session,
            tx.id,
            pin="0000",
            pin_verifier=lambda _s, _u, _p: _p == "1234",
            daily_limit_reader=express_service.wallet_daily_limit_reader,
        )
    session.rollback()
    pending = session.get(_transactions.Transaction, tx.id)
    assert pending.status == "PENDING_AUTHORIZATION"

    settled = biometric_service.authorize_transaction_with_pin(
        session,
        tx.id,
        pin="1234",
        pin_verifier=lambda _s, _u, _p: _p == "1234",
        daily_limit_reader=express_service.wallet_daily_limit_reader,
    )
    session.commit()
    assert settled.status == "SETTLED"
    final = _balances(session, env["payer"], env["payee"])
    assert final[str(env["payer"])] == (FUNDED - amount, 0)
    assert final[str(env["payee"])] == (amount, 0)
    holds = session.scalars(
        sa.select(_transactions.Hold).where(_transactions.Hold.transaction_id == tx.id)
    ).all()
    assert len(holds) == 1 and holds[0].status == "CAPTURED"
    entries = session.scalars(
        sa.select(_ledger.JournalEntry).where(_ledger.JournalEntry.transaction_id == tx.id)
    ).all()
    assert len(entries) == 2  # hold + liquidacion, sin duplicar
    assert _balanced_postings(session, tx.id)


def test_express_daily_limit_rejected_without_moving_funds(env):
    session = env["session"]
    params.set_parameter(session, key=params.WALLET_DAILY_LIMIT_MINOR_KEY, value=10_000)
    session.flush()
    first = express_service.pay_express(
        session,
        initiator_user_id=USER,
        link_id=env["link"],
        target_account_id=env["payee"],
        amount_minor=5_000,
        currency="PEN",
        idempotency_key="exp-daily-1",
    )
    assert first.status == "SETTLED"
    entries_after_first = _entry_count(session)
    second = express_service.pay_express(
        session,
        initiator_user_id=USER,
        link_id=env["link"],
        target_account_id=env["payee"],
        amount_minor=6_000,
        currency="PEN",
        idempotency_key="exp-daily-2",
    )
    session.commit()
    assert second.status == "REJECTED"
    assert second.failure_reason == "DAILY_LIMIT_EXCEEDED"
    assert (second.meta or {}).get("failure_code") == "DAILY_LIMIT_EXCEEDED"
    balances = _balances(session, env["payer"], env["payee"])
    assert balances[str(env["payer"])] == (FUNDED - 5_000, 0)
    assert balances[str(env["payee"])] == (5_000, 0)
    assert _entry_count(session) == entries_after_first


def test_express_insufficient_funds_rejected_without_moving_funds(env):
    session = env["session"]
    entries_before = _entry_count(session)
    tx = express_service.pay_express(
        session,
        initiator_user_id=USER,
        link_id=env["low_link"],
        target_account_id=env["payee"],
        amount_minor=5_000,
        currency="PEN",
        idempotency_key="exp-nofunds-1",
    )
    session.commit()
    assert tx.status == "REJECTED"
    balances = _balances(session, env["payer_low"], env["payee"])
    assert balances[str(env["payer_low"])] == (FUNDED_LOW, 0)
    assert balances[str(env["payee"])] == (0, 0)
    assert _entry_count(session) == entries_before


def test_express_idempotent_replay_moves_once_and_conflicts_on_other_body(env):
    session = env["session"]
    first = express_service.pay_express(
        session,
        initiator_user_id=USER,
        link_id=env["link"],
        target_account_id=env["payee"],
        amount_minor=5_000,
        currency="PEN",
        idempotency_key="exp-idem-1",
    )
    session.commit()
    again = express_service.pay_express(
        session,
        initiator_user_id=USER,
        link_id=env["link"],
        target_account_id=env["payee"],
        amount_minor=5_000,
        currency="PEN",
        idempotency_key="exp-idem-1",
    )
    session.commit()
    assert again.id == first.id
    assert _tx_count(session) == 1
    balances = _balances(session, env["payer"], env["payee"])
    assert balances[str(env["payer"])] == (FUNDED - 5_000, 0)
    with pytest.raises(express_service.ExpressConflictError):
        express_service.pay_express(
            session,
            initiator_user_id=USER,
            link_id=env["link"],
            target_account_id=env["payee"],
            amount_minor=7_000,
            currency="PEN",
            idempotency_key="exp-idem-1",
        )
    session.rollback()
    assert _tx_count(session) == 1
    balances = _balances(session, env["payer"], env["payee"])
    assert balances[str(env["payer"])] == (FUNDED - 5_000, 0)


def test_express_rejects_card_link_without_creating_transaction(env):
    session = env["session"]
    card = wallet_service.link_source(session, USER, type="CARD", card={"pan": VISA_PAN})
    session.flush()
    before = _tx_count(session)
    with pytest.raises(express_service.ExpressValidationError):
        express_service.pay_express(
            session,
            initiator_user_id=USER,
            link_id=card["id"],
            target_account_id=env["payee"],
            amount_minor=1_000,
            currency="PEN",
            idempotency_key="exp-card-1",
        )
    session.rollback()
    assert _tx_count(session) == before


def test_express_rejects_foreign_or_missing_link_neutrally(env):
    session = env["session"]
    stranger_link = wallet_service.link_source(session, OTHER, type="CARD", card={"pan": VISA_PAN})
    session.flush()
    before = _tx_count(session)
    with pytest.raises(express_service.ExpressNotFoundError):
        express_service.pay_express(
            session,
            initiator_user_id=USER,
            link_id=stranger_link["id"],
            target_account_id=env["payee"],
            amount_minor=1_000,
            currency="PEN",
            idempotency_key="exp-foreign-1",
        )
    with pytest.raises(express_service.ExpressNotFoundError):
        express_service.pay_express(
            session,
            initiator_user_id=USER,
            link_id=uuid.uuid4(),
            target_account_id=env["payee"],
            amount_minor=1_000,
            currency="PEN",
            idempotency_key="exp-missing-1",
        )
    session.rollback()
    assert _tx_count(session) == before


def test_express_rejects_invalid_input_without_creating_transaction(env):
    session = env["session"]
    before = _tx_count(session)
    with pytest.raises(express_service.ExpressValidationError):
        express_service.pay_express(
            session,
            initiator_user_id=USER,
            link_id=env["link"],
            target_account_id=env["payee"],
            amount_minor=0,
            currency="PEN",
            idempotency_key="exp-bad-1",
        )
    with pytest.raises(express_service.ExpressValidationError):
        express_service.pay_express(
            session,
            initiator_user_id=USER,
            link_id=env["link"],
            target_account_id=env["payee"],
            amount_minor=1_000,
            currency="pen",
            idempotency_key="exp-bad-2",
        )
    with pytest.raises(express_service.ExpressValidationError):
        express_service.pay_express(
            session,
            initiator_user_id=USER,
            link_id=env["link"],
            target_account_id=env["payee"],
            amount_minor=1_000,
            currency="PEN",
            idempotency_key="",
        )
    session.rollback()
    assert _tx_count(session) == before


def test_express_parameters_change_decision_without_redeploy(env):
    session = env["session"]
    assert params.get_wallet_express_limit_minor(session) == EXPRESS_BASE
    assert params.get_wallet_daily_limit_minor(session) == DAILY
    params.set_parameter(session, key=params.WALLET_EXPRESS_LIMIT_MINOR_KEY, value=1_000)
    session.flush()
    assert params.get_wallet_express_limit_minor(session) == 1_000
    tx = express_service.pay_express(
        session,
        initiator_user_id=USER,
        link_id=env["link"],
        target_account_id=env["payee"],
        amount_minor=5_000,
        currency="PEN",
        idempotency_key="exp-param-1",
    )
    assert tx.status == "PENDING_AUTHORIZATION"
    session.rollback()
    with pytest.raises(params.ParameterValidationError):
        params.set_parameter(session, key=params.WALLET_EXPRESS_LIMIT_MINOR_KEY, value=1.5)
    session.rollback()
    session.execute(sa.text("DELETE FROM config.parameters"))
    session.flush()
    assert (
        params.get_wallet_express_limit_minor(session) == params.DEFAULT_WALLET_EXPRESS_LIMIT_MINOR
    )
    assert params.get_wallet_daily_limit_minor(session) == params.DEFAULT_WALLET_DAILY_LIMIT_MINOR


def test_express_motor_events_and_audit_without_pii(env):
    from app.modules.shared.models import OutboxEntry

    session = env["session"]
    tx = express_service.pay_express(
        session,
        initiator_user_id=USER,
        link_id=env["link"],
        target_account_id=env["payee"],
        amount_minor=5_000,
        currency="PEN",
        idempotency_key="exp-events-1",
    )
    session.commit()
    kinds = {
        row.event_type
        for row in session.scalars(
            sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx.id)
        ).all()
    }
    assert "transaction.initiated" in kinds
    assert "transfer.settled" in kinds
    audit_rows = session.execute(
        sa.text("SELECT action FROM audit.audit_log WHERE action = :action"),
        {"action": express_service.AUDIT_ACTION},
    ).all()
    assert len(audit_rows) == 1
    payloads = [
        row.payload
        for row in session.scalars(
            sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx.id)
        ).all()
    ]
    blob = str(payloads)
    for secret in (PAYER_NUMBER, PAYEE_NUMBER, VISA_PAN):
        assert secret not in blob


def test_express_logs_without_pii(env, caplog):
    session = env["session"]
    with caplog.at_level(logging.INFO, logger="app.modules.wallet.service.express"):
        tx = express_service.pay_express(
            session,
            initiator_user_id=USER,
            link_id=env["link"],
            target_account_id=env["payee"],
            amount_minor=5_000,
            currency="PEN",
            idempotency_key="exp-logs-1",
        )
        assert tx.status == "SETTLED"
    session.rollback()
    text = caplog.text
    assert "wallet express" in text
    for secret in (PAYER_NUMBER, PAYEE_NUMBER, VISA_PAN):
        assert secret not in text


# ------------------------------------------------------- Parte C: migraciones


def test_migration_0038_chained_after_0037_with_single_head():
    import pathlib
    import re

    versions_dir = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"
    revisions: dict[str, str | None] = {}
    for path in versions_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision\s*=\s*"([^"]+)"', text, re.MULTILINE)
        down = re.search(r'^down_revision\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if rev is None:
            continue
        assert len(rev.group(1)) <= 32, f"revision > 32 chars: {rev.group(1)}"
        revisions[rev.group(1)] = down.group(1) if down else None
    # E4-T11 (2026-10-13): la cabeza avanza a 0040_wallet_qr_payments
    # (cobro QR; un solo head encadenado tras 0039). Fix guarda
    # (2026-10-13): la cabeza avanza a 0041_wallet_qr_payment_guard
    # (indice unico parcial anti doble cobro, tras 0040). E4-T16
    # (2026-10-13): la cabeza avanza a 0042_wallet_services
    # (catalogo `billers` + servicios, tras 0041). E6-T12
    # (2026-10-13): la cabeza avanza a 0044_notifications_preferences
    # (preferencias por canal + claves de seguridad, tras 0043).
    assert revisions.get("0042_wallet_services") == "0041_wallet_qr_payment_guard"
    assert revisions.get("0041_wallet_qr_payment_guard") == "0040_wallet_qr_payments"
    assert revisions.get("0040_wallet_qr_payments") == "0039_wallet_qr"
    assert revisions.get("0039_wallet_qr") == "0038_wallet_express_params"
    assert revisions.get("0038_wallet_express_params") == "0037_wallet"
    assert revisions.get("0037_wallet") == "0036_loan_reminder_param"
    referenced = {down for down in revisions.values() if down is not None}
    heads = [rev for rev in revisions if rev not in referenced]
    assert heads == ["0044_notifications_preferences"], f"heads inesperados: {heads}"
