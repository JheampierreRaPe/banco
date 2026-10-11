"""Fix validador sprint-2 (HU11, frontera `credits`, sin commit).

- DEFECTO 1 (MEDIO, funcional): `GET .../contract` hardcodeaba
  `status: GENERATED` en `documents.to_payload`, aun tras firmar (las
  filas ya estaban `SIGNED`): la UI mostraba "Aprobado" en un contrato
  firmado. Regresion: tras firmar, el GET devuelve `SIGNED` (y antes,
  `GENERATED`); sin el fix falla.
- DEFECTO 2 (MEDIO, seguridad): el `request_hash` de idempotencia del
  `POST .../sign` persistia `sha256(PIN)` sin sal (reversible por fuerza
  bruta en un PIN de 6 digitos). Regresion: el hash almacenado NO es el
  `sha256` en claro, el PIN no aparece en hash/snapshot/logs, y la
  idempotencia sigue intacta (replay + `409` con cuerpo distinto).

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron del
repo); `get_db` y `get_current_user_id` (credits) se inyectan via
`app.dependency_overrides`; la fachada del PIN se sustituye
(`p == PIN`) y el sender de correo es el mock. Sin red. Sin PII ni
secretos en logs.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.modules.audit.models as _audit
import app.modules.credits.models as _credits
import app.modules.identity.models as _identity
import app.modules.notifications.models as _notifications
import app.modules.shared.models as _shared
from app.adapters.credit_bureau import BureauScore, MockCreditBureau
from app.adapters.notification_sender import MockNotificationSender
from app.core.db import Base, get_db
from app.main import app
from app.modules.credits import repository as repo
from app.modules.credits import service as catalog
from app.modules.credits.api import get_current_user_id
from app.modules.credits.service import applications as apps_service
from app.modules.credits.service import contract_signing as sign_flow
from app.modules.credits.service import signing as signing_service
from app.modules.identity.domain import nonce as nonce_domain
from app.modules.shared.repository.idempotency import find_key

USER = uuid.uuid4()
PIN = "482913"  # 6 digitos: el `sha256` en claro seria reversible por fuerza bruta
EMAIL = "titular.e3fix@example.com"

TABLES = [
    "credits.loan_products",
    "credits.loan_applications",
    "credits.contracts",
    "shared.outbox",
    "shared.processed_events",
    "shared.idempotency_keys",
    "notifications.notifications",
    "notifications.notification_templates",
    "audit.audit_log",
    "identity.users",
    "identity.credentials",
]

assert _audit is not None and _credits is not None
assert _identity is not None and _notifications is not None and _shared is not None


def _bureau_for(user_id: uuid.UUID):
    return MockCreditBureau(
        profiles={str(user_id): BureauScore(score=780, deudas_minor=0, morosidad=False)}
    )


def _make_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("credits", "shared", "notifications", "audit", "identity"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    return engine


def _seed(session: Session) -> None:
    from app.modules.identity.models import User

    repo.seed_demo_products(session)
    session.add(
        User(
            id=USER,
            doc_type="DNI",
            doc_number_hash="e3fix-user-hash",
            doc_number_masked="****1234",
            first_name="Test",
            last_name="User",
            email=EMAIL,
            status="ACTIVE",
            kyc_status="VERIFIED",
        )
    )
    session.commit()


@pytest.fixture()
def app_client(monkeypatch):
    nonce_domain.reset_nonces()
    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    _seed(session)
    mock_sender = MockNotificationSender()
    monkeypatch.setattr(signing_service, "default_pin_verifier", lambda s, u, p: p == PIN)
    monkeypatch.setattr(sign_flow, "_default_email_sender", lambda: mock_sender)
    try:

        def _override_db():
            yield session

        def _override_user():
            return USER

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_user_id] = _override_user
        with TestClient(app) as client:
            yield {"client": client, "session": session, "sender": mock_sender}
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        nonce_domain.reset_nonces()
        session.close()
        engine.dispose()


def _approved_app_id(session: Session) -> str:
    product_id = str(catalog.get_product(session, code="PERSONAL_LIBRE")["id"])
    view = apps_service.submit_application(
        session,
        USER,
        product_id=product_id,
        requested_amount_minor=1_000_000,
        term_months=12,
        total_income_minor=2_000_000,
        total_debt_minor=100_000,
        bureau_authorized=True,
        bureau_provider=_bureau_for(USER),
    )
    assert view["status"] == "APPROVED"
    session.commit()
    return view["id"]


def _email_copies(session: Session) -> list:
    table = Base.metadata.tables["notifications.notifications"]
    session.expire_all()
    return list(
        session.execute(
            sa.select(table.c.template_code)
            .where(table.c.template_code == "credit_contract_signed")
            .order_by(table.c.created_at)
        ).all()
    )


def test_get_contract_status_signed_after_sign(app_client):
    """DEFECTO 1: el GET refleja el estado del juego (`SIGNED` tras firmar)."""
    client, session = app_client["client"], app_client["session"]
    app_id = _approved_app_id(session)

    gen = client.post(
        f"/api/v1/credits/applications/{app_id}/contract",
        headers={"Idempotency-Key": "k-fix-status-gen"},
    )
    assert gen.status_code == 201, gen.text
    assert gen.json()["data"]["status"] == "GENERATED"

    before = client.get(f"/api/v1/credits/applications/{app_id}/contract")
    assert before.status_code == 200, before.text
    assert before.json()["data"]["status"] == "GENERATED"
    assert {doc["status"] for doc in before.json()["data"]["documents"]} == {"GENERATED"}

    signed = client.post(
        f"/api/v1/credits/applications/{app_id}/sign",
        json={"pin": PIN},
        headers={"Idempotency-Key": "k-fix-status-sign"},
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["data"]["status"] == "SIGNED"

    after = client.get(f"/api/v1/credits/applications/{app_id}/contract")
    assert after.status_code == 200, after.text
    assert after.json()["data"]["status"] == "SIGNED"
    assert {doc["status"] for doc in after.json()["data"]["documents"]} == {"SIGNED"}


def test_request_hash_is_pin_hmac_and_idempotency_holds(app_client, caplog):
    """DEFECTO 2: el `request_hash` no es `sha256(PIN)` reversible; idempotencia intacta."""
    client, session = app_client["client"], app_client["session"]
    app_id = _approved_app_id(session)
    gen = client.post(
        f"/api/v1/credits/applications/{app_id}/contract",
        headers={"Idempotency-Key": "k-fix-hmac-gen"},
    )
    assert gen.status_code == 201, gen.text

    with caplog.at_level("INFO"):
        first = client.post(
            f"/api/v1/credits/applications/{app_id}/sign",
            json={"pin": PIN},
            headers={"Idempotency-Key": "k-fix-hmac"},
        )
    assert first.status_code == 200, first.text
    first_data = first.json()["data"]
    assert first.json()["meta"]["replayed"] is False

    row = find_key(session, key="k-fix-hmac", user_id=USER)
    assert row is not None
    plain_sha256 = hashlib.sha256(PIN.encode("utf-8")).hexdigest()
    assert row.request_hash != plain_sha256
    assert PIN not in row.request_hash
    snapshot = row.response_snapshot
    assert isinstance(snapshot, dict) and snapshot
    assert PIN not in json.dumps(snapshot, ensure_ascii=False)
    assert PIN not in caplog.text

    # El digest distingue un PIN de otro (semantica de idempotencia intacta).
    assert sign_flow._pin_digest("111111") != sign_flow._pin_digest("222222")
    assert sign_flow._pin_digest(PIN) != plain_sha256

    # Replay: misma clave + mismo cuerpo -> misma respuesta, sin re-firmar ni reenviar.
    replay = client.post(
        f"/api/v1/credits/applications/{app_id}/sign",
        json={"pin": PIN},
        headers={"Idempotency-Key": "k-fix-hmac"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["meta"]["replayed"] is True
    assert replay.json()["data"] == first_data
    assert len(_email_copies(session)) == 1

    # Conflicto: misma clave + PIN distinto -> 409 (el cuerpo cambio).
    conflict = client.post(
        f"/api/v1/credits/applications/{app_id}/sign",
        json={"pin": "000000"},
        headers={"Idempotency-Key": "k-fix-hmac"},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
