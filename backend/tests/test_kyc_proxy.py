"""Proxy KYC pre-registro (E1-T02, HU01 CA-01/CA-02).

- Sin BD ni auth (pre-registro): `TestClient(app)` + `dependency_overrides`
  de `get_kyc_provider` con `MockKycProvider` (patron de
  `tests/test_accounts_endpoints.py`).
- Casos: challenge valido (token/steps/expires_in); submit exitoso via mock;
  payload invalido (imagen corrupta / tamano excedido / base64 roto -> 422
  `VALIDATION_ERROR`); servicio caido (mock `down` -> 503 `KYC_UNAVAILABLE`,
  timeout -> 504, sin filtrar internos); rate limit (429); API key ausente en
  todas las respuestas; OpenAPI expone ambas rutas.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.adapters.kyc_provider import (
    FullVerification,
    KycInvalidError,
    LivenessEvaluation,
    MockKycProvider,
)
from app.core.db import get_db
from app.main import app
from app.modules.identity.api.kyc import get_kyc_provider
from app.modules.identity.service import kyc_proxy

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100).decode()
JPEG_B64 = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 100).decode()
CORRUPT_B64 = base64.b64encode(b"esto-no-es-una-imagen").decode()


class RecordingKycProvider(MockKycProvider):
    """Mock que registra los payloads reenviados al adaptador (sin red)."""

    def __init__(self, mode: str = "success") -> None:
        super().__init__(mode=mode)
        self.calls: list = []

    def verify_full(self, *, session_id, payload=None):
        self.calls.append(("verify_full", session_id, payload))
        return super().verify_full(session_id=session_id, payload=payload)

    def evaluate_liveness(self, *, session_id, task, payload=None):
        self.calls.append(("evaluate", session_id, task, payload))
        return super().evaluate_liveness(session_id=session_id, task=task, payload=payload)

    def validate_document(self, *, session_id, image_b64=""):
        self.calls.append(("validate_document", session_id, image_b64))
        return super().validate_document(session_id=session_id, image_b64=image_b64)


class InvalidKycProvider(MockKycProvider):
    """Simula un 4xx del microservicio con `step`/`reason` en el `detail`."""

    def verify_full(self, *, session_id, payload=None):
        raise KycInvalidError(
            "KYC rechazo la solicitud (400)", step="blink", reason="not_enough_frames"
        )

    def evaluate_liveness(self, *, session_id, task, payload=None):
        return LivenessEvaluation(
            session_id=session_id, passed=False, step=task, reason="no_face_detected"
        )

    def validate_document(self, *, session_id, image_b64=""):
        raise KycInvalidError("KYC rechazo la solicitud (400)", reason="not_an_image")


def _submit_payload(token: str = "tok-abc", doc_b64: str = PNG_B64) -> dict:
    return {
        "challenge_token": token,
        "document": {"type": "DNI", "number": "12345678", "image_b64": doc_b64},
        "segments": [{"task": "blink", "image_b64": JPEG_B64}],
        "applicant": {
            "first_name": "Ana",
            "last_name": "Quispe",
            "email": "ana@example.com",
            "phone": "999888777",
        },
    }


@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada para el alta KYC (schemas ATTACH; E1-T24)."""
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401
    import app.modules.audit.models as _au  # noqa: F401
    import app.modules.identity.models as _i  # noqa: F401
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.notifications.models as _n  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401
    from app.core.db import Base

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
    """TestClient con proveedor mock en modo `success` y rate limit limpio."""
    monkeypatch.delenv("KYC_RATE_LIMIT_MAX_REQUESTS", raising=False)
    monkeypatch.delenv("KYC_RATE_LIMIT_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("KYC_MAX_IMAGE_BYTES", raising=False)
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


def _override_provider(mode: str):
    app.dependency_overrides[get_kyc_provider] = lambda: MockKycProvider(mode=mode)


def test_challenge_returns_token_steps_expires(kyc_client: TestClient):
    resp = kyc_client.post("/api/v1/auth/kyc/challenge", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"data", "meta"}
    data = body["data"]
    assert isinstance(data["token"], str) and data["token"]
    assert isinstance(data["steps"], list) and len(data["steps"]) > 0
    assert isinstance(data["expires_in"], int) and data["expires_in"] > 0
    assert body["meta"]["request_id"]


def test_submit_success_via_mock(kyc_client: TestClient):
    payload = _submit_payload()
    resp = kyc_client.post("/api/v1/auth/kyc/submit", json=payload)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["overall_result"] is True
    assert data["detail_code"] == "OK"
    # La imagen enviada nunca se refleja en la respuesta.
    assert payload["document"]["image_b64"] not in resp.text


def test_submit_success_propagates_liveness_detail(kyc_client: TestClient):
    resp = kyc_client.post("/api/v1/auth/kyc/submit", json=_submit_payload())
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["steps_verified"] == ["blink", "turn-left", "smile"]
    assert data["steps_total"] == ["blink", "turn-left", "smile"]
    assert data["failed_step"] is None
    assert data["step_results"]["blink"]["passed"] is True
    assert data["overall_reason"] == "OK"


class GuidedKycProvider(MockKycProvider):
    """Proveedor con la forma REAL guiada del microservicio (listas de pasos)."""

    def verify_full(self, *, session_id, payload=None):
        return FullVerification(
            session_id=session_id,
            overall_result=False,
            distance=0.81,
            detail_code="LIVENESS_FAILED",
            steps_verified=["arriba"],
            steps_total=["arriba", "abajo", "parpadeo"],
            failed_step="abajo",
            step_results={
                "arriba": {"passed": True, "reason": "ok"},
                "abajo": {"passed": False, "reason": "movement_not_detected"},
                "parpadeo": {"passed": False, "reason": "no_blink"},
            },
            overall_reason="Falló: abajo, parpadeo",
        )


def test_submit_real_guided_payload_propagates_lists_and_failure(kyc_client: TestClient):
    """El payload guiado real (listas) no revienta el submit y propaga el fallo."""
    app.dependency_overrides[get_kyc_provider] = lambda: GuidedKycProvider()
    try:
        resp = kyc_client.post("/api/v1/auth/kyc/submit", json=_submit_payload())
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["steps_verified"] == ["arriba"]
        assert data["steps_total"] == ["arriba", "abajo", "parpadeo"]
        assert data["failed_step"] == "abajo"
        assert data["overall_reason"] == "Falló: abajo, parpadeo"
        assert data["step_results"]["abajo"]["passed"] is False
    finally:
        _override_provider("success")


def test_submit_frames_burst_reaches_adapter_and_keeps_image_b64(kyc_client: TestClient):
    recording = RecordingKycProvider()
    app.dependency_overrides[get_kyc_provider] = lambda: recording
    try:
        payload = _submit_payload()
        burst = [JPEG_B64] * 5
        payload["segments"] = [{"task": "blink", "frames_b64": burst, "image_b64": JPEG_B64}]
        resp = kyc_client.post("/api/v1/auth/kyc/submit", json=payload)
        assert resp.status_code == 200
        kind, _session, sent = recording.calls[0]
        assert kind == "verify_full"
        segment = sent["segments"][0]
        assert segment["task"] == "blink"
        assert segment["frames_b64"] == burst
        assert segment["image_b64"] == JPEG_B64
    finally:
        _override_provider("success")


def test_submit_requires_frames_or_image_per_segment(kyc_client: TestClient):
    payload = _submit_payload()
    payload["segments"] = [{"task": "blink"}]
    resp = kyc_client.post("/api/v1/auth/kyc/submit", json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_submit_4xx_propagates_step_and_reason(kyc_client: TestClient):
    app.dependency_overrides[get_kyc_provider] = lambda: InvalidKycProvider()
    try:
        resp = kyc_client.post("/api/v1/auth/kyc/submit", json=_submit_payload())
        assert resp.status_code == 422
        error = resp.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert error["details"]["step"] == "blink"
        assert error["details"]["reason"] == "not_enough_frames"
    finally:
        _override_provider("success")


def test_evaluate_calls_provider_and_returns_passed_reason(kyc_client: TestClient):
    recording = RecordingKycProvider()
    app.dependency_overrides[get_kyc_provider] = lambda: recording
    try:
        burst = [JPEG_B64] * 5
        resp = kyc_client.post(
            "/api/v1/auth/kyc/evaluate",
            json={"challenge_token": "tok-abc", "step": "blink", "frames_b64": burst},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["step"] == "blink"
        assert data["passed"] is True
        assert data["reason"] == "ok"
        assert data["frames_analyzed"] == 5
        kind, _session, task, sent = recording.calls[0]
        assert kind == "evaluate"
        assert task == "blink"
        assert sent["frames_base64"] == burst
    finally:
        _override_provider("success")


def test_evaluate_rejects_corrupt_frame_422(kyc_client: TestClient):
    resp = kyc_client.post(
        "/api/v1/auth/kyc/evaluate",
        json={"challenge_token": "tok", "step": "blink", "frames_b64": [CORRUPT_B64]},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_evaluate_4xx_propagates_step_and_reason(kyc_client: TestClient):
    app.dependency_overrides[get_kyc_provider] = lambda: InvalidKycProvider()
    try:
        resp = kyc_client.post(
            "/api/v1/auth/kyc/evaluate",
            json={"challenge_token": "tok", "step": "blink", "frames_b64": [JPEG_B64]},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["passed"] is False
        assert data["reason"] == "no_face_detected"
        assert data["step"] == "blink"
    finally:
        _override_provider("success")


def test_evaluate_service_down_returns_503(kyc_client: TestClient):
    _override_provider("down")
    try:
        resp = kyc_client.post(
            "/api/v1/auth/kyc/evaluate",
            json={"challenge_token": "tok", "step": "blink", "frames_b64": [JPEG_B64]},
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "KYC_UNAVAILABLE"
        assert "Traceback" not in resp.text
    finally:
        _override_provider("success")


def test_document_validate_success_via_mock(kyc_client: TestClient):
    recording = RecordingKycProvider()
    app.dependency_overrides[get_kyc_provider] = lambda: recording
    try:
        resp = kyc_client.post("/api/v1/auth/kyc/document/validate", json={"image_b64": PNG_B64})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {"data", "meta"}
        data = body["data"]
        assert data["is_valid"] is True
        assert data["issues"] == []
        assert isinstance(data["checks"], dict)
        assert body["meta"]["request_id"]
        # La imagen enviada nunca se refleja en la respuesta.
        assert PNG_B64 not in resp.text
        kind, _session, sent = recording.calls[0]
        assert kind == "validate_document"
        assert sent == PNG_B64
    finally:
        _override_provider("success")


def test_document_validate_invalid_is_200_with_issues(kyc_client: TestClient):
    _override_provider("failure")
    try:
        resp = kyc_client.post("/api/v1/auth/kyc/document/validate", json={"image_b64": JPEG_B64})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["is_valid"] is False
        assert data["issues"]
        assert all(isinstance(issue, str) for issue in data["issues"])
    finally:
        _override_provider("success")


@pytest.mark.parametrize("bad_b64", ["", "!!!no-es-base64!!!", CORRUPT_B64])
def test_document_validate_local_validation_422(kyc_client: TestClient, bad_b64: str):
    resp = kyc_client.post("/api/v1/auth/kyc/document/validate", json={"image_b64": bad_b64})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_document_validate_rejects_oversize_422(kyc_client: TestClient, monkeypatch):
    monkeypatch.setenv("KYC_MAX_IMAGE_BYTES", "10")
    resp = kyc_client.post("/api/v1/auth/kyc/document/validate", json={"image_b64": PNG_B64})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_document_validate_4xx_maps_to_validation_error_422(kyc_client: TestClient):
    app.dependency_overrides[get_kyc_provider] = lambda: InvalidKycProvider()
    try:
        resp = kyc_client.post("/api/v1/auth/kyc/document/validate", json={"image_b64": PNG_B64})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        _override_provider("success")


def test_document_validate_service_down_returns_503(kyc_client: TestClient):
    _override_provider("down")
    try:
        resp = kyc_client.post("/api/v1/auth/kyc/document/validate", json={"image_b64": PNG_B64})
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "KYC_UNAVAILABLE"
        assert "Traceback" not in resp.text
    finally:
        _override_provider("success")


def test_document_validate_timeout_returns_504(kyc_client: TestClient):
    _override_provider("timeout")
    try:
        resp = kyc_client.post("/api/v1/auth/kyc/document/validate", json={"image_b64": PNG_B64})
        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "KYC_UNAVAILABLE"
        assert "Traceback" not in resp.text
    finally:
        _override_provider("success")


def test_document_validate_rate_limit_returns_429(kyc_client: TestClient, monkeypatch):
    monkeypatch.setenv("KYC_RATE_LIMIT_MAX_REQUESTS", "1")
    monkeypatch.setenv("KYC_RATE_LIMIT_WINDOW_SECONDS", "60")
    kyc_proxy.reset_rate_limits()
    assert (
        kyc_client.post(
            "/api/v1/auth/kyc/document/validate", json={"image_b64": PNG_B64}
        ).status_code
        == 200
    )
    resp = kyc_client.post("/api/v1/auth/kyc/document/validate", json={"image_b64": PNG_B64})
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMITED"


def test_document_validate_does_not_log_image(kyc_client: TestClient, caplog):
    import logging

    with caplog.at_level(logging.INFO):
        resp = kyc_client.post("/api/v1/auth/kyc/document/validate", json={"image_b64": PNG_B64})
    assert resp.status_code == 200
    assert PNG_B64 not in caplog.text
    assert "X-API-Key" not in resp.text
    assert "change-me" not in resp.text


def test_submit_rejects_corrupt_image_422(kyc_client: TestClient):
    resp = kyc_client.post("/api/v1/auth/kyc/submit", json=_submit_payload(doc_b64=CORRUPT_B64))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_submit_rejects_broken_base64_422(kyc_client: TestClient):
    payload = _submit_payload()
    payload["document"]["image_b64"] = "!!!no-es-base64!!!"
    resp = kyc_client.post("/api/v1/auth/kyc/submit", json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_submit_rejects_oversize_image_422(kyc_client: TestClient, monkeypatch):
    monkeypatch.setenv("KYC_MAX_IMAGE_BYTES", "10")
    resp = kyc_client.post("/api/v1/auth/kyc/submit", json=_submit_payload())
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_submit_rejects_unknown_doc_type_422(kyc_client: TestClient):
    payload = _submit_payload()
    payload["document"]["type"] = "LICENCIA"
    resp = kyc_client.post("/api/v1/auth/kyc/submit", json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_service_down_returns_503_without_internals(kyc_client: TestClient):
    _override_provider("down")
    try:
        resp = kyc_client.post("/api/v1/auth/kyc/submit", json=_submit_payload())
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "KYC_UNAVAILABLE"
        raw = resp.text
        for leaked in ("Traceback", "X-API-Key", "change-me", "mock", "Exception"):
            assert leaked not in raw, f"fuga de interno: {leaked}"

        resp = kyc_client.post("/api/v1/auth/kyc/challenge", json={})
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "KYC_UNAVAILABLE"
    finally:
        _override_provider("success")


def test_service_timeout_returns_504(kyc_client: TestClient):
    _override_provider("timeout")
    try:
        resp = kyc_client.post("/api/v1/auth/kyc/submit", json=_submit_payload())
        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "KYC_UNAVAILABLE"
        assert "Traceback" not in resp.text
    finally:
        _override_provider("success")


def test_rate_limit_returns_429(kyc_client: TestClient, monkeypatch):
    monkeypatch.setenv("KYC_RATE_LIMIT_MAX_REQUESTS", "2")
    monkeypatch.setenv("KYC_RATE_LIMIT_WINDOW_SECONDS", "60")
    kyc_proxy.reset_rate_limits()
    assert kyc_client.post("/api/v1/auth/kyc/challenge", json={}).status_code == 200
    assert kyc_client.post("/api/v1/auth/kyc/challenge", json={}).status_code == 200
    resp = kyc_client.post("/api/v1/auth/kyc/challenge", json={})
    assert resp.status_code == 429


def test_api_key_absent_in_all_responses(kyc_client: TestClient):
    raws = [
        kyc_client.post("/api/v1/auth/kyc/challenge", json={}).text,
        kyc_client.post("/api/v1/auth/kyc/submit", json=_submit_payload()).text,
        kyc_client.post("/api/v1/auth/kyc/submit", json=_submit_payload(doc_b64=CORRUPT_B64)).text,
        kyc_client.post("/api/v1/auth/kyc/document/validate", json={"image_b64": PNG_B64}).text,
    ]
    for raw in raws:
        assert "X-API-Key" not in raw
        assert "change-me" not in raw


def test_openapi_includes_kyc_paths(kyc_client: TestClient):
    spec = kyc_client.get("/openapi.json").json()
    assert "/api/v1/auth/kyc/challenge" in spec["paths"]
    assert "/api/v1/auth/kyc/evaluate" in spec["paths"]
    assert "/api/v1/auth/kyc/document/validate" in spec["paths"]
    assert "/api/v1/auth/kyc/submit" in spec["paths"]
