"""Endpoint `POST /payments/qr` + autorizacion biometrica condicional (E4-T12, HU15).

- Camino feliz: QR vigente + monto bajo el limite base -> `201`
  `SETTLED` en un toque (cargo `PAID`, aviso unico).
- Condicional: monto sobre el base -> `201` `PENDING_AUTHORIZATION`
  + reto; tras autorizar (biometria/PIN con
  `daily_limit_reader=wallet_daily_limit_reader`, G75) liquida y el
  reintento con la misma clave finaliza el cargo (`finalize`,
  `qr_charge_paid` unico, cierra G72 lado endpoint); sin factor
  valido -> `REJECTED` sin liquidar.
- Errores: QR vencido/corrupto/manipulado/ajeno -> `422` sin mover
  dinero; ya pagado -> `409`; misma clave + mismo cuerpo -> replay;
  cuerpo distinto -> `409`; sin clave -> `422`; vinculo ajeno ->
  `404`; sin JWT -> `401`.
- Limite diario: supera -> `201` `REJECTED` +
  `DAILY_LIMIT_EXCEEDED` sin tocar saldos.
- Sin PII/RUC en respuestas/logs; montos enteros (sin `float`);
  lectura/respuesta < 2 s (CA-01).

HTTP real (TestClient) sobre SQLite en memoria + schemas ATTACH con
el motor real; auth falsa por `dependency_overrides`. Sin red.
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
from app.modules.transactions.api import get_current_user_id as tx_get_current_user_id
from app.modules.transactions.service import biometric as biometric_service
from app.modules.wallet import service as wallet_service
from app.modules.wallet.api import get_current_user_id as wallet_get_current_user_id
from app.modules.wallet.repository import qr as qr_repo
from app.modules.wallet.service import qr as qr_service

assert _accounts is not None and _audit is not None and _ledger is not None
assert _notifications is not None and _shared is not None
assert _transactions is not None and _wallet is not None

USER = uuid.uuid4()
OTHER = uuid.uuid4()

PAYER_NUMBER = "00110001111111"
PAYER_LOW_NUMBER = "00110002222222"
OTHER_NUMBER = "00110003333333"
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
def env():
    """Motor real en SQLite: pagador + comercio + vinculos."""
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
        other_link = wallet_service.link_source(
            session, OTHER, type="ACCOUNT", account_id=other_acct.id
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
            "other_link": other_link["id"],
        }
        session.rollback()
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def api_client(env, monkeypatch):
    """TestClient con BD SQLite y auth falsa como el pagador."""
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


def _act_as(user_id: uuid.UUID):
    """Cambia el usuario autenticado del TestClient (ambos routers)."""
    app.dependency_overrides[wallet_get_current_user_id] = lambda: user_id
    app.dependency_overrides[tx_get_current_user_id] = lambda: user_id


def _make_charge(session, merchant, amount_minor=5_000, **kwargs):
    return qr_service.create_qr_charge(
        session,
        merchant_id=merchant.id,
        amount_minor=amount_minor,
        currency="PEN",
        secret=None,
        **kwargs,
    )


def _post(client, qr_content, link_id, key="k-1", headers=None):
    heads = {"Idempotency-Key": key}
    if headers:
        heads.update(headers)
    return client.post(
        "/api/v1/payments/qr",
        json={"qr_content": qr_content, "link_id": str(link_id)},
        headers=heads,
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


def _payment_count(session: Session) -> int:
    return int(session.execute(sa.text("SELECT COUNT(*) FROM wallet.qr_payments")).scalar())


def _entry_count(session: Session) -> int:
    return int(session.execute(sa.text("SELECT COUNT(*) FROM ledger.journal_entries")).scalar())


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


# ------------------------------------------------------- camino feliz (un toque)


def test_post_under_base_settles_one_touch(api_client, env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    started = time.perf_counter()
    resp = _post(api_client, view["qr_content"], env["link"], key="qr-api-happy-1")
    elapsed = time.perf_counter() - started
    assert resp.status_code == 201, resp.text
    body = resp.json()
    data = body["data"]
    assert data["status"] == "SETTLED"
    assert data["amount_minor"] == 5_000
    assert isinstance(data["amount_minor"], int)
    assert data["currency"] == "PEN"
    assert data["merchant"]["merchant_id"] == view["merchant_id"]
    assert data["merchant"]["business_name"] == merchant.business_name
    assert data["charge_id"] == view["charge_id"]
    assert data["failure_code"] is None
    assert data["authorization"] is None
    assert body["meta"]["replayed"] is False
    assert "request_id" in body["meta"]
    assert elapsed < 2.0  # CA-01: lectura/validacion + respuesta < 2 s
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "PAID" and charge.paid_at is not None
    payment_id = uuid.UUID(data["qr_payment_id"])
    assert len(_confirmed_events(session, payment_id)) == 1
    assert len(_merchant_notices(session)) == 1
    balances = _balances(session, env["payer"], env["merchant_accounts"][str(merchant.id)])
    assert balances[str(env["payer"])] == (FUNDED - 5_000, 0)
    assert balances[str(env["merchant_accounts"][str(merchant.id)])] == (5_000, 0)
    assert _tx_count(session) == 1
    assert _payment_count(session) == 1


# ------------------------------------------------------- condicional (reto + biometria)


def test_post_over_base_returns_challenge(api_client, env):
    session = env["session"]
    merchant = env["merchants"][0]
    amount = EXPRESS_BASE + 1
    view = _make_charge(session, merchant, amount_minor=amount)
    session.commit()
    resp = _post(api_client, view["qr_content"], env["link"], key="qr-api-pending-1")
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "PENDING_AUTHORIZATION"
    assert data["amount_minor"] == amount
    auth = data["authorization"]
    assert auth["method"] == "BIOMETRIC"
    assert isinstance(auth["nonce"], str) and auth["nonce"]
    assert isinstance(auth["expires_at"], str) and auth["expires_at"]
    assert resp.json()["meta"]["replayed"] is False
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "ACTIVE"  # sin marcar PAID aun
    assert _merchant_notices(session) == []
    balances = _balances(session, env["payer"])
    # La reserva vive en el hold (`transactions.holds` + asiento
    # `TRANSFER_HOLD`); la proyeccion `account_balances` descuenta el
    # disponible (el `held` lo sincroniza el adaptador productivo).
    assert balances[str(env["payer"])] == (FUNDED - amount, 0)
    from app.modules.transactions import repository as tx_repository

    holds = tx_repository.list_holds_by_transaction(session, uuid.UUID(data["transaction_id"]))
    assert len(holds) == 1
    assert holds[0].status == "ACTIVE" and int(holds[0].amount_minor) == amount


def test_pending_then_authorize_finalizes_replay(api_client, env):
    session = env["session"]
    merchant = env["merchants"][0]
    amount = EXPRESS_BASE + 1
    view = _make_charge(session, merchant, amount_minor=amount)
    session.commit()
    first = _post(api_client, view["qr_content"], env["link"], key="qr-api-final-1")
    assert first.status_code == 201, first.text
    assert first.json()["data"]["status"] == "PENDING_AUTHORIZATION"
    tx_id = first.json()["data"]["transaction_id"]
    payment_id = first.json()["data"]["qr_payment_id"]

    settled = biometric_service.authorize_transaction_with_pin(
        session,
        tx_id,
        pin="1234",
        pin_verifier=lambda _s, _u, _p: _p == "1234",
        daily_limit_reader=wallet_service.wallet_daily_limit_reader,
    )
    session.commit()
    assert settled.status == "SETTLED"

    second = _post(api_client, view["qr_content"], env["link"], key="qr-api-final-1")
    assert second.status_code == 201, second.text
    data = second.json()["data"]
    assert data["status"] == "SETTLED"
    assert data["qr_payment_id"] == payment_id
    assert data["authorization"] is None
    assert second.json()["meta"]["replayed"] is True
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "PAID" and charge.paid_at is not None
    assert len(_confirmed_events(session, uuid.UUID(payment_id))) == 1
    assert len(_merchant_notices(session)) == 1  # aviso unico
    balances = _balances(session, env["payer"], env["merchant_accounts"][str(merchant.id)])
    assert balances[str(env["payer"])] == (FUNDED - amount, 0)
    assert balances[str(env["merchant_accounts"][str(merchant.id)])] == (amount, 0)

    again = wallet_service.finalize_qr_payment(
        session, qr_payment_id=payment_id, transaction=settled
    )
    session.commit()
    assert str(again.id) == payment_id
    assert len(_confirmed_events(session, uuid.UUID(payment_id))) == 1


def test_wrong_factor_blocks_without_settling(api_client, env):
    session = env["session"]
    merchant = env["merchants"][0]
    amount = EXPRESS_BASE + 1
    view = _make_charge(session, merchant, amount_minor=amount)
    session.commit()
    first = _post(api_client, view["qr_content"], env["link"], key="qr-api-block-1")
    assert first.status_code == 201, first.text
    tx_id = first.json()["data"]["transaction_id"]
    attempts = int(biometric_service.default_max_attempts_reader(session))
    assert attempts >= 2
    result = None
    for _ in range(attempts):
        try:
            result = biometric_service.authorize_transaction_with_pin(
                session,
                tx_id,
                pin="0000",
                pin_verifier=lambda _s, _u, _p: False,
                daily_limit_reader=wallet_service.wallet_daily_limit_reader,
            )
        except biometric_service.NotAuthorizedError:
            result = None
            continue
    session.commit()
    assert result is not None and result.status == "REJECTED"

    second = _post(api_client, view["qr_content"], env["link"], key="qr-api-block-1")
    assert second.status_code == 201, second.text
    data = second.json()["data"]
    assert data["status"] == "REJECTED"
    assert data["failure_code"] == "BIOMETRIC_PREVENTIVE_BLOCK"
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "ACTIVE"
    assert _merchant_notices(session) == []
    balances = _balances(session, env["payer"], env["merchant_accounts"][str(merchant.id)])
    assert balances[str(env["payer"])] == (FUNDED, 0)  # hold liberado
    assert balances[str(env["merchant_accounts"][str(merchant.id)])] == (0, 0)


def test_http_authorize_with_wrong_factor_is_rejected(api_client, env):
    """El reto se confirma por `POST /transactions/{id}/authorize` (patron E2-T19)."""
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant, amount_minor=EXPRESS_BASE + 1)
    session.commit()
    first = _post(api_client, view["qr_content"], env["link"], key="qr-api-httpauth-1")
    assert first.status_code == 201, first.text
    tx_id = first.json()["data"]["transaction_id"]
    resp = api_client.post(
        f"/api/v1/transactions/{tx_id}/authorize",
        json={"pin": "0000"},
        headers={"Idempotency-Key": "qr-api-httpauth-pin-1"},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "ACTIVE"
    assert _merchant_notices(session) == []


# ------------------------------------------------------- errores sin mover dinero


def test_expired_qr_rejected_without_moving_funds(api_client, env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    row = qr_repo.get_qr_charge(session, view["charge_id"])
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    before = _balances(session, env["payer"])
    entries = _entry_count(session)
    resp = _post(api_client, view["qr_content"], env["link"], key="qr-api-exp-1")
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _tx_count(session) == 0
    assert _payment_count(session) == 0
    assert _entry_count(session) == entries
    assert _balances(session, env["payer"]) == before


def test_corrupt_qr_rejected_without_moving_funds(api_client, env):
    session = env["session"]
    entries = _entry_count(session)
    resp = _post(api_client, "esto-no-es-un-qr", env["link"], key="qr-api-corrupt-1")
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _tx_count(session) == 0
    assert _payment_count(session) == 0
    assert _entry_count(session) == entries


def test_tampered_signature_rejected_without_moving_funds(api_client, env):
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
    before = _balances(session, env["payer"])
    resp = _post(api_client, forged, env["link"], key="qr-api-forged-1")
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _tx_count(session) == 0
    assert _payment_count(session) == 0
    assert _balances(session, env["payer"]) == before


def test_foreign_qr_rejected_without_moving_funds(api_client, env):
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
    resp = _post(api_client, foreign, env["link"], key="qr-api-foreign-1")
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _tx_count(session) == 0


def test_already_paid_conflicts(api_client, env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    first = _post(api_client, view["qr_content"], env["link"], key="qr-api-paid-1")
    assert first.status_code == 201, first.text
    assert first.json()["data"]["status"] == "SETTLED"
    second = _post(api_client, view["qr_content"], env["link"], key="qr-api-paid-2")
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "CONFLICT"
    assert _tx_count(session) == 1
    assert _payment_count(session) == 1
    assert len(_merchant_notices(session)) == 1


def test_insufficient_funds_rejected_without_moving_funds(api_client, env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant, amount_minor=50_000)
    session.commit()
    resp = _post(api_client, view["qr_content"], env["low_link"], key="qr-api-nofunds-1")
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "REJECTED"
    assert data["failure_code"] == "INSUFFICIENT_FUNDS"
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "ACTIVE"
    assert _balances(session, env["payer_low"])[str(env["payer_low"])] == (FUNDED_LOW, 0)
    assert _merchant_notices(session) == []


# ------------------------------------------------------- idempotencia


def test_idempotency_replay_and_conflict(api_client, env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    first = _post(api_client, view["qr_content"], env["link"], key="qr-api-idem-1")
    assert first.status_code == 201, first.text
    assert first.json()["meta"]["replayed"] is False
    replay = _post(api_client, view["qr_content"], env["link"], key="qr-api-idem-1")
    assert replay.status_code == 201, replay.text
    assert replay.json()["meta"]["replayed"] is True
    assert replay.json()["data"]["qr_payment_id"] == first.json()["data"]["qr_payment_id"]
    assert replay.json()["data"]["transaction_id"] == first.json()["data"]["transaction_id"]
    assert _tx_count(session) == 1
    assert _payment_count(session) == 1
    assert len(_merchant_notices(session)) == 1
    conflict = _post(api_client, view["qr_content"], env["low_link"], key="qr-api-idem-1")
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "CONFLICT"


def test_missing_idempotency_key_is_rejected(api_client, env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    resp = api_client.post(
        "/api/v1/payments/qr",
        json={"qr_content": view["qr_content"], "link_id": str(env["link"])},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _tx_count(session) == 0


def test_invalid_body_is_rejected(api_client, env):
    resp = api_client.post(
        "/api/v1/payments/qr",
        json={"link_id": str(uuid.uuid4())},
        headers={"Idempotency-Key": "qr-api-badbody-1"},
    )
    assert resp.status_code == 422, resp.text
    resp = api_client.post(
        "/api/v1/payments/qr",
        json={"qr_content": "x", "link_id": "no-es-uuid"},
        headers={"Idempotency-Key": "qr-api-badbody-2"},
    )
    assert resp.status_code == 422, resp.text


# ------------------------------------------------------- aislamiento del titular


def test_foreign_link_is_neutral(api_client, env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    resp = _post(api_client, view["qr_content"], env["other_link"], key="qr-api-alien-1")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert _tx_count(session) == 0
    assert _payment_count(session) == 0


def test_no_auth_is_rejected(api_client, env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    app.dependency_overrides.pop(wallet_get_current_user_id, None)
    try:
        resp = api_client.post(
            "/api/v1/payments/qr",
            json={"qr_content": view["qr_content"], "link_id": str(env["link"])},
            headers={"Idempotency-Key": "qr-api-noauth-1"},
        )
    finally:
        app.dependency_overrides[wallet_get_current_user_id] = lambda: USER
    assert resp.status_code == 401, resp.text
    assert _tx_count(session) == 0


# ------------------------------------------------------- limite diario


def test_daily_limit_rejected_without_moving_funds(api_client, env):
    session = env["session"]
    merchant = env["merchants"][0]
    params.set_parameter(session, key=params.WALLET_DAILY_LIMIT_MINOR_KEY, value=10_000)
    session.commit()
    first_view = _make_charge(session, merchant, amount_minor=5_000)
    session.commit()
    first = _post(api_client, first_view["qr_content"], env["link"], key="qr-api-daily-1")
    assert first.status_code == 201, first.text
    assert first.json()["data"]["status"] == "SETTLED"
    entries = _entry_count(session)
    second_view = _make_charge(session, merchant, amount_minor=6_000)
    session.commit()
    second = _post(api_client, second_view["qr_content"], env["link"], key="qr-api-daily-2")
    assert second.status_code == 201, second.text
    data = second.json()["data"]
    assert data["status"] == "REJECTED"
    assert data["failure_code"] == "DAILY_LIMIT_EXCEEDED"
    charge = qr_repo.get_qr_charge(session, second_view["charge_id"])
    assert charge.status == "ACTIVE"
    balances = _balances(session, env["payer"], env["merchant_accounts"][str(merchant.id)])
    assert balances[str(env["payer"])] == (FUNDED - 5_000, 0)
    assert balances[str(env["merchant_accounts"][str(merchant.id)])] == (5_000, 0)
    assert _entry_count(session) == entries


# ------------------------------------------------------- contrato, PII y frontera


def test_response_has_no_pii_and_integer_amounts(api_client, env, caplog):
    session = env["session"]
    merchant = env["merchants"][0]
    view = _make_charge(session, merchant)
    session.commit()
    with caplog.at_level(logging.DEBUG, logger="app.modules.wallet"):
        resp = _post(api_client, view["qr_content"], env["link"], key="qr-api-nopii-1")
    assert resp.status_code == 201, resp.text
    text = resp.text
    assert "payload_hash" not in text
    assert "qr_content" not in text
    assert "signature" not in text
    assert PAYER_NUMBER not in text
    assert "20123456789" not in text
    lowered = text.lower()
    assert "ruc" not in lowered
    assert "secret" not in lowered
    data = resp.json()["data"]
    assert isinstance(data["amount_minor"], int) and not isinstance(data["amount_minor"], bool)
    assert isinstance(data["merchant"]["business_name"], str)
    logs = caplog.text
    assert view["qr_content"] not in logs
    assert PAYER_NUMBER not in logs


def test_openapi_contract_lists_payments_qr(api_client):
    resp = api_client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()["paths"].get("/api/v1/payments/qr")
    assert spec is not None and "post" in spec
    post = spec["post"]
    assert post["responses"].get("201") is not None
    assert post["responses"].get("409") is not None
    assert post["responses"].get("422") is not None
    schema = resp.json()["components"]["schemas"]["QrPayRequest"]
    assert set(schema["required"]) == {"qr_content", "link_id"}


def test_wallet_payment_files_never_touch_foreign_tables():
    import pathlib

    module_dir = pathlib.Path(__file__).resolve().parents[1] / "app" / "modules" / "wallet"
    forbidden = (
        "transactions.repository",
        "transactions.models",
        "accounts.models",
        "accounts.repository",
        "ledger.repository",
        "ledger.models",
        "ledger.service",
        "wallet_interop",
        "domain.qr_payment",
        "domain import qr",
    )
    for name in ("api/payments.py", "schemas/payment.py"):
        content = (module_dir / name).read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in content, f"{name} toca frontera ajena: {marker}"


def test_migration_head_still_single_0040():
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
        revisions[rev.group(1)] = down.group(1) if down else None
    assert revisions.get("0040_wallet_qr_payments") == "0039_wallet_qr"
    # Head vigente tras la guarda de doble cobro (`0041`, fix ALTO-2)
    # + el catalogo de servicios (`0042`, E4-T16) + las preferencias
    # de notificaciones (`0044`, E6-T12).
    assert revisions.get("0042_wallet_services") == "0041_wallet_qr_payment_guard"
    assert revisions.get("0041_wallet_qr_payment_guard") == "0040_wallet_qr_payments"
    referenced = {down for down in revisions.values() if down is not None}
    heads = [rev for rev in revisions if rev not in referenced]
    assert heads == ["0044_notifications_preferences"], f"heads inesperados: {heads}"
