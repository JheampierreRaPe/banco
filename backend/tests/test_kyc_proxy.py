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

from app.adapters.kyc_provider import MockKycProvider
from app.main import app
from app.modules.identity.api.kyc import get_kyc_provider
from app.modules.identity.service import kyc_proxy

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100).decode()
JPEG_B64 = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 100).decode()
CORRUPT_B64 = base64.b64encode(b"esto-no-es-una-imagen").decode()


def _submit_payload(token: str = "tok-abc", doc_b64: str = PNG_B64) -> dict:
    return {
        "challenge_token": token,
        "document": {"type": "DNI", "image_b64": doc_b64},
        "segments": [{"task": "blink", "image_b64": JPEG_B64}],
    }


@pytest.fixture()
def kyc_client(monkeypatch):
    """TestClient con proveedor mock en modo `success` y rate limit limpio."""
    monkeypatch.delenv("KYC_RATE_LIMIT_MAX_REQUESTS", raising=False)
    monkeypatch.delenv("KYC_RATE_LIMIT_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("KYC_MAX_IMAGE_BYTES", raising=False)
    kyc_proxy.reset_rate_limits()
    app.dependency_overrides[get_kyc_provider] = lambda: MockKycProvider(mode="success")
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_kyc_provider, None)
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
    ]
    for raw in raws:
        assert "X-API-Key" not in raw
        assert "change-me" not in raw


def test_openapi_includes_kyc_paths(kyc_client: TestClient):
    spec = kyc_client.get("/openapi.json").json()
    assert "/api/v1/auth/kyc/challenge" in spec["paths"]
    assert "/api/v1/auth/kyc/submit" in spec["paths"]
