"""QA del flujo KYC (E1-T07, HU01 CA-01..CA-04).

Suite a nivel de flujo sobre `POST /api/v1/auth/kyc/*` usando solo el mock
del adaptador (`MockKycProvider` y stubs que lo especializan, sin red ni
`kyc-service/`):

- CA-01 (documento/OCR): `document/validate` OK vs ilegible.
- CA-02 (liveness): `evaluate` por paso + `submit` con liveness fallido.
- CA-03 (match): `submit` con match fallido (liveness por pasos OK, pero
  `overall_result` decide la aprobacion).
- CA-04 (excepcion): KYC caido (503) / timeout (504) neutros + reintentos.
- Reglas: sin frames/imagenes/PII en respuestas ni logs; sin persistencia
  de frames; un fallo (o timeout) no deja alta parcial ni bloquea el alta
  al reintentar con el mismo documento.

Datos 100% sinteticos (`@example.com`, documentos `DNI` + sufijo aleatorio).
"""

from __future__ import annotations

import base64
import logging
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.adapters.kyc_provider import (
    FullVerification,
    KycTimeoutError,
    MockKycProvider,
)
from app.core.db import Base, get_db
from app.main import app
from app.modules.identity.api.kyc import get_kyc_provider
from app.modules.identity.service import kyc_proxy

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100).decode()
JPEG_B64 = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 100).decode()


class MatchFailedKycProvider(MockKycProvider):
    """Liveness por pasos OK pero `overall_result=False` por match facial."""

    def verify_full(self, *, session_id, payload=None):
        steps = ["blink", "turn-left", "smile"]
        return FullVerification(
            session_id=session_id,
            overall_result=False,
            distance=0.91,
            detail_code="MATCH_FAILED",
            steps_verified=list(steps),
            steps_total=list(steps),
            failed_step=None,
            step_results={step: {"passed": True, "reason": "ok"} for step in steps},
            overall_reason="MATCH_FAILED",
        )


class FlakyKycProvider(MockKycProvider):
    """Falla una vez con timeout y luego responde como `success`."""

    def __init__(self) -> None:
        super().__init__(mode="success")
        self.calls = 0

    def verify_full(self, *, session_id, payload=None):
        self.calls += 1
        if self.calls == 1:
            raise KycTimeoutError("timeout transitorio simulado")
        return super().verify_full(session_id=session_id, payload=payload)


def _payload(*, doc_number: str | None = None, email: str | None = None) -> dict:
    suffix = uuid.uuid4().hex[:8]
    return {
        "challenge_token": "tok-abc",
        "document": {
            "type": "DNI",
            "number": doc_number or f"DNI{suffix}",
            "image_b64": PNG_B64,
        },
        "segments": [{"task": "blink", "image_b64": JPEG_B64}],
        "applicant": {
            "first_name": "Test",
            "last_name": "User",
            "email": email or f"qa.{suffix}@example.com",
            "phone": "999888777",
        },
    }


def _sqlite_session():
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
def sqlite_session():
    yield from _sqlite_session()


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


def _override_provider(provider) -> None:
    app.dependency_overrides[get_kyc_provider] = lambda: provider


def _restore_success_provider() -> None:
    app.dependency_overrides[get_kyc_provider] = lambda: MockKycProvider(mode="success")


def _count(session, table_name: str) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[table_name]))


# ---------------------------------------------------------------- CA-01..CA-03: exito
def test_qa_submit_success_onboards_user_account_and_event(kyc_client, sqlite_session):
    from app.modules.accounts import repository as accounts_repo
    from app.modules.identity import repository as repo

    payload = _payload()
    resp = kyc_client.post("/api/v1/auth/kyc/submit", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["overall_result"] is True
    assert data["status"] == "ONBOARDED"
    assert data["detail_code"] == "OK"
    assert data["user_id"] and data["account_id"]

    user = repo.get_user(sqlite_session, data["user_id"])
    assert user is not None and user.status == "PENDING_ACTIVATION"
    assert user.kyc_status == "VERIFIED"
    assert accounts_repo.get_account(sqlite_session, data["account_id"]) is not None

    verifications = repo.list_by_user(sqlite_session, user.id)
    assert len(verifications) == 1 and verifications[0].overall_result is True
    outbox = sqlite_session.execute(
        sa.select(Base.metadata.tables["shared.outbox"]).where(
            Base.metadata.tables["shared.outbox"].c.event_type == "kyc.completed"
        )
    ).all()
    assert len(outbox) == 1


# ---------------------------------------------------------------- CA-01: documento/OCR
def test_qa_document_validate_ok_and_unreadable(kyc_client: TestClient):
    resp = kyc_client.post("/api/v1/auth/kyc/document/validate", json={"image_b64": PNG_B64})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["is_valid"] is True
    assert data["issues"] == []

    _override_provider(MockKycProvider(mode="failure"))
    try:
        resp = kyc_client.post("/api/v1/auth/kyc/document/validate", json={"image_b64": JPEG_B64})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["is_valid"] is False
        assert data["issues"] and all(isinstance(issue, str) for issue in data["issues"])
    finally:
        _restore_success_provider()


# ---------------------------------------------------------------- CA-02: liveness por paso
def test_qa_evaluate_liveness_pass_and_fail(kyc_client: TestClient):
    body = {"challenge_token": "tok", "step": "blink", "frames_b64": [JPEG_B64] * 5}
    resp = kyc_client.post("/api/v1/auth/kyc/evaluate", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["passed"] is True and data["step"] == "blink"

    _override_provider(MockKycProvider(mode="failure"))
    try:
        resp = kyc_client.post("/api/v1/auth/kyc/evaluate", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["passed"] is False
        assert data["step"] == "blink"
        assert data["reason"]
    finally:
        _restore_success_provider()


# ---------------------------------------------------------------- CA-02: liveness fallido en submit
def test_qa_liveness_failed_submit_rejected_without_user(kyc_client, sqlite_session):
    from app.modules.identity.models import KycVerification

    _override_provider(MockKycProvider(mode="failure"))
    try:
        resp = kyc_client.post("/api/v1/auth/kyc/submit", json=_payload())
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["overall_result"] is False
        assert data["status"] == "REJECTED"
        assert data["detail_code"] == "LIVENESS_FAILED"
        assert data["failed_step"] == "blink"
        assert data["user_id"] is None and data["account_id"] is None
    finally:
        _restore_success_provider()

    assert _count(sqlite_session, "identity.users") == 0
    assert _count(sqlite_session, "accounts.accounts") == 0
    assert _count(sqlite_session, "shared.outbox") == 0
    verifications = list(sqlite_session.scalars(sa.select(KycVerification)).all())
    assert len(verifications) == 1
    assert verifications[0].overall_result is False
    assert verifications[0].failure_reason == "LIVENESS_FAILED"
    assert verifications[0].user_id is None


# ---------------------------------------------------------------- CA-03: match fallido en submit
def test_qa_match_failed_submit_rejected_overall_decides(kyc_client, sqlite_session):
    """Liveness por pasos OK pero `overall_result=False`: manda el overall."""
    from app.modules.identity.models import KycVerification

    _override_provider(MatchFailedKycProvider())
    try:
        resp = kyc_client.post("/api/v1/auth/kyc/submit", json=_payload())
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["overall_result"] is False
        assert data["status"] == "REJECTED"
        assert data["detail_code"] == "MATCH_FAILED"
        # El liveness paso en todos los pasos: igual se rechaza por el match.
        assert data["steps_verified"] == ["blink", "turn-left", "smile"]
        assert data["failed_step"] is None
        assert data["user_id"] is None and data["account_id"] is None
    finally:
        _restore_success_provider()

    assert _count(sqlite_session, "identity.users") == 0
    assert _count(sqlite_session, "accounts.accounts") == 0
    assert _count(sqlite_session, "shared.outbox") == 0
    verifications = list(sqlite_session.scalars(sa.select(KycVerification)).all())
    assert len(verifications) == 1
    assert verifications[0].overall_result is False
    assert verifications[0].failure_reason == "MATCH_FAILED"


# ---------------------------------------------------------------- CA-04: KYC caido / timeout
def test_qa_kyc_down_submit_503_neutral(kyc_client, sqlite_session):
    _override_provider(MockKycProvider(mode="down"))
    try:
        resp = kyc_client.post("/api/v1/auth/kyc/submit", json=_payload())
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "KYC_UNAVAILABLE"
        for leaked in ("Traceback", "X-API-Key", "change-me", "Exception"):
            assert leaked not in resp.text, f"fuga de interno: {leaked}"
    finally:
        _restore_success_provider()
    assert _count(sqlite_session, "identity.users") == 0


def test_qa_kyc_timeout_submit_504_neutral(kyc_client, sqlite_session):
    _override_provider(MockKycProvider(mode="timeout"))
    try:
        resp = kyc_client.post("/api/v1/auth/kyc/submit", json=_payload())
        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "KYC_UNAVAILABLE"
        assert "Traceback" not in resp.text
    finally:
        _restore_success_provider()
    assert _count(sqlite_session, "identity.users") == 0


# ---------------------------------------------------------------- Reintentos
def test_qa_retry_after_timeout_succeeds_without_partial_state(kyc_client, sqlite_session):
    """Timeout y reintento con el mismo payload: un solo usuario, sin parcial."""
    _override_provider(FlakyKycProvider())
    try:
        payload = _payload()
        first = kyc_client.post("/api/v1/auth/kyc/submit", json=payload)
        assert first.status_code == 504
        assert _count(sqlite_session, "identity.users") == 0

        second = kyc_client.post("/api/v1/auth/kyc/submit", json=payload)
        assert second.status_code == 200, second.text
        data = second.json()["data"]
        assert data["overall_result"] is True
        assert data["status"] == "ONBOARDED"
    finally:
        _restore_success_provider()
    assert _count(sqlite_session, "identity.users") == 1


def test_qa_retry_after_liveness_failure_same_document(kyc_client, sqlite_session):
    """Un `submit` rechazado no bloquea el alta al reintentar (mismo documento)."""
    from app.modules.identity.models import KycVerification

    doc = f"DNI{uuid.uuid4().hex[:8]}"
    email = f"retry.{uuid.uuid4().hex[:8]}@example.com"

    _override_provider(MockKycProvider(mode="failure"))
    try:
        rejected = kyc_client.post(
            "/api/v1/auth/kyc/submit", json=_payload(doc_number=doc, email=email)
        )
        assert rejected.status_code == 200
        assert rejected.json()["data"]["status"] == "REJECTED"
    finally:
        _restore_success_provider()

    retry = kyc_client.post("/api/v1/auth/kyc/submit", json=_payload(doc_number=doc, email=email))
    assert retry.status_code == 200, retry.text
    assert retry.json()["data"]["status"] == "ONBOARDED"

    assert _count(sqlite_session, "identity.users") == 1
    assert len(list(sqlite_session.scalars(sa.select(KycVerification)).all())) == 2


# ---------------------------------------------------------------- Regla 7: sin frames/PII en respuestas ni logs
def test_qa_no_frames_images_or_tokens_in_responses_and_logs(kyc_client: TestClient, caplog):
    payload = _payload()
    with caplog.at_level(logging.INFO):
        resp = kyc_client.post("/api/v1/auth/kyc/submit", json=payload)
    assert resp.status_code == 200, resp.text
    assert PNG_B64 not in resp.text
    assert JPEG_B64 not in resp.text
    assert payload["challenge_token"] not in resp.text
    assert PNG_B64 not in caplog.text
    assert JPEG_B64 not in caplog.text


# ---------------------------------------------------------------- Contrato OpenAPI
def test_qa_openapi_kyc_contract(kyc_client: TestClient):
    spec = kyc_client.get("/openapi.json").json()
    for path in (
        "/api/v1/auth/kyc/challenge",
        "/api/v1/auth/kyc/evaluate",
        "/api/v1/auth/kyc/document/validate",
        "/api/v1/auth/kyc/submit",
    ):
        assert path in spec["paths"], f"falta en OpenAPI: {path}"
    schemas = spec["components"]["schemas"]
    assert {"overall_result", "status", "user_id", "account_id"} <= set(
        schemas["KycSubmitData"]["properties"]
    )
