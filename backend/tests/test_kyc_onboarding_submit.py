"""Alta de cliente + persistencia KYC en `POST /auth/kyc/submit` (E1-T24).

HU01 CA-03 (match/alta coherente) y base de HU02 CA-01/CA-04; CA-01 de HU01
soporta OCR/frames sin persistirlos.

- Dominio puro: hash/enmascarado del numero de documento (normalizacion,
  determinismo, sin claro).
- API (SQLite ATTACH + `TestClient`): camino feliz (User+Credential+Account+
  KycVerification+OTP `ACTIVATION`+`kyc.completed` en la misma transaccion y
  respuesta `user_id`/`status`/`account_id`); `overall_result=false` guarda
  el fallo y no crea usuario/cuenta/evento; duplicados documento/email ->
  409 tipado; atomicidad (fallo de `ledger` revierte todo); sin numero en
  claro ni frames en `kyc_verifications`.
"""

from __future__ import annotations

import base64
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.adapters.kyc_provider import MockKycProvider
from app.core.db import Base, get_db
from app.main import app
from app.modules.identity.api.kyc import get_kyc_provider
from app.modules.identity.service import kyc_onboarding, kyc_proxy

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100).decode()
JPEG_B64 = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 100).decode()


def _payload(*, doc_number: str | None = None, email: str | None = None) -> dict:
    return {
        "challenge_token": "tok-abc",
        "document": {
            "type": "DNI",
            "number": doc_number or f"DNI{uuid.uuid4().hex[:8]}",
            "image_b64": PNG_B64,
        },
        "segments": [{"task": "blink", "image_b64": JPEG_B64}],
        "applicant": {
            "first_name": "Ana",
            "last_name": "Quispe",
            "email": email or f"ana.{uuid.uuid4().hex[:8]}@example.com",
            "phone": "999888777",
        },
    }


@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con los schemas que toca el alta (ATTACH)."""
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401
    import app.modules.audit.models as _au  # noqa: F401
    import app.modules.identity.models as _i  # noqa: F401
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.notifications.models as _n  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("identity", "accounts", "ledger", "shared", "notifications", "audit"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["identity.users"],
            Base.metadata.tables["identity.credentials"],
            Base.metadata.tables["identity.kyc_verifications"],
            Base.metadata.tables["identity.otp_codes"],
            Base.metadata.tables["accounts.accounts"],
            Base.metadata.tables["accounts.account_balances"],
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["notifications.notifications"],
            Base.metadata.tables["notifications.notification_templates"],
            Base.metadata.tables["audit.audit_log"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def kyc_client(monkeypatch, sqlite_session):
    """TestClient con `get_db` -> SQLite y proveedor mock en `success`."""
    monkeypatch.delenv("KYC_RATE_LIMIT_MAX_REQUESTS", raising=False)
    monkeypatch.delenv("KYC_RATE_LIMIT_WINDOW_SECONDS", raising=False)
    kyc_proxy.reset_rate_limits()
    app.dependency_overrides[get_kyc_provider] = lambda: MockKycProvider(mode="success")

    def _override_db():
        yield sqlite_session

    app.dependency_overrides[get_db] = _override_db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_kyc_provider, None)
        app.dependency_overrides.pop(get_db, None)
        kyc_proxy.reset_rate_limits()


def _count(session, table_name: str) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[table_name]))


# ---------------------------------------------------------------- Dominio puro
def test_hash_and_mask_document_number():
    raw = "12.345.678"
    hashed = kyc_onboarding.hash_document_number(raw)
    assert hashed == kyc_onboarding.hash_document_number("12345678")
    assert len(hashed) == 64
    assert raw not in hashed
    assert "12345678" not in hashed

    masked = kyc_onboarding.mask_document_number(raw)
    assert masked == "****5678"
    assert "12345" not in masked

    assert kyc_onboarding.hash_document_number("12345678") != kyc_onboarding.hash_document_number(
        "87654321"
    )


def test_hash_document_number_uses_pepper(monkeypatch):
    """Mismo documento + mismo pepper -> mismo hash; distinto pepper -> distinto."""
    raw = "12345678"
    monkeypatch.setenv(kyc_onboarding.DOC_HASH_PEPPER_ENV, "pepper-uno")
    first = kyc_onboarding.hash_document_number(raw)
    assert first == kyc_onboarding.hash_document_number(raw), "determinista (UQ)"
    assert len(first) == 64 and raw not in first

    monkeypatch.setenv(kyc_onboarding.DOC_HASH_PEPPER_ENV, "pepper-dos")
    other = kyc_onboarding.hash_document_number(raw)
    assert other != first, "el pepper cambia el hash (no reversible sin el)"


def test_openapi_submit_contract_includes_onboarding_fields(kyc_client):
    """OpenAPI documenta el request ampliado y los campos de respuesta (E1-T24)."""
    schemas = kyc_client.get("/openapi.json").json()["components"]["schemas"]
    assert {"challenge_token", "document", "segments", "applicant"} <= set(
        schemas["KycSubmitRequest"]["properties"]
    )
    assert "number" in schemas["KycDocumentPayload"]["properties"]
    assert {"first_name", "last_name", "email", "phone"} <= set(
        schemas["KycApplicantPayload"]["properties"]
    )
    assert {"user_id", "status", "account_id"} <= set(schemas["KycSubmitData"]["properties"])


# ---------------------------------------------------------------- Camino feliz
def test_submit_success_onboards_and_persists_same_transaction(kyc_client, sqlite_session):
    from app.modules.accounts import repository as accounts_repo
    from app.modules.identity import repository as repo

    payload = _payload()
    resp = kyc_client.post("/api/v1/auth/kyc/submit", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["overall_result"] is True
    assert data["status"] == "ONBOARDED"
    assert data["user_id"] and data["account_id"]

    user = repo.get_user(sqlite_session, data["user_id"])
    assert user is not None and user.status == "PENDING_ACTIVATION"
    assert user.kyc_status == "VERIFIED"
    assert user.doc_number_hash == kyc_onboarding.hash_document_number(
        payload["document"]["number"]
    )
    assert user.doc_number_masked == kyc_onboarding.mask_document_number(
        payload["document"]["number"]
    )

    assert repo.get_credential(sqlite_session, user.id) is not None

    account = accounts_repo.get_account(sqlite_session, data["account_id"])
    assert account is not None and account.user_id == user.id

    verification = repo.list_by_user(sqlite_session, user.id)
    assert len(verification) == 1
    assert verification[0].overall_result is True
    assert verification[0].failure_reason is None

    pending = repo.get_active_otp(sqlite_session, user.id, "ACTIVATION")
    assert pending is not None and pending.status == "PENDING"

    outbox = sqlite_session.execute(
        sa.select(Base.metadata.tables["shared.outbox"]).where(
            Base.metadata.tables["shared.outbox"].c.event_type == "kyc.completed"
        )
    ).all()
    assert len(outbox) == 1


def test_submit_failure_persists_verification_without_user(kyc_client, sqlite_session):
    from app.modules.identity.models import KycVerification

    app.dependency_overrides[get_kyc_provider] = lambda: MockKycProvider(mode="failure")
    payload = _payload()
    resp = kyc_client.post("/api/v1/auth/kyc/submit", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["overall_result"] is False
    assert data["status"] == "REJECTED"
    assert data["user_id"] is None and data["account_id"] is None

    assert _count(sqlite_session, "identity.users") == 0
    assert _count(sqlite_session, "accounts.accounts") == 0
    verifications = list(sqlite_session.scalars(sa.select(KycVerification)).all())
    assert len(verifications) == 1
    assert verifications[0].overall_result is False
    assert verifications[0].failure_reason
    assert verifications[0].user_id is None
    assert _count(sqlite_session, "shared.outbox") == 0


# ---------------------------------------------------------------- Duplicados
def test_duplicate_document_returns_409(kyc_client, sqlite_session):
    doc = f"DNI{uuid.uuid4().hex[:8]}"
    assert (
        kyc_client.post("/api/v1/auth/kyc/submit", json=_payload(doc_number=doc)).status_code == 200
    )
    resp = kyc_client.post(
        "/api/v1/auth/kyc/submit", json=_payload(doc_number=doc, email="otro@example.com")
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_DOCUMENT"
    assert _count(sqlite_session, "identity.users") == 1


def test_duplicate_email_returns_409(kyc_client, sqlite_session):
    email = f"dup.{uuid.uuid4().hex[:8]}@example.com"
    assert kyc_client.post("/api/v1/auth/kyc/submit", json=_payload(email=email)).status_code == 200
    resp = kyc_client.post(
        "/api/v1/auth/kyc/submit", json=_payload(doc_number="DNI00009999", email=email)
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_EMAIL"
    assert _count(sqlite_session, "identity.users") == 1


@pytest.mark.parametrize(
    ("constraint", "expected_code"),
    [
        ("uq_users_doc_number_hash", "DUPLICATE_DOCUMENT"),
        ("uq_users_email", "DUPLICATE_EMAIL"),
    ],
)
def test_race_integrity_error_maps_to_typed_409(
    kyc_client, monkeypatch, constraint: str, expected_code: str
):
    """Carrera check-then-insert: la violacion UNIQUE de BD -> 409 tipado, no 500."""
    from sqlalchemy.exc import IntegrityError

    def _boom(*_args, **_kwargs):
        raise IntegrityError(
            "INSERT INTO identity.users ...",
            {},
            Exception(f'duplicate key value violates unique constraint "{constraint}"'),
        )

    monkeypatch.setattr(kyc_onboarding, "onboard_customer", _boom)
    resp = kyc_client.post("/api/v1/auth/kyc/submit", json=_payload())
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == expected_code


# ---------------------------------------------------------------- Atomicidad / no persistir
def test_ledger_failure_rolls_back_everything(kyc_client, sqlite_session, monkeypatch):
    from app.modules.ledger import service as ledger_service

    def _boom(*_args, **_kwargs):
        raise RuntimeError("ledger caido")

    monkeypatch.setattr(ledger_service, "ensure_customer_accounts", _boom)
    with pytest.raises(RuntimeError, match="ledger caido"):
        kyc_client.post("/api/v1/auth/kyc/submit", json=_payload())

    assert _count(sqlite_session, "identity.users") == 0
    assert _count(sqlite_session, "identity.kyc_verifications") == 0
    assert _count(sqlite_session, "shared.outbox") == 0


def test_document_number_and_frames_not_persisted(kyc_client, sqlite_session):
    from app.modules.identity.models import KycVerification, User

    raw_number = "12345678"
    resp = kyc_client.post("/api/v1/auth/kyc/submit", json=_payload(doc_number=raw_number))
    assert resp.status_code == 200, resp.text
    user_id = resp.json()["data"]["user_id"]

    user = sqlite_session.get(User, uuid.UUID(user_id))
    assert user is not None
    assert raw_number not in user.doc_number_hash
    assert raw_number not in (user.doc_number_masked or "")

    verification = sqlite_session.scalars(
        sa.select(KycVerification).where(KycVerification.user_id == user.id)
    ).one()
    payload_text = " ".join(
        str(part)
        for part in (
            verification.document_json,
            verification.liveness_json,
            verification.face_match_json,
            verification.challenge_token_hash,
        )
    )
    assert raw_number not in payload_text
    assert "tok-abc" not in payload_text
    assert "image" not in payload_text.lower()
    assert "frame" not in payload_text.lower()
    assert "base64" not in payload_text.lower()
    assert resp.text.count(raw_number) == 0
