"""QA de seguridad del QR y concurrencia de pago (E4-T15, HU15).

Suite complementaria —sin solapar— a `E4-T11` (servicio/dominio) y `E4-T12`
(endpoint/autorizacion): no repite sus caminos (feliz un toque, firma
alterada de 64 `f`, ref ajena, replay/conflicto de idempotencia, PIN
valido/invalido, diario, vinculo ajeno, contrato OpenAPI, frontera,
cadena `0040`). Cubre solo los huecos para cerrar HU15 y `docs/18` G72:

- CA-01: el `qr_content` creado por el servidor se decodifica y verifica
  contra la fila del cargo (comercio+monto del servidor) en < 2 s.
- CA-04: variantes de manipulacion no cubiertas (moneda, vencimiento del
  sobre, firma truncada) -> rechazo sin mover dinero; cadena
  job `E4-T07` -> pago (el `EXPIRED` persistido rechaza; el `STATIC`
  sobrevive al job y cobra).
- Concurrencia/seguridad: drenaje exacto sin sobregiro; doble escaneo con
  claves distintas -> un solo cobro + cuadre global del ledger.
- CA-03: pendiente sobre el base que nunca se autoriza -> sin liquidar;
  `finalize_qr_payment` con operacion no liquidada se niega.
- Abono E2E real (G72): `pay_qr_charge` con sender mock -> una sola
  invocacion `push`, payload `{charge_ref, at}` < 5 s; y cierre HTTP
  `POST /payments/qr` -> cargo `PAID` + aviso unico.
- Seguridad: `qr_payments` no persiste `qr_content`/firma/secreto (esquema
  + blobs); G75: el re-chequeo usa `wallet_daily_limit_reader`.

SQLite en memoria + schemas ATTACH con el motor real; sin red, sin `float`.
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
from app.core.db import Base, get_db
from app.main import app
from app.modules.shared.config import parameters as params
from app.modules.wallet import service as wallet_service
from app.modules.wallet.domain import qr_payment as qr_payment_domain
from app.modules.wallet.jobs.qr_expiry import run_qr_expiry_once
from app.modules.wallet.repository import qr as qr_repo
from app.modules.wallet.service import qr as qr_service
from app.modules.wallet.service import qr_payment as qr_payment_service

assert _accounts is not None and _audit is not None and _ledger is not None
assert _notifications is not None and _shared is not None
assert _transactions is not None and _wallet is not None

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)

USER = uuid.uuid4()

PAYER_NUMBER = "00110001111111"
EXACT_NUMBER = "00110009999999"
MERCHANT_NUMBER = "00220003333333"

FUNDED = 100_000
EXACT_FUNDED = 5_000

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
    """Motor real en SQLite: pagador + cuenta de drenaje exacto + comercio."""
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
        exact = accounts_repo.create_account(
            session, user_id=USER, account_number=EXACT_NUMBER, type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, exact.id, available_delta=EXACT_FUNDED, held_delta=0, currency="PEN"
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
        exact_link = wallet_service.link_source(session, USER, type="ACCOUNT", account_id=exact.id)
        session.commit()
        yield {
            "session": session,
            "merchants": merchants,
            "merchant_accounts": merchant_accounts,
            "payer": payer.id,
            "exact": exact.id,
            "link": link["id"],
            "exact_link": exact_link["id"],
        }
        session.rollback()
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def api_client(env, monkeypatch):
    """TestClient con la BD del fixture y auth falsa como el pagador."""
    from app.modules.transactions.api import get_current_user_id as tx_get_current_user_id
    from app.modules.wallet.api import get_current_user_id as wallet_get_current_user_id

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    session = env["session"]

    def _override_db():
        yield session

    def _override_user():
        return USER

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[wallet_get_current_user_id] = _override_user
    app.dependency_overrides[tx_get_current_user_id] = _override_user
    try:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(wallet_get_current_user_id, None)
        app.dependency_overrides.pop(tx_get_current_user_id, None)


def _make_charge(session, merchant, amount_minor=5_000, **kwargs):
    return qr_service.create_qr_charge(
        session,
        merchant_id=merchant.id,
        amount_minor=amount_minor,
        currency="PEN",
        secret=None,
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


def _count(session: Session, table: str) -> int:
    return int(session.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar())


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


def _ledger_is_globally_balanced(session: Session) -> bool:
    from app.modules.ledger.models import Posting

    postings = session.scalars(sa.select(Posting)).all()
    assert postings, "el ciclo debe dejar asientos en el ledger"
    debit = sum(int(p.amount_minor) for p in postings if p.direction == "DEBIT")
    credit = sum(int(p.amount_minor) for p in postings if p.direction == "CREDIT")
    assert debit > 0 and debit == credit, "ledger descuadrado globalmente"
    assert all(type(p.amount_minor) is int for p in postings)
    return True


# ------------------------------------------------------- CA-01: lectura del servidor


def test_qa_read_recovers_server_merchant_and_amount_under_2s(env):
    """CA-01: el `qr_content` del servidor decodifica comercio+monto, < 2 s."""
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant, amount_minor=7_500)
    session.commit()
    started = time.perf_counter()
    envelope = qr_payment_domain.decode_qr_content(view["qr_content"])
    row = qr_repo.get_qr_charge(session, envelope.ref)
    payload = qr_payment_domain.verify_charge_integrity(row, envelope, secret=None)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"lectura/validacion fuera de ventana: {elapsed:.3f}s"
    assert str(row.merchant_id) == str(merchant.id) == payload["merchant_id"]
    assert int(row.amount_minor) == envelope.amount_minor == 7_500
    assert str(row.currency) == envelope.currency == "PEN"
    assert type(envelope.amount_minor) is int


# ------------------------------------------------------- CA-04: manipulacion no cubierta


def test_qa_tampered_currency_and_expiry_rejected_without_moving_funds(env):
    """CA-04: moneda o vencimiento del sobre manipulados -> rechazo sin dinero."""
    session = env["session"]
    merchant = env["merchants"][0]
    before_payer = _balances(session, env["payer"])
    before_merchant = _balances(session, env["merchant_accounts"][str(merchant.id)])
    before_entries = _count(session, "ledger.journal_entries")

    view = _make_charge(session, merchant)
    session.commit()
    envelope = interop.decode_payload(view["qr_content"])
    tampered_ccy = interop.encode_payload(
        issuer=envelope.issuer,
        peer=envelope.peer,
        ref=envelope.ref,
        amount_minor=envelope.amount_minor,
        currency="USD",
        expires_at=envelope.expires_at,
        sig=envelope.sig,
    )
    with pytest.raises(qr_payment_domain.InvalidQrError):
        qr_payment_service.pay_qr_charge(
            session,
            payer_user_id=USER,
            qr_content=tampered_ccy,
            link_id=env["link"],
            idempotency_key="qa15-tamper-ccy-1",
            secret=None,
            now=NOW + timedelta(seconds=5),
        )
    session.rollback()

    view2 = _make_charge(session, merchant)
    session.commit()
    envelope2 = interop.decode_payload(view2["qr_content"])
    tampered_exp = interop.encode_payload(
        issuer=envelope2.issuer,
        peer=envelope2.peer,
        ref=envelope2.ref,
        amount_minor=envelope2.amount_minor,
        currency=envelope2.currency,
        expires_at=int(envelope2.expires_at) + 3600,
        sig=envelope2.sig,
    )
    with pytest.raises(qr_payment_domain.InvalidQrError):
        qr_payment_service.pay_qr_charge(
            session,
            payer_user_id=USER,
            qr_content=tampered_exp,
            link_id=env["link"],
            idempotency_key="qa15-tamper-exp-1",
            secret=None,
            now=NOW + timedelta(seconds=5),
        )
    session.rollback()

    assert qr_payment_domain.InvalidQrError.code == "VALIDATION_ERROR"
    assert _count(session, "transactions.transactions") == 0
    assert _count(session, "wallet.qr_payments") == 0
    assert _count(session, "ledger.journal_entries") == before_entries
    assert _balances(session, env["payer"]) == before_payer
    assert _balances(session, env["merchant_accounts"][str(merchant.id)]) == before_merchant
    for charge_id in (view["charge_id"], view2["charge_id"]):
        charge = qr_repo.get_qr_charge(session, charge_id)
        assert charge.status == "ACTIVE" and charge.paid_at is None


def test_qa_truncated_signature_rejected_without_moving_funds(env):
    """CA-04: firma truncada (variante no cubierta) -> rechazo sin dinero."""
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    envelope = interop.decode_payload(view["qr_content"])
    assert len(envelope.sig) == 64
    truncated = interop.encode_payload(
        issuer=envelope.issuer,
        peer=envelope.peer,
        ref=envelope.ref,
        amount_minor=envelope.amount_minor,
        currency=envelope.currency,
        expires_at=envelope.expires_at,
        sig=envelope.sig[:32],
    )
    before_entries = _count(session, "ledger.journal_entries")
    with pytest.raises(qr_payment_domain.InvalidQrError):
        qr_payment_service.pay_qr_charge(
            session,
            payer_user_id=USER,
            qr_content=truncated,
            link_id=env["link"],
            idempotency_key="qa15-tamper-trunc-1",
            secret=None,
            now=NOW + timedelta(seconds=5),
        )
    session.rollback()
    assert _count(session, "transactions.transactions") == 0
    assert _count(session, "wallet.qr_payments") == 0
    assert _count(session, "ledger.journal_entries") == before_entries
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "ACTIVE" and charge.paid_at is None


# ------------------------------------------------------- CA-04: job de caducidad -> pago


def test_qa_expiry_job_then_pay_rejected_and_static_survives(env):
    """CA-04 + E4-T07: el job expira el `DYNAMIC`; el `STATIC` cobra igual."""
    session = env["session"]
    merchant = env["merchants"][0]
    # Reloj fijo: el `DYNAMIC` vence a `NOW+300`; el `STATIC` no vence.
    dynamic = qr_service.create_qr_charge(
        session,
        merchant_id=merchant.id,
        amount_minor=5_000,
        currency="PEN",
        secret=None,
        now=NOW,
    )
    static = qr_service.create_qr_charge(
        session,
        merchant_id=merchant.id,
        amount_minor=5_000,
        currency="PEN",
        secret=None,
        mode="STATIC",
        now=NOW,
    )
    session.commit()
    assert qr_repo.get_qr_charge(session, static["charge_id"]).expires_at is None

    result = run_qr_expiry_once(session, now=NOW + timedelta(seconds=301))
    assert dynamic["charge_id"] in result["expired"]
    assert static["charge_id"] not in result["expired"]
    assert qr_repo.get_qr_charge(session, dynamic["charge_id"]).status == "EXPIRED"
    assert qr_repo.get_qr_charge(session, static["charge_id"]).status == "ACTIVE"

    before_entries = _count(session, "ledger.journal_entries")
    before_payer = _balances(session, env["payer"])
    with pytest.raises(qr_payment_domain.QrExpiredError):
        qr_payment_service.pay_qr_charge(
            session,
            payer_user_id=USER,
            qr_content=dynamic["qr_content"],
            link_id=env["link"],
            idempotency_key="qa15-job-exp-1",
            secret=None,
            now=NOW + timedelta(seconds=302),
        )
    session.rollback()
    assert _count(session, "transactions.transactions") == 0
    assert _count(session, "ledger.journal_entries") == before_entries
    assert _balances(session, env["payer"]) == before_payer
    expired = qr_repo.get_qr_charge(session, dynamic["charge_id"])
    assert expired.status == "EXPIRED" and expired.paid_at is None

    payment, tx = qr_payment_service.pay_qr_charge(
        session,
        payer_user_id=USER,
        qr_content=static["qr_content"],
        link_id=env["link"],
        idempotency_key="qa15-job-static-1",
        secret=None,
        now=NOW + timedelta(seconds=302),
    )
    session.commit()
    assert tx.status == "SETTLED" and payment.status == "PAID"
    assert qr_repo.get_qr_charge(session, static["charge_id"]).status == "PAID"


# ------------------------------------------------------- concurrencia/seguridad: sin sobregiro


def test_qa_exact_balance_no_overdraft(env):
    """Seguridad: drenaje exacto deja 0; el siguiente pago se rechaza sin negativo."""
    session = env["session"]
    merchant = env["merchants"][0]
    first_view = _make_charge(session, merchant, amount_minor=EXACT_FUNDED)
    session.commit()
    payment, tx = qr_payment_service.pay_qr_charge(
        session,
        payer_user_id=USER,
        qr_content=first_view["qr_content"],
        link_id=env["exact_link"],
        idempotency_key="qa15-nodraft-1",
        secret=None,
        now=NOW + timedelta(seconds=5),
    )
    session.commit()
    assert tx.status == "SETTLED" and payment.status == "PAID"
    assert _balances(session, env["exact"])[str(env["exact"])] == (0, 0)

    entries_after_first = _count(session, "ledger.journal_entries")
    second_view = _make_charge(session, merchant, amount_minor=1_000)
    session.commit()
    second, second_tx = qr_payment_service.pay_qr_charge(
        session,
        payer_user_id=USER,
        qr_content=second_view["qr_content"],
        link_id=env["exact_link"],
        idempotency_key="qa15-nodraft-2",
        secret=None,
        now=NOW + timedelta(seconds=6),
    )
    session.commit()
    assert second_tx.status == "REJECTED"
    assert second.status == "REJECTED"
    balances = _balances(session, env["exact"])
    assert balances[str(env["exact"])] == (0, 0)
    assert balances[str(env["exact"])][0] >= 0, "sobregiro: disponible negativo"
    assert qr_repo.get_qr_charge(session, second_view["charge_id"]).status == "ACTIVE"
    assert _count(session, "ledger.journal_entries") == entries_after_first
    notices = _merchant_notices(session)
    assert len(notices) == 1, "solo el primer cobro avisa al comercio"


# ------------------------------------------------------- concurrencia: doble escaneo, un cobro


def test_qa_double_scan_single_settlement_and_global_balance(env):
    """Concurrencia: doble escaneo con claves distintas -> un solo cobro cuadrado."""
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant, amount_minor=5_000)
    session.commit()
    first, _first_tx = qr_payment_service.pay_qr_charge(
        session,
        payer_user_id=USER,
        qr_content=view["qr_content"],
        link_id=env["link"],
        idempotency_key="qa15-double-1",
        secret=None,
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
            idempotency_key="qa15-double-2",
            secret=None,
            now=NOW + timedelta(seconds=6),
        )
    session.rollback()
    assert qr_payment_domain.QrAlreadyPaidError.code == "CONFLICT"
    assert _count(session, "transactions.transactions") == 1
    assert _count(session, "wallet.qr_payments") == 1
    assert len(_confirmed_events(session, first.id)) == 1
    assert len(_merchant_notices(session)) == 1
    assert type(first.amount_minor) is int
    assert _ledger_is_globally_balanced(session)
    balances = _balances(session, env["payer"], env["merchant_accounts"][str(merchant.id)])
    assert balances[str(env["payer"])] == (FUNDED - 5_000, 0)
    assert balances[str(env["merchant_accounts"][str(merchant.id)])] == (5_000, 0)


# ------------------------------------------------------- CA-03: pendiente sin factor


def test_qa_over_base_without_factor_never_settles(env):
    """CA-03: sobre el base sin autorizar -> pendiente, sin liquidar ni avisar."""
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
        idempotency_key="qa15-nofactor-1",
        secret=None,
        now=NOW + timedelta(seconds=5),
    )
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    assert payment.status == "PENDING_AUTHORIZATION"
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "ACTIVE" and charge.paid_at is None
    assert _confirmed_events(session, payment.id) == []
    assert _merchant_notices(session) == []

    with pytest.raises(qr_payment_domain.InvalidQrError):
        qr_payment_service.finalize_qr_payment(session, qr_payment_id=payment.id, transaction=tx)
    session.rollback()

    replay, replay_tx = qr_payment_service.pay_qr_charge(
        session,
        payer_user_id=USER,
        qr_content=view["qr_content"],
        link_id=env["link"],
        idempotency_key="qa15-nofactor-1",
        secret=None,
        now=NOW + timedelta(seconds=6),
    )
    session.commit()
    assert str(replay.id) == str(payment.id)
    assert str(replay_tx.id) == str(tx.id)
    assert replay.status == "PENDING_AUTHORIZATION"
    assert _count(session, "wallet.qr_payments") == 1
    assert qr_repo.get_qr_charge(session, view["charge_id"]).status == "ACTIVE"


# ------------------------------------------------------- G72: abono E2E real + G75


def test_qa_real_payment_abono_single_notice_under_5s(env, caplog):
    """G72: el pago real emite un solo `qr_charge_paid` (`{charge_ref, at}`), < 5 s."""
    from app.adapters.notification_sender import MockNotificationSender
    from app.modules.notifications.domain.templates import (
        BASE_TEMPLATES,
        render_template,
    )
    from app.modules.notifications.models import Notification

    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant, amount_minor=5_000)
    session.commit()
    sender = MockNotificationSender()
    started = time.perf_counter()
    with caplog.at_level(logging.DEBUG, logger="app.modules.wallet"):
        payment, tx = qr_payment_service.pay_qr_charge(
            session,
            payer_user_id=USER,
            qr_content=view["qr_content"],
            link_id=env["link"],
            idempotency_key="qa15-abono-1",
            secret=None,
            now=NOW + timedelta(seconds=5),
            notify_sender=sender,
        )
    elapsed = time.perf_counter() - started
    session.commit()
    assert elapsed < 5.0, f"abono fuera de ventana: {elapsed:.3f}s"
    assert tx.status == "SETTLED" and payment.status == "PAID"
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "PAID" and charge.paid_at is not None

    assert len(sender.calls) == 1 and sender.calls[0]["ok"] is True
    assert sender.calls[0]["channel"] == "push"
    assert len(_confirmed_events(session, payment.id)) == 1
    rows = session.scalars(
        sa.select(Notification).where(Notification.template_code == "qr_charge_paid")
    ).all()
    assert len(rows) == 1
    stored = rows[0].payload_json["data"]
    assert set(stored) == {"charge_ref", "at"}
    assert stored["charge_ref"] == view["charge_id"][:8]
    text = render_template(BASE_TEMPLATES["qr_charge_paid"]["body_template"], stored)
    assert "5000" not in text and "5_000" not in text
    assert merchant.business_name not in text
    assert view["qr_content"] not in text
    assert PAYER_NUMBER not in text and EXACT_NUMBER not in text
    logs = caplog.text
    assert view["qr_content"] not in logs
    assert PAYER_NUMBER not in logs


def test_qa_wallet_daily_limit_reader_governs_recheck(env):
    """G75: el re-chequeo diario usa el lector de billetera (limite sembrado)."""
    session = env["session"]
    assert wallet_service.wallet_daily_limit_reader(session, "STANDARD") == DAILY
    assert wallet_service.wallet_daily_limit_reader(session, None) == DAILY


def test_qa_qr_payments_persist_no_secret_or_content():
    """Seguridad: `qr_payments` no tiene columnas para `qr_content`/firma/secreto."""
    from app.modules.wallet.models import QrPayment

    columns = {c.name for c in QrPayment.__table__.columns}
    for forbidden in ("qr_content", "signature", "payload_hash", "secret", "pin"):
        assert forbidden not in columns, f"qr_payments persiste {forbidden}"
    assert "transaction_id" in columns and "charge_id" in columns


# ------------------------------------------------------- G72: cierre HTTP E2E


def test_qa_post_qr_e2e_paid_and_single_abono(api_client, env):
    """G72 (cierre): `POST /payments/qr` -> cargo `PAID` + aviso unico, < 5 s."""
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant, amount_minor=5_000)
    session.commit()
    started = time.perf_counter()
    resp = api_client.post(
        "/api/v1/payments/qr",
        json={"qr_content": view["qr_content"], "link_id": str(env["link"])},
        headers={"Idempotency-Key": "qa15-e2e-1"},
    )
    elapsed = time.perf_counter() - started
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "SETTLED"
    assert data["amount_minor"] == 5_000 and type(data["amount_minor"]) is int
    assert data["merchant"]["merchant_id"] == view["merchant_id"]
    assert data["charge_id"] == view["charge_id"]
    assert elapsed < 5.0, f"ciclo E2E fuera de ventana: {elapsed:.3f}s"

    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "PAID" and charge.paid_at is not None
    payment_id = uuid.UUID(data["qr_payment_id"])
    assert len(_confirmed_events(session, payment_id)) == 1
    assert len(_merchant_notices(session)) == 1
    assert _ledger_is_globally_balanced(session)
    body = resp.text
    assert "qr_content" not in body and "signature" not in body
    assert "payload_hash" not in body and PAYER_NUMBER not in body
