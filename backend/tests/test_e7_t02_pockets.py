"""Bolsillos de ahorro `fx` (E7-T02 corte 2, HU24 CA-01..CA-04).

Cubre: apertura `PEN`/`USD`/`EUR` (cuenta `POCKET` por la fachada de
`accounts` + fila `fx.savings_pockets`), listar, detalle, ajeno ->
`404` neutro, duplicado exacto sin clave -> `409`, idempotencia con
misma clave (replay sin duplicar) y con cuerpo distinto (`409`), RBAC
(`401` sin auth), moneda/meta/nombre invalidos (`422`), sin
asientos/saldos (no mueve dinero), evento `pocket.created` emitido por
`outbox` + auditoria best-effort, y frontera (`fx` no importa modelos
de `accounts`/`transactions`/`ledger`: solo la fachada por import
perezoso).

SQLite en memoria con schemas ATTACH (`accounts`, `ledger`, `shared`,
`transactions`, `fx`, `audit`); `get_db` y `get_current_user_id` de
`fx.api` se inyectan via `app.dependency_overrides` (auth falsa).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base, get_db
from app.main import app
from app.modules.fx.api import get_current_user_id

BACKEND_DIR = Path(__file__).resolve().parents[1]

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()

CURRENT = {"user": USER_A}


@pytest.fixture()
def pocket_client():
    """TestClient con BD SQLite aislada y auth falsa conmutabe (A/B)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a
    import app.modules.audit.models as _au
    import app.modules.fx.models as _fx
    import app.modules.ledger.models as _l
    import app.modules.shared.models as _s
    import app.modules.transactions.models as _t

    assert _a is not None and _au is not None and _fx is not None
    assert _l is not None and _s is not None and _t is not None

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "ledger", "shared", "transactions", "fx", "audit"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["accounts.accounts"],
            Base.metadata.tables["accounts.account_balances"],
            Base.metadata.tables["accounts.daily_balance_snapshots"],
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["ledger.journal_entries"],
            Base.metadata.tables["ledger.postings"],
            Base.metadata.tables["ledger.ledger_balances"],
            Base.metadata.tables["fx.savings_pockets"],
            Base.metadata.tables["fx.pocket_movements"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["shared.idempotency_keys"],
            Base.metadata.tables["audit.audit_log"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    CURRENT["user"] = USER_A
    try:

        def _override_db():
            yield session

        def _override_user():
            return CURRENT["user"]

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_user_id] = _override_user
        with TestClient(app) as client:
            yield {"client": client, "session": session}
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        session.close()
        engine.dispose()


def _count(session: Session, table: str) -> int:
    return int(session.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[table])))


def _open(client: TestClient, body: dict, key: str | None = None):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post("/api/v1/pockets", json=body, headers=headers)


# ------------------------------------------------- apertura por moneda (CA-01)
@pytest.mark.parametrize("currency", ["PEN", "USD", "EUR"])
def test_open_pocket_per_currency_creates_account_and_row(pocket_client, currency: str):
    client, session = pocket_client["client"], pocket_client["session"]
    resp = _open(client, {"name": f"Meta {currency}", "currency": currency})
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert resp.json()["meta"] == {"replayed": False}
    assert uuid.UUID(data["pocket_id"])
    assert uuid.UUID(data["account_id"])
    assert data["name"] == f"Meta {currency}"
    assert data["currency"] == currency
    assert data["target_minor"] is None
    assert data["balance_minor"] == 0
    assert data["roundup_enabled"] is False
    assert data["status"] == "ACTIVE"

    # La cuenta `POCKET` la creo `accounts` (misma moneda, balance cero).
    from app.modules.accounts import repository as accounts_repo

    account = accounts_repo.get_account(session, data["account_id"])
    assert account is not None
    assert account.user_id == USER_A
    assert account.type == "POCKET"
    assert account.currency == currency
    assert account.status == "ACTIVE"
    balance = accounts_repo.get_balance(session, data["account_id"])
    assert (balance.available_minor, balance.held_minor) == (0, 0)

    # Aparece en `GET /pockets` con saldo 0 (CA-01).
    listed = client.get("/api/v1/pockets")
    assert listed.status_code == 200
    assert listed.json()["meta"] == {"total": 1}
    assert listed.json()["data"][0]["pocket_id"] == data["pocket_id"]


def test_open_with_target_roundup_and_yield(pocket_client):
    client = pocket_client["client"]
    resp = _open(
        client,
        {
            "name": "Viaje",
            "currency": "USD",
            "target_minor": 100000,
            "roundup_enabled": True,
            "yield_rate": "0.03",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["target_minor"] == 100000
    assert data["roundup_enabled"] is True
    assert float(data["yield_rate"]) == pytest.approx(0.03)


def test_detail_returns_pocket(pocket_client):
    client = pocket_client["client"]
    pocket_id = _open(client, {"name": "Fondo", "currency": "PEN"}).json()["data"]["pocket_id"]
    resp = client.get(f"/api/v1/pockets/{pocket_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["pocket_id"] == pocket_id


# ------------------------------------------------- RBAC titular (CA-03)
def test_foreign_and_missing_pocket_not_found(pocket_client):
    client = pocket_client["client"]
    pocket_id = _open(client, {"name": "Mio", "currency": "PEN"}).json()["data"]["pocket_id"]

    CURRENT["user"] = USER_B
    try:
        assert client.get(f"/api/v1/pockets/{pocket_id}").status_code == 404
        listed = client.get("/api/v1/pockets")
        assert listed.status_code == 200
        assert listed.json() == {"data": [], "meta": {"total": 0}}
    finally:
        CURRENT["user"] = USER_A
    assert client.get(f"/api/v1/pockets/{uuid.uuid4()}").status_code == 404


def test_missing_auth_unauthorized(pocket_client):
    client = pocket_client["client"]
    app.dependency_overrides.pop(get_current_user_id, None)
    try:
        assert client.get("/api/v1/pockets").status_code == 401
        assert _open(client, {"name": "X", "currency": "PEN"}).status_code == 401
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: CURRENT["user"]


# ------------------------------------------------- duplicados e idempotencia (CA-04)
def test_exact_duplicate_without_key_conflicts(pocket_client):
    client, session = pocket_client["client"], pocket_client["session"]
    assert _open(client, {"name": "Ahorro", "currency": "PEN"}).status_code == 201
    dup = _open(client, {"name": "Ahorro", "currency": "PEN"})
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "CONFLICT"
    assert _count(session, "fx.savings_pockets") == 1

    # Mismo nombre + otra moneda, u otro nombre + misma moneda: no duplica.
    assert _open(client, {"name": "Ahorro", "currency": "USD"}).status_code == 201
    assert _open(client, {"name": "Otro", "currency": "PEN"}).status_code == 201
    assert _count(session, "fx.savings_pockets") == 3


def test_same_key_same_body_replays_without_duplicating(pocket_client):
    client, session = pocket_client["client"], pocket_client["session"]
    body = {"name": "Viaje", "currency": "EUR", "target_minor": 50000}
    first = _open(client, body, key="clave-1")
    assert first.status_code == 201
    assert first.json()["meta"] == {"replayed": False}
    second = _open(client, body, key="clave-1")
    assert second.status_code == 201
    assert second.json()["meta"] == {"replayed": True}
    assert second.json()["data"]["pocket_id"] == first.json()["data"]["pocket_id"]
    assert _count(session, "fx.savings_pockets") == 1


def test_same_key_different_body_conflicts(pocket_client):
    client, session = pocket_client["client"], pocket_client["session"]
    assert _open(client, {"name": "A", "currency": "PEN"}, key="clave-2").status_code == 201
    conflict = _open(client, {"name": "B", "currency": "PEN"}, key="clave-2")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CONFLICT"
    assert _count(session, "fx.savings_pockets") == 1


def test_closed_pocket_name_can_be_reopened(pocket_client):
    client, session = pocket_client["client"], pocket_client["session"]
    pocket_id = _open(client, {"name": "Ciclo", "currency": "PEN"}).json()["data"]["pocket_id"]
    from app.modules.fx.models import SavingsPocket

    pocket = session.get(SavingsPocket, uuid.UUID(pocket_id))
    assert pocket is not None
    pocket.status = "CLOSED"
    session.commit()
    assert _open(client, {"name": "Ciclo", "currency": "PEN"}).status_code == 201


# ------------------------------------------------- validacion (CA-03)
@pytest.mark.parametrize("currency", ["GBP", "USDD", "P3N", "PE", "", "US1"])
def test_invalid_currency_rejected(pocket_client, currency: str):
    resp = _open(pocket_client["client"], {"name": "X", "currency": currency})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "currency": "PEN"},
        {"name": "   ", "currency": "PEN"},
        {"name": "X", "currency": "PEN", "target_minor": -1},
        {"name": "X", "currency": "PEN", "yield_rate": "-0.01"},
        {"name": "X", "currency": "PEN", "roundup_enabled": "si"},
        {"name": "X", "currency": "USD", "target_minor": 10.5},
        {"name": "X" * 81, "currency": "PEN"},
        {"currency": "PEN"},
    ],
)
def test_invalid_pocket_body_rejected(pocket_client, body: dict):
    resp = _open(pocket_client["client"], body)
    assert resp.status_code == 422, resp.text


# ------------------------------------------------- no mueve dinero (CA-04)
def test_open_moves_no_money(pocket_client):
    client, session = pocket_client["client"], pocket_client["session"]
    account_id = _open(client, {"name": "Fondo", "currency": "PEN"}).json()["data"]["account_id"]
    assert _count(session, "ledger.journal_entries") == 0
    assert _count(session, "ledger.postings") == 0
    assert _count(session, "fx.pocket_movements") == 0
    from app.modules.accounts import repository as accounts_repo

    balance = accounts_repo.get_balance(session, account_id)
    assert (balance.available_minor, balance.held_minor) == (0, 0)


# ------------------------------------------------- eventos y auditoria
def test_pocket_created_event_emitted_without_pii(pocket_client):
    client, session = pocket_client["client"], pocket_client["session"]
    pocket_id = _open(client, {"name": "Secreto", "currency": "USD"}).json()["data"]["pocket_id"]
    from app.modules.shared.models import OutboxEntry

    rows = session.scalars(
        sa.select(OutboxEntry).where(OutboxEntry.event_type == "pocket.created")
    ).all()
    assert len(rows) == 1
    entry = rows[0]
    assert entry.status == "PENDING"
    assert entry.payload["pocket_id"] == pocket_id
    assert entry.payload["user_id"] == str(USER_A)
    assert entry.payload["currency"] == "USD"
    assert entry.payload["status"] == "ACTIVE"
    assert uuid.UUID(entry.payload["account_id"])
    assert set(entry.payload) == {"pocket_id", "account_id", "user_id", "currency", "status"}


def test_audit_best_effort_recorded(pocket_client):
    client, session = pocket_client["client"], pocket_client["session"]
    pocket_id = _open(client, {"name": "Audit", "currency": "PEN"}).json()["data"]["pocket_id"]
    from app.modules.audit.models import AuditLog

    rows = session.scalars(sa.select(AuditLog).where(AuditLog.action == "fx.pocket.opened")).all()
    assert len(rows) == 1
    assert str(rows[0].entity_id) == pocket_id
    assert rows[0].entity_type == "savings_pocket"
    assert str(rows[0].actor_id) == str(USER_A)


# ------------------------------------------------- frontera (CA-02)
def test_frontera_fx_no_importa_modelos_ajenos():
    """`fx` no importa modelos/tablas de otros modulos (regla de oro 4).

    Unica arista permitida: la fachada `app.modules.accounts.service`
    (import perezoso dentro de `open_pocket`) + `shared`/`audit`/`core`
    transversales. Jamas `transactions`/`ledger` ni modelos de `accounts`.
    """
    for rel in (
        "app/modules/fx/service/__init__.py",
        "app/modules/fx/api/__init__.py",
        "app/modules/fx/schemas/__init__.py",
    ):
        text = (BACKEND_DIR / rel).read_text(encoding="utf-8")
        for forbidden in (
            "app.modules.accounts.models",
            "app.modules.accounts.repository",
            "app.modules.transactions",
            "app.modules.ledger",
            "post_entry",
        ):
            assert forbidden not in text, f"frontera rota en {rel}: {forbidden}"
    service_text = (BACKEND_DIR / "app/modules/fx/service/__init__.py").read_text(encoding="utf-8")
    assert "from app.modules.accounts.service import open_customer_account" in service_text
