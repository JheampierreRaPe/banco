"""Adaptador CreditBureau (E3-T06, HU10 CA-03 parte bureau).

Sin BD ni red real (mock determinista + `httpx.MockTransport`): el mock es
reproducible por perfil; los fallos HTTP provocan reintentos con backoff y,
agotados, error tipado (`BUREAU_UNAVAILABLE`/`BUREAU_TIMEOUT`); el circuit
breaker abre y se recupera; la seleccion `mock|http` no toca las llamadas;
los logs/errores nunca contienen PII (documento/ingresos/deuda) ni la key.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.adapters.credit_bureau import (
    BUREAU_TIMEOUT,
    BUREAU_UNAVAILABLE,
    BUREAU_VALIDATION_ERROR,
    MOCK_PROFILES,
    BureauScore,
    BureauTimeoutError,
    BureauUnavailableError,
    BureauValidationError,
    CreditBureauSettings,
    HttpCreditBureau,
    MockCreditBureau,
    create_credit_bureau_provider,
    hash_subject,
    mock_profile_for,
    normalize_morosidad,
    parse_bureau_payload,
)

SUBJECT_A = "doc-hash-sujeto-a"
SUBJECT_B = "doc-hash-sujeto-b"
INCOME = 250_00
DEBT = 80_00


def _settings(**overrides) -> CreditBureauSettings:
    base = {
        "provider": "http",
        "base_url": "http://bureau.test",
        "api_key": "k",
        "timeout_seconds": 5.0,
        "max_retries": 2,
        "backoff_base_seconds": 0.0,
        "breaker_failures": 3,
        "breaker_cooldown_seconds": 30.0,
    }
    base.update(overrides)
    return CreditBureauSettings(**base)


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------- Camino feliz: mock determinista
def test_mock_deterministic_same_subject_same_result():
    provider = MockCreditBureau()
    first = provider.get_score(document_hash=SUBJECT_A, income_minor=INCOME, debt_minor=DEBT)
    second = provider.get_score(document_hash=SUBJECT_A, income_minor=INCOME, debt_minor=DEBT)
    assert first == second
    assert isinstance(first, BureauScore)
    assert isinstance(first.score, int)
    assert isinstance(first.deudas_minor, int)
    assert isinstance(first.morosidad, bool)


def test_mock_profile_derivation_is_stable_and_known():
    expected = MOCK_PROFILES[mock_profile_for(SUBJECT_A)]
    assert MockCreditBureau().get_score(document_hash=SUBJECT_A) == expected
    assert (
        MockCreditBureau().get_score(document_hash=SUBJECT_B)
        == MOCK_PROFILES[mock_profile_for(SUBJECT_B)]
    )


def test_mock_fixture_override_by_subject():
    fixture = BureauScore(score=700, deudas_minor=10_00, morosidad=False)
    provider = MockCreditBureau(profiles={SUBJECT_A: fixture})
    assert provider.get_score(document_hash=SUBJECT_A) == fixture
    # Otro sujeto sigue la derivacion por hash (sin pisar el perfil).
    assert provider.get_score(document_hash=SUBJECT_B) == MOCK_PROFILES[mock_profile_for(SUBJECT_B)]


def test_mock_fixture_accepts_provider_shape_dict():
    provider = MockCreditBureau(
        profiles={SUBJECT_A: {"score": 710, "deudas": 20_00, "morosidad": "SI"}}
    )
    assert provider.get_score(document_hash=SUBJECT_A) == BureauScore(
        score=710, deudas_minor=20_00, morosidad=True
    )


def test_mock_invalid_amount_never_scores():
    with pytest.raises(BureauValidationError) as exc_info:
        MockCreditBureau().get_score(document_hash=SUBJECT_A, income_minor=-1)
    assert exc_info.value.code == BUREAU_VALIDATION_ERROR


def test_mock_down_and_timeout_modes_are_typed():
    with pytest.raises(BureauUnavailableError) as exc_info:
        MockCreditBureau(mode="down").get_score(document_hash=SUBJECT_A)
    assert exc_info.value.code == BUREAU_UNAVAILABLE
    with pytest.raises(BureauTimeoutError) as exc_info:
        MockCreditBureau(mode="timeout").get_score(document_hash=SUBJECT_A)
    assert exc_info.value.code == BUREAU_TIMEOUT


def test_mock_invalid_mode_rejected():
    with pytest.raises(ValueError):
        MockCreditBureau(mode="aleatorio")


# ---------------------------------------------------------------- Normalizacion del contrato
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"score": 720, "deudas": 30_00, "morosidad": False}, (720, 30_00, False)),
        ({"score": 720, "deudas_minor": 30_00, "morosidad": "SI"}, (720, 30_00, True)),
        ({"score": 720, "debt_minor": 30_00, "morosidad": "no"}, (720, 30_00, False)),
        ({"data": {"score": 600, "deudas": 0, "morosidad": "alta"}}, (600, 0, True)),
    ],
)
def test_parse_bureau_payload_shapes(payload, expected):
    assert parse_bureau_payload(payload) == BureauScore(
        score=expected[0], deudas_minor=expected[1], morosidad=expected[2]
    )


@pytest.mark.parametrize(
    "bad", [{}, {"score": "alto"}, {"score": 700, "deudas": -5}, "texto", None]
)
def test_parse_bureau_payload_invalid_is_unavailable(bad):
    with pytest.raises(BureauUnavailableError) as exc_info:
        parse_bureau_payload(bad)
    assert exc_info.value.code == BUREAU_UNAVAILABLE


# ---------------------------------------------------------------- HTTP: feliz + reintentos
def test_http_ok_returns_normalized_score():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/score"
        assert request.headers["Authorization"] == "Bearer k"
        return httpx.Response(200, json={"score": 730, "deudas": 25_00, "morosidad": False})

    client = httpx.Client(transport=_transport(handler), base_url="http://bureau.test")
    provider = HttpCreditBureau(settings=_settings(), client=client)
    result = provider.get_score(document_hash=SUBJECT_A, income_minor=INCOME, debt_minor=DEBT)
    assert result == BureauScore(score=730, deudas_minor=25_00, morosidad=False)


def test_http_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, json={"error": "caida"})
        return httpx.Response(200, json={"score": 700, "deudas": 0, "morosidad": False})

    client = httpx.Client(transport=_transport(handler), base_url="http://bureau.test")
    provider = HttpCreditBureau(settings=_settings(max_retries=2), client=client)
    result = provider.get_score(document_hash=SUBJECT_A)
    assert result.score == 700
    assert calls["n"] == 3


def test_http_backoff_grows_exponentially():
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "caida"})

    client = httpx.Client(transport=_transport(handler), base_url="http://bureau.test")
    provider = HttpCreditBureau(
        settings=_settings(max_retries=2, backoff_base_seconds=0.1),
        client=client,
        sleeper=delays.append,
    )
    with pytest.raises(BureauUnavailableError):
        provider.get_score(document_hash=SUBJECT_A)
    assert delays == pytest.approx([0.1, 0.2, 0.4])


def test_http_exhausted_server_errors_collapse_to_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "caida"})

    client = httpx.Client(transport=_transport(handler), base_url="http://bureau.test")
    provider = HttpCreditBureau(settings=_settings(max_retries=1), client=client)
    with pytest.raises(BureauUnavailableError) as exc_info:
        provider.get_score(document_hash=SUBJECT_A)
    assert exc_info.value.code == BUREAU_UNAVAILABLE


def test_http_transport_timeout_collapses_to_bureau_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("lento", request=request)

    client = httpx.Client(transport=_transport(handler), base_url="http://bureau.test")
    provider = HttpCreditBureau(settings=_settings(max_retries=1), client=client)
    with pytest.raises(BureauTimeoutError) as exc_info:
        provider.get_score(document_hash=SUBJECT_A)
    assert exc_info.value.code == BUREAU_TIMEOUT


def test_http_invalid_amount_never_hits_provider():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"score": 700, "deudas": 0, "morosidad": False})

    client = httpx.Client(transport=_transport(handler), base_url="http://bureau.test")
    provider = HttpCreditBureau(settings=_settings(), client=client)
    with pytest.raises(BureauValidationError):
        provider.get_score(document_hash=SUBJECT_A, debt_minor=-10)
    assert calls["n"] == 0


# ---------------------------------------------------------------- Circuit breaker
def test_circuit_opens_and_recovers():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "caida"})

    client = httpx.Client(transport=_transport(handler), base_url="http://bureau.test")
    provider = HttpCreditBureau(
        settings=_settings(max_retries=0, breaker_failures=2, breaker_cooldown_seconds=3600.0),
        client=client,
    )
    with pytest.raises(BureauUnavailableError):
        provider.get_score(document_hash=SUBJECT_A)
    with pytest.raises(BureauUnavailableError):
        provider.get_score(document_hash=SUBJECT_A)
    assert provider.circuit_open is True
    # Con el circuito abierto no hay mas intentos de red: falla de inmediato.
    with pytest.raises(BureauUnavailableError):
        provider.get_score(document_hash=SUBJECT_A)
    # Tras la ventana, el circuito se cierra y reintenta (aqui vuelve a fallar).
    provider._circuit_opened_at -= 3601.0
    assert provider.circuit_open is False
    with pytest.raises(BureauUnavailableError):
        provider.get_score(document_hash=SUBJECT_A)


# ---------------------------------------------------------------- Seleccion por config
def test_factory_selects_mock_by_default(monkeypatch):
    monkeypatch.delenv("CREDIT_BUREAU_PROVIDER", raising=False)
    monkeypatch.setenv("CREDIT_BUREAU_MOCK_MODE", "success")
    assert isinstance(create_credit_bureau_provider(), MockCreditBureau)


@pytest.mark.parametrize("kind", ["http", "real", "remote"])
def test_factory_selects_http_variants(monkeypatch, kind):
    monkeypatch.setenv("CREDIT_BUREAU_PROVIDER", kind)
    assert isinstance(create_credit_bureau_provider(), HttpCreditBureau)


def test_factory_unknown_provider_falls_back_to_mock(monkeypatch):
    monkeypatch.setenv("CREDIT_BUREAU_PROVIDER", "inexistente")
    assert isinstance(create_credit_bureau_provider(), MockCreditBureau)


def test_factory_kind_overrides_env(monkeypatch):
    monkeypatch.setenv("CREDIT_BUREAU_PROVIDER", "http")
    provider = create_credit_bureau_provider("mock")
    assert isinstance(provider, MockCreditBureau)
    assert provider.get_score(document_hash=SUBJECT_A) == MOCK_PROFILES[mock_profile_for(SUBJECT_A)]


def test_settings_read_required_env_names(monkeypatch):
    monkeypatch.setenv("CREDIT_BUREAU_PROVIDER", "http")
    monkeypatch.setenv("CREDIT_BUREAU_BASE_URL", "https://bureau.example")
    monkeypatch.setenv("CREDIT_BUREAU_API_KEY", "secreta")
    monkeypatch.setenv("CREDIT_BUREAU_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("CREDIT_BUREAU_MAX_RETRIES", "4")
    settings = CreditBureauSettings.from_env()
    assert settings.provider == "http"
    assert settings.base_url == "https://bureau.example"
    assert settings.api_key == "secreta"
    assert settings.timeout_seconds == 7.5
    assert settings.max_retries == 4


# ---------------------------------------------------------------- Sin PII ni secretos en logs
def test_logs_and_errors_contain_no_pii_or_secrets(caplog):
    secret_key = "clave-bureau-super-secreta-123"
    document = "documento-sensible-44556677"
    provider = HttpCreditBureau(
        settings=_settings(api_key=secret_key, max_retries=0),
        client=httpx.Client(
            transport=_transport(lambda request: httpx.Response(500, json={"error": "caida"})),
            base_url="http://bureau.test",
        ),
    )
    with caplog.at_level(logging.INFO, logger="app.adapters.credit_bureau"):
        with pytest.raises(BureauUnavailableError) as exc_info:
            provider.get_score(document_hash=document, income_minor=INCOME, debt_minor=DEBT)
        MockCreditBureau().get_score(document_hash=document)
    logged = "\n".join(
        record.getMessage() for record in caplog.records if "credit_bureau" in record.name
    )
    assert hash_subject(document) in logged  # correlacion solo hasheada
    for sensitive in (document, str(INCOME), str(DEBT), secret_key, str(exc_info.value)):
        assert sensitive not in logged
    assert secret_key not in str(exc_info.value)


def test_normalize_morosidad_text_shapes():
    assert normalize_morosidad(True) is True
    assert normalize_morosidad("SI") is True
    assert normalize_morosidad("no") is False
    assert normalize_morosidad("") is False
