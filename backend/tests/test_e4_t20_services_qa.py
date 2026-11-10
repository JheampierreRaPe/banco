"""QA de punta a punta de HU16: pago, recarga, recordatorio, comprobante (E4-T20).

Suite de integracion/API sobre el app real (SQLite en memoria + schemas
ATTACH + `TestClient`, sin red, sin `float`), complementaria —sin
solapar— a `E4-T16` (catalogo/adaptador), `E4-T17` (pago/recarga) y
`E4-T18` (recordatorios). No repite sus casos: cada prueba cruza dos o
mas piezas (jornada buscar -> suscribir -> pagar, recordatorio desde la
suscripcion creada por API, alcance de la idempotencia por titular,
barridos globales de PII/cuadre) o cubre huecos (latencia < 2 s,
biller nuevo pagable sin cambio de codigo, unicidad por ventana al dia
siguiente, montos enteros sin `float`).

Cobertura de CA de HU16 (`docs/09#HU16`):
- CA-01: busqueda por nombre/suministro + latencia + catalogo extensible.
- CA-02: recarga por el motor con asiento cuadrado (`docs/04#7.10`).
- CA-03: recordatorio en ventana exacta (3 dias), unico por ventana,
  idempotente por dia.
- CA-04: comprobante estructurado con `receipt_no` oficial `OP-…`.
- Seguridad/concurrencia: sin fondos -> rechazo sin efectos; misma clave
  con alcance por titular; suministro invalido antes de cobrar; sin PII
  en respuestas/logs/outbox/avisos; dinero solo en centimos enteros.
"""

from __future__ import annotations

import json
import logging
import time
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
from app.adapters.notification_sender import MockNotificationSender
from app.adapters.service_provider import create_service_provider
from app.core.db import Base, get_db
from app.main import app
from app.modules.shared.config import parameters as params
from app.modules.wallet import service as wallet_service
from app.modules.wallet.api import get_current_user_id
from app.modules.wallet.jobs.service_reminders import (
    next_due_date,
    run_service_reminders_once,
)
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
SOL_NAME = "Recarga Movil Sol Demo"
SUPPLY = "10293847"
PHONE = "999888777"

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
    "notifications.notification_templates",
    "notifications.notifications",
]

SCHEMAS = (
    "wallet",
    "accounts",
    "ledger",
    "transactions",
    "shared",
    "audit",
    "notifications",
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
    """Motor real en SQLite: pagadores + liquidacion + vinculos + billers."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from app.modules.accounts import repository as accounts_repo

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "0")
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
        _seed_param(session, params.WALLET_SERVICE_REMINDER_DAYS_BEFORE_KEY, "3")
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
            "other_acct": other_acct.id,
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
    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "0")
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


def _count(session: Session, table: str) -> int:
    return int(session.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar())


def _assert_square_entry(session: Session, tx_id: uuid.UUID) -> None:
    from app.modules.ledger.models import JournalEntry, Posting

    entries = session.scalars(
        sa.select(JournalEntry).where(JournalEntry.transaction_id == tx_id)
    ).all()
    assert entries, "la operacion debe dejar asiento(s) en el ledger"
    for entry in entries:
        postings = session.scalars(
            sa.select(Posting).where(Posting.journal_entry_id == entry.id)
        ).all()
        assert postings, "el asiento debe tener patas"
        debit = sum(int(p.amount_minor) for p in postings if p.direction == "DEBIT")
        credit = sum(int(p.amount_minor) for p in postings if p.direction == "CREDIT")
        assert debit > 0 and debit == credit, f"asiento descuadrado: {entry.id}"
        assert all(isinstance(p.amount_minor, int) for p in postings)


def _outbox_payloads(session: Session) -> list[str]:
    from app.modules.shared.models import OutboxEntry

    return [
        json.dumps(row.payload, default=str)
        for row in session.scalars(sa.select(OutboxEntry)).all()
    ]


def _post_payment(client, env, key="qa-pay-1", **overrides):
    body = {
        "biller_id": LUZ_ID,
        "supply_ref": SUPPLY,
        "amount_minor": 5_000,
        "currency": "PEN",
        "link_id": str(env["link"]),
    }
    body.update(overrides)
    return client.post("/api/v1/services/payments", json=body, headers={"Idempotency-Key": key})


def _post_topup(client, env, key="qa-topup-1", **overrides):
    body = {
        "operator": SOL_NAME,
        "phone": PHONE,
        "amount_minor": 3_000,
        "currency": "PEN",
        "link_id": str(env["link"]),
    }
    body.update(overrides)
    return client.post("/api/v1/topups", json=body, headers={"Idempotency-Key": key})


def _subscribe(client, key_biller=LUZ_ID, supply=SUPPLY, **overrides):
    body = {"biller_id": key_biller, "supply_ref": supply}
    body.update(overrides)
    return client.post("/api/v1/services/subscriptions", json=body)


# ------------------------------------------------------- CA-01: busqueda


def test_e2e_search_by_name_and_supply_resolves_same_biller(api_client):
    """CA-01: el catalogo (API) y el adaptador resuelven el mismo servicio.

    `E4-T16` probo cada pieza por separado; aqui se cruzan: `q` por nombre
    en el endpoint + `lookup_biller` por nombre/suministro en el adaptador
    deben coincidir en el `biller` de Luz.
    """
    start = time.perf_counter()
    resp = api_client.get("/api/v1/services/catalog", params={"q": "luz"})
    elapsed = time.perf_counter() - start
    assert resp.status_code == 200, resp.text
    assert elapsed < 2, f"busqueda por nombre supero 2 s: {elapsed:.2f}s"
    found = resp.json()["data"]
    assert found, "el catalogo debe resolver 'luz'"
    luz_ids = {item["biller_id"] for item in found}
    assert LUZ_ID in luz_ids

    provider = create_service_provider("mock")
    start = time.perf_counter()
    by_name = provider.lookup_biller("LUZ DEL HOGAR")
    by_supply = provider.lookup_biller(SUPPLY)
    elapsed = time.perf_counter() - start
    assert elapsed < 2, f"lookup del adaptador supero 2 s: {elapsed:.2f}s"
    assert by_name and by_supply, "el adaptador debe resolver por nombre y suministro"
    assert {match.biller_id for match in by_name} & luz_ids
    assert {match.biller_id for match in by_supply} & luz_ids
    # Determinista: misma consulta -> mismo resultado.
    assert [m.biller_id for m in provider.lookup_biller("luz")] == [
        m.biller_id for m in provider.lookup_biller("LUZ")
    ]


def test_catalog_search_latency_under_2s(api_client):
    """CA-01: consulta < 2 s (regla de HU16) en catalogo completo y filtros."""
    for query in (None, {"category": "TELEFONIA"}, {"q": "sol"}, {"q": "zzz-sin-coincidencias"}):
        start = time.perf_counter()
        resp = api_client.get("/api/v1/services/catalog", params=query)
        elapsed = time.perf_counter() - start
        assert resp.status_code == 200, resp.text
        assert elapsed < 2, f"catalogo {query} supero 2 s: {elapsed:.2f}s"
    operators = api_client.get("/api/v1/services/catalog", params={"category": "TELEFONIA"})
    names = {item["name"] for item in operators.json()["data"]}
    assert SOL_NAME in names, "TELEFONIA debe exponer los operadores de recarga"
    empty = api_client.get("/api/v1/services/catalog", params={"q": "zzz-sin-coincidencias"})
    assert empty.json()["data"] == []


def test_new_biller_row_lists_but_provider_gate_rejects_before_charging(api_client, env):
    """CA-01 (limite documentado): la fila nueva se lista, pero el
    proveedor `mock` solo conoce los `billers` demo (`get_demo_biller`):
    suscribir/pagar el biller nuevo se rechaza con `422` **antes de
    cobrar**, sin filas, asientos ni eventos (fail-closed).

    `E4-T16` probo que el catalogo se amplia sin migracion; aqui se
    verifica la frontera real de esa extensibilidad de punta a punta y
    que el rechazo no mueve dinero. Hallazgo QA: hacer pagable un biller
    nuevo exige ampliar el proveedor (`DEMO_BILLERS`/proveedor real),
    no solo insertar la fila.
    """
    session = env["session"]
    session.add(
        _wallet.Biller(
            category="CABLE",
            name="Cable Andino QA",
            required_fields=["supply_ref"],
            enabled=True,
        )
    )
    session.commit()

    listed = api_client.get("/api/v1/services/catalog", params={"category": "CABLE"})
    assert listed.status_code == 200, listed.text
    cable = [item for item in listed.json()["data"] if item["name"] == "Cable Andino QA"]
    assert len(cable) == 1
    assert cable[0]["required_fields"] == ["supply_ref"]

    before = {
        "tx": _count(session, "transactions.transactions"),
        "entries": _count(session, "ledger.journal_entries"),
        "payments": _count(session, "wallet.service_payments"),
        "subs": _count(session, "wallet.service_subscriptions"),
        "outbox": _count(session, "shared.outbox"),
    }
    before_balances = _balances(session, env["payer"], env["settlement"])

    sub = _subscribe(api_client, key_biller=cable[0]["biller_id"], supply="77700011")
    assert sub.status_code == 422, sub.text
    paid = _post_payment(
        api_client,
        env,
        key="qa-cable-1",
        biller_id=cable[0]["biller_id"],
        supply_ref="77700011",
        amount_minor=4_000,
    )
    assert paid.status_code == 422, paid.text

    assert _count(session, "transactions.transactions") == before["tx"]
    assert _count(session, "ledger.journal_entries") == before["entries"]
    assert _count(session, "wallet.service_payments") == before["payments"]
    assert _count(session, "wallet.service_subscriptions") == before["subs"]
    assert _count(session, "shared.outbox") == before["outbox"]
    assert _balances(session, env["payer"], env["settlement"]) == before_balances


# ------------------------------------------------------- CA-02: recarga


def test_topup_e2e_moves_money_with_square_entry_and_single_event(api_client, env):
    """CA-02: la recarga mueve dinero exacto por el motor y deja `topups`.

    Complementa `E4-T17` (servicio + API por separado) con la verificacion
    contable de punta a punta: delta exacto pagador/liquidacion, asiento
    cuadrado `docs/04#7.10` y un solo `topup.completed` sin PII.
    """
    session = env["session"]
    before = _balances(session, env["payer"], env["settlement"])

    resp = _post_topup(api_client, env, key="qa-topup-e2e-1")
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "SETTLED"
    assert isinstance(data["amount_minor"], int) and data["amount_minor"] == 3_000

    row = session.scalars(
        sa.select(_wallet.Topup).where(_wallet.Topup.id == uuid.UUID(data["topup_id"]))
    ).one()
    assert row.status == "PAID"
    assert str(row.transaction_id) == data["transaction_id"]
    assert isinstance(row.amount_minor, int) and int(row.amount_minor) == 3_000
    assert row.phone == PHONE

    after = _balances(session, env["payer"], env["settlement"])
    assert after[str(env["payer"])][0] == before[str(env["payer"])][0] - 3_000
    assert after[str(env["settlement"])][0] == before[str(env["settlement"])][0] + 3_000
    _assert_square_entry(session, uuid.UUID(data["transaction_id"]))

    from app.modules.shared.models import OutboxEntry

    events = session.scalars(
        sa.select(OutboxEntry).where(
            OutboxEntry.event_type == "topup.completed",
            OutboxEntry.aggregate_id == row.id,
        )
    ).all()
    assert len(events) == 1
    assert PHONE not in json.dumps(events[0].payload, default=str)


# ------------------------------------------------------- CA-04: comprobante


def test_receipts_structured_masked_and_stable_on_replay(api_client, env):
    """CA-04: comprobante estructurado en pago y recarga, estable en replay.

    `E4-T17` valido el formato unitariamente; aqui se exige la estructura
    exacta en ambas respuestas, el mismo `receipt_no` oficial tras el
    replay y el enmascarado (cola visible, resto oculto, sin PII en claro).
    """
    first_pay = _post_payment(api_client, env, key="qa-receipt-pay-1")
    assert first_pay.status_code == 201, first_pay.text
    pay = first_pay.json()["data"]
    assert set(pay["receipt"]) == {
        "receipt_no",
        "transaction_id",
        "concept",
        "supply_masked",
        "phone_masked",
        "amount_minor",
        "currency",
        "paid_at",
        "status",
    }
    assert pay["receipt"]["receipt_no"] == wallet_service.build_receipt_no(
        uuid.UUID(pay["transaction_id"])
    )
    assert pay["receipt"]["transaction_id"] == pay["transaction_id"]
    assert pay["receipt"]["status"] == "PAID"
    assert pay["receipt"]["phone_masked"] is None
    assert SUPPLY not in json.dumps(pay["receipt"])
    assert pay["receipt"]["supply_masked"].endswith(SUPPLY[-4:])
    datetime.fromisoformat(pay["receipt"]["paid_at"])

    first_topup = _post_topup(api_client, env, key="qa-receipt-topup-1")
    assert first_topup.status_code == 201, first_topup.text
    topup = first_topup.json()["data"]
    assert set(topup["receipt"]) == {
        "receipt_no",
        "transaction_id",
        "concept",
        "supply_masked",
        "phone_masked",
        "amount_minor",
        "currency",
        "paid_at",
        "status",
    }
    assert topup["receipt"]["receipt_no"] == wallet_service.build_receipt_no(
        uuid.UUID(topup["transaction_id"])
    )
    assert topup["receipt"]["concept"] == f"Recarga {SOL_NAME}"
    assert topup["receipt"]["supply_masked"] is None
    assert PHONE not in json.dumps(topup["receipt"])
    assert topup["receipt"]["phone_masked"].endswith(PHONE[-4:])

    replay_pay = _post_payment(api_client, env, key="qa-receipt-pay-1")
    assert replay_pay.json()["data"]["receipt"]["receipt_no"] == pay["receipt"]["receipt_no"]
    replay_topup = _post_topup(api_client, env, key="qa-receipt-topup-1")
    assert replay_topup.json()["data"]["receipt"]["receipt_no"] == topup["receipt"]["receipt_no"]
    assert replay_pay.json()["meta"]["replayed"] is True
    assert replay_topup.json()["meta"]["replayed"] is True


# ------------------------------------------------------- CA-03: recordatorio


def test_reminder_e2e_from_api_subscription_to_single_push(api_client, env):
    """CA-03 E2E: suscripcion por API -> job avisa una vez en ventana exacta.

    `E4-T16` probo el alta y `E4-T18` el job con filas directas; aqui se
    encadenan: fuera de ventana no hay aviso, en ventana exacta hay un
    solo push y repetir la pasada no duplica.
    """
    session = env["session"]
    today = datetime.now(UTC).date()
    sub = _subscribe(api_client, supply="20195555")
    assert sub.status_code == 201, sub.text
    sub_id = uuid.UUID(sub.json()["data"]["subscription_id"])
    due = next_due_date(sub_id, today)
    assert (due - today).days >= 0

    from datetime import timedelta

    sender = MockNotificationSender()
    outside = run_service_reminders_once(session, now=due - timedelta(days=4), sender=sender)
    assert str(sub_id) in outside["skipped"]
    assert sender.calls == [] and outside["notified"] == []

    inside = run_service_reminders_once(session, now=due - timedelta(days=3), sender=sender)
    assert inside["days_before"] == 3
    assert inside["notified"] == [str(sub_id)]
    assert len(sender.calls) == 1, "el aviso debe invocarse una sola vez"

    rows = list(session.scalars(sa.select(_notifications.Notification)).all())
    assert len(rows) == 1
    assert rows[0].template_code == "service_reminder"
    assert rows[0].channel == "push"
    payload = dict(rows[0].payload_json or {})
    assert set(payload.get("data", {})) == {"service_ref", "due_on"}
    assert payload["data"] == {"service_ref": str(sub_id)[:8], "due_on": due.isoformat()}
    assert "20195555" not in json.dumps(payload, default=str)

    rerun = run_service_reminders_once(session, now=due - timedelta(days=3), sender=sender)
    assert rerun["notified"] == []
    assert str(sub_id) in rerun["skipped"]
    assert len(sender.calls) == 1
    assert _count(session, "notifications.notifications") == 1


def test_reminder_unique_per_window_next_day_stays_silent(api_client, env):
    """CA-03: el aviso es unico por ventana (al dia siguiente no re-avisa).

    `E4-T18` cubrio el reintento el mismo dia; aqui se verifica que, una
    vez notificada la ventana exacta, la pasada del dia siguiente queda
    en silencio aunque la suscripcion siga activa.
    """
    import datetime as dt

    session = env["session"]
    today = datetime.now(UTC).date()
    sub = _subscribe(api_client, supply="30296666")
    assert sub.status_code == 201, sub.text
    sub_id = uuid.UUID(sub.json()["data"]["subscription_id"])
    due = next_due_date(sub_id, today)

    sender = MockNotificationSender()
    notified = run_service_reminders_once(session, now=due - dt.timedelta(days=3), sender=sender)
    assert notified["notified"] == [str(sub_id)]

    next_day = run_service_reminders_once(session, now=due - dt.timedelta(days=2), sender=sender)
    assert next_day["notified"] == []
    assert str(sub_id) in next_day["skipped"]
    assert len(sender.calls) == 1
    assert _count(session, "notifications.notifications") == 1


# ------------------------------------------------------- seguridad/concurrencia


def test_insufficient_funds_rejected_end_to_end_without_side_effects(api_client, env):
    """Sin fondos -> `REJECTED` por API sin tocar saldos ni dejar rastros.

    `E4-T17` lo probo a nivel servicio; aqui se exige a nivel endpoint:
    sin asientos nuevos, sin eventos y con `receipt` nulo.
    """
    session = env["session"]
    before_balances = _balances(session, env["payer_low"], env["settlement"])
    before_entries = _count(session, "ledger.journal_entries")
    before_tx = _count(session, "transactions.transactions")

    resp = _post_topup(
        api_client,
        env,
        key="qa-nofunds-1",
        link_id=str(env["low_link"]),
        amount_minor=FUNDED_LOW + 1_000,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "REJECTED"
    assert data["failure_code"] == "INSUFFICIENT_FUNDS"
    assert data["receipt"] is None

    assert _balances(session, env["payer_low"], env["settlement"]) == before_balances
    assert _count(session, "ledger.journal_entries") == before_entries
    assert _count(session, "transactions.transactions") == before_tx + 1
    from app.modules.shared.models import OutboxEntry

    leaked = session.scalars(
        sa.select(OutboxEntry).where(
            OutboxEntry.event_type.in_(["service.paid", "topup.completed"]),
            OutboxEntry.aggregate_id == uuid.UUID(data["topup_id"]),
        )
    ).all()
    assert leaked == []


def test_same_idempotency_key_is_scoped_per_holder(api_client, env):
    """La misma clave en dos titulares crea dos movimientos independientes.

    Complementa la idempotencia de `E4-T17` (un titular, una clave): la
    clave no cruza titulares y el replay de cada uno devuelve lo suyo.
    """
    session = env["session"]
    mine = _post_payment(api_client, env, key="qa-shared-key-1")
    assert mine.status_code == 201, mine.text
    mine_data = mine.json()["data"]
    assert mine_data["status"] == "SETTLED"
    before_tx = _count(session, "transactions.transactions")

    _act_as(OTHER)
    try:
        theirs = api_client.post(
            "/api/v1/services/payments",
            json={
                "biller_id": LUZ_ID,
                "supply_ref": SUPPLY,
                "amount_minor": 5_000,
                "currency": "PEN",
                "link_id": str(env["other_link"]),
            },
            headers={"Idempotency-Key": "qa-shared-key-1"},
        )
    finally:
        _act_as(USER)
    assert theirs.status_code == 201, theirs.text
    theirs_data = theirs.json()["data"]
    assert theirs_data["status"] == "SETTLED"
    assert theirs_data["payment_id"] != mine_data["payment_id"]
    assert theirs_data["transaction_id"] != mine_data["transaction_id"]
    assert _count(session, "transactions.transactions") == before_tx + 1

    replay = _post_payment(api_client, env, key="qa-shared-key-1")
    assert replay.json()["data"]["payment_id"] == mine_data["payment_id"]
    assert replay.json()["meta"]["replayed"] is True

    _act_as(OTHER)
    try:
        other_replay = api_client.post(
            "/api/v1/services/payments",
            json={
                "biller_id": LUZ_ID,
                "supply_ref": SUPPLY,
                "amount_minor": 5_000,
                "currency": "PEN",
                "link_id": str(env["other_link"]),
            },
            headers={"Idempotency-Key": "qa-shared-key-1"},
        )
    finally:
        _act_as(USER)
    assert other_replay.json()["data"]["payment_id"] == theirs_data["payment_id"]
    assert _count(session, "transactions.transactions") == before_tx + 1


def test_invalid_supply_rejected_before_any_charge(api_client, env):
    """Suministro invalido -> `422` sin crear transaccion, asiento ni filas."""
    session = env["session"]
    before = {
        "tx": _count(session, "transactions.transactions"),
        "entries": _count(session, "ledger.journal_entries"),
        "payments": _count(session, "wallet.service_payments"),
        "outbox": _count(session, "shared.outbox"),
    }
    before_balances = _balances(session, env["payer"], env["settlement"])

    resp = _post_payment(api_client, env, key="qa-badsupply-1", supply_ref="!!")
    assert resp.status_code == 422, resp.text
    assert SUPPLY not in resp.text
    assert _count(session, "transactions.transactions") == before["tx"]
    assert _count(session, "ledger.journal_entries") == before["entries"]
    assert _count(session, "wallet.service_payments") == before["payments"]
    assert _count(session, "shared.outbox") == before["outbox"]
    assert _balances(session, env["payer"], env["settlement"]) == before_balances

    healthy = _post_payment(api_client, env, key="qa-badsupply-2")
    assert healthy.status_code == 201, healthy.text
    assert healthy.json()["data"]["status"] == "SETTLED"


def test_no_pii_in_responses_logs_outbox_and_notifications(api_client, env, caplog):
    """Sin PII en respuestas/logs/outbox/avisos en la jornada completa.

    `E4-T16`/`E4-T17`/`E4-T18` lo probaron por pieza; aqui el barrido es
    de punta a punta (catalogo -> suscripcion -> pago -> recarga ->
    recordatorio), incluyendo el cuerpo enviado por el `sender` mock.
    """
    session = env["session"]
    today = datetime.now(UTC).date()
    secrets = (SUPPLY, PHONE, PAYER_NUMBER, SETTLEMENT_NUMBER, OTHER_NUMBER)
    response_texts: list[str] = []
    with caplog.at_level(logging.INFO):
        response_texts.append(api_client.get("/api/v1/services/catalog").text)
        sub = _subscribe(api_client, supply="40197777")
        response_texts.append(sub.text)
        assert sub.status_code == 201, sub.text
        pay = _post_payment(api_client, env, key="qa-pii-pay-1")
        response_texts.append(pay.text)
        assert pay.status_code == 201, pay.text
        topup = _post_topup(api_client, env, key="qa-pii-topup-1")
        response_texts.append(topup.text)
        assert topup.status_code == 201, topup.text
        sub_id = uuid.UUID(sub.json()["data"]["subscription_id"])
        due = next_due_date(sub_id, today)
        from datetime import timedelta

        sender = MockNotificationSender()
        run_service_reminders_once(session, now=due - timedelta(days=3), sender=sender)

    for text in response_texts:
        for secret in secrets:
            assert secret not in text, f"PII en respuesta: {secret[:4]}…"
    for secret in secrets:
        assert secret not in caplog.text, f"PII en logs: {secret[:4]}…"
    for blob in _outbox_payloads(session):
        for secret in secrets:
            assert secret not in blob, f"PII en outbox: {secret[:4]}…"
    rows = list(session.scalars(sa.select(_notifications.Notification)).all())
    assert rows, "la jornada debe dejar el aviso del recordatorio"
    for row in rows:
        blob = json.dumps(dict(row.payload_json or {}), default=str)
        for secret in secrets:
            assert secret not in blob, f"PII en notificacion: {secret[:4]}…"
    for call in sender.calls:
        blob = json.dumps(call, default=str)
        for secret in secrets:
            assert secret not in blob, f"PII en el envio: {secret[:4]}…"
    # La suscripcion vive solo para su titular (el alta no cruza titulares).
    assert _count(session, "wallet.service_subscriptions") == 1


def test_amounts_are_integer_cents_without_float(api_client, env):
    """Todo dinero en centimos enteros: respuestas, filas y sin `float`.

    `E4-T17` afirmo tipos por caso; aqui el barrido cubre la jornada
    (pago + recarga + replay) mas un escaneo estatico de `float(` en el
    codigo de `wallet` que mueve dinero (regla de oro 3).
    """
    import pathlib

    session = env["session"]
    pay = _post_payment(api_client, env, key="qa-int-pay-1")
    topup = _post_topup(api_client, env, key="qa-int-topup-1")
    assert pay.status_code == 201 and topup.status_code == 201
    for resp in (pay, topup, _post_payment(api_client, env, key="qa-int-pay-1")):
        assert isinstance(resp.json()["data"]["amount_minor"], int)
        assert isinstance(resp.json()["data"]["receipt"]["amount_minor"], int)

    from app.modules.ledger.models import Posting

    for posting in session.scalars(sa.select(Posting)).all():
        assert isinstance(posting.amount_minor, int)
        assert int(posting.amount_minor) > 0
    for model, field in (
        (_wallet.ServicePayment, "amount_minor"),
        (_wallet.Topup, "amount_minor"),
    ):
        for row in session.scalars(sa.select(model)).all():
            assert isinstance(getattr(row, field), int)

    service_root = pathlib.Path(wallet_service.__file__).parent
    for name in ("services.py", "express.py"):
        text = (service_root / name).read_text(encoding="utf-8")
        assert "float(" not in text, f"`float(` en wallet/service/{name}"
    jobs_root = service_root.parent / "jobs"
    assert "float(" not in (jobs_root / "service_reminders.py").read_text(encoding="utf-8")


def test_global_ledger_stays_square_after_mixed_journey(api_client, env):
    """Cuadre global tras jornada mixta: pagos, recarga, rechazo y replay."""
    session = env["session"]
    assert _post_payment(api_client, env, key="qa-square-pay-1").status_code == 201
    assert _post_topup(api_client, env, key="qa-square-topup-1").status_code == 201
    rejected = _post_topup(
        api_client,
        env,
        key="qa-square-no-1",
        link_id=str(env["low_link"]),
        amount_minor=FUNDED_LOW + 1_000,
    )
    assert rejected.json()["data"]["status"] == "REJECTED"
    assert _post_payment(api_client, env, key="qa-square-pay-1").json()["meta"]["replayed"] is True

    from app.modules.ledger.models import JournalEntry, Posting

    total_debit = total_credit = 0
    entries = session.scalars(sa.select(JournalEntry)).all()
    assert entries, "la jornada debe dejar asientos"
    for entry in entries:
        postings = session.scalars(
            sa.select(Posting).where(Posting.journal_entry_id == entry.id)
        ).all()
        debit = sum(int(p.amount_minor) for p in postings if p.direction == "DEBIT")
        credit = sum(int(p.amount_minor) for p in postings if p.direction == "CREDIT")
        assert debit > 0 and debit == credit, f"asiento descuadrado: {entry.id}"
        total_debit += debit
        total_credit += credit
    assert total_debit == total_credit


def test_settlement_emits_exactly_one_event_per_operation(api_client, env):
    """Cada liquidacion emite un solo evento aunque se reintente la clave.

    Cruza `service.paid` + `topup.completed` (uno por operacion) con los
    replays de `E4-T17`, mas el aviso unico del recordatorio: emision y
    consumo unicos de punta a punta.
    """
    import datetime as dt

    from app.modules.shared.models import OutboxEntry

    session = env["session"]
    today = datetime.now(UTC).date()
    pay = _post_payment(api_client, env, key="qa-once-pay-1")
    topup = _post_topup(api_client, env, key="qa-once-topup-1")
    assert pay.status_code == 201 and topup.status_code == 201
    sub = _subscribe(api_client, supply="50298888")
    assert sub.status_code == 201, sub.text

    assert _post_payment(api_client, env, key="qa-once-pay-1").json()["meta"]["replayed"] is True
    assert _post_topup(api_client, env, key="qa-once-topup-1").json()["meta"]["replayed"] is True

    pay_id = uuid.UUID(pay.json()["data"]["payment_id"])
    topup_id = uuid.UUID(topup.json()["data"]["topup_id"])
    paid_events = session.scalars(
        sa.select(OutboxEntry).where(
            OutboxEntry.event_type == "service.paid", OutboxEntry.aggregate_id == pay_id
        )
    ).all()
    topup_events = session.scalars(
        sa.select(OutboxEntry).where(
            OutboxEntry.event_type == "topup.completed", OutboxEntry.aggregate_id == topup_id
        )
    ).all()
    assert len(paid_events) == 1
    assert len(topup_events) == 1

    sub_id = uuid.UUID(sub.json()["data"]["subscription_id"])
    due = next_due_date(sub_id, today)
    sender = MockNotificationSender()
    result = run_service_reminders_once(session, now=due - dt.timedelta(days=3), sender=sender)
    assert result["notified"] == [str(sub_id)]
    assert len(sender.calls) == 1
