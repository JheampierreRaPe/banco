"""Q-T04: integracion con el microservicio KYC via proxy (HU01).

Flujo challenge -> evaluate -> document/validate -> document/lookup ->
submit contra el proveedor (mock en este entorno), verificando el contrato
de `docs/05#6.1` y `docs/05#8`: formas de respuesta, campos ampliados del
`submit` (`steps_verified`, `steps_total`, `failed_step`, `step_results`,
`overall_reason`) y ausencia total de la API key en respuestas.

Aislamiento: fixture `db_client` (rollback por prueba sobre `banca_test`) +
documentos/emails unicos por prueba. No persiste imagenes ni frames: solo se
envia un PNG 1x1 sintetico. Sin PII real en payloads ni logs.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.contract.conftest_helpers import (
    PNG_1PX_B64,
    assert_app_error_envelope,
    assert_no_key_leak,
    assert_success_envelope,
    unique_dni,
    unique_email,
)


def _post(client: TestClient, path: str, body: dict):
    response = client.post(path, json=body)
    assert_no_key_leak(response)
    return response


def test_kyc_challenge_evaluate_flow(db_client: TestClient) -> None:
    challenge = assert_success_envelope(_post(db_client, "/api/v1/auth/kyc/challenge", {}).json())[
        "data"
    ]
    assert challenge["steps"], "el desafio debe traer pasos de liveness"
    assert challenge["expires_in"] > 0

    first_step = challenge["steps"][0]
    evaluated = assert_success_envelope(
        _post(
            db_client,
            "/api/v1/auth/kyc/evaluate",
            {
                "challenge_token": challenge["token"],
                "step": first_step,
                "frames_b64": [PNG_1PX_B64],
            },
        ).json()
    )["data"]
    assert evaluated["step"] == first_step
    assert evaluated["passed"] is True
    assert evaluated["frames_analyzed"] == 1


def test_document_validate_and_lookup_contract(db_client: TestClient) -> None:
    validated = assert_success_envelope(
        _post(db_client, "/api/v1/auth/kyc/document/validate", {"image_b64": PNG_1PX_B64}).json()
    )["data"]
    assert validated["is_valid"] is True
    assert validated["issues"] == []

    holder = assert_success_envelope(
        _post(
            db_client,
            "/api/v1/auth/kyc/document/lookup",
            {"type": "DNI", "number": unique_dni()},
        ).json()
    )["data"]
    assert holder["document_type"] == "DNI"
    assert holder["first_name"] and holder["last_name"]
    assert holder["business_name"] == ""  # persona natural: sin razon social


def test_submit_returns_extended_kyc_result(db_client: TestClient) -> None:
    token = _post(db_client, "/api/v1/auth/kyc/challenge", {}).json()["data"]["token"]
    result = assert_success_envelope(
        _post(
            db_client,
            "/api/v1/auth/kyc/submit",
            {
                "challenge_token": token,
                "document": {
                    "type": "DNI",
                    "number": unique_dni(),
                    "image_b64": PNG_1PX_B64,
                },
                "applicant": {
                    "first_name": "Juan",
                    "last_name": "Perez",
                    "email": unique_email(),
                },
                "segments": [{"task": "blink", "frames_b64": [PNG_1PX_B64]}],
            },
        ).json()
    )["data"]
    # Contrato ampliado docs/05#6.1 (E1-T29): nombres de pasos, no conteos.
    assert isinstance(result["steps_verified"], list) and result["steps_verified"]
    assert isinstance(result["steps_total"], list) and result["steps_total"]
    assert result["failed_step"] is None
    assert isinstance(result["step_results"], dict) and result["step_results"]
    assert result["overall_reason"]
    assert result["user_id"] and result["status"]


def test_submit_duplicate_document_uses_catalogued_409(db_client: TestClient) -> None:
    """El precheck local (E1-T37) responde 409 sin consultar al proveedor."""
    doc_number = unique_dni()
    applicant = {
        "first_name": "Juan",
        "last_name": "Perez",
        "email": unique_email("qt04dup"),
    }
    document = {"type": "DNI", "number": doc_number, "image_b64": PNG_1PX_B64}
    token = _post(db_client, "/api/v1/auth/kyc/challenge", {}).json()["data"]["token"]
    first = _post(
        db_client,
        "/api/v1/auth/kyc/submit",
        {
            "challenge_token": token,
            "document": document,
            "applicant": applicant,
            "segments": [{"task": "blink", "frames_b64": [PNG_1PX_B64]}],
        },
    )
    assert first.status_code == 200

    retry = _post(
        db_client,
        "/api/v1/auth/kyc/submit",
        {
            "challenge_token": token,
            "document": document,
            "applicant": {**applicant, "email": unique_email("qt04dup2")},
            "segments": [{"task": "blink", "frames_b64": [PNG_1PX_B64]}],
        },
    )
    assert retry.status_code == 409
    error = assert_app_error_envelope(retry.json())
    assert error["code"] == "DUPLICATE_DOCUMENT"


def test_kyc_error_paths_never_leak_provider_key(db_client: TestClient) -> None:
    token = _post(db_client, "/api/v1/auth/kyc/challenge", {}).json()["data"]["token"]
    malformed = [
        (
            "/api/v1/auth/kyc/evaluate",
            {"challenge_token": token, "step": "blink"},
        ),  # sin frames -> 422 esquema
        (
            "/api/v1/auth/kyc/document/lookup",
            {"type": "DNI", "number": "12"},
        ),  # 422 VALIDATION_ERROR
        (
            "/api/v1/auth/kyc/document/validate",
            {"image_b64": "no-es-base64!!!"},
        ),  # 422 VALIDATION_ERROR
    ]
    for path, body in malformed:
        response = _post(db_client, path, body)
        assert response.status_code == 422, f"{path}: {response.text[:200]}"
        # assert_no_key_leak ya corrio dentro de _post.
