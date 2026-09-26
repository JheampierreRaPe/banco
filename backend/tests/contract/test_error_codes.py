"""Q-T04: codigos de error y sobres de respuesta (HU01/HU02/HU03).

Cubre docs/05#4 (formato de respuesta) y docs/05#5 (codigos HTTP):
exito `{data, meta}`, error de app `{error: {code, message, details,
request_id}}` con `code` dentro del catalogo, 401 sin JWT en endpoints
guardados y 422 de validacion (de esquema FastAPI o `VALIDATION_ERROR`).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.contract.conftest_helpers import (
    PNG_1PX_B64,
    assert_app_error_envelope,
    assert_no_key_leak,
    assert_success_envelope,
)


def _post(client: TestClient, path: str, body: dict):
    response = client.post(path, json=body)
    assert_no_key_leak(response)
    return response


def test_success_envelope_on_kyc_challenge(db_client: TestClient) -> None:
    body = _post(db_client, "/api/v1/auth/kyc/challenge", {}).json()
    assert _post(db_client, "/api/v1/auth/kyc/challenge", {}).status_code == 200
    envelope = assert_success_envelope(body)
    assert {"token", "steps", "expires_in"} <= set(envelope["data"])
    assert envelope["meta"]["request_id"]


def test_unauthenticated_guarded_endpoints_return_401(db_client: TestClient) -> None:
    guarded = [
        ("GET", "/api/v1/me", None),
        ("POST", "/api/v1/auth/biometric/consent", {"enabled": True}),
    ]
    for method, path, body in guarded:
        response = db_client.get(path) if method == "GET" else _post(db_client, path, body or {})
        assert response.status_code == 401, f"{method} {path}"
        error = assert_app_error_envelope(response.json())
        assert error["code"] == "NOT_AUTHENTICATED"


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/v1/auth/kyc/email/check", {"email": "no-es-email"}),
        ("/api/v1/auth/pin/setup", {}),
        ("/api/v1/auth/refresh", {}),
        ("/api/v1/auth/kyc/submit", {}),
    ],
)
def test_schema_validation_returns_422(db_client: TestClient, path: str, body: dict) -> None:
    response = _post(db_client, path, body)
    assert response.status_code == 422
    assert "detail" in response.json()  # 422 de esquema FastAPI (estandar)


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/v1/auth/kyc/document/lookup", {"type": "DNI", "number": "123"}),
        ("/api/v1/auth/kyc/document/lookup", {"type": "XX", "number": "12345678"}),
        ("/api/v1/auth/kyc/document/lookup", {"type": "RUC", "number": "12345678"}),
        ("/api/v1/auth/kyc/document/validate", {"image_b64": "aGk="}),
    ],
)
def test_domain_validation_returns_catalogued_422(
    db_client: TestClient, path: str, body: dict
) -> None:
    response = _post(db_client, path, body)
    assert response.status_code == 422
    error = assert_app_error_envelope(response.json())
    assert error["code"] == "VALIDATION_ERROR"


def test_evaluate_rejects_unsupported_frame_format(db_client: TestClient) -> None:
    token = _post(db_client, "/api/v1/auth/kyc/challenge", {}).json()["data"]["token"]
    response = _post(
        db_client,
        "/api/v1/auth/kyc/evaluate",
        {"challenge_token": token, "step": "blink", "frames_b64": ["aGk="]},
    )
    assert response.status_code == 422
    error = assert_app_error_envelope(response.json())
    assert error["code"] == "VALIDATION_ERROR"


def test_evaluate_happy_path_envelope_uses_valid_png(db_client: TestClient) -> None:
    token = _post(db_client, "/api/v1/auth/kyc/challenge", {}).json()["data"]["token"]
    response = _post(
        db_client,
        "/api/v1/auth/kyc/evaluate",
        {"challenge_token": token, "step": "blink", "frames_b64": [PNG_1PX_B64]},
    )
    assert response.status_code == 200
    envelope = assert_success_envelope(response.json())
    assert {"step", "passed", "frames_analyzed"} <= set(envelope["data"])
