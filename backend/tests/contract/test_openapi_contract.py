"""Q-T04: contrato OpenAPI de HU01/HU02/HU03 (identity).

Valida que el `openapi.json` generado por FastAPI cumple el estandar
(OpenAPI 3.1), que los endpoints del catalogo `docs/05#6.1` existen con el
metodo esperado, que `POST /auth/activate` sigue deprecado pero vivo y que
todas las referencias `$ref` resuelven.

`schemathesis` no esta instalado en el entorno (ver reporte Q-T04); esta
suite implementa la validacion de contrato con lo disponible: estructura
del spec + TestClient (en `test_error_codes.py` y `test_kyc_integration.py`).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

# Catalogo esperado segun docs/05#6.1 (HU01-HU04, foco HU01/HU02/HU03).
EXPECTED_ENDPOINTS: dict[tuple[str, str], dict] = {
    ("POST", "/api/v1/auth/kyc/challenge"): {},
    ("POST", "/api/v1/auth/kyc/evaluate"): {},
    ("POST", "/api/v1/auth/kyc/document/validate"): {},
    ("POST", "/api/v1/auth/kyc/document/lookup"): {},
    ("POST", "/api/v1/auth/kyc/email/check"): {},
    ("POST", "/api/v1/auth/kyc/submit"): {},
    ("POST", "/api/v1/auth/activate"): {"deprecated": True},
    ("POST", "/api/v1/auth/otp/resend"): {},
    ("POST", "/api/v1/auth/login/challenge"): {},
    ("POST", "/api/v1/auth/login/facial"): {},
    ("POST", "/api/v1/auth/login/pin"): {},
    ("POST", "/api/v1/auth/biometric/consent"): {},
    ("POST", "/api/v1/auth/refresh"): {},
    ("POST", "/api/v1/auth/logout"): {},
    ("POST", "/api/v1/auth/recovery/request"): {},
    ("POST", "/api/v1/auth/pin-reset"): {},
    ("POST", "/api/v1/auth/login/device/request"): {},
    ("POST", "/api/v1/auth/login/device/complete"): {},
    ("POST", "/api/v1/auth/pin/setup"): {},
    ("GET", "/api/v1/me"): {},
}


@pytest.fixture(scope="module")
def spec() -> dict:
    return app.openapi()


def _resolve_ref(spec: dict, ref: str) -> dict:
    assert ref.startswith("#/"), f"$ref externo no soportado: {ref}"
    node: object = spec
    for part in ref[2:].split("/"):
        assert isinstance(node, dict) and part in node, f"$ref roto: {ref}"
        node = node[part]
    assert isinstance(node, dict)
    return node


def _walk_refs(node: object, found: list[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            found.append(ref)
        for value in node.values():
            _walk_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk_refs(item, found)


def test_openapi_version_and_info(spec: dict) -> None:
    assert spec["openapi"].startswith("3.1.")
    assert spec["info"]["title"]
    assert spec["info"]["version"]


def test_catalog_endpoints_present_with_method(spec: dict) -> None:
    paths = spec["paths"]
    missing = [
        f"{method} {path}"
        for (method, path) in EXPECTED_ENDPOINTS
        if path not in paths or method.lower() not in paths[path]
    ]
    assert not missing, f"endpoints del catalogo ausentes en OpenAPI: {missing}"


def test_activate_deprecated_but_alive(spec: dict, client: TestClient) -> None:
    op = spec["paths"]["/api/v1/auth/activate"]["post"]
    assert op.get("deprecated") is True
    # Vivo: responde 422 de esquema (no 404/405/410) ante cuerpo vacio.
    response = client.post("/api/v1/auth/activate", json={})
    assert response.status_code == 422


def test_operations_have_ids_and_base_responses(spec: dict) -> None:
    problems: list[str] = []
    for method, path in EXPECTED_ENDPOINTS:
        op = spec["paths"][path][method.lower()]
        if not op.get("operationId"):
            problems.append(f"{method} {path}: sin operationId")
        responses = op.get("responses", {})
        for expected in ("200", "422"):
            if expected not in responses:
                problems.append(f"{method} {path}: sin respuesta {expected}")
    assert not problems, problems


def test_all_refs_resolve(spec: dict) -> None:
    found: list[str] = []
    _walk_refs(spec, found)
    assert found, "el spec no contiene $ref"
    for ref in set(found):
        _resolve_ref(spec, ref)


def test_documented_required_fields_present_in_handlers(db_client: TestClient) -> None:
    """Coherencia esquema<->respuesta: los `required` del 200 documentado
    deben existir en la respuesta real; los 401 usan el sobre de error."""
    from tests.contract.conftest_helpers import assert_required_present

    cases = [
        ("POST", "/api/v1/auth/kyc/challenge", {}, 200),
        ("POST", "/api/v1/auth/kyc/email/check", {"email": "qt04@example.com"}, 200),
        ("GET", "/api/v1/me", None, 401),
    ]
    for method, path, body, expected in cases:
        assert_required_present(app, db_client, method, path, body, expected)
