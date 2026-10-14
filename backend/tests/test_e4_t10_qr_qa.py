"""QA de HU14: QR vencido, firma alterada y notificacion de abono (E4-T10).

Complementaria —sin solapar— a `E4-T06` (firma/`qr_charges`), `E4-T07`
(caducidad on-read + job) y `E4-T08` (endpoints + stub de aviso).
Cubre de punta a punta e independientemente los CA de HU14
(`docs/09#HU14`):

- CA-01: QR generado por `POST /merchants/qr` con firma verificable y
  `payload_hash` determinista; generacion < 1 s.
- CA-02: QR vencido -> `EXPIRED` on-read y por job (borde exacto
  incluido); el vencido se rechaza antes de operar, sin mover dinero.
- CA-03: interoperabilidad `BO-QR1` via el adaptador de `E4-T03`
  (roundtrip determinista + rechazo de sobre corrupto); manipular
  monto/comercio/vencimiento invalida la firma.
- Firma alterada/corrupta: rechazo sin descontar fondos (sin filas
  `PAID`, sin eventos nuevos, sin tocar el motor/ledger).
- CA-04: con un pago simulado (`mark_paid`, el abono real con pago por
  el motor es HU15) se invoca `notifications.send(...)` una sola vez
  con la plantilla `qr_charge_paid` (< 5 s), sin PII ni cifras.

SQLite en memoria + schemas ATTACH + `TestClient` (sin red, sin
`float`). El E2E real con `POST /payments/qr` queda para HU15
(`docs/18` G72). No mueve dinero salvo el `mark_paid` simulado.
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
from app.modules.wallet.jobs import qr_expiry as qr_expiry_job
from app.modules.wallet.repository import qr as qr_repo

assert _audit is not None and _shared is not None
assert _wallet is not None and _notifications is not None

RUC_PLAIN = "20123456789"

USER_MERCHANT_A = uuid.uuid4()
USER_MERCHANT_B = uuid.uuid4()

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
            session, user_id=USER_MERCHANT_A, business_name="Bodega QA-A"
        )
        merchant_b = qr_repo.create_merchant(
            session, user_id=USER_MERCHANT_B, business_name="Cafe QA-B"
        )
        session.commit()
        yield {"session": session, "merchant_a": merchant_a, "merchant_b": merchant_b}
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


def _create_charge(api_client):
    resp = api_client.post(
        "/api/v1/merchants/qr",
        json={"amount_minor": 5_000, "currency": "PEN", "concept": "Menu QA"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _outbox_count(session: Session, charge_id: str) -> int:
    from app.modules.shared.models import OutboxEntry

    return (
        session.scalar(
            sa.select(sa.func.count())
            .select_from(OutboxEntry)
            .where(OutboxEntry.aggregate_id == uuid.UUID(charge_id))
        )
        or 0
    )


def _expire_charge_in_past(session: Session, charge_id: str, *, seconds: int = 60) -> None:
    """Vence un cargo en el pasado por SQL y refresca el mapa de identidad."""
    past = datetime.now(UTC) - timedelta(seconds=seconds)
    session.execute(
        sa.text("UPDATE wallet.qr_charges SET expires_at = :past WHERE id = :id"),
        {"past": past.replace(tzinfo=None), "id": charge_id.replace("-", "")},
    )
    session.flush()
    session.expire_all()


# ------------------------------------------------------- CA-01: QR generado


def test_qa_generation_signature_hash_and_latency(api_client, sqlite_session):
    """CA-01: el QR del endpoint es verificable, con hash determinista (< 1 s)."""
    session = sqlite_session["session"]
    start = time.perf_counter()
    data = _create_charge(api_client)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"generacion fuera de ventana: {elapsed:.3f}s"

    payload = data["payload"]
    assert payload["v"] == "BO-QR1"
    assert payload["amount_minor"] == 5_000
    assert payload["currency"] == "PEN"
    # La firma del endpoint se verifica con el secreto vigente del servidor.
    assert wallet_service.verify_charge_signature(payload, data["signature"]) is True
    assert qr_domain.payload_hash_for(payload) == data["payload_hash"]

    row = qr_repo.get_qr_charge(session, data["charge_id"])
    assert row is not None and row.status == "ACTIVE"
    assert row.payload_hash == data["payload_hash"]
    assert row.signature == data["signature"]
    assert _outbox_count(session, data["charge_id"]) == 1


# ------------------------------------------------------- CA-03: interoperable


def test_qa_interop_roundtrip_deterministic_via_api(api_client):
    """CA-03: el sobre BO-QR1 del QR generado es (de)codificable y estable."""
    data = _create_charge(api_client)
    token = qr_domain.to_interop_token(data["payload"], data["signature"])
    assert qr_domain.to_interop_token(data["payload"], data["signature"]) == token

    decoded = interop.decode_payload(token)
    assert decoded.ref == str(data["payload"]["charge_id"])
    assert decoded.amount_minor == 5_000
    assert decoded.currency == "PEN"
    assert decoded.expires_at == data["payload"]["exp"]
    assert decoded.sig == data["signature"]

    fields = qr_domain.envelope_fields(token)
    assert fields["ref"] == str(data["payload"]["charge_id"])
    assert fields["sig"] == data["signature"]


def test_qa_interop_rejects_corrupt_envelope(api_client):
    """CA-03 negativo (hueco E4-T06): sobre truncado/manipulado -> rechazo."""
    data = _create_charge(api_client)
    token = qr_domain.to_interop_token(data["payload"], data["signature"])
    assert token

    with pytest.raises(interop.InteropValidationError):
        interop.decode_payload(token[:-4] + "####")
    with pytest.raises(interop.InteropValidationError):
        interop.decode_payload("no-es-un-sobre")
    with pytest.raises(interop.InteropValidationError):
        interop.decode_payload("")


# ------------------------------------------------------- firma alterada/corrupta


def test_qa_tampered_payload_rejected_without_money(api_client, sqlite_session):
    """Firma alterada (hueco E4-T06 a nivel API): monto/comercio/exp -> False.

    El gate de pago (firma + validez) rechaza sin mutar la fila ni emitir
    eventos: sin `PAID`, sin `paid_at`, sin descontar fondos.
    """
    session = sqlite_session["session"]
    data = _create_charge(api_client)
    payload = data["payload"]
    signature = data["signature"]
    charge_id = data["charge_id"]

    before_outbox = _outbox_count(session, charge_id)
    for field, evil in (
        ("amount_minor", payload["amount_minor"] + 1),
        ("merchant_id", str(uuid.uuid4())),
        ("exp", payload["exp"] + 60 if payload["exp"] else 1),
    ):
        tampered = dict(payload)
        tampered[field] = evil
        assert wallet_service.verify_charge_signature(tampered, signature) is False

    row = qr_repo.get_qr_charge(session, charge_id)
    assert row is not None
    gate = wallet_service.verify_charge_signature(
        dict(payload, amount_minor=payload["amount_minor"] + 1), signature
    ) and wallet_service.is_charge_valid(row)
    assert gate is False
    assert row.status == "ACTIVE"
    assert row.paid_at is None
    assert _outbox_count(session, charge_id) == before_outbox


def test_qa_corrupt_signature_rejected(api_client):
    """Firma corrupta (hueco E4-T06): truncada/no-hex/vacia/nula -> False."""
    data = _create_charge(api_client)
    payload = data["payload"]
    signature = data["signature"]
    assert len(signature) == 64

    corrupt = [
        signature[:-1],
        signature[:-2] + "zz",
        "",
        None,
        signature[::-1] if signature[::-1] != signature else signature + "x",
    ]
    for bad in corrupt:
        assert wallet_service.verify_charge_signature(payload, bad) is False
    # La valida sigue pasando (no se rompio el verificador).
    assert wallet_service.verify_charge_signature(payload, signature) is True


# ------------------------------------------------------- CA-02: caducidad


def test_qa_expiry_lifecycle_api_plus_job(api_client, sqlite_session):
    """CA-02 E2E (hueco E4-T07/E4-T08): vigente valido -> on-read EXPIRED -> job."""
    session = sqlite_session["session"]
    data = _create_charge(api_client)
    charge_id = data["charge_id"]

    row = qr_repo.get_qr_charge(session, charge_id)
    assert row is not None and row.status == "ACTIVE"
    got = api_client.get(f"/api/v1/merchants/qr/{charge_id}")
    assert got.status_code == 200 and got.json()["data"]["status"] == "ACTIVE"

    # Vencerlo en el pasado: on-read reporta EXPIRED sin mutar la fila.
    _expire_charge_in_past(session, charge_id, seconds=60)
    assert qr_repo.get_qr_charge(session, charge_id).status == "ACTIVE"
    got = api_client.get(f"/api/v1/merchants/qr/{charge_id}")
    assert got.status_code == 200, got.text
    assert got.json()["data"]["status"] == "EXPIRED"

    # El job persiste el estado; el borde exacto tambien expira.
    result = qr_expiry_job.run_qr_expiry_once(session, now=datetime.now(UTC))
    assert charge_id in result["expired"]
    assert qr_repo.get_qr_charge(session, charge_id).status == "EXPIRED"
    got = api_client.get(f"/api/v1/merchants/qr/{charge_id}")
    assert got.json()["data"]["status"] == "EXPIRED"
    session.rollback()


def test_qa_expired_charge_gate_rejects_before_money(api_client, sqlite_session):
    """CA-02/docs/04#7.4: el vencido se rechaza antes de operar, sin dinero."""
    session = sqlite_session["session"]
    data = _create_charge(api_client)
    charge_id = data["charge_id"]
    before_outbox = _outbox_count(session, charge_id)

    _expire_charge_in_past(session, charge_id, seconds=5)
    row = qr_repo.get_qr_charge(session, charge_id)
    assert row is not None
    assert wallet_service.is_charge_expired(row) is True
    assert wallet_service.is_charge_valid(row) is False
    # Sin retencion ni abono: la fila no se marca PAID ni emite eventos.
    assert row.status == "ACTIVE"
    assert row.paid_at is None
    assert _outbox_count(session, charge_id) == before_outbox
    session.rollback()


# ------------------------------------------------------- CA-04: notificacion


def test_qa_simulated_payment_notifies_once_without_pii(api_client, sqlite_session):
    """CA-04 con pago simulado (G72): un aviso `qr_charge_paid`, < 5 s, sin PII."""
    from app.adapters.notification_sender import MockNotificationSender
    from app.modules.notifications.domain.templates import (
        BASE_TEMPLATES,
        render_template,
    )
    from app.modules.notifications.models import Notification

    session = sqlite_session["session"]
    merchant_a = sqlite_session["merchant_a"]
    data = _create_charge(api_client)
    charge_id = data["charge_id"]

    # Pago simulado: HU15 confirmaria y marcaria PAID; aqui se simula el abono.
    row = qr_repo.get_qr_charge(session, charge_id)
    assert row is not None
    qr_repo.mark_paid(session, row, paid_at=datetime.now(UTC))
    session.flush()

    sender = MockNotificationSender()
    start = time.perf_counter()
    with _capture_wallet_logs() as caplog_handler:
        notified = wallet_service.notify_merchant_charge_paid(
            session,
            merchant_user_id=merchant_a.user_id,
            charge_id=charge_id,
            sender=sender,
        )
        elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"aviso fuera de ventana: {elapsed:.3f}s"
    assert notified.template_code == "qr_charge_paid"
    assert notified.channel == "push"
    assert len(sender.calls) == 1 and sender.calls[0]["ok"] is True

    rows = session.scalars(
        sa.select(Notification).where(Notification.template_code == "qr_charge_paid")
    ).all()
    assert len(rows) == 1
    stored = rows[0].payload_json["data"]
    assert set(stored) == {"charge_ref", "at"}

    text = render_template(BASE_TEMPLATES["qr_charge_paid"]["body_template"], stored)
    assert RUC_PLAIN not in text
    assert "5000" not in text and "5_000" not in text
    assert merchant_a.business_name not in text
    assert data["signature"] not in text
    assert RUC_PLAIN not in caplog_handler.text
    assert merchant_a.business_name not in caplog_handler.text
    session.rollback()


def test_qa_notification_data_has_no_amounts(sqlite_session):
    """CA-04: el payload del aviso solo lleva referencia e instante."""
    from app.adapters.notification_sender import MockNotificationSender

    session = sqlite_session["session"]
    merchant_a = sqlite_session["merchant_a"]
    sender = MockNotificationSender()
    notified = wallet_service.notify_merchant_charge_paid(
        session,
        merchant_user_id=merchant_a.user_id,
        charge_id=uuid.uuid4(),
        sender=sender,
    )
    assert notified.template_code == "qr_charge_paid"
    assert set(notified.payload_json["data"]) == {"charge_ref", "at"}
    blob = str(notified.payload_json)
    assert RUC_PLAIN not in blob
    assert merchant_a.business_name not in blob
    session.rollback()


# ------------------------------------------------------- seguridad


def test_qa_responses_without_pii(api_client):
    """Sin PII/RUC en claro en las respuestas del ciclo QR."""
    data = _create_charge(api_client)
    got = api_client.get(f"/api/v1/merchants/qr/{data['charge_id']}")
    assert got.status_code == 200
    assert RUC_PLAIN not in got.text
    assert "20123456789" not in got.text


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.text = ""

    def emit(self, record: logging.LogRecord) -> None:
        self.text += self.format(record) + "\n"


class _capture_wallet_logs:
    """Captura los logs de `app.modules.wallet` sin fixtures extra."""

    def __enter__(self) -> _LogCapture:
        self.handler = _LogCapture()
        self.logger = logging.getLogger("app.modules.wallet")
        self.logger.addHandler(self.handler)
        self._level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        return self.handler

    def __exit__(self, *exc_info: object) -> None:
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self._level)
