"""Guarda de doble cobro + reintento tras `REJECTED` + preview (fix ALTO-2/MEDIO-2/MEDIO-3).

- ALTO-2: dos pagos concurrentes del mismo `qr_charge` con
  `Idempotency-Key` distintas resultan en UN solo cobro (uno gana, el
  otro `CONFLICT`) y UN solo asiento. La carrera se simula
  neutralizando la lectura Python de la guarda (ambos hilos la
  superan): la BD decide via el indice unico parcial
  `uq_qr_payments_charge_active` (migracion `0041`). Sin el fix hay
  dos pagos y dos asientos (falla).
- MEDIO-2: tras un `REJECTED` sin fondos (sin mover dinero), un nuevo
  intento con fondos liquida `SETTLED`. Sin el fix el reintento da
  `409` (falla).
- MEDIO-3: `preview_qr_charge` + `POST /payments/qr/preview`
  devuelven comercio/monto sin mover dinero; vencido/corrupto/
  manipulado -> `422` sin efectos.
- Migracion `0041_wallet_qr_payment_guard` encadenada tras `0040`
  (un solo head) y reversible en PG.

SQLite en memoria + schemas ATTACH (motor real), patron `E4-T11`;
API con `TestClient` y auth falsa, patron `E4-T12`. Sin `float`;
sin red; sin PII.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
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
from app.modules.transactions.api import get_current_user_id as tx_get_current_user_id
from app.modules.wallet import service as wallet_service
from app.modules.wallet.api import get_current_user_id as wallet_get_current_user_id
from app.modules.wallet.domain import qr_payment as qr_payment_domain
from app.modules.wallet.repository import qr as qr_repo
from app.modules.wallet.repository import qr_payment as qr_payment_repo
from app.modules.wallet.service import qr as qr_service
from app.modules.wallet.service import qr_payment as qr_payment_service

assert _accounts is not None and _audit is not None and _ledger is not None
assert _notifications is not None and _shared is not None
assert _transactions is not None and _wallet is not None

SECRET = "test-qr-guard-secret"
NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)

USER = uuid.uuid4()

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


@pytest.fixture()
def api_client(env, monkeypatch):
    """TestClient con BD SQLite y auth falsa como el pagador (patron `E4-T12`)."""
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


def _make_charge(session, merchant, amount_minor=5_000, secret=SECRET, now=None, **kwargs):
    # `now=None` usa el reloj real (lo exigen los tests via endpoint);
    # los tests de servicio pasan `now=NOW` (determinista).
    return qr_service.create_qr_charge(
        session,
        merchant_id=merchant.id,
        amount_minor=amount_minor,
        currency="PEN",
        now=now,
        secret=secret,
        **kwargs,
    )


def _pay(session, qr_content, link_id, key, **kwargs):
    return qr_payment_service.pay_qr_charge(
        session,
        payer_user_id=USER,
        qr_content=qr_content,
        link_id=link_id,
        idempotency_key=key,
        secret=SECRET,
        now=NOW + timedelta(seconds=5),
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


# ------------------------------------------------------- ALTO-2: carrera por el cargo


def test_race_two_keys_single_winner_single_entry(env, monkeypatch):
    """ALTO-2: ambos hilos superan la lectura -> la BD deja UN cobro (409 al otro).

    Simula la carrera neutralizando las lecturas Python (estado del
    cargo + pago existente: el segundo pago arranca antes de que el
    primero sea visible). Sin el fix hay dos pagos y dos asientos.
    """
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant, now=NOW)
    session.commit()
    # Simula la carrera: ambos pagos pasan la guarda Python (el segundo
    # arranca antes de que el primero sea visible). Se neutralizan las
    # tres lecturas (estado, guarda nueva e historica) para que el test
    # falle sin el fix (dos pagos y dos asientos).
    monkeypatch.setattr(qr_payment_domain, "ensure_charge_payable", lambda *a, **k: None)
    monkeypatch.setattr(qr_payment_repo, "get_active_by_charge", lambda *a, **k: None)
    monkeypatch.setattr(qr_payment_repo, "get_by_charge", lambda *a, **k: None)

    first, first_tx = _pay(session, view["qr_content"], env["link"], "qr-race-1")
    session.commit()
    assert first.status == "PAID" and first_tx.status == "SETTLED"

    with pytest.raises(qr_payment_domain.QrAlreadyPaidError):
        _pay(session, view["qr_content"], env["link"], "qr-race-2")
    session.rollback()

    assert qr_payment_domain.QrAlreadyPaidError.code == "CONFLICT"
    assert _count(session, "transactions.transactions") == 1
    assert _count(session, "wallet.qr_payments") == 1
    assert _balanced_postings(session, first_tx.id)
    assert len(_confirmed_events(session, first.id)) == 1
    assert len(_merchant_notices(session)) == 1
    balances = _balances(session, env["payer"], env["merchant_accounts"][str(merchant.id)])
    assert balances[str(env["payer"])] == (FUNDED - 5_000, 0)
    assert balances[str(env["merchant_accounts"][str(merchant.id)])] == (5_000, 0)


def test_guard_index_rejects_second_active_payment(env):
    """ALTO-2 (nivel BD): dos pagos activos del mismo cargo violan la unicidad."""
    import uuid as uuid_mod

    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant, now=NOW)
    session.commit()
    first, _ = _pay(session, view["qr_content"], env["link"], "qr-guard-idx-1")
    session.commit()
    with pytest.raises(IntegrityError):
        qr_payment_repo.create_qr_payment(
            session,
            transaction_id=uuid_mod.uuid4(),
            charge_id=first.charge_id,
            payer_user_id=USER,
            merchant_id=merchant.id,
            amount_minor=5_000,
            currency="PEN",
            status="PAID",
        )
    session.rollback()
    assert _count(session, "wallet.qr_payments") == 1


# ------------------------------------------------------- MEDIO-2: reintento tras REJECTED


def test_retry_after_rejected_settles(env):
    """MEDIO-2: `REJECTED` sin fondos no bloquea el reintento con fondos."""
    from app.modules.accounts import repository as accounts_repo

    session = env["session"]
    merchant = env["merchants"][0]
    # Monto sobre los fondos (3_000) pero dentro del base (20_000): el
    # primer intento es `REJECTED` sin fondos y el reintento liquida.
    view = _make_charge(session, merchant, amount_minor=5_000, now=NOW)
    session.commit()
    rejected, rejected_tx = _pay(session, view["qr_content"], env["low_link"], "qr-retry-1")
    session.commit()
    assert rejected_tx.status == "REJECTED" and rejected.status == "REJECTED"
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "ACTIVE"

    accounts_repo.apply_delta(
        session, env["payer_low"], available_delta=5_000, held_delta=0, currency="PEN"
    )
    session.commit()
    payment, tx = _pay(session, view["qr_content"], env["low_link"], "qr-retry-2")
    session.commit()

    assert tx.status == "SETTLED" and payment.status == "PAID"
    assert qr_repo.get_qr_charge(session, view["charge_id"]).status == "PAID"
    assert _count(session, "wallet.qr_payments") == 2  # REJECTED + PAID conviven
    assert _balanced_postings(session, tx.id)
    balances = _balances(session, env["payer_low"], env["merchant_accounts"][str(merchant.id)])
    assert balances[str(env["payer_low"])] == (FUNDED_LOW, 0)
    assert balances[str(env["merchant_accounts"][str(merchant.id)])] == (5_000, 0)
    assert len(_confirmed_events(session, payment.id)) == 1
    assert len(_merchant_notices(session)) == 1


# ------------------------------------------------------- MEDIO-3: preview


def test_preview_valid_returns_merchant_amount_without_effects(env):
    """MEDIO-3: el preview devuelve comercio/monto sin mover dinero."""
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant, now=NOW)
    session.commit()
    before_entries = _count(session, "ledger.journal_entries")
    before_balances = _balances(session, env["payer"])

    data = qr_payment_service.preview_qr_charge(
        session, qr_content=view["qr_content"], secret=SECRET, now=NOW + timedelta(seconds=5)
    )

    assert data["charge_id"] == view["charge_id"]
    assert data["merchant"]["merchant_id"] == str(merchant.id)
    assert data["merchant"]["business_name"] == merchant.business_name
    assert data["amount_minor"] == 5_000
    assert isinstance(data["amount_minor"], int)
    assert data["currency"] == "PEN"
    assert data["status"] == "ACTIVE"
    assert data["expires_at"] is not None
    blob = str(data)
    assert view["qr_content"] not in blob
    assert SECRET not in blob
    assert _count(session, "transactions.transactions") == 0
    assert _count(session, "wallet.qr_payments") == 0
    assert _count(session, "ledger.journal_entries") == before_entries
    assert _balances(session, env["payer"]) == before_balances
    assert qr_repo.get_qr_charge(session, view["charge_id"]).status == "ACTIVE"


def test_preview_rejects_expired_tampered_unknown_without_effects(env):
    """MEDIO-3: vencido/corrupto/manipulado/ajeno -> `422` sin efectos."""
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant, now=NOW)
    session.commit()
    envelope = interop.decode_payload(view["qr_content"])
    forged = interop.encode_payload(
        issuer=envelope.issuer,
        peer=envelope.peer,
        ref=envelope.ref,
        amount_minor=envelope.amount_minor,
        currency=envelope.currency,
        expires_at=envelope.expires_at,
        sig="0" * 64,
    )
    foreign = interop.encode_payload(
        issuer=envelope.issuer,
        peer=envelope.peer,
        ref=str(uuid.uuid4()),
        amount_minor=envelope.amount_minor,
        currency=envelope.currency,
        expires_at=envelope.expires_at,
        sig=envelope.sig,
    )
    with pytest.raises(qr_payment_domain.QrExpiredError):
        qr_payment_service.preview_qr_charge(
            session,
            qr_content=view["qr_content"],
            secret=SECRET,
            now=NOW + timedelta(seconds=301),
        )
    session.rollback()
    with pytest.raises(qr_payment_domain.InvalidQrError):
        qr_payment_service.preview_qr_charge(
            session, qr_content="esto-no-es-un-qr", secret=SECRET, now=NOW
        )
    session.rollback()
    with pytest.raises(qr_payment_domain.InvalidQrError):
        qr_payment_service.preview_qr_charge(session, qr_content=forged, secret=SECRET, now=NOW)
    session.rollback()
    with pytest.raises(qr_payment_domain.InvalidQrError):
        qr_payment_service.preview_qr_charge(session, qr_content=foreign, secret=SECRET, now=NOW)
    session.rollback()
    assert qr_payment_domain.QrExpiredError.code == "VALIDATION_ERROR"
    assert _count(session, "transactions.transactions") == 0
    assert _count(session, "wallet.qr_payments") == 0


def test_preview_paid_charge_conflicts(env):
    """MEDIO-3: el preview de un cargo ya cobrado es `409` (coherente con el pago)."""
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant, now=NOW)
    session.commit()
    _pay(session, view["qr_content"], env["link"], "qr-preview-paid-1")
    session.commit()
    with pytest.raises(qr_payment_domain.QrAlreadyPaidError):
        qr_payment_service.preview_qr_charge(
            session, qr_content=view["qr_content"], secret=SECRET, now=NOW
        )
    session.rollback()


def test_api_preview_contract_and_rejections(api_client, env):
    """MEDIO-3 (endpoint): `POST /payments/qr/preview` read-only con contrato."""
    session = env["session"]
    merchant = env["merchants"][0]
    # El endpoint verifica con el secreto del servidor: el cargo se firma
    # con el default (patron `E4-T12`, `secret=None`).
    view = _make_charge(session, merchant, secret=None)
    session.commit()

    ok = api_client.post("/api/v1/payments/qr/preview", json={"qr_content": view["qr_content"]})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["data"]["charge_id"] == view["charge_id"]
    assert body["data"]["merchant"]["business_name"] == merchant.business_name
    assert body["data"]["amount_minor"] == 5_000
    assert body["data"]["currency"] == "PEN"
    assert body["data"]["status"] == "ACTIVE"
    assert body["data"]["expires_at"] is not None
    assert "request_id" in body["meta"]
    assert "qr_content" not in ok.text
    assert "signature" not in ok.text
    assert _count(session, "transactions.transactions") == 0
    assert _count(session, "wallet.qr_payments") == 0

    bad = api_client.post("/api/v1/payments/qr/preview", json={"qr_content": "roto"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _count(session, "wallet.qr_payments") == 0


def test_api_preview_paid_charge_conflicts(api_client, env):
    """MEDIO-3 (endpoint): preview del cargo cobrado -> `409 CONFLICT`."""
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant, secret=None)
    session.commit()
    paid = api_client.post(
        "/api/v1/payments/qr",
        json={"qr_content": view["qr_content"], "link_id": env["link"]},
        headers={"Idempotency-Key": "qr-guard-api-paid-1"},
    )
    assert paid.status_code == 201, paid.text
    conflict = api_client.post(
        "/api/v1/payments/qr/preview", json={"qr_content": view["qr_content"]}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CONFLICT"


def test_preview_logs_without_secrets(env, caplog):
    """Sin PII/secretos en logs del preview ni del cobro con guarda."""
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant, now=NOW)
    session.commit()
    with caplog.at_level(logging.DEBUG, logger="app.modules.wallet"):
        qr_payment_service.preview_qr_charge(
            session, qr_content=view["qr_content"], secret=SECRET, now=NOW
        )
        _pay(session, view["qr_content"], env["link"], "qr-guard-nopii-1")
    session.commit()
    text = caplog.text
    assert SECRET not in text
    assert view["qr_content"] not in text
    assert PAYER_NUMBER not in text
    assert merchant.business_name not in text


# ------------------------------------------------------- migracion 0041


def test_migration_0041_chained_after_0040_with_single_head():
    """La guarda (`0041`) encadena tras `0040` (un solo head)."""
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
    # E4-T16 (2026-10-13): la cabeza avanza a 0042 (catalogo `billers`).
    # E6-T12 (2026-10-13): la cabeza avanza a 0044_notifications_preferences
    # (preferencias por canal + claves de seguridad, tras 0043). Pulido de
    # correos (2026-10-15): la cabeza avanza a
    # 0045_notification_email_content (textos de semillas, tras 0044).
    # E4-T21 (2026-10-15): la cabeza avanza a 0047_wallet_personal_qr
    # (QR personal estatico, tras 0046; la guarda se acota a
    # `reusable = false`).
    assert revisions.get("0047_wallet_personal_qr") == "0046_services_settlement"
    assert revisions.get("0046_services_settlement") == "0045_notification_email_content"
    assert revisions.get("0045_notification_email_content") == "0044_notifications_preferences"
    assert revisions.get("0042_wallet_services") == "0041_wallet_qr_payment_guard"
    assert revisions.get("0041_wallet_qr_payment_guard") == "0040_wallet_qr_payments"
    assert revisions.get("0040_wallet_qr_payments") == "0039_wallet_qr"
    referenced = {down for down in revisions.values() if down is not None}
    heads = [rev for rev in revisions if rev not in referenced]
    assert heads == ["0047_wallet_personal_qr"], f"heads inesperados: {heads}"
    content = (versions_dir / "0041_wallet_qr_payment_guard.py").read_text(encoding="utf-8")
    assert "uq_qr_payments_charge_active" in content
    assert "REJECTED" in content
    assert "def downgrade" in content


def test_pg_0041_downgrade_reversible(test_engine):
    """`upgrade -> downgrade(0040) -> upgrade(head)` con la guarda parcial."""
    import sqlalchemy as sa
    from sqlalchemy.orm import Session as SASession

    from tests.db_utils import (
        downgrade_in_process,
        resolve_test_database_url,
        run_migrations_in_process,
    )

    url = resolve_test_database_url()
    try:
        downgrade_in_process(url, "0040_wallet_qr_payments")
        with test_engine.connect() as conn:
            indexes = (
                conn.execute(
                    sa.text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname='wallet' AND tablename='qr_payments'"
                    )
                )
                .scalars()
                .all()
            )
            assert "uq_qr_payments_charge_active" not in indexes
            assert "ix_qr_payments_charge_id" in indexes
        run_migrations_in_process(url, "head")
        with test_engine.connect() as conn:
            pred = conn.execute(
                sa.text(
                    "SELECT pg_get_indexdef(indexrelid) FROM pg_index "
                    "WHERE indexrelid='wallet.uq_qr_payments_charge_active'::regclass"
                )
            ).scalar()
            assert "REJECTED" in (pred or "")
    finally:
        run_migrations_in_process(url, "head")
        session = SASession(bind=test_engine, autoflush=False, expire_on_commit=False)
        try:
            session.execute(sa.text("DELETE FROM wallet.qr_payments"))
            session.commit()
        finally:
            session.close()
