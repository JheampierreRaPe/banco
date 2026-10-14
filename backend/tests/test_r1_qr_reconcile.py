"""Reconciliacion del cobro QR sin replay del cliente (R-1, HU15).

Escenario "autorizado pero sin replay": `pay_qr_charge` deja el pago en
`PENDING_AUTHORIZATION` -> `authorize_transaction_with_pin` liquida la
operacion del motor (`SETTLED`, el dinero ya se movio) y el cliente
nunca reintenta `POST /payments/qr` (sin `finalize_qr_payment`). El job
`wallet/jobs/qr_reconcile.py::run_qr_reconcile_once` debe cerrar el
cobro server-side: cargo `PAID` + exactamente un evento
`qr.payment.confirmed` + un solo aviso `qr_charge_paid`; y el job de
caducidad ya no lo marca `EXPIRED` (la reconciliacion corre antes).
Un pago cuya operacion NO esta `SETTLED` no se finaliza.

Falla sin el fix (no existe `qr_reconcile.py` ni la pasada en el
runner); pasa con el. Sin `float`; sin red; sin PII en logs.
"""

from __future__ import annotations

import logging
import pathlib
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
from app.core.db import Base
from app.modules.shared.config import parameters as params
from app.modules.wallet import service as wallet_service
from app.modules.wallet.jobs import qr_reconcile as qr_reconcile_job
from app.modules.wallet.repository import qr as qr_repo
from app.modules.wallet.repository import qr_payment as qr_payment_repo
from app.modules.wallet.service import qr as qr_service
from app.modules.wallet.service import qr_payment as qr_payment_service

assert _accounts is not None and _audit is not None and _ledger is not None
assert _notifications is not None and _shared is not None
assert _transactions is not None and _wallet is not None

SECRET = "test-qr-reconcile-secret"
NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)

USER = uuid.uuid4()

PAYER_NUMBER = "00110001111111"
MERCHANT_NUMBER = "00220003333333"

FUNDED = 100_000
EXPRESS_BASE = 20_000
DAILY = 500_000
AMOUNT = EXPRESS_BASE + 1

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
    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from app.modules.accounts import repository as accounts_repo

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
        session.commit()
        yield {
            "engine": engine,
            "session": session,
            "merchants": merchants,
            "merchant_accounts": merchant_accounts,
            "payer": payer.id,
            "link": link["id"],
        }
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def _make_charge(session, merchant, amount_minor=AMOUNT):
    return qr_service.create_qr_charge(
        session,
        merchant_id=merchant.id,
        amount_minor=amount_minor,
        currency="PEN",
        now=NOW,
        secret=SECRET,
    )


def _pay_pending(session, link_id, qr_content, key):
    payment, tx = qr_payment_service.pay_qr_charge(
        session,
        payer_user_id=USER,
        qr_content=qr_content,
        link_id=link_id,
        idempotency_key=key,
        secret=SECRET,
        now=NOW + timedelta(seconds=5),
    )
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    assert payment.status == "PENDING_AUTHORIZATION"
    return payment, tx


def _authorize(session, tx):
    from app.modules.transactions.service import biometric as biometric_service

    settled = biometric_service.authorize_transaction_with_pin(
        session,
        tx.id,
        pin="1234",
        pin_verifier=lambda _s, _u, _p: _p == "1234",
        daily_limit_reader=wallet_service.wallet_daily_limit_reader,
    )
    session.commit()
    assert settled.status == "SETTLED"
    return settled


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


def _balances(session: Session, *account_ids: uuid.UUID) -> dict[str, tuple[int, int]]:
    from app.modules.accounts.models import AccountBalance

    out = {}
    for aid in account_ids:
        row = session.scalars(
            sa.select(AccountBalance).where(AccountBalance.account_id == aid)
        ).one()
        out[str(aid)] = (int(row.available_minor), int(row.held_minor))
    return out


def test_reconcile_finalizes_settled_without_replay(env, caplog):
    """R-1: autorizado sin replay -> el job cierra a `PAID` con un solo aviso."""
    from app.modules.wallet.jobs.qr_expiry import run_qr_expiry_once

    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    payment, tx = _pay_pending(session, env["link"], view["qr_content"], "qr-r1-noreplay-1")
    _authorize(session, tx)
    # El cliente nunca reintenta: sin `finalize_qr_payment` el cobro sigue pendiente.
    pending = qr_payment_repo.get_qr_payment(session, payment.id)
    assert pending.status == "PENDING_AUTHORIZATION"
    assert qr_repo.get_qr_charge(session, view["charge_id"]).status == "ACTIVE"
    # El cargo vence antes de que corra la caducidad: sin reconciliacion se perderia.
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    charge.expires_at = NOW - timedelta(seconds=1)
    session.commit()

    with caplog.at_level(logging.INFO, logger="app.modules.wallet"):
        result = qr_reconcile_job.run_qr_reconcile_once(session)
    assert result == {"reconciled": [str(payment.id)], "skipped": []}

    final = qr_payment_repo.get_qr_payment(session, payment.id)
    assert final.status == "PAID"
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "PAID" and charge.paid_at is not None
    assert len(_confirmed_events(session, payment.id)) == 1
    assert len(_merchant_notices(session)) == 1
    balances = _balances(session, env["payer"], env["merchant_accounts"][str(merchant.id)])
    assert balances[str(env["payer"])] == (FUNDED - AMOUNT, 0)
    assert balances[str(env["merchant_accounts"][str(merchant.id)])] == (AMOUNT, 0)
    assert SECRET not in caplog.text
    assert view["qr_content"] not in caplog.text

    # Idempotente: segunda pasada sin duplicar evento ni aviso.
    again = qr_reconcile_job.run_qr_reconcile_once(session)
    assert again == {"reconciled": [], "skipped": []}
    assert len(_confirmed_events(session, payment.id)) == 1
    assert len(_merchant_notices(session)) == 1

    # La caducidad ya no lo toca (cargo `PAID`), pero sigue viva para otros vencidos.
    control = qr_service.create_qr_charge(
        session,
        merchant_id=merchant.id,
        amount_minor=1_000,
        currency="PEN",
        now=NOW - timedelta(seconds=600),
        secret=SECRET,
    )
    session.commit()
    expiry = run_qr_expiry_once(session, now=NOW)
    assert expiry["expired"] == [control["charge_id"]]
    assert qr_repo.get_qr_charge(session, view["charge_id"]).status == "PAID"


def test_reconcile_skips_payment_whose_tx_is_not_settled(env):
    """R-1: sin `SETTLED` no se finaliza (ni evento, ni aviso, ni cambio de estado)."""
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    payment, tx = _pay_pending(session, env["link"], view["qr_content"], "qr-r1-unsettled-1")

    result = qr_reconcile_job.run_qr_reconcile_once(session)
    assert result == {"reconciled": [], "skipped": [str(payment.id)]}

    assert qr_payment_repo.get_qr_payment(session, payment.id).status == "PENDING_AUTHORIZATION"
    assert qr_repo.get_qr_charge(session, view["charge_id"]).status == "ACTIVE"
    assert _confirmed_events(session, payment.id) == []
    assert _merchant_notices(session) == []
    # La operacion sigue pendiente: el dinero aun no se movio al comercio.
    from app.modules.transactions.service import find_transaction_by_id

    assert find_transaction_by_id(session, tx.id).status == "PENDING_AUTHORIZATION"

    # Tras autorizar, la siguiente pasada si lo cierra (sin replay del cliente).
    _authorize(session, tx)
    result = qr_reconcile_job.run_qr_reconcile_once(session)
    assert result == {"reconciled": [str(payment.id)], "skipped": []}
    assert qr_payment_repo.get_qr_payment(session, payment.id).status == "PAID"
    assert len(_merchant_notices(session)) == 1


def test_reconcile_validates_limit():
    """R-1: `limit` invalido se rechaza como en los demas jobs."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite://", poolclass=StaticPool)
    session = Session(bind=engine, autoflush=False)
    try:
        with pytest.raises(ValueError):
            qr_reconcile_job.run_qr_reconcile_once(session, limit=0)
        with pytest.raises(ValueError):
            qr_reconcile_job.run_qr_reconcile_once(session, limit=-3)
        with pytest.raises(ValueError):
            qr_payment_repo.list_pending_authorization(session, limit=0)
    finally:
        session.close()
        engine.dispose()


def test_runner_runs_reconcile_before_expiry_and_respects_flags(env, monkeypatch):
    """R-1: el runner incluye la reconciliacion antes que la caducidad, con flags."""
    from app import composition

    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    payment, tx = _pay_pending(session, env["link"], view["qr_content"], "qr-r1-runner-1")
    _authorize(session, tx)
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    charge.expires_at = NOW - timedelta(seconds=1)
    session.commit()

    engine = env["engine"]

    def _factory():
        return Session(bind=engine, autoflush=False, expire_on_commit=False)

    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "1")
    monkeypatch.setenv("WALLET_QR_EXPIRY_ENABLED", "1")
    monkeypatch.setenv("WALLET_QR_RECONCILE_ENABLED", "1")
    ran = composition.run_wallet_qr_jobs_once(session_factory=_factory, now=NOW)
    assert ran["qr_reconcile"] == {"reconciled": [str(payment.id)], "skipped": []}
    assert ran["qr_expiry"]["expired"] == []

    check = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        assert qr_payment_repo.get_qr_payment(check, payment.id).status == "PAID"
        assert qr_repo.get_qr_charge(check, view["charge_id"]).status == "PAID"
    finally:
        check.close()

    monkeypatch.setenv("WALLET_QR_RECONCILE_ENABLED", "0")
    disabled = composition.run_wallet_qr_jobs_once(session_factory=_factory, now=NOW)
    assert disabled["qr_reconcile"] == {"skipped": "disabled"}

    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_RECONCILE_ENABLED", "1")
    off = composition.run_wallet_qr_jobs_once(session_factory=_factory, now=NOW)
    assert off == {
        "qr_reconcile": {"skipped": "disabled"},
        "qr_expiry": {"skipped": "disabled"},
    }


def test_transaction_facade_reads_by_id_and_wallet_uses_no_foreign_tables():
    """R-1/M-1: la fachada de lectura por id existe y el job no toca tablas ajenas."""
    import app.modules.wallet.jobs.qr_reconcile as job_module
    from app.modules.transactions import service as tx_service

    assert callable(tx_service.find_transaction_by_id)
    assert "find_transaction_by_id" in tx_service.__all__
    source = pathlib.Path(job_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("transactions.repository", "transactions.models"):
        assert forbidden not in source
    assert "find_transaction_by_id" in source
    repo_source = pathlib.Path(qr_payment_repo.__file__).read_text(encoding="utf-8")
    assert "list_pending_authorization" in repo_source


def test_find_transaction_by_id_returns_none_when_missing(env):
    """R-1: la fachada por id es solo lectura (`None` si no existe o es invalido)."""
    from app.modules.transactions import service as tx_service

    session = env["session"]
    assert tx_service.find_transaction_by_id(session, uuid.uuid4()) is None
    assert tx_service.find_transaction_by_id(session, "no-es-un-uuid") is None
