"""Adaptador KycProvider (E1-T01): mock + HTTP con reintentos y circuito."""

from __future__ import annotations

import httpx
import pytest

from app.adapters.kyc_provider import (
    KYC_INVALID,
    KYC_TIMEOUT,
    KYC_UNAVAILABLE,
    Challenge,
    HttpKycProvider,
    KycInvalidError,
    KycSettings,
    KycTimeoutError,
    KycUnavailableError,
    MockKycProvider,
    create_kyc_provider,
    hash_token,
)

API_KEY = "test-key-123"
SESSION = "sess-001"


def _settings(**overrides) -> KycSettings:
    base = {
        "base_url": "http://kyc.local",
        "api_key": API_KEY,
        "timeout_seconds": 1.0,
        "max_retries": 2,
        "backoff_base_seconds": 0.0,
        "breaker_failures": 3,
        "breaker_cooldown_seconds": 60.0,
    }
    base.update(overrides)
    return KycSettings(**base)


def _client(handler, captured: list) -> httpx.Client:
    def _wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    return httpx.Client(
        base_url="http://kyc.local",
        timeout=1.0,
        headers={"X-API-Key": API_KEY},
        transport=httpx.MockTransport(_wrapped),
    )


# ------------------------------------------------------------- Mock
def test_mock_success_returns_results_and_token_hash():
    mock = MockKycProvider(mode="success")
    challenge = mock.challenge(session_id=SESSION)
    assert isinstance(challenge, Challenge)
    assert challenge.steps == ("blink", "turn-left", "smile")
    assert challenge.challenge_token_hash == hash_token(f"mock-challenge-{SESSION}")
    assert challenge.expose_token() == f"mock-challenge-{SESSION}"

    evaluation = mock.evaluate_liveness(session_id=SESSION, task="blink")
    assert evaluation.passed is True

    full = mock.verify_full(session_id=SESSION)
    assert full.overall_result is True
    assert full.detail_code == "OK"


def test_mock_failure_returns_negative_results_without_raising():
    mock = MockKycProvider(mode="failure")
    assert mock.evaluate_liveness(session_id=SESSION, task="blink").passed is False
    full = mock.verify_full(session_id=SESSION)
    assert full.overall_result is False
    assert full.detail_code == "LIVENESS_FAILED"


def test_mock_timeout_and_down_raise_typed_errors():
    with pytest.raises(KycTimeoutError) as exc:
        MockKycProvider(mode="timeout").challenge(session_id=SESSION)
    assert exc.value.code == KYC_TIMEOUT
    with pytest.raises(KycUnavailableError) as exc2:
        MockKycProvider(mode="down").verify_full(session_id=SESSION)
    assert exc2.value.code == KYC_UNAVAILABLE


def test_mock_rejects_unknown_mode():
    with pytest.raises(ValueError):
        MockKycProvider(mode="otro")


# ------------------------------------------------------------- HTTP real (transporte simulado)
def test_http_success_maps_endpoints_and_keeps_key_in_header_only():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/liveness/challenge":
            return httpx.Response(
                200,
                json={"challenge_token": "tok-abc", "steps": ["blink"], "expires_in_seconds": 60},
            )
        if request.url.path == "/api/v1/liveness/evaluate":
            return httpx.Response(200, json={"passed": True, "score": 0.99})
        return httpx.Response(
            200, json={"overall_result": True, "distance": 0.2, "detail_code": "OK"}
        )

    provider = HttpKycProvider(settings=_settings(), client=_client(handler, captured))
    challenge = provider.challenge(session_id=SESSION)
    assert challenge.challenge_token_hash == hash_token("tok-abc")
    assert provider.evaluate_liveness(session_id=SESSION, task="blink").passed is True
    assert provider.verify_full(session_id=SESSION).overall_result is True

    assert [r.url.path for r in captured] == [
        "/api/v1/liveness/challenge",
        "/api/v1/liveness/evaluate",
        "/api/v1/identity/verify-full",
    ]
    for request in captured:
        assert request.headers["X-API-Key"] == API_KEY
        assert API_KEY not in request.content.decode()


def test_http_invalid_request_raises_kyc_invalid_without_retry():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "bad task"})

    provider = HttpKycProvider(settings=_settings(), client=_client(handler, captured))
    with pytest.raises(KycInvalidError) as exc:
        provider.evaluate_liveness(session_id=SESSION, task="???")
    assert exc.value.code == KYC_INVALID
    assert len(captured) == 1  # 4xx no se reintenta


def test_http_timeout_retries_then_raises_kyc_timeout():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    provider = HttpKycProvider(settings=_settings(), client=_client(handler, captured))
    with pytest.raises(KycTimeoutError) as exc:
        provider.challenge(session_id=SESSION)
    assert exc.value.code == KYC_TIMEOUT
    assert len(captured) == 3  # 1 intento + 2 reintentos


def test_http_circuit_opens_after_falls_and_blocks_calls():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "caido"})

    provider = HttpKycProvider(
        settings=_settings(max_retries=0, breaker_failures=2, breaker_cooldown_seconds=3600.0),
        client=_client(handler, captured),
    )
    with pytest.raises(KycUnavailableError):
        provider.challenge(session_id=SESSION)
    with pytest.raises(KycUnavailableError):
        provider.challenge(session_id=SESSION)
    assert provider.circuit_open is True
    calls_before = len(captured)
    with pytest.raises(KycUnavailableError) as exc:
        provider.challenge(session_id=SESSION)
    assert exc.value.code == KYC_UNAVAILABLE
    assert len(captured) == calls_before, "circuito abierto: sin llamadas HTTP"


def test_http_returns_never_expose_api_key():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"challenge_token": "tok-abc", "steps": [], "expires_in_seconds": 5}
        )

    provider = HttpKycProvider(settings=_settings(), client=_client(handler, captured))
    challenge = provider.challenge(session_id=SESSION)
    blob = repr((challenge.challenge_token_hash, challenge.steps, challenge.expires_in_seconds))
    assert API_KEY not in blob
    assert API_KEY not in challenge.expose_token()


# ------------------------------------------------------------- Fabrica
def test_factory_selects_mock_or_http_from_env(monkeypatch):
    monkeypatch.setenv("KYC_PROVIDER", "mock")
    monkeypatch.setenv("KYC_MOCK_MODE", "failure")
    provider = create_kyc_provider()
    assert isinstance(provider, MockKycProvider)
    assert provider.mode == "failure"

    monkeypatch.setenv("KYC_PROVIDER", "http")
    monkeypatch.setenv("KYC_BASE_URL", "http://kyc.local")
    monkeypatch.setenv("KYC_API_KEY", API_KEY)
    provider = create_kyc_provider()
    assert isinstance(provider, HttpKycProvider)
    assert provider.settings.base_url == "http://kyc.local"

    assert isinstance(create_kyc_provider(kind="mock"), MockKycProvider)
    # Sin env: default mock (apto para local sin microservicio).
    monkeypatch.delenv("KYC_PROVIDER", raising=False)
    assert isinstance(create_kyc_provider(), MockKycProvider)


def test_settings_from_env_with_documented_defaults(monkeypatch):
    for var in (
        "KYC_BASE_URL",
        "KYC_API_KEY",
        "KYC_TIMEOUT_SECONDS",
        "KYC_MAX_RETRIES",
        "KYC_BACKOFF_BASE_SECONDS",
        "KYC_BREAKER_FAILURES",
        "KYC_BREAKER_COOLDOWN_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = KycSettings.from_env()
    assert (settings.base_url, settings.max_retries) == ("http://localhost:8000", 2)
