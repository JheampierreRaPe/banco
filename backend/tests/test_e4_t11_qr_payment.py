"""Cobro QR: validacion de integridad y pago por el motor (E4-T11, HU15).

- Parte A (sin BD): dominio puro `wallet.domain.qr_payment` (decodifica
  `BO-QR1`, firma/hash/envelope, caducidad/estado, errores tipados
  mapeables, sin `float`, sin PII).
- Parte B (SQLite en memoria + schemas ATTACH, motor real): feliz
  (`PAID` + cargo `PAID`/`paid_at` + `qr.payment.confirmed` + aviso
  `qr_charge_paid` + asiento cuadrado `docs/04#7.4`); vencido/corrupto/
  manipulado -> rechazo sin mover dinero; ya pagado -> `CONFLICT`;
  idempotencia (replay unico + `CONFLICT`); insuficiente/`REJECTED`;
  sobre el base -> `PENDING_AUTHORIZATION` + `finalize_qr_payment`;
  sobre el diario -> `REJECTED`; comercio sin cuenta; cuadre y
  aislamiento de frontera (`wallet` no escribe tablas ajenas); sin PII.
- Parte C (migraciones): `0040_wallet_qr_payments` encadena tras
  `0039_wallet_qr` y deja un solo head.

Sin endpoint publico (el router es `E4-T12`); sin `float`; sin red.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

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
from app.adapters import wallet_interop as interop
from app.core.db import Base
from app.modules.shared.config import parameters as params
from app.modules.wallet import service as wallet_service
from app.modules.wallet.domain import qr as qr_domain
from app.modules.wallet.domain import qr_payment as qr_payment_domain
from app.modules.wallet.repository import qr as qr_repo
from app.modules.wallet.service import qr as qr_service
from app.modules.wallet.service import qr_payment as qr_payment_service

assert _accounts is not None and _audit is not None and _ledger is not None
assert _notifications is not None and _shared is not None
assert _transactions is not None and _wallet is not None

SECRET = "test-qr-payment-secret"
NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)

USER = uuid.uuid4()
OTHER = uuid.uuid4()

PAYER_NUMBER = "00110001111111"
PAYER_LOW_NUMBER = "00110002222222"
MERCHANT_NUMBER = "00220003333333"

FUNDED = 100_000
FUNDED_LOW = 3_000

EXPRESS_BASE = 20_000
DAILY = 500_000

TABLES = [
    "wallet.wallets",
    "wallet.wallet_links",
    "wallet.merchant_accounts",
    "wallet.qr_charges",
    "wallet.qr_payments",
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
    "notifications.notifications",
    "notifications.notification_templates",
]

SCHEMAS = (
    "wallet",
    "accounts",
    "ledger",
    "transactions",
    "shared",
    "audit",
    "config",
    "notifications",
)


# ------------------------------------------------------- Parte A: dominio puro


def _charge_row(**overrides):
    base = {
        "id": uuid.uuid4(),
        "merchant_id": uuid.uuid4(),
        "amount_minor": 5_000,
        "currency": "PEN",
        "concept": "Menu",
        "mode": "DYNAMIC",
        "expires_at": NOW + timedelta(seconds=300),
        "status": "ACTIVE",
    }
    base.update(overrides)
    payload = qr_domain.build_payload(
        charge_id=base["id"],
        merchant_id=base["merchant_id"],
        amount_minor=base["amount_minor"],
        currency=base["currency"],
        concept=base["concept"],
        mode=base["mode"],
        expires_at=base["expires_at"],
    )
    base["payload_hash"] = qr_domain.payload_hash_for(payload)
    base["signature"] = qr_domain.sign_payload(payload, secret=SECRET)
    base["payload"] = payload
    base["qr_content"] = qr_domain.to_interop_token(payload, base["signature"])
    return base


def test_decode_qr_content_roundtrip_under_2s():
    row = _charge_row()
    started = time.perf_counter()
    envelope = qr_payment_domain.decode_qr_content(row["qr_content"])
    payload = qr_payment_domain.verify_charge_integrity(row, envelope, secret=SECRET)
    qr_payment_domain.ensure_charge_payable(row, NOW)
    elapsed = time.perf_counter() - started
    assert envelope.ref == str(row["id"])
    assert payload["amount_minor"] == 5_000
    assert elapsed < 2.0


def test_decode_rejects_corrupt_qr():
    for bad in ("", "  ", "no-es-un-qr", "!!!", None, 12345):
        with pytest.raises(qr_payment_domain.InvalidQrError):
            qr_payment_domain.decode_qr_content(bad)
    assert qr_payment_domain.InvalidQrError.code == "VALIDATION_ERROR"


def test_integrity_rejects_tampered_envelope():
    row = _charge_row()
    envelope = qr_payment_domain.decode_qr_content(row["qr_content"])
    tampered = interop.encode_payload(
        issuer=envelope.issuer,
        peer=envelope.peer,
        ref=envelope.ref,
        amount_minor=envelope.amount_minor + 1,
        currency=envelope.currency,
        expires_at=envelope.expires_at,
        sig=envelope.sig,
    )
    with pytest.raises(qr_payment_domain.InvalidQrError):
        qr_payment_domain.verify_charge_integrity(
            row, qr_payment_domain.decode_qr_content(tampered), secret=SECRET
        )


def test_integrity_rejects_tampered_signature():
    row = _charge_row()
    envelope = qr_payment_domain.decode_qr_content(row["qr_content"])
    forged = interop.encode_payload(
        issuer=envelope.issuer,
        peer=envelope.peer,
        ref=envelope.ref,
        amount_minor=envelope.amount_minor,
        currency=envelope.currency,
        expires_at=envelope.expires_at,
        sig="0" * 64,
    )
    with pytest.raises(qr_payment_domain.InvalidQrError):
        qr_payment_domain.verify_charge_integrity(
            row, qr_payment_domain.decode_qr_content(forged), secret=SECRET
        )


def test_integrity_rejects_foreign_charge():
    row = _charge_row()
    other = _charge_row()
    envelope = qr_payment_domain.decode_qr_content(other["qr_content"])
    with pytest.raises(qr_payment_domain.InvalidQrError):
        qr_payment_domain.verify_charge_integrity(row, envelope, secret=SECRET)


def test_payable_states():
    with pytest.raises(qr_payment_domain.QrAlreadyPaidError):
        qr_payment_domain.ensure_charge_payable(_charge_row(status="PAID"), NOW)
    assert qr_payment_domain.QrAlreadyPaidError.code == "CONFLICT"
    with pytest.raises(qr_payment_domain.QrExpiredError):
        qr_payment_domain.ensure_charge_payable(_charge_row(status="EXPIRED"), NOW)
    with pytest.raises(qr_payment_domain.QrExpiredError):
        qr_payment_domain.ensure_charge_payable(
            _charge_row(expires_at=NOW - timedelta(seconds=1)), NOW
        )
    for status in ("CANCELLED", "BLOQUEADO", None):
        with pytest.raises(qr_payment_domain.InvalidQrError):
            qr_payment_domain.ensure_charge_payable(_charge_row(status=status), NOW)
    with pytest.raises(qr_payment_domain.InvalidQrError):
        qr_payment_domain.ensure_charge_payable(None, NOW)
    assert qr_payment_domain.QrExpiredError.code == "VALIDATION_ERROR"
    assert qr_payment_domain.QrMerchantUnknownError.code == "VALIDATION_ERROR"
    assert qr_payment_domain.QrPaymentConflictError.code == "CONFLICT"
    assert qr_payment_domain.QrPaymentNotFoundError.code == "NOT_FOUND"


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
    """Cobro QR con motor real en SQLite: pagador + comercio + vinculo."""
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
        _seed_param(session, params.WALLET_QR_TTL_SECONDS_KEY, "300")
        merchants = qr_repo.ensure_demo_merchants(session)
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
        merchant_accounts = {}
        for merchant in merchants:
            acct = accounts_repo.create_account(
                session,
                user_id=merchant.user_id,
                account_number=MERCHANT_NUMBER if merchant == merchants[0] else "00220004444444",
                type="AHORRO",
                currency="PEN",
            )
            merchant_accounts[str(merchant.id)] = acct.id
        link = wallet_service.link_source(session, USER, type="ACCOUNT", account_id=payer.id)
        low_link = wallet_service.link_source(
            session, USER, type="ACCOUNT", account_id=payer_low.id
        )
        session.commit()
        yield {
            "session": session,
            "merchants": merchants,
            "merchant_accounts": merchant_accounts,
            "payer": payer.id,
            "payer_low": payer_low.id,
            "link": link["id"],
            "low_link": low_link["id"],
        }
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def _make_charge(session, merchant, amount_minor=5_000, **kwargs):
    return qr_service.create_qr_charge(
        session,
        merchant_id=merchant.id,
        amount_minor=amount_minor,
        currency="PEN",
        now=NOW,
        secret=SECRET,
        **kwargs,
    )


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
    return int(session.execute(sa.text("SELECT COUNT(*) FROM wallet.qr_payments")).scalar())


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


def _confirmed_events(session: Session, payment_id: uuid.UUID) -> list:
    from app.modules.shared.models import OutboxEntry

    return session.scalars(
        sa.select(OutboxEntry).where(
            OutboxEntry.event_type == "qr.payment.confirmed",
            OutboxEntry.aggregate_id == payment_id,
        )
    ).all()


def _merchant_notices(session: Session) -> list:
    from app.modules.notifications.models import Notification

    return session.scalars(
        sa.select(Notification).where(Notification.template_code == "qr_charge_paid")
    ).all()


def test_pay_qr_happy_path_settles_and_notifies(env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    payment, tx = qr_payment_service.pay_qr_charge(
        session,
        payer_user_id=USER,
        qr_content=view["qr_content"],
        link_id=env["link"],
        idempotency_key="qr-pay-happy-1",
        secret=SECRET,
        now=NOW + timedelta(seconds=10),
    )
    session.commit()
    assert payment.status == "PAID"
    assert tx.status == "SETTLED"
    assert str(payment.charge_id) == view["charge_id"]
    assert int(payment.amount_minor) == 5_000
    assert isinstance(payment.amount_minor, int)
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "PAID"
    assert charge.paid_at is not None
    events = _confirmed_events(session, payment.id)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["qr_payment_id"] == str(payment.id)
    assert payload["charge_id"] == view["charge_id"]
    assert payload["merchant_id"] == str(merchant.id)
    assert payload["amount_minor"] == 5_000
    assert payload["currency"] == "PEN"
    assert payload["status"] == "PAID"
    notices = _merchant_notices(session)
    assert len(notices) == 1
    assert _balanced_postings(session, tx.id)
    balances = _balances(session, env["payer"], env["merchant_accounts"][str(merchant.id)])
    assert balances[str(env["payer"])] == (FUNDED - 5_000, 0)
    assert balances[str(env["merchant_accounts"][str(merchant.id)])] == (5_000, 0)
    assert _tx_count(session) == 1
    assert _payment_count(session) == 1


def test_pay_qr_expired_rejected_without_moving_funds(env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    before_payer = _balances(session, env["payer"])
    before_entries = _entry_count(session)
    with pytest.raises(qr_payment_domain.QrExpiredError):
        qr_payment_service.pay_qr_charge(
            session,
            payer_user_id=USER,
            qr_content=view["qr_content"],
            link_id=env["link"],
            idempotency_key="qr-pay-expired-1",
            secret=SECRET,
            now=NOW + timedelta(seconds=301),
        )
    session.rollback()
    assert _tx_count(session) == 0
    assert _payment_count(session) == 0
    assert _entry_count(session) == before_entries
    assert _balances(session, env["payer"]) == before_payer
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "ACTIVE"


def test_pay_qr_corrupt_rejected_without_moving_funds(env):
    session = env["session"]
    before_entries = _entry_count(session)
    with pytest.raises(qr_payment_domain.InvalidQrError):
        qr_payment_service.pay_qr_charge(
            session,
            payer_user_id=USER,
            qr_content="esto-no-es-un-qr",
            link_id=env["link"],
            idempotency_key="qr-pay-corrupt-1",
            secret=SECRET,
            now=NOW,
        )
    session.rollback()
    assert _tx_count(session) == 0
    assert _payment_count(session) == 0
    assert _entry_count(session) == before_entries


def test_pay_qr_tampered_signature_rejected_without_moving_funds(env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    envelope = interop.decode_payload(view["qr_content"])
    forged = interop.encode_payload(
        issuer=envelope.issuer,
        peer=envelope.peer,
        ref=envelope.ref,
        amount_minor=envelope.amount_minor,
        currency=envelope.currency,
        expires_at=envelope.expires_at,
        sig="f" * 64,
    )
    before_entries = _entry_count(session)
    with pytest.raises(qr_payment_domain.InvalidQrError):
        qr_payment_service.pay_qr_charge(
            session,
            payer_user_id=USER,
            qr_content=forged,
            link_id=env["link"],
            idempotency_key="qr-pay-forged-1",
            secret=SECRET,
            now=NOW + timedelta(seconds=10),
        )
    session.rollback()
    assert _tx_count(session) == 0
    assert _payment_count(session) == 0
    assert _entry_count(session) == before_entries
    assert _balances(session, env["payer"])[str(env["payer"])] == (FUNDED, 0)


def test_pay_qr_unknown_charge_rejected_neutrally(env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    envelope = interop.decode_payload(view["qr_content"])
    foreign = interop.encode_payload(
        issuer=envelope.issuer,
        peer=envelope.peer,
        ref=str(uuid.uuid4()),
        amount_minor=envelope.amount_minor,
        currency=envelope.currency,
        expires_at=envelope.expires_at,
        sig=envelope.sig,
    )
    with pytest.raises(qr_payment_domain.InvalidQrError):
        qr_payment_service.pay_qr_charge(
            session,
            payer_user_id=USER,
            qr_content=foreign,
            link_id=env["link"],
            idempotency_key="qr-pay-foreign-1",
            secret=SECRET,
            now=NOW,
        )
    session.rollback()
    assert _tx_count(session) == 0


def test_pay_qr_double_scan_conflicts_with_single_entry(env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    first, first_tx = qr_payment_service.pay_qr_charge(
        session,
        payer_user_id=USER,
        qr_content=view["qr_content"],
        link_id=env["link"],
        idempotency_key="qr-pay-double-1",
        secret=SECRET,
        now=NOW + timedelta(seconds=5),
    )
    session.commit()
    assert first.status == "PAID"
    with pytest.raises(qr_payment_domain.QrAlreadyPaidError):
        qr_payment_service.pay_qr_charge(
            session,
            payer_user_id=USER,
            qr_content=view["qr_content"],
            link_id=env["link"],
            idempotency_key="qr-pay-double-2",
            secret=SECRET,
            now=NOW + timedelta(seconds=6),
        )
    session.rollback()
    assert _tx_count(session) == 1
    assert _payment_count(session) == 1
    assert _balanced_postings(session, first_tx.id)
    assert len(_confirmed_events(session, first.id)) == 1


def test_pay_qr_idempotency_replay_and_conflict(env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    first, first_tx = qr_payment_service.pay_qr_charge(
        session,
        payer_user_id=USER,
        qr_content=view["qr_content"],
        link_id=env["link"],
        idempotency_key="qr-pay-idem-1",
        secret=SECRET,
        now=NOW + timedelta(seconds=5),
    )
    session.commit()
    replay, replay_tx = qr_payment_service.pay_qr_charge(
        session,
        payer_user_id=USER,
        qr_content=view["qr_content"],
        link_id=env["link"],
        idempotency_key="qr-pay-idem-1",
        secret=SECRET,
        now=NOW + timedelta(seconds=6),
    )
    session.commit()
    assert str(replay.id) == str(first.id)
    assert str(replay_tx.id) == str(first_tx.id)
    assert _tx_count(session) == 1
    assert _payment_count(session) == 1
    with pytest.raises(qr_payment_domain.QrPaymentConflictError):
        qr_payment_service.pay_qr_charge(
            session,
            payer_user_id=USER,
            qr_content=view["qr_content"],
            link_id=env["low_link"],
            idempotency_key="qr-pay-idem-1",
            secret=SECRET,
            now=NOW + timedelta(seconds=7),
        )
    session.rollback()
    assert _tx_count(session) == 1


def test_pay_qr_insufficient_funds_rejected_without_moving_funds(env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant, amount_minor=50_000)
    session.commit()
    before_entries = _entry_count(session)
    payment, tx = qr_payment_service.pay_qr_charge(
        session,
        payer_user_id=USER,
        qr_content=view["qr_content"],
        link_id=env["low_link"],
        idempotency_key="qr-pay-nofunds-1",
        secret=SECRET,
        now=NOW + timedelta(seconds=5),
    )
    session.commit()
    assert tx.status == "REJECTED"
    assert payment.status == "REJECTED"
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "ACTIVE"
    assert _balances(session, env["payer_low"])[str(env["payer_low"])] == (FUNDED_LOW, 0)
    assert _entry_count(session) == before_entries
    assert _confirmed_events(session, payment.id) == []
    assert _merchant_notices(session) == []


def test_pay_qr_over_base_requires_pin_and_finalizes(env):
    from app.modules.transactions.service import biometric as biometric_service

    session = env["session"]
    merchant = env["merchants"][0]
    amount = EXPRESS_BASE + 1
    view = _make_charge(session, merchant, amount_minor=amount)
    session.commit()
    payment, tx = qr_payment_service.pay_qr_charge(
        session,
        payer_user_id=USER,
        qr_content=view["qr_content"],
        link_id=env["link"],
        idempotency_key="qr-pay-pending-1",
        secret=SECRET,
        now=NOW + timedelta(seconds=5),
    )
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    assert payment.status == "PENDING_AUTHORIZATION"
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "ACTIVE"
    assert _confirmed_events(session, payment.id) == []
    assert _merchant_notices(session) == []

    settled = biometric_service.authorize_transaction_with_pin(
        session,
        tx.id,
        pin="1234",
        pin_verifier=lambda _s, _u, _p: _p == "1234",
        daily_limit_reader=wallet_service.wallet_daily_limit_reader,
    )
    session.commit()
    assert settled.status == "SETTLED"
    final = qr_payment_service.finalize_qr_payment(
        session,
        qr_payment_id=payment.id,
        transaction=settled,
        now=NOW + timedelta(seconds=60),
    )
    session.commit()
    assert final.status == "PAID"
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "PAID" and charge.paid_at is not None
    assert len(_confirmed_events(session, payment.id)) == 1
    assert len(_merchant_notices(session)) == 1
    assert _balanced_postings(session, tx.id)
    balances = _balances(session, env["payer"], env["merchant_accounts"][str(merchant.id)])
    assert balances[str(env["payer"])] == (FUNDED - amount, 0)
    assert balances[str(env["merchant_accounts"][str(merchant.id)])] == (amount, 0)
    again = qr_payment_service.finalize_qr_payment(
        session, qr_payment_id=payment.id, transaction=settled
    )
    session.commit()
    assert str(again.id) == str(payment.id)
    assert len(_confirmed_events(session, payment.id)) == 1


def test_pay_qr_daily_limit_rejected_without_moving_funds(env):
    session = env["session"]
    merchant = env["merchants"][0]
    params.set_parameter(session, key=params.WALLET_DAILY_LIMIT_MINOR_KEY, value=10_000)
    session.flush()
    first_view = _make_charge(session, merchant, amount_minor=5_000)
    session.commit()
    first, first_tx = qr_payment_service.pay_qr_charge(
        session,
        payer_user_id=USER,
        qr_content=first_view["qr_content"],
        link_id=env["link"],
        idempotency_key="qr-pay-daily-1",
        secret=SECRET,
        now=NOW + timedelta(seconds=5),
    )
    session.commit()
    assert first.status == "PAID" and first_tx.status == "SETTLED"
    entries_after_first = _entry_count(session)
    second_view = _make_charge(session, merchant, amount_minor=6_000)
    session.commit()
    second, second_tx = qr_payment_service.pay_qr_charge(
        session,
        payer_user_id=USER,
        qr_content=second_view["qr_content"],
        link_id=env["link"],
        idempotency_key="qr-pay-daily-2",
        secret=SECRET,
        now=NOW + timedelta(seconds=6),
    )
    session.commit()
    assert second_tx.status == "REJECTED"
    assert second.status == "REJECTED"
    charge = qr_repo.get_qr_charge(session, second_view["charge_id"])
    assert charge.status == "ACTIVE"
    balances = _balances(session, env["payer"], env["merchant_accounts"][str(merchant.id)])
    assert balances[str(env["payer"])] == (FUNDED - 5_000, 0)
    assert balances[str(env["merchant_accounts"][str(merchant.id)])] == (5_000, 0)
    assert _entry_count(session) == entries_after_first


def test_pay_qr_merchant_without_settlement_account(env):
    session = env["session"]
    lonely = qr_repo.create_merchant(session, user_id=uuid.uuid4(), business_name="Sin cuenta")
    session.flush()
    view = qr_service.create_qr_charge(
        session,
        merchant_id=lonely.id,
        amount_minor=1_000,
        currency="PEN",
        now=NOW,
        secret=SECRET,
    )
    session.commit()
    with pytest.raises(qr_payment_domain.QrMerchantUnknownError):
        qr_payment_service.pay_qr_charge(
            session,
            payer_user_id=USER,
            qr_content=view["qr_content"],
            link_id=env["link"],
            idempotency_key="qr-pay-nobank-1",
            secret=SECRET,
            now=NOW + timedelta(seconds=5),
        )
    session.rollback()
    assert _tx_count(session) == 0
    assert _payment_count(session) == 0


def test_pay_qr_requires_idempotency_key(env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    with pytest.raises(qr_payment_domain.InvalidQrError):
        qr_payment_service.pay_qr_charge(
            session,
            payer_user_id=USER,
            qr_content=view["qr_content"],
            link_id=env["link"],
            idempotency_key="",
            secret=SECRET,
            now=NOW,
        )
    session.rollback()
    assert _tx_count(session) == 0


def test_pay_qr_logs_and_events_without_pii(env, caplog):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    with caplog.at_level(logging.DEBUG, logger="app.modules.wallet"):
        payment, _tx = qr_payment_service.pay_qr_charge(
            session,
            payer_user_id=USER,
            qr_content=view["qr_content"],
            link_id=env["link"],
            idempotency_key="qr-pay-nopii-1",
            secret=SECRET,
            now=NOW + timedelta(seconds=5),
        )
    session.commit()
    text = caplog.text
    assert SECRET not in text
    assert view["qr_content"] not in text
    assert PAYER_NUMBER not in text
    assert merchant.business_name not in text
    events = _confirmed_events(session, payment.id)
    assert len(events) == 1
    blob = str(events[0].payload)
    assert SECRET not in blob
    assert PAYER_NUMBER not in blob
    assert merchant.business_name not in blob


def test_wallet_never_touches_foreign_money_tables():
    import pathlib

    module_dir = pathlib.Path(__file__).resolve().parents[1] / "app" / "modules" / "wallet"
    forbidden = (
        "transactions.repository",
        "transactions.models",
        "transactions import repository",
        "accounts.models",
        "accounts.repository",
        "ledger.repository",
        "ledger.models",
        "ledger import service",
        "ledger.service",
    )
    for name in ("domain/qr_payment.py", "repository/qr_payment.py", "service/qr_payment.py"):
        content = (module_dir / name).read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in content, f"{name} toca frontera ajena: {marker}"


# ------------------------------------------------------- Parte C: migraciones


def test_migration_0040_chained_after_0039_with_single_head():
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
    assert revisions.get("0040_wallet_qr_payments") == "0039_wallet_qr"
    assert revisions.get("0039_wallet_qr") == "0038_wallet_express_params"
    # La guarda de doble cobro (`0041`, fix ALTO-2) encadena tras `0040`:
    # el head vigente vive en `test_e4_t11b_qr_payment_guard.py`.
    # E4-T16 (2026-10-13): la cabeza avanza a 0042 (catalogo `billers`).
    # E6-T12 (2026-10-13): la cabeza avanza a 0044_notifications_preferences
    # (preferencias por canal + claves de seguridad, tras 0043).
    assert revisions.get("0042_wallet_services") == "0041_wallet_qr_payment_guard"
    assert revisions.get("0041_wallet_qr_payment_guard") == "0040_wallet_qr_payments"
    referenced = {down for down in revisions.values() if down is not None}
    heads = [rev for rev in revisions if rev not in referenced]
    assert heads == ["0044_notifications_preferences"], f"heads inesperados: {heads}"
    content = (versions_dir / "0040_wallet_qr_payments.py").read_text(encoding="utf-8")
    assert "qr_payments" in content
    assert "uq_qr_payments_transaction_id" in content
    assert "ix_qr_payments_charge_id" in content
    assert "fk_qr_payments_charge" in content
    assert "fk_qr_payments_merchant" in content


def test_pg_0040_downgrade_reversible(test_engine):
    """`upgrade -> downgrade(0039) -> upgrade(head)` sin error de FK."""
    import sqlalchemy as sa
    from sqlalchemy.orm import Session as SASession

    from tests.db_utils import (
        downgrade_in_process,
        resolve_test_database_url,
        run_migrations_in_process,
    )

    url = resolve_test_database_url()
    try:
        downgrade_in_process(url, "0039_wallet_qr")
        with test_engine.connect() as conn:
            assert (
                conn.execute(sa.text("SELECT to_regclass('wallet.qr_payments')")).scalar() is None
            )
        run_migrations_in_process(url, "head")
        with test_engine.connect() as conn:
            assert (
                conn.execute(sa.text("SELECT to_regclass('wallet.qr_payments')")).scalar()
                == "wallet.qr_payments"
            )
    finally:
        run_migrations_in_process(url, "head")
        session = SASession(bind=test_engine, autoflush=False, expire_on_commit=False)
        try:
            session.execute(sa.text("DELETE FROM wallet.qr_payments"))
            session.commit()
        finally:
            session.close()
