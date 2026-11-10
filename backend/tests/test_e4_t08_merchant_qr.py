"""Endpoints del comercio `/merchants/qr` + consulta de estado (E4-T08, HU14).

- Camino feliz: `POST` genera el QR firmado (`201` con `payload`,
  `signature`, `expires_at` + evento `qr.charge.created`) y `GET`
  reporta `ACTIVE`.
- Caducidad on-read: un QR vencido se reporta `EXPIRED` sin esperar
  al job (`E4-T07`).
- Errores: monto `<= 0`/moneda/modo invalidos -> `422`;
  ajeno/inexistente -> `404` neutro; sin rol de comercio -> `403`;
  sin JWT -> `401`; idempotencia (`Idempotency-Key` opcional, patron
  `E2-T13`): mismo cuerpo -> un solo cargo (`replayed`); distinto
  cuerpo -> `409`.
- Notificacion (stub): el abono invoca `notifications.send(...)`
  con la plantilla `qr_charge_paid`, sin PII ni cifras.
- Sin PII/RUC en claro en respuestas ni logs; no mueve dinero.

SQLite en memoria + schemas ATTACH (sin red).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.modules.audit.models as _audit
import app.modules.notifications.models as _notifications
import app.modules.shared.models as _shared
import app.modules.wallet.models as _wallet
from app.adapters import wallet_interop as interop
from app.core.db import Base, get_db
from app.main import app
from app.modules.shared.config import parameters as params
from app.modules.wallet import service as wallet_service
from app.modules.wallet.api import get_current_user_id
from app.modules.wallet.domain import qr as qr_domain
from app.modules.wallet.repository import qr as qr_repo

assert _audit is not None and _shared is not None and _wallet is not None
assert _notifications is not None

SECRET = "test-qr-api-secret"
RUC_PLAIN = "20123456789"

USER_MERCHANT_A = uuid.uuid4()
USER_MERCHANT_B = uuid.uuid4()
USER_PLAIN = uuid.uuid4()  # titular sin cuenta de comercio

TABLES = [
    "wallet.merchant_accounts",
    "wallet.qr_charges",
    "shared.outbox",
    "shared.idempotency_keys",
    "notifications.notifications",
    "notifications.notification_templates",
    "audit.audit_log",
]

SCHEMAS = ("wallet", "shared", "config", "audit", "notifications")


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
def sqlite_session():
    """SQLite con QR + idempotencia + notificaciones; comercios A y B."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

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
        _seed_param(session, params.WALLET_QR_TTL_SECONDS_KEY, "300")
        merchant_a = qr_repo.create_merchant(
            session, user_id=USER_MERCHANT_A, business_name="Bodega A"
        )
        merchant_b = qr_repo.create_merchant(
            session, user_id=USER_MERCHANT_B, business_name="Cafe B"
        )
        session.commit()
        yield {
            "session": session,
            "merchant_a": merchant_a,
            "merchant_b": merchant_b,
        }
        session.rollback()
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def api_client(sqlite_session, monkeypatch):
    """TestClient con BD SQLite y auth falsa como comercio A."""
    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    session = sqlite_session["session"]

    def _override_db():
        yield session

    def _override_user():
        return USER_MERCHANT_A

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
    """Cambia el usuario autenticado del TestClient (aislamiento/RBAC)."""
    app.dependency_overrides[get_current_user_id] = lambda: user_id


# ------------------------------------------------------- camino feliz


def test_post_creates_signed_qr_and_get_reports_active(api_client, sqlite_session):
    session = sqlite_session["session"]
    resp = api_client.post(
        "/api/v1/merchants/qr",
        json={"amount_minor": 5_000, "currency": "PEN", "concept": "Menu del dia"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    data = body["data"]
    assert data["status"] == "ACTIVE"
    assert data["amount_minor"] == 5_000
    assert data["currency"] == "PEN"
    assert data["concept"] == "Menu del dia"
    assert data["mode"] == "DYNAMIC"
    assert len(data["payload_hash"]) == 64
    assert len(data["signature"]) == 64
    assert data["payload"]["amount_minor"] == 5_000
    assert data["payload"]["exp"] > 0
    assert data["expires_at"] is not None
    assert body["meta"]["replayed"] is False
    assert "request_id" in body["meta"]

    row = qr_repo.get_qr_charge(session, data["charge_id"])
    assert row is not None and row.status == "ACTIVE"

    from app.modules.shared.models import OutboxEntry

    events = session.scalars(sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == row.id)).all()
    assert len(events) == 1
    assert events[0].event_type == "qr.charge.created"

    got = api_client.get(f"/api/v1/merchants/qr/{data['charge_id']}")
    assert got.status_code == 200, got.text
    status = got.json()["data"]
    assert status["charge_id"] == data["charge_id"]
    assert status["status"] == "ACTIVE"
    assert status["amount_minor"] == 5_000
    assert status["currency"] == "PEN"
    assert status["expires_at"] is not None
    assert "payload" not in status and "signature" not in status


def test_post_static_mode_without_expiry(api_client):
    resp = api_client.post(
        "/api/v1/merchants/qr",
        json={"amount_minor": 2_500, "currency": "PEN", "mode": "STATIC"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["mode"] == "STATIC"
    assert data["expires_at"] is None
    got = api_client.get(f"/api/v1/merchants/qr/{data['charge_id']}")
    assert got.status_code == 200
    assert got.json()["data"]["status"] == "ACTIVE"


# ------------------------------------------------------- caducidad on-read


def test_get_expired_charge_reports_expired_without_job(api_client, sqlite_session):
    session = sqlite_session["session"]
    resp = api_client.post("/api/v1/merchants/qr", json={"amount_minor": 1_000, "currency": "PEN"})
    assert resp.status_code == 201, resp.text
    charge_id = resp.json()["data"]["charge_id"]
    # Vencerlo en el pasado (el job no ha corrido: la fila sigue ACTIVE).
    past = datetime.now(UTC) - timedelta(seconds=60)
    session.execute(
        sa.text("UPDATE wallet.qr_charges SET expires_at = :past WHERE id = :id"),
        {"past": past.replace(tzinfo=None), "id": charge_id.replace("-", "")},
    )
    session.flush()
    assert qr_repo.get_qr_charge(session, charge_id).status == "ACTIVE"

    got = api_client.get(f"/api/v1/merchants/qr/{charge_id}")
    assert got.status_code == 200, got.text
    assert got.json()["data"]["status"] == "EXPIRED"
    # Solo lectura: el job es quien muta la fila.
    assert qr_repo.get_qr_charge(session, charge_id).status == "ACTIVE"
    session.rollback()


# ------------------------------------------------------- errores


def test_post_rejects_bad_input_with_422(api_client):
    for payload in (
        {"amount_minor": 0, "currency": "PEN"},
        {"amount_minor": -5, "currency": "PEN"},
        {"amount_minor": 1_000, "currency": "XX"},
        {"amount_minor": 1_000, "currency": "PEN", "mode": "REUSABLE"},
        {"amount_minor": 1_000, "currency": "PEN", "concept": "x" * 121},
    ):
        resp = api_client.post("/api/v1/merchants/qr", json=payload)
        assert resp.status_code == 422, (payload, resp.text)


def test_get_foreign_or_missing_charge_is_neutral_404(api_client, sqlite_session):
    session = sqlite_session["session"]
    merchant_b = sqlite_session["merchant_b"]
    other = wallet_service.create_qr_charge(
        session,
        merchant_id=merchant_b.id,
        amount_minor=1_000,
        currency="PEN",
        secret=SECRET,
    )
    session.commit()

    foreign = api_client.get(f"/api/v1/merchants/qr/{other['charge_id']}")
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "NOT_FOUND"

    # Determinista: el 404 neutro no filtra el monto (recorrido estructurado
    # del JSON en vez de subcadena sobre el texto, cuyo `request_id` hex
    # colisiona por azar con "500").
    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from _walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                yield from _walk(value)
        else:
            yield node

    flat = list(_walk(foreign.json()))
    assert "amount_minor" not in flat
    assert 1_000 not in flat and "1000" not in flat

    missing = api_client.get(f"/api/v1/merchants/qr/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"

    malformed = api_client.get("/api/v1/merchants/qr/no-es-uuid")
    assert malformed.status_code == 404


def test_without_merchant_role_is_403(api_client):
    _act_as(USER_PLAIN)
    try:
        resp = api_client.post(
            "/api/v1/merchants/qr", json={"amount_minor": 1_000, "currency": "PEN"}
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"
        got = api_client.get(f"/api/v1/merchants/qr/{uuid.uuid4()}")
        assert got.status_code == 403
    finally:
        _act_as(USER_MERCHANT_A)


def test_without_jwt_is_401(api_client):
    app.dependency_overrides.pop(get_current_user_id, None)
    try:
        resp = api_client.post(
            "/api/v1/merchants/qr", json={"amount_minor": 1_000, "currency": "PEN"}
        )
        assert resp.status_code == 401
        got = api_client.get(f"/api/v1/merchants/qr/{uuid.uuid4()}")
        assert got.status_code == 401
    finally:
        _act_as(USER_MERCHANT_A)


# ------------------------------------------------------- idempotencia


def test_idempotency_same_body_replays_single_charge(api_client, sqlite_session):
    session = sqlite_session["session"]
    headers = {"Idempotency-Key": "qr-key-1"}
    payload = {"amount_minor": 3_000, "currency": "PEN", "concept": "Caja"}
    first = api_client.post("/api/v1/merchants/qr", json=payload, headers=headers)
    second = api_client.post("/api/v1/merchants/qr", json=payload, headers=headers)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["data"]["charge_id"] == second.json()["data"]["charge_id"]
    assert first.json()["meta"]["replayed"] is False
    assert second.json()["meta"]["replayed"] is True
    merchant_a = sqlite_session["merchant_a"]
    charges = qr_repo.list_charges_by_merchant(session, merchant_a.id)
    assert len(charges) == 1


def test_idempotency_distinct_body_conflicts_409(api_client):
    headers = {"Idempotency-Key": "qr-key-2"}
    first = api_client.post(
        "/api/v1/merchants/qr",
        json={"amount_minor": 1_000, "currency": "PEN"},
        headers=headers,
    )
    assert first.status_code == 201, first.text
    second = api_client.post(
        "/api/v1/merchants/qr",
        json={"amount_minor": 2_000, "currency": "PEN"},
        headers=headers,
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# ------------------------------------------------------- notificacion (stub)


def test_charge_paid_notifies_merchant_without_pii(sqlite_session):
    from app.adapters.notification_sender import MockNotificationSender

    session = sqlite_session["session"]
    merchant_a = sqlite_session["merchant_a"]
    sender = MockNotificationSender()
    charge_id = uuid.uuid4()
    row = wallet_service.notify_merchant_charge_paid(
        session,
        merchant_user_id=merchant_a.user_id,
        charge_id=charge_id,
        sender=sender,
    )
    assert row.template_code == "qr_charge_paid"
    assert row.channel == "push"
    assert row.status in ("SENT", "QUEUED")
    assert len(sender.calls) == 1 and sender.calls[0]["ok"] is True
    import string as _string

    from app.modules.notifications.domain.templates import BASE_TEMPLATES, render_template

    stored_data = row.payload_json["data"]
    # Determinista: el aviso solo porta referencia + instante (sin buscar
    # subcadenas sobre el texto renderizado, cuyo timestamp ISO con
    # microsegundos colisiona por azar con "5000").
    assert set(stored_data) == {"charge_ref", "at"}
    for forbidden in (
        "amount_minor",
        "amount",
        "currency",
        "concept",
        "mode",
        "ruc",
        "tax_id",
        "tax_id_hash",
        "business_name",
        "merchant",
        "payer",
        "user_id",
        "payload",
        "signature",
    ):
        assert forbidden not in stored_data
    # Los valores son solo la referencia del cargo y un instante ISO.
    assert stored_data["charge_ref"] == str(charge_id)[:8]
    assert datetime.fromisoformat(stored_data["at"]).tzinfo is not None
    assert all(v != 5_000 and v != "5000" and v != "5_000" for v in stored_data.values())
    # La plantilla solo interpola esos dos campos (sin placeholders de monto/PII).
    placeholders = {
        name
        for _, name, _, _ in _string.Formatter().parse(
            BASE_TEMPLATES["qr_charge_paid"]["body_template"]
        )
        if name
    }
    assert placeholders == {"charge_ref", "at"}
    text = render_template(BASE_TEMPLATES["qr_charge_paid"]["body_template"], stored_data)
    assert "20123456789" not in text  # RUC nunca en claro
    assert merchant_a.business_name not in text
    session.rollback()


# ------------------------------------------------------- contrato qr_content (ALTO-2)


def test_post_qr_content_is_scannable_boqr1_roundtrip(api_client):
    """ALTO-2: `qr_content` es `str` escaneable y reproduce el cargo.

    Sin el fix el `POST` no exponia `qr_content` (falla con
    `KeyError`): el frontend lo trataba como `String` pero el backend
    solo devolvia `payload` como objeto. El token se decodifica con el
    adaptador `E4-T03` y su contenido coincide con el cargo
    (ref/monto/moneda/exp/firma); la firma verifica contra el
    `payload` estructurado; el `GET` de estado devuelve el mismo
    contenido y el replay de idempotencia lo reconstruye identico.
    """
    resp = api_client.post(
        "/api/v1/merchants/qr",
        json={"amount_minor": 5_000, "currency": "PEN", "concept": "Menu del dia"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    # El objeto estructurado se mantiene y no sustituye al string.
    assert isinstance(data["payload"], dict)
    token = data["qr_content"]
    assert isinstance(token, str) and token

    decoded = interop.decode_payload(token)
    assert decoded.ref == data["charge_id"] == str(data["payload"]["charge_id"])
    assert decoded.amount_minor == data["amount_minor"] == data["payload"]["amount_minor"]
    assert decoded.currency == data["currency"] == data["payload"]["currency"]
    assert decoded.expires_at == data["payload"]["exp"]
    assert decoded.sig == data["signature"]
    assert wallet_service.verify_charge_signature(data["payload"], data["signature"]) is True

    got = api_client.get(f"/api/v1/merchants/qr/{data['charge_id']}")
    assert got.status_code == 200, got.text
    status = got.json()["data"]
    assert status["qr_content"] == token
    assert "payload" not in status and "signature" not in status

    headers = {"Idempotency-Key": f"qr-content-{uuid.uuid4().hex}"}
    payload = {"amount_minor": 7_500, "currency": "PEN"}
    first = api_client.post("/api/v1/merchants/qr", json=payload, headers=headers)
    second = api_client.post("/api/v1/merchants/qr", json=payload, headers=headers)
    assert first.status_code == 201 and second.status_code == 201
    assert second.json()["meta"]["replayed"] is True
    assert second.json()["data"]["qr_content"] == first.json()["data"]["qr_content"]


# ------------------------------------------------------- sin PII / sin dinero


def test_responses_and_logs_without_pii(api_client, sqlite_session, caplog):
    hashed = qr_domain.hash_tax_id(RUC_PLAIN)
    assert RUC_PLAIN not in hashed
    with caplog.at_level(logging.DEBUG, logger="app.modules.wallet"):
        resp = api_client.post(
            "/api/v1/merchants/qr",
            json={"amount_minor": 1_000, "currency": "PEN", "concept": "Pan"},
        )
        assert resp.status_code == 201, resp.text
        got = api_client.get(f"/api/v1/merchants/qr/{resp.json()['data']['charge_id']}")
        assert got.status_code == 200
    assert RUC_PLAIN not in resp.text
    assert RUC_PLAIN not in got.text
    assert RUC_PLAIN not in caplog.text
    assert hashed not in caplog.text
    assert "jwt" not in caplog.text.lower() or "secret" not in caplog.text.lower()
    sqlite_session["session"].rollback()


def test_wallet_never_touches_money_tables():
    import pathlib

    for module in ("api/qr.py", "service/qr.py", "schemas/qr.py"):
        source = (
            pathlib.Path(__file__).resolve().parents[1] / "app" / "modules" / "wallet" / module
        ).read_text(encoding="utf-8")
        for forbidden in (
            "transactions.models",
            "transactions.repository",
            "ledger.models",
            "ledger.repository",
            "accounts.models",
        ):
            assert forbidden not in source, f"wallet/{module}: {forbidden}"
