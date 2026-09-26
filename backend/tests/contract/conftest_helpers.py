"""Utilidades compartidas de la suite de contrato Q-T04.

Sin PII real: PNG de 1px generado inline, email `@example.com` y documentos
de prueba del mock. Sin secretos: las API keys solo se leen del entorno para
verificar que NUNCA aparecen en respuestas, jamas se registran en logs.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

# PNG 1x1 valido para los happy paths de imagen (document/validate, evaluate).
PNG_1PX_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)

# Catalogo de codigos de error segun docs/05#4 (verificados en codigo).
ERROR_CATALOG = frozenset(
    {
        "ACCOUNT_LOCKED",
        "DOC_LOOKUP_UNAVAILABLE",
        "DOCUMENT_NOT_FOUND",
        "DUPLICATE_DOCUMENT",
        "DUPLICATE_EMAIL",
        "EXPIRED_NONCE",
        "EXPIRED_OTP",
        "EXPORT_FORMAT_NOT_SUPPORTED",
        "INVALID_CREDENTIALS",
        "INVALID_LOGIN",
        "INVALID_OTP",
        "INVALID_PIN_FORMAT",
        "INVALID_PIN_RESET",
        "INVALID_REFRESH",
        "INVALID_SETUP_CODE",
        "KYC_UNAVAILABLE",
        "NOT_AUTHENTICATED",
        "NOT_AUTHORIZED",
        "NOT_FOUND",
        "PIN_ALREADY_SET",
        "PIN_REQUIRED",
        "RATE_LIMITED",
        "REFRESH_EXPIRED",
        "REFRESH_REUSED",
        "RESEND_LIMIT",
        "SESSION_INACTIVE",
        "VALIDATION_ERROR",
    }
)

# Marcadores de fuga de credenciales de integracion (nombres, nunca valores).
LEAK_MARKERS = ("apiinti", "x-api-key", "apikey", "api_key")


def unique_email(prefix: str = "qt04") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def unique_dni() -> str:
    suffix = uuid.uuid4().int % 90000000 + 10000000
    return str(suffix)


def secret_values_from_env() -> list[str]:
    """Valores de keys de integracion presentes en el entorno (si existen)."""
    values = []
    for name in ("APIINTI_API_KEY", "KYC_API_KEY", "KYC_SERVICE_API_KEY"):
        value = os.environ.get(name, "")
        if value and len(value) >= 8:
            values.append(value)
    return values


def assert_success_envelope(body: Any) -> dict:
    assert isinstance(body, dict), f"exito sin objeto JSON: {body!r}"
    assert "data" in body, f"exito sin 'data': {body!r}"
    assert isinstance(body.get("meta"), dict), f"exito sin 'meta': {body!r}"
    return body


def assert_app_error_envelope(body: Any) -> dict:
    assert isinstance(body, dict), f"error sin objeto JSON: {body!r}"
    error = body.get("error")
    assert isinstance(error, dict), f"error sin sobre 'error': {body!r}"
    for field in ("code", "message", "request_id"):
        assert field in error, f"error sin '{field}': {body!r}"
    assert error["code"] in ERROR_CATALOG, f"codigo fuera de catalogo: {body!r}"
    return error


def _resolve_local_ref(spec: dict, ref: str) -> dict:
    node: Any = spec
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    assert isinstance(node, dict)
    return node


def documented_required_top_level(app: FastAPI, method: str, path: str) -> list[str]:
    """Campos `required` del 200 documentado (resuelve $ref de primer nivel)."""
    spec = app.openapi()
    # FastAPI documenta la ruta templada; buscar coincidencia exacta.
    operation = spec["paths"][path][method.lower()]
    content = operation["responses"]["200"]["content"]["application/json"]
    schema: Any = content["schema"]
    if isinstance(schema, dict) and schema.get("$ref"):
        schema = _resolve_local_ref(spec, schema["$ref"])
    required = schema.get("required", []) if isinstance(schema, dict) else []
    return [name for name in required if isinstance(name, str)]


def assert_required_present(
    app: FastAPI,
    test_client: TestClient,
    method: str,
    path: str,
    body: dict | None,
    expected_status: int = 200,
) -> None:
    """Coherencia esquema<->respuesta para un happy path documentado."""
    if method == "GET":
        response = test_client.get(path)
    else:
        response = test_client.post(path, json=body or {})
    assert (
        response.status_code == expected_status
    ), f"{method} {path}: status {response.status_code}, body {response.text[:200]}"
    if expected_status == 200:
        payload = response.json()
        assert_success_envelope(payload)
        for name in documented_required_top_level(app, method, path):
            assert name in payload, f"{method} {path}: falta '{name}' documentado"
    else:
        assert_app_error_envelope(response.json())


def assert_no_key_leak(response) -> None:
    """Ninguna respuesta debe exponer la API key de integracion (docs/05#8)."""
    text = response.text.lower()
    for marker in LEAK_MARKERS:
        if marker == "api_key":
            continue  # `api_key` puede aparecer en textos descriptivos; ver abajo
        assert marker not in text, f"posible fuga de key en {response.request.url}"
    for secret in secret_values_from_env():
        assert secret not in response.text, "la API key aparece en la respuesta"
        assert secret not in str(response.headers), "la API key aparece en headers"
    assert "x-api-key" not in {k.lower() for k in response.headers}
