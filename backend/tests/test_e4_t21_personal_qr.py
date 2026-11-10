"""QR personal estatico (E4-T21, HU14).

- Camino feliz: titular sin rol comercial genera su QR personal
  (`POST /wallet/qr` -> `201`, sin monto ni caducidad, reutilizable)
  con `qr_content` (`BO-QR1`) escaneable y verificable; el pagador
  escanea, digita el monto y paga (`POST /payments/qr`); el cobro se
  contabiliza por el motor (`docs/04#7.4`) y abona a la cuenta del
  titular personal; aviso unico (`qr_charge_paid` reutilizada).
- Reutilizable: varios cobros del mismo QR coexisten; el cargo sigue
  `ACTIVE` (no se agota); sin caducidad (`expires_at = NULL`, el job
  no lo vence).
- Errores: pago sin `amount_minor` sobre QR sin monto -> `422`; QR
  ajeno/de comercio -> `404` neutro; titular sin cuenta -> `422`;
  generar no exige comercio (nunca `403`).
- Concurrencia/idempotencia: misma clave + mismo cuerpo = replay (un
  solo cobro); misma clave + distinto monto = `409`; dos claves
  distintas = dos cobros; el QR dinamico de comercio conserva el
  `409` al segundo cobro.
- Migracion: `0047` encadena tras `0046` (un solo head),
  reversible, `amount_minor`/`merchant_id` nullable + `CHECK`
  relajados, indice `uq_qr_payments_charge_active` acotado a
  `reusable = false`.

HTTP real (TestClient) sobre SQLite en memoria + schemas ATTACH con
el motor real; auth falsa por `dependency_overrides`. Sin red.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

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
from app.modules.wallet import service as wallet_service
from app.modules.wallet.api import get_current_user_id as wallet_get_current_user_id
from app.modules.wallet.domain import qr as qr_domain
from app.modules.wallet.domain import qr_payment as qr_payment_domain
from app.modules.wallet.repository import qr as qr_repo

assert _accounts is not None and _audit is not None and _ledger is not None
assert _notifications is not None and _shared is not None
assert _transactions is not None and _wallet is not None

OWNER = uuid.uuid4()
OTHER = uuid.uuid4()
PAYER = uuid.uuid4()

OWNER_NUMBER = "00330001111111"
OTHER_NUMBER = "00330002222222"
PAYER_NUMBER = "00330003333333"
MERCHANT_NUMBER = "00330004444444"

FUNDED = 100_000
AMOUNT = 5_000

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
    """Motor real en SQLite: dueno + pagador + comercio + vinculos."""
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
        owner_acct = accounts_repo.create_account(
            session, user_id=OWNER, account_number=OWNER_NUMBER, type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, owner_acct.id, available_delta=FUNDED, held_delta=0, currency="PEN"
        )
        other_acct = accounts_repo.create_account(
            session, user_id=OTHER, account_number=OTHER_NUMBER, type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, other_acct.id, available_delta=FUNDED, held_delta=0, currency="PEN"
        )
        payer_acct = accounts_repo.create_account(
            session, user_id=PAYER, account_number=PAYER_NUMBER, type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, payer_acct.id, available_delta=FUNDED, held_delta=0, currency="PEN"
        )
        merchant = merchants[0]
        merchant_acct = accounts_repo.create_account(
            session,
            user_id=merchant.user_id,
            account_number=MERCHANT_NUMBER,
            type="AHORRO",
            currency="PEN",
        )
        accounts_repo.apply_delta(
            session, merchant_acct.id, available_delta=0, held_delta=0, currency="PEN"
        )
        payer_link = wallet_service.link_source(
            session, PAYER, type="ACCOUNT", account_id=payer_acct.id
        )
        session.commit()
        yield {
            "session": session,
            "merchants": merchants,
            "merchant": merchant,
            "merchant_account": merchant_acct.id,
            "owner_account": owner_acct.id,
            "other_account": other_acct.id,
            "payer_account": payer_acct.id,
            "payer_link": payer_link["id"],
        }
        session.rollback()
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def api_client(env, monkeypatch):
    """TestClient con BD SQLite y auth falsa (el usuario se cambia por test)."""
    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    session = env["session"]

    def _override_db():
        yield session

    def _override_user():
        return OWNER

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


def _balances(session: Session, *account_ids: uuid.UUID) -> dict[str, tuple[int, int]]:
    from app.modules.accounts.models import AccountBalance

    out = {}
    for aid in account_ids:
        row = session.scalars(
            sa.select(AccountBalance).where(AccountBalance.account_id == aid)
        ).one()
        out[str(aid)] = (int(row.available_minor), int(row.held_minor))
    return out


def _confirmed_events(session: Session, payment_id: uuid.UUID) -> list:
    from app.modules.shared.models import OutboxEntry

    return session.scalars(
        sa.select(OutboxEntry).where(
            OutboxEntry.event_type == "qr.payment.confirmed",
            OutboxEntry.aggregate_id == payment_id,
        )
    ).all()


def _personal_notices(session: Session, owner: uuid.UUID) -> list:
    from app.modules.notifications.models import Notification

    return session.scalars(
        sa.select(Notification).where(
            Notification.template_code == "qr_charge_paid",
            Notification.user_id == owner,
        )
    ).all()


def _payment_count(session: Session) -> int:
    return int(session.execute(sa.text("SELECT COUNT(*) FROM wallet.qr_payments")).scalar())


def _make_personal(session: Session, owner: uuid.UUID = OWNER, **kwargs):
    view = wallet_service.create_personal_qr_charge(
        session, owner_user_id=owner, currency="PEN", secret=None, **kwargs
    )
    session.commit()
    return view


# ------------------------------------------------------- generacion (CA-01/CA-02)


def test_owner_without_merchant_role_generates_personal_qr(api_client, env):
    """CA-01: sin rol comercial -> `201` (nunca `403`); estatico CA-02."""
    resp = api_client.post("/api/v1/wallet/qr", json={"currency": "PEN"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    data = body["data"]
    assert data["mode"] == "STATIC"
    assert data["amount_minor"] is None
    assert data["expires_at"] is None
    assert data["status"] == "ACTIVE"
    assert data["payee_type"] == "PERSONAL"
    assert data["owner_user_id"] == str(OWNER)
    assert data["payee_account_id"] == str(env["owner_account"])
    assert data["reusable"] is True
    assert isinstance(data["qr_content"], str) and data["qr_content"]
    assert len(data["payload_hash"]) == 64
    assert body["meta"]["replayed"] is False
    row = qr_repo.get_qr_charge(env["session"], data["charge_id"])
    assert row is not None and row.merchant_id is None and row.amount_minor is None
    assert row.expires_at is None and row.reusable is True


def test_qr_content_scannable_and_verifiable(api_client, env):
    """CA-01: el `qr_content` (`BO-QR1`) se decodifica y verifica en local."""
    session = env["session"]
    view = _make_personal(session)
    envelope = interop.decode_payload(view["qr_content"])
    assert envelope.ref == view["charge_id"]
    assert envelope.amount_minor == 0  # "lo define el pagador"
    assert envelope.currency == "PEN" and envelope.expires_at == 0
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    payload = qr_payment_domain.expected_payload_for_charge(charge)
    assert payload["amount_minor"] is None
    assert payload["owner_user_id"] == str(OWNER)
    assert qr_payment_domain.verify_charge_integrity(charge, envelope) == payload
    assert qr_domain.verify_signature(payload, charge.signature)


def test_get_personal_status_owner_only(api_client, env):
    """Solo el dueno ve su QR; ajeno o de comercio -> `404` neutro."""
    view = _make_personal(env["session"])
    ok = api_client.get(f"/api/v1/wallet/qr/{view['charge_id']}")
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["status"] == "ACTIVE"
    _act_as(OTHER)
    foreign = api_client.get(f"/api/v1/wallet/qr/{view['charge_id']}")
    assert foreign.status_code == 404
    assert "amount_minor" not in foreign.text
    merchant_view = wallet_service.create_qr_charge(
        env["session"],
        merchant_id=env["merchant"].id,
        amount_minor=AMOUNT,
        currency="PEN",
        secret=None,
    )
    env["session"].commit()
    _act_as(OWNER)
    mismatch = api_client.get(f"/api/v1/wallet/qr/{merchant_view['charge_id']}")
    assert mismatch.status_code == 404


def test_generate_idempotent_replay_and_conflict(api_client, env):
    """`Idempotency-Key` opcional: replay con mismo cuerpo, `409` con otro."""
    first = api_client.post(
        "/api/v1/wallet/qr", json={"currency": "PEN"}, headers={"Idempotency-Key": "pq-1"}
    )
    assert first.status_code == 201, first.text
    assert first.json()["meta"]["replayed"] is False
    replay = api_client.post(
        "/api/v1/wallet/qr", json={"currency": "PEN"}, headers={"Idempotency-Key": "pq-1"}
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["meta"]["replayed"] is True
    assert replay.json()["data"]["charge_id"] == first.json()["data"]["charge_id"]
    conflict = api_client.post(
        "/api/v1/wallet/qr",
        json={"currency": "PEN", "concept": "otro"},
        headers={"Idempotency-Key": "pq-1"},
    )
    assert conflict.status_code == 409


def test_owner_without_account_is_rejected(api_client, env):
    """Sin cuenta `ACTIVE` no hay QR personal (`422`, antes de persistir)."""
    nobody = uuid.uuid4()
    _act_as(nobody)
    resp = api_client.post("/api/v1/wallet/qr", json={"currency": "PEN"})
    assert resp.status_code == 422, resp.text


# ------------------------------------------------------- pago con monto del pagador (CA-03)


def test_preview_personal_has_no_amount(api_client, env):
    """El preview devuelve `amount_minor: null` + destino personal."""
    view = _make_personal(env["session"])
    resp = api_client.post("/api/v1/payments/qr/preview", json={"qr_content": view["qr_content"]})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["amount_minor"] is None
    assert data["merchant"] is None
    assert data["payee"]["owner_user_id"] == str(OWNER)
    assert data["status"] == "ACTIVE"


def test_pay_without_amount_is_422(api_client, env):
    """Sin `amount_minor` sobre un QR sin monto -> `422` sin mover dinero."""
    session = env["session"]
    view = _make_personal(session)
    _act_as(PAYER)
    before = _balances(session, env["payer_account"], env["owner_account"])
    resp = api_client.post(
        "/api/v1/payments/qr",
        json={"qr_content": view["qr_content"], "link_id": str(env["payer_link"])},
        headers={"Idempotency-Key": "pq-pay-no-amount"},
    )
    assert resp.status_code == 422, resp.text
    assert _balances(session, env["payer_account"], env["owner_account"]) == before
    assert _payment_count(session) == 0


def test_pay_personal_settles_and_credits_owner(api_client, env):
    """CA-03: el pagador digita el monto; el motor abona al titular; aviso unico."""
    session = env["session"]
    view = _make_personal(session)
    _act_as(PAYER)
    resp = api_client.post(
        "/api/v1/payments/qr",
        json={
            "qr_content": view["qr_content"],
            "link_id": str(env["payer_link"]),
            "amount_minor": AMOUNT,
        },
        headers={"Idempotency-Key": "pq-pay-happy-1"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "SETTLED"
    assert data["amount_minor"] == AMOUNT
    assert data["merchant"] is None
    assert data["payee"]["owner_user_id"] == str(OWNER)
    assert resp.json()["meta"]["replayed"] is False
    balances = _balances(session, env["payer_account"], env["owner_account"])
    assert balances[str(env["payer_account"])] == (FUNDED - AMOUNT, 0)
    assert balances[str(env["owner_account"])] == (FUNDED + AMOUNT, 0)
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "ACTIVE" and charge.paid_at is None  # no se agota
    payment_id = uuid.UUID(data["qr_payment_id"])
    assert len(_confirmed_events(session, payment_id)) == 1
    assert len(_personal_notices(session, OWNER)) == 1


# ------------------------------------------------------- reutilizable (CA-05)


def test_personal_qr_accepts_several_charges(api_client, env):
    """CA-05: dos cobros validos del mismo QR coexisten (distintas claves)."""
    session = env["session"]
    view = _make_personal(session)
    _act_as(PAYER)
    for n, key in enumerate(("pq-reuse-1", "pq-reuse-2"), start=1):
        resp = api_client.post(
            "/api/v1/payments/qr",
            json={
                "qr_content": view["qr_content"],
                "link_id": str(env["payer_link"]),
                "amount_minor": AMOUNT,
            },
            headers={"Idempotency-Key": key},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["status"] == "SETTLED"
    assert _payment_count(session) == 2
    balances = _balances(session, env["payer_account"], env["owner_account"])
    assert balances[str(env["payer_account"])] == (FUNDED - 2 * AMOUNT, 0)
    assert balances[str(env["owner_account"])] == (FUNDED + 2 * AMOUNT, 0)
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    assert charge.status == "ACTIVE"  # reutilizable: nunca se marca PAID
    assert len(_personal_notices(session, OWNER)) == 2


def test_same_key_replays_single_charge(api_client, env):
    """Misma clave + mismo cuerpo = replay (un solo cobro); otro monto = `409`."""
    session = env["session"]
    view = _make_personal(session)
    _act_as(PAYER)
    body = {
        "qr_content": view["qr_content"],
        "link_id": str(env["payer_link"]),
        "amount_minor": AMOUNT,
    }
    first = api_client.post(
        "/api/v1/payments/qr", json=body, headers={"Idempotency-Key": "pq-replay-1"}
    )
    assert first.status_code == 201, first.text
    replay = api_client.post(
        "/api/v1/payments/qr", json=body, headers={"Idempotency-Key": "pq-replay-1"}
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["meta"]["replayed"] is True
    assert replay.json()["data"]["qr_payment_id"] == first.json()["data"]["qr_payment_id"]
    assert _payment_count(session) == 1
    other_amount = dict(body, amount_minor=AMOUNT + 100)
    conflict = api_client.post(
        "/api/v1/payments/qr", json=other_amount, headers={"Idempotency-Key": "pq-replay-1"}
    )
    assert conflict.status_code == 409


def test_merchant_dynamic_keeps_single_charge_guard(api_client, env):
    """CA-05: el QR dinamico de comercio conserva el `409` al segundo cobro."""
    session = env["session"]
    merchant_view = wallet_service.create_qr_charge(
        session,
        merchant_id=env["merchant"].id,
        amount_minor=AMOUNT,
        currency="PEN",
        secret=None,
    )
    session.commit()
    _act_as(PAYER)
    first = api_client.post(
        "/api/v1/payments/qr",
        json={"qr_content": merchant_view["qr_content"], "link_id": str(env["payer_link"])},
        headers={"Idempotency-Key": "pq-guard-1"},
    )
    assert first.status_code == 201, first.text
    assert first.json()["data"]["status"] == "SETTLED"
    second = api_client.post(
        "/api/v1/payments/qr",
        json={"qr_content": merchant_view["qr_content"], "link_id": str(env["payer_link"])},
        headers={"Idempotency-Key": "pq-guard-2"},
    )
    assert second.status_code == 409, second.text
    assert _payment_count(session) == 1


def test_personal_never_expires_and_job_skips_it(api_client, env):
    """Sin caducidad: `expires_at = NULL`, valido siempre; el job no lo vence."""
    from app.modules.wallet.jobs import qr_expiry as qr_expiry_job

    session = env["session"]
    view = _make_personal(session)
    charge = qr_repo.get_qr_charge(session, view["charge_id"])
    far = datetime.now(UTC).replace(year=2099)
    assert wallet_service.is_charge_valid(charge, far) is True
    outcome = qr_expiry_job.run_qr_expiry_once(session, now=far)
    session.commit()
    assert outcome.get("expired", []) == []
    assert qr_repo.get_qr_charge(session, view["charge_id"]).status == "ACTIVE"


def test_personal_qr_without_pii_in_responses(api_client, env):
    """Sin PII: ni RUC ni cuentas en claro en generar/preview/pagar."""
    session = env["session"]
    view = _make_personal(session)
    assert OWNER_NUMBER not in view["qr_content"]
    preview = api_client.post(
        "/api/v1/payments/qr/preview", json={"qr_content": view["qr_content"]}
    )
    assert preview.status_code == 200, preview.text
    assert OWNER_NUMBER not in preview.text
    _act_as(PAYER)
    paid = api_client.post(
        "/api/v1/payments/qr",
        json={
            "qr_content": view["qr_content"],
            "link_id": str(env["payer_link"]),
            "amount_minor": AMOUNT,
        },
        headers={"Idempotency-Key": "pq-pii-1"},
    )
    assert paid.status_code == 201, paid.text
    assert OWNER_NUMBER not in paid.text and PAYER_NUMBER not in paid.text


# ------------------------------------------------------- migracion 0047


def test_migration_0047_chained_after_0046_with_single_head():
    """`0047` encadena tras `0046` (un solo head), con la guarda acotada."""
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
    assert revisions.get("0047_wallet_personal_qr") == "0046_services_settlement"
    assert revisions.get("0046_services_settlement") == "0045_notification_email_content"
    referenced = {down for down in revisions.values() if down is not None}
    heads = [rev for rev in revisions if rev not in referenced]
    assert heads == ["0047_wallet_personal_qr"], f"heads inesperados: {heads}"
    content = (versions_dir / "0047_wallet_personal_qr.py").read_text(encoding="utf-8")
    assert "payee_type" in content and "owner_user_id" in content
    assert "payee_account_id" in content and "reusable" in content
    assert "amount_minor IS NULL OR amount_minor > 0" in content
    assert "reusable = false" in content
    assert "def upgrade" in content and "def downgrade" in content
