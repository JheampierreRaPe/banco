"""Titular por documento via apiinti (E1-T35, HU01).

- Sin BD ni auth (pre-registro, read-only): `TestClient(app)` +
  `dependency_overrides` de `get_document_lookup_provider` con
  `MockDocumentLookupProvider` (patron de `test_kyc_proxy.py`).
- Casos: lookup DNI feliz (persona natural); lookup RUC feliz (persona
  juridica con razon social); parseo tolerante (envuelto/plano, claves
  alternativas, `apellido_paterno`/`apellido_materno`); validacion previa
  422 sin invocar al proveedor; `not_found` -> 404 neutro; `down` -> 503;
  `timeout` -> 504; rate limit 429; con `DOC_LOOKUP_PROVIDER=http` y httpx
  mockeado se envia `Authorization: Bearer <APIINTI_API_KEY>` +
  `Content-Type: application/json` sin filtrar la key ni el numero en logs;
  cableado (`Settings.apiinti_api_key`, `docker-compose.yml`);
  `DOC_LOOKUP_PROVIDER` desconocido cae al mock; OpenAPI expone la ruta.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.document_lookup_provider import (
    DocLookupSettings,
    DocumentHolder,
    DocumentNotFoundError,
    HttpDocumentLookupProvider,
    MockDocumentLookupProvider,
    clean_text,
    create_document_lookup_provider,
)
from app.core.config import Settings
from app.main import app
from app.modules.identity.api.kyc import get_document_lookup_provider
from app.modules.identity.service import document_lookup, kyc_proxy

DNI_OK = "12345678"
RUC_OK = "20123456789"


class RecordingLookupProvider(MockDocumentLookupProvider):
    """Mock que registra las llamadas reenviadas al adaptador (sin red)."""

    def __init__(self, mode: str = "success") -> None:
        super().__init__(mode=mode)
        self.calls: list = []

    def lookup_document(self, *, session_id, doc_type, number):
        self.calls.append((session_id, doc_type, number))
        return super().lookup_document(session_id=session_id, doc_type=doc_type, number=number)


def _mock_handler(request: httpx.Request) -> httpx.Response:
    """Transporte httpx mockeado: responde segun el path (sin red real)."""
    if request.url.path.endswith("/dni/12345678"):
        return httpx.Response(200, json={"data": {"nombres": "Ana", "apellidos": "Quispe"}})
    if request.url.path.endswith("/ruc/20123456789"):
        return httpx.Response(200, json={"razon_social": "EMPRESA EJEMPLO S.A.C."})
    return httpx.Response(404, json={"message": "no encontrado"})


def _http_provider(api_key: str = "sentinel-key-xyz") -> HttpDocumentLookupProvider:
    transport = httpx.MockTransport(_mock_handler)
    client = httpx.Client(
        transport=transport, base_url="https://app.apiinti.dev/api/v1", timeout=5.0
    )
    return HttpDocumentLookupProvider(
        settings=DocLookupSettings(
            base_url="https://app.apiinti.dev/api/v1", api_key=api_key, backoff_base_seconds=0.0
        ),
        client=client,
    )


@pytest.fixture()
def doc_client(monkeypatch):
    """TestClient con proveedor mock en modo `success` y rate limit limpio."""
    monkeypatch.delenv("DOC_LOOKUP_PROVIDER", raising=False)
    monkeypatch.delenv("DOC_LOOKUP_MOCK_MODE", raising=False)
    monkeypatch.delenv("KYC_RATE_LIMIT_MAX_REQUESTS", raising=False)
    monkeypatch.delenv("KYC_RATE_LIMIT_WINDOW_SECONDS", raising=False)
    kyc_proxy.reset_rate_limits()
    app.dependency_overrides[get_document_lookup_provider] = lambda: MockDocumentLookupProvider(
        mode="success"
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_document_lookup_provider, None)
        kyc_proxy.reset_rate_limits()


def _override_lookup(provider) -> None:
    app.dependency_overrides[get_document_lookup_provider] = lambda: provider


# ------------------------------------------------------- Camino feliz
def test_lookup_dni_returns_natural_person(doc_client: TestClient):
    resp = doc_client.post(
        "/api/v1/auth/kyc/document/lookup", json={"type": "DNI", "number": DNI_OK}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"data", "meta"}
    data = body["data"]
    assert data["document_type"] == "DNI"
    assert data["first_name"] and data["last_name"]
    assert data["business_name"] == ""
    assert body["meta"]["request_id"]


def test_lookup_ruc_returns_business_name(doc_client: TestClient):
    resp = doc_client.post(
        "/api/v1/auth/kyc/document/lookup", json={"type": "RUC", "number": RUC_OK}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["document_type"] == "RUC"
    assert data["business_name"]
    assert data["first_name"] == "" and data["last_name"] == ""


def test_lookup_ruc_routes_to_ruc_path(doc_client: TestClient):
    recording = RecordingLookupProvider()
    _override_lookup(recording)
    try:
        resp = doc_client.post(
            "/api/v1/auth/kyc/document/lookup", json={"type": "RUC", "number": RUC_OK}
        )
        assert resp.status_code == 200
        assert recording.calls and recording.calls[0][1] == "RUC"
        assert recording.calls[0][2] == RUC_OK
    finally:
        _override_lookup(MockDocumentLookupProvider(mode="success"))


def test_lookup_dni_routes_to_dni_path(doc_client: TestClient):
    recording = RecordingLookupProvider()
    _override_lookup(recording)
    try:
        resp = doc_client.post(
            "/api/v1/auth/kyc/document/lookup", json={"type": "DNI", "number": DNI_OK}
        )
        assert resp.status_code == 200
        assert recording.calls and recording.calls[0][1] == "DNI"
    finally:
        _override_lookup(MockDocumentLookupProvider(mode="success"))


# ------------------------------------------------------- Parseo tolerante (punto unico)
@pytest.mark.parametrize(
    "payload, expected",
    [
        (
            {"data": {"nombres": "  Ana  ", "apellidos": "Quispe  Rojas "}},
            ("Ana", "Quispe Rojas", ""),
        ),
        ({"first_name": "Luis", "last_name": "Tello"}, ("Luis", "Tello", "")),
        ({"data": {"razon_social": "ACME S.A.C."}}, ("", "", "ACME S.A.C.")),
        ({"business_name": "ACME S.A.C."}, ("", "", "ACME S.A.C.")),
        (
            {"nombres": "Rosa", "apellido_paterno": "Diaz", "apellido_materno": "Paredes"},
            ("Rosa", "Diaz Paredes", ""),
        ),
    ],
)
def test_http_provider_tolerant_parsing(payload: dict, expected: tuple):
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport, base_url="https://app.apiinti.dev/api/v1")
    provider = HttpDocumentLookupProvider(
        settings=DocLookupSettings(api_key="k", backoff_base_seconds=0.0), client=client
    )
    holder = provider.lookup_document(session_id="s", doc_type="DNI", number=DNI_OK)
    assert (holder.first_name, holder.last_name, holder.business_name) == expected


# ------------------------------------------------------- Regresion apiinti real (camelCase)
def _camel_provider(handler) -> HttpDocumentLookupProvider:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="https://app.apiinti.dev/api/v1")
    return HttpDocumentLookupProvider(
        settings=DocLookupSettings(api_key="k", backoff_base_seconds=0.0), client=client
    )


def test_http_provider_apiinti_dni_camelcase_real_shape():
    """Forma REAL apiinti DNI: `apellidoPaterno`/`apellidoMaterno` -> `last_name`.

    Sin el fix solo se componia desde snake_case y `last_name` quedaba vacio.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "dni": "12345678",
                    "nombres": "JUAN CARLOS",
                    "apellidoPaterno": "GARCIA",
                    "apellidoMaterno": "LOPEZ",
                },
            },
        )

    holder = _camel_provider(_handler).lookup_document(
        session_id="s", doc_type="DNI", number=DNI_OK
    )
    assert holder.first_name == "JUAN CARLOS"
    assert holder.last_name == "GARCIA LOPEZ"
    assert holder.business_name == ""


def test_http_provider_apiinti_ruc_camelcase_real_shape():
    """Forma REAL apiinti RUC: `razonSocial` -> `business_name`.

    Sin el fix `business_name` quedaba vacio y un RUC valido terminaba en
    `DocumentNotFoundError`.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"ruc": "20100017491", "razonSocial": "SUNAT", "estado": "ACTIVO"},
            },
        )

    holder = _camel_provider(_handler).lookup_document(
        session_id="s", doc_type="RUC", number="20100017491"
    )
    assert holder.business_name == "SUNAT"
    assert holder.first_name == "" and holder.last_name == ""


def test_lookup_endpoint_with_apiinti_camelcase_shapes(doc_client: TestClient):
    """`POST /auth/kyc/document/lookup` con las formas reales camelCase."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/dni/12345678"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "dni": "12345678",
                        "nombres": "JUAN CARLOS",
                        "apellidoPaterno": "GARCIA",
                        "apellidoMaterno": "LOPEZ",
                    },
                },
            )
        if request.url.path.endswith("/ruc/20100017491"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "ruc": "20100017491",
                        "razonSocial": "SUNAT",
                        "estado": "ACTIVO",
                    },
                },
            )
        return httpx.Response(404, json={"message": "no encontrado"})

    _override_lookup(_camel_provider(_handler))
    try:
        dni = doc_client.post(
            "/api/v1/auth/kyc/document/lookup", json={"type": "DNI", "number": DNI_OK}
        )
        assert dni.status_code == 200, dni.text
        assert dni.json()["data"]["first_name"] == "JUAN CARLOS"
        assert dni.json()["data"]["last_name"] == "GARCIA LOPEZ"
        ruc = doc_client.post(
            "/api/v1/auth/kyc/document/lookup",
            json={"type": "RUC", "number": "20100017491"},
        )
        assert ruc.status_code == 200, ruc.text
        assert ruc.json()["data"]["business_name"] == "SUNAT"
    finally:
        _override_lookup(MockDocumentLookupProvider(mode="success"))


@pytest.mark.parametrize("payload", [{}, {"data": {}}, {"foo": "bar"}, {"nombres": ""}, []])
def test_http_provider_empty_payload_is_not_found(payload: object):
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport, base_url="https://app.apiinti.dev/api/v1")
    provider = HttpDocumentLookupProvider(
        settings=DocLookupSettings(api_key="k", backoff_base_seconds=0.0), client=client
    )
    with pytest.raises(DocumentNotFoundError):
        provider.lookup_document(session_id="s", doc_type="DNI", number=DNI_OK)


def test_http_provider_uses_mocked_shapes_end_to_end(doc_client: TestClient):
    _override_lookup(_http_provider())
    try:
        dni = doc_client.post(
            "/api/v1/auth/kyc/document/lookup", json={"type": "DNI", "number": DNI_OK}
        )
        assert dni.status_code == 200
        assert dni.json()["data"]["first_name"] == "Ana"
        ruc = doc_client.post(
            "/api/v1/auth/kyc/document/lookup", json={"type": "RUC", "number": RUC_OK}
        )
        assert ruc.status_code == 200
        assert ruc.json()["data"]["business_name"] == "EMPRESA EJEMPLO S.A.C."
    finally:
        _override_lookup(MockDocumentLookupProvider(mode="success"))


# ------------------------------------------------------- Validacion previa (422, sin red)
@pytest.mark.parametrize(
    "payload",
    [
        {"type": "CE", "number": DNI_OK},
        {"type": "PASSPORT", "number": DNI_OK},
        {"type": "", "number": DNI_OK},
        {"type": "DNI", "number": "1234ABCD"},
        {"type": "DNI", "number": "1234567"},
        {"type": "DNI", "number": "123456789"},
        {"type": "RUC", "number": "2012345678"},
        {"type": "RUC", "number": "201234567890"},
        {"type": "RUC", "number": ""},
        {"type": "dni ", "number": " 1234567a "},
    ],
)
def test_lookup_invalid_input_returns_422_without_provider_call(
    doc_client: TestClient, payload: dict
):
    recording = RecordingLookupProvider()
    _override_lookup(recording)
    try:
        resp = doc_client.post("/api/v1/auth/kyc/document/lookup", json=payload)
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        assert recording.calls == []
    finally:
        _override_lookup(MockDocumentLookupProvider(mode="success"))


def test_lookup_type_is_case_insensitive(doc_client: TestClient):
    resp = doc_client.post(
        "/api/v1/auth/kyc/document/lookup", json={"type": "dni", "number": DNI_OK}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["document_type"] == "DNI"


# ------------------------------------------------------- Errores neutros del proveedor
def test_lookup_not_found_returns_404_without_number(doc_client: TestClient):
    _override_lookup(MockDocumentLookupProvider(mode="not_found"))
    try:
        resp = doc_client.post(
            "/api/v1/auth/kyc/document/lookup", json={"type": "DNI", "number": DNI_OK}
        )
        assert resp.status_code == 404, resp.text
        error = resp.json()["error"]
        assert error["code"] == "DOCUMENT_NOT_FOUND"
        assert DNI_OK not in resp.text
    finally:
        _override_lookup(MockDocumentLookupProvider(mode="success"))


def test_lookup_down_returns_503_without_internals(doc_client: TestClient):
    _override_lookup(MockDocumentLookupProvider(mode="down"))
    try:
        resp = doc_client.post(
            "/api/v1/auth/kyc/document/lookup", json={"type": "RUC", "number": RUC_OK}
        )
        assert resp.status_code == 503, resp.text
        assert resp.json()["error"]["code"] == "DOC_LOOKUP_UNAVAILABLE"
        for leaked in ("Traceback", "Exception", RUC_OK, "apiinti"):
            assert leaked not in resp.text, f"fuga de interno: {leaked}"
    finally:
        _override_lookup(MockDocumentLookupProvider(mode="success"))


def test_lookup_timeout_returns_504(doc_client: TestClient):
    _override_lookup(MockDocumentLookupProvider(mode="timeout"))
    try:
        resp = doc_client.post(
            "/api/v1/auth/kyc/document/lookup", json={"type": "DNI", "number": DNI_OK}
        )
        assert resp.status_code == 504, resp.text
        assert resp.json()["error"]["code"] == "DOC_LOOKUP_UNAVAILABLE"
        assert DNI_OK not in resp.text
    finally:
        _override_lookup(MockDocumentLookupProvider(mode="success"))


def test_lookup_http_404_maps_to_document_not_found():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport, base_url="https://app.apiinti.dev/api/v1")
    provider = HttpDocumentLookupProvider(
        settings=DocLookupSettings(api_key="k", backoff_base_seconds=0.0), client=client
    )
    with pytest.raises(DocumentNotFoundError):
        provider.lookup_document(session_id="s", doc_type="DNI", number=DNI_OK)


# ------------------------------------------------------- Rate limit
def test_lookup_rate_limit_returns_429(doc_client: TestClient, monkeypatch):
    monkeypatch.setenv("KYC_RATE_LIMIT_MAX_REQUESTS", "1")
    monkeypatch.setenv("KYC_RATE_LIMIT_WINDOW_SECONDS", "60")
    kyc_proxy.reset_rate_limits()
    first = doc_client.post(
        "/api/v1/auth/kyc/document/lookup", json={"type": "DNI", "number": DNI_OK}
    )
    assert first.status_code == 200
    resp = doc_client.post(
        "/api/v1/auth/kyc/document/lookup", json={"type": "DNI", "number": DNI_OK}
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMITED"


# ------------------------------------------------------- Seguridad: key y numero fuera de logs/respuestas
def test_http_provider_sends_bearer_and_json_headers():
    seen: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["content_type"] = request.headers.get("content-type")
        seen["path"] = request.url.path
        return httpx.Response(200, json={"first_name": "Ana", "last_name": "Quispe"})

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport, base_url="https://app.apiinti.dev/api/v1")
    provider = HttpDocumentLookupProvider(
        settings=DocLookupSettings(api_key="probe-key", backoff_base_seconds=0.0), client=client
    )
    provider.lookup_document(session_id="s", doc_type="DNI", number=DNI_OK)
    assert seen["authorization"] == "Bearer probe-key"
    assert seen["content_type"] == "application/json"
    assert seen["path"] == "/api/v1/dni/12345678"


def test_http_provider_ruc_uses_ruc_path():
    seen: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"razon_social": "ACME S.A.C."})

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport, base_url="https://app.apiinti.dev/api/v1")
    provider = HttpDocumentLookupProvider(
        settings=DocLookupSettings(api_key="probe-key", backoff_base_seconds=0.0), client=client
    )
    holder = provider.lookup_document(session_id="s", doc_type="RUC", number=RUC_OK)
    assert seen["path"] == "/api/v1/ruc/20123456789"
    assert isinstance(holder, DocumentHolder)
    assert holder.business_name == "ACME S.A.C."


def test_lookup_does_not_log_key_or_number(doc_client: TestClient, caplog):
    _override_lookup(_http_provider(api_key="sentinel-key-xyz"))
    try:
        with caplog.at_level(logging.INFO):
            resp = doc_client.post(
                "/api/v1/auth/kyc/document/lookup", json={"type": "DNI", "number": DNI_OK}
            )
        assert resp.status_code == 200
        assert "sentinel-key-xyz" not in caplog.text
        assert DNI_OK not in caplog.text
        assert "Ana" not in caplog.text
        assert "sentinel-key-xyz" not in resp.text
    finally:
        _override_lookup(MockDocumentLookupProvider(mode="success"))


def test_lookup_service_unit_never_logs_pii(caplog):
    provider = MockDocumentLookupProvider(mode="success")
    with caplog.at_level(logging.INFO):
        data = document_lookup.lookup_holder(provider, doc_type="DNI", number=DNI_OK)
    assert data["first_name"] == "Juan"
    assert DNI_OK not in caplog.text
    assert "Juan" not in caplog.text
    assert "Perez" not in caplog.text


# ------------------------------------------------------- Cableado y config
def test_settings_reads_apiinti_key_from_env(monkeypatch):
    monkeypatch.setenv("APIINTI_API_KEY", "dummy-key-for-wiring-check")
    monkeypatch.setenv("APIINTI_BASE_URL", "https://app.apiinti.dev/api/v1")
    settings = Settings()
    assert settings.apiinti_api_key == "dummy-key-for-wiring-check"
    assert settings.apiinti_base_url == "https://app.apiinti.dev/api/v1"
    assert settings.doc_lookup_provider == "mock"
    assert settings.doc_lookup_timeout_seconds == 5.0
    assert settings.doc_lookup_max_retries == 2


def test_compose_backend_declares_apiinti_key_pattern():
    compose = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    assert "APIINTI_API_KEY: ${APIINTI_API_KEY:-}" in text


def test_env_example_declares_apiinti_placeholder():
    example = Path(__file__).resolve().parents[1] / ".env.example"
    text = example.read_text(encoding="utf-8")
    assert "APIINTI_API_KEY=change-me" in text
    assert "DOC_LOOKUP_PROVIDER=mock" in text


def test_unknown_provider_falls_back_to_mock(monkeypatch):
    monkeypatch.setenv("DOC_LOOKUP_PROVIDER", "proveedor-desconocido")
    provider = create_document_lookup_provider()
    assert isinstance(provider, MockDocumentLookupProvider)


def test_http_provider_selected_by_env(monkeypatch):
    monkeypatch.setenv("DOC_LOOKUP_PROVIDER", "http")
    provider = create_document_lookup_provider()
    assert isinstance(provider, HttpDocumentLookupProvider)


def test_apiinti_alias_selects_http_provider(monkeypatch):
    monkeypatch.setenv("DOC_LOOKUP_PROVIDER", "apiinti")
    provider = create_document_lookup_provider()
    assert isinstance(provider, HttpDocumentLookupProvider)


def test_mock_provider_selected_by_default(monkeypatch):
    monkeypatch.setenv("DOC_LOOKUP_PROVIDER", "mock")
    provider = create_document_lookup_provider()
    assert isinstance(provider, MockDocumentLookupProvider)


def test_mock_invalid_mode_rejected():
    with pytest.raises(ValueError):
        MockDocumentLookupProvider(mode="inexistente")


# ------------------------------------------------------- Contrato
def test_openapi_includes_document_lookup(doc_client: TestClient):
    spec = doc_client.get("/openapi.json").json()
    assert "/api/v1/auth/kyc/document/lookup" in spec["paths"]


# ------------------------------------------------------- Regresion: digitos ASCII estrictos
def test_lookup_rejects_non_ascii_digits_without_provider_call(doc_client: TestClient):
    """`str.isdigit()` acepta digitos no ASCII; el contrato exige `[0-9]`."""
    recording = RecordingLookupProvider()
    _override_lookup(recording)
    try:
        resp = doc_client.post(
            "/api/v1/auth/kyc/document/lookup",
            json={"type": "DNI", "number": "١٢٣٤٥٦٧٨"},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        assert recording.calls == []
    finally:
        _override_lookup(MockDocumentLookupProvider(mode="success"))


def test_validate_lookup_input_rejects_non_ascii_digits():
    with pytest.raises(document_lookup.DocumentLookupValidationError):
        document_lookup.validate_lookup_input("DNI", "١٢٣٤٥٦٧٨")


# ------------------------------------------------------- Fuente unica: Settings
def test_doc_lookup_settings_derive_from_settings(monkeypatch):
    monkeypatch.setenv("APIINTI_BASE_URL", "https://ejemplo.test/api")
    monkeypatch.setenv("APIINTI_API_KEY", "llave-de-prueba")
    monkeypatch.setenv("DOC_LOOKUP_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("DOC_LOOKUP_MAX_RETRIES", "4")
    expected = Settings()
    actual = DocLookupSettings.from_env()
    assert actual.base_url == expected.apiinti_base_url == "https://ejemplo.test/api"
    assert actual.api_key == expected.apiinti_api_key == "llave-de-prueba"
    assert actual.timeout_seconds == expected.doc_lookup_timeout_seconds == 7.0
    assert actual.max_retries == expected.doc_lookup_max_retries == 4


def test_create_provider_reads_mock_mode_from_settings(monkeypatch):
    monkeypatch.setenv("DOC_LOOKUP_PROVIDER", "mock")
    monkeypatch.setenv("DOC_LOOKUP_MOCK_MODE", "down")
    provider = create_document_lookup_provider()
    assert isinstance(provider, MockDocumentLookupProvider)
    assert provider.mode == "down"


def test_clean_text_public_helper_trims_and_collapses():
    assert clean_text("  Ana   Quispe  ") == "Ana Quispe"
    assert clean_text(123) == ""


# ------------------------------------------------------- Cableado ampliado (compose + root .env.example)
def test_compose_backend_declares_doc_lookup_selector():
    compose = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    assert "DOC_LOOKUP_PROVIDER: ${DOC_LOOKUP_PROVIDER:-mock}" in text
    assert "APIINTI_BASE_URL: ${APIINTI_BASE_URL:-https://app.apiinti.dev/api/v1}" in text
    assert "DOC_LOOKUP_TIMEOUT_SECONDS: ${DOC_LOOKUP_TIMEOUT_SECONDS:-5}" in text
    assert "DOC_LOOKUP_MAX_RETRIES: ${DOC_LOOKUP_MAX_RETRIES:-2}" in text


def test_root_env_example_declares_apiinti_placeholders():
    example = Path(__file__).resolve().parents[2] / ".env.example"
    text = example.read_text(encoding="utf-8")
    assert "APIINTI_API_KEY=change-me" in text
    assert "APIINTI_BASE_URL=https://app.apiinti.dev/api/v1" in text
