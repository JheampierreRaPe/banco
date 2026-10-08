"""Alcance de `Idempotency-Key` entre `transfer` y `authorize` (regresion causa raiz).

La UQ es global `(key, user_id)` sin `endpoint`: reutilizar la clave del
`POST /transfers/third-party` en `POST /transactions/{id}/authorize`
(cuerpo distinto) responde `409 CONFLICT` y el reto nunca se ejecuta (la tx
queda `PENDING_AUTHORIZATION` con hold `ACTIVE`). Por eso el frontend debe
usar clave derivada `<borrador>-auth` en el `authorize`.

- Misma clave transfer -> authorize: `409` + codigo `CONFLICT`.
- Clave distinta (`<K>-auth`): `200` + `SETTLED` (sin doble debito).

Dinero de juguete (UUIDs sinteticos, sin PII), SQLite en memoria + schemas
ATTACH, sin red, sin `float`. No toca migraciones ni endpoints.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
DEVICE_A = "dev-e2t99-1"

THRESHOLD_KEY = "transfer.biometric_threshold_minor"
DAILY_KEY = "transfer.daily_limit_minor"
DAILY_STANDARD_KEY = "transfer.daily_limit_STANDARD_minor"
MAX_ATTEMPTS_KEY = "auth.max_failed_attempts"

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
    "identity.users",
]


def _make_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a
    import app.modules.identity.models as _i
    import app.modules.ledger.models as _l
    import app.modules.shared.models as _s
    import app.modules.transactions.models as _t

    assert _a is not None and _i is not None and _l is not None
    assert _s is not None and _t is not None
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "ledger", "shared", "transactions", "config", "identity"):
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


@pytest.fixture()
def t99_session():
    from app.modules.accounts import repository as accounts_repo

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        a1 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="00990001111", type="AHORRO", currency="PEN"
        )
        b1 = accounts_repo.create_account(
            session, user_id=USER_B, account_number="00880003333", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a1.id, available_delta=100_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, b1.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        _seed_param(session, THRESHOLD_KEY, "2000")
        _seed_param(session, DAILY_KEY, "500000")
        _seed_param(session, DAILY_STANDARD_KEY, "500000")
        _seed_param(session, MAX_ATTEMPTS_KEY, "5", module="identity")
        session.commit()
        yield {"session": session, "a1": a1.id, "b1": b1.id}
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def hmac_identity(monkeypatch):
    from app.modules.identity.service import device_login as device_login_service

    secret = bytes.fromhex("ab" * 16)
    bindings: dict[tuple[str, str], dict] = {}

    def _fake_binding(session, user_id, device_id):
        found = bindings.get((str(user_id), device_id))
        return dict(found) if found is not None else None

    monkeypatch.setattr(device_login_service, "get_device_binding", _fake_binding)
    return {"secret": secret, "bindings": bindings}


def _add_binding(hmac_identity, user_id, device_id=DEVICE_A):
    hmac_identity["bindings"][(str(user_id), device_id)] = {
        "device_id": device_id,
        "public_key": "hmac:" + "ab" * 16,
        "status": "ACTIVE",
    }


def _sign(secret: bytes, nonce: str) -> str:
    return hmac.new(secret, nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def _nonce_of(tx_id, session) -> str:
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    tx = tx_repo.get_transaction(session, tx_id)
    return (bio_svc.get_block(tx) or {}).get("nonce") or ""


@contextlib.contextmanager
def _http(session, user_id, monkeypatch):
    from fastapi.testclient import TestClient

    from app.core.db import get_db
    from app.main import app
    from app.modules.transactions.api import get_current_user_id

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")

    def _override_db():
        yield session

    def _override_user():
        return user_id

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = _override_user
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)


def test_reusing_transfer_key_in_authorize_conflicts_and_derived_key_settles(
    t99_session, hmac_identity, monkeypatch
):
    """Misma clave transfer+authorize -> `409`; clave derivada -> `200 SETTLED`."""
    session, a1, b1 = t99_session["session"], t99_session["a1"], t99_session["b1"]
    _add_binding(hmac_identity, USER_A)
    draft_key = f"e2t99-{uuid.uuid4()}"
    derived_key = f"{draft_key}-auth"

    with _http(session, USER_A, monkeypatch) as client:
        created = client.post(
            "/api/v1/transfers/third-party",
            json={
                "source_account_id": str(a1),
                "target_account_id": str(b1),
                "amount_minor": 5_000,
                "currency": "PEN",
            },
            headers={"Idempotency-Key": draft_key},
        )
    assert created.status_code == 201, created.text
    assert created.json()["data"]["status"] == "PENDING_AUTHORIZATION"
    tx_id = created.json()["data"]["transaction_id"]
    session.expire_all()
    nonce = _nonce_of(uuid.UUID(tx_id), session)
    assert nonce, "el transfer sobre el umbral deja reto vigente"
    good = {"device_id": DEVICE_A, "signature": _sign(hmac_identity["secret"], nonce)}

    with _http(session, USER_A, monkeypatch) as client:
        same_key = client.post(
            f"/api/v1/transactions/{tx_id}/authorize",
            json=good,
            headers={"Idempotency-Key": draft_key},
        )
    # Causa raiz documentada: la UQ `(key, user_id)` no distingue endpoint,
    # asi que el cuerpo del `authorize` choca con el del `transfer`.
    assert same_key.status_code == 409, same_key.text
    assert same_key.json()["error"]["code"] == "CONFLICT"

    with _http(session, USER_A, monkeypatch) as client:
        retry = client.post(
            f"/api/v1/transactions/{tx_id}/authorize",
            json=good,
            headers={"Idempotency-Key": derived_key},
        )
    assert retry.status_code == 200, retry.text
    assert retry.json()["data"]["status"] == "SETTLED"
