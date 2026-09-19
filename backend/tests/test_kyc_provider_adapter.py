"""Adaptador KycProvider (E1-T01): mock + HTTP con reintentos y circuito."""

from __future__ import annotations

import base64
import json

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
PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10).decode()


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


def test_mock_validate_document_success_and_failure():
    ok = MockKycProvider(mode="success").validate_document(session_id=SESSION)
    assert ok.is_valid is True
    assert ok.issues == []
    assert isinstance(ok.checks, dict)

    bad = MockKycProvider(mode="failure").validate_document(session_id=SESSION)
    assert bad.is_valid is False
    assert bad.issues and isinstance(bad.issues[0], str)

    with pytest.raises(KycTimeoutError):
        MockKycProvider(mode="timeout").validate_document(session_id=SESSION)
    with pytest.raises(KycUnavailableError):
        MockKycProvider(mode="down").validate_document(session_id=SESSION)


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


def test_http_4xx_dict_detail_propagates_step_and_reason():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": {"step": "blink", "reason": "no_face_detected"}})

    provider = HttpKycProvider(settings=_settings(), client=_client(handler, captured))
    with pytest.raises(KycInvalidError) as exc:
        provider.evaluate_liveness(session_id=SESSION, task="blink")
    assert exc.value.step == "blink"
    assert exc.value.reason == "no_face_detected"
    assert exc.value.code == KYC_INVALID


def test_http_4xx_string_detail_becomes_reason():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "not enough frames"})

    provider = HttpKycProvider(settings=_settings(), client=_client(handler, captured))
    with pytest.raises(KycInvalidError) as exc:
        provider.evaluate_liveness(session_id=SESSION, task="blink")
    assert exc.value.step == ""
    assert exc.value.reason == "not enough frames"


def test_http_evaluate_maps_step_reason_frames_and_burst():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "step": "blink",
                "passed": True,
                "reason": "ok",
                "frames_analyzed": 6,
                "details": {"eyes": 2},
            },
        )

    provider = HttpKycProvider(settings=_settings(), client=_client(handler, captured))
    result = provider.evaluate_liveness(
        session_id=SESSION, task="blink", payload={"token": "tok", "frames_base64": ["a", "b"]}
    )
    assert (result.step, result.reason, result.frames_analyzed) == ("blink", "ok", 6)
    assert result.details == {"eyes": 2}
    sent = json.loads(captured[-1].content.decode())
    assert sent == {"token": "tok", "step": "blink", "frames_base64": ["a", "b"]}


def test_http_verify_full_uses_dedicated_timeout_and_burst():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "overall_result": True,
                "overall_reason": "OK",
                "liveness": {
                    "steps_verified": ["arriba", "parpadeo"],
                    "steps_total": ["arriba", "abajo", "parpadeo"],
                    "step_results": {"blink": {"passed": True, "reason": "ok"}},
                },
            },
        )

    provider = HttpKycProvider(
        settings=_settings(timeout_seconds=3.0, verify_timeout_seconds=60.0),
        client=_client(handler, captured),
    )
    frames = [PNG_B64, PNG_B64, PNG_B64]
    result = provider.verify_full(
        session_id="tok",
        payload={
            "document_type": "DNI",
            "document_image_b64": PNG_B64,
            "segments": [{"task": "blink", "frames_b64": frames}],
        },
    )
    assert result.overall_result is True
    assert result.steps_verified == ["arriba", "parpadeo"]
    assert result.steps_total == ["arriba", "abajo", "parpadeo"]
    assert result.step_results["blink"]["passed"] is True
    timeout = captured[-1].extensions.get("timeout")
    assert timeout is not None
    assert set(timeout.values()) == {60.0}
    assert b"blink" in captured[-1].content
    assert frames[0].encode() in captured[-1].content


def test_http_verify_full_derives_failed_step_and_reason():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "overall_result": False,
                "overall_reason": "LIVENESS_FAILED",
                "liveness": {
                    "steps_verified": ["turn-left"],
                    "steps_total": ["blink", "turn-left", "parpadeo"],
                    "step_results": {
                        "blink": {"passed": False, "reason": "eyes_not_detected"},
                        "turn-left": {"passed": True, "reason": "ok"},
                    },
                },
            },
        )

    provider = HttpKycProvider(settings=_settings(), client=_client(handler, captured))
    result = provider.verify_full(
        session_id="tok",
        payload={
            "document_type": "DNI",
            "document_image_b64": PNG_B64,
            "segments": [{"task": "blink", "image_b64": PNG_B64}],
        },
    )
    assert result.failed_step == "blink"
    assert result.overall_reason == "LIVENESS_FAILED"
    assert result.step_results["blink"]["reason"] == "eyes_not_detected"


def test_http_verify_full_accepts_real_guided_step_lists_without_crashing():
    """Forma REAL del microservicio: `steps_verified`/`steps_total` son listas.

    Antes del fix el adapter hacia `int(lista)` -> TypeError con el payload real
    (`liveness_service.py:267-268`), lo que reventaba el submit con 500.
    """
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "overall_result": False,
                "overall_reason": "Falló: abajo, parpadeo",
                "liveness": {
                    "steps_verified": ["arriba"],
                    "steps_total": ["arriba", "abajo", "parpadeo"],
                    "step_results": {
                        "arriba": {"passed": True, "reason": "ok"},
                        "abajo": {"passed": False, "reason": "movement_not_detected"},
                        "parpadeo": {"passed": False, "reason": "no_blink"},
                    },
                },
                "face_match": {"distance": 0.81},
            },
        )

    provider = HttpKycProvider(settings=_settings(), client=_client(handler, captured))
    result = provider.verify_full(
        session_id="tok",
        payload={
            "document_type": "DNI",
            "document_image_b64": PNG_B64,
            "segments": [{"task": "arriba", "frames_b64": [PNG_B64] * 5}],
        },
    )
    assert result.steps_total == ["arriba", "abajo", "parpadeo"]
    assert result.steps_verified == ["arriba"]
    assert result.failed_step == "abajo"
    assert result.overall_reason == "Falló: abajo, parpadeo"
    assert result.distance == 0.81


def test_http_verify_full_does_not_duplicate_first_frame_with_image_b64():
    """`frames_b64` es la fuente unica: no concatena `image_b64` (primer frame)."""
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"overall_result": True, "detail_code": "OK"})

    provider = HttpKycProvider(settings=_settings(), client=_client(handler, captured))
    provider.verify_full(
        session_id="tok",
        payload={
            "document_type": "DNI",
            "document_image_b64": PNG_B64,
            "segments": [
                {"task": "arriba", "frames_b64": ["AAA", "BBB", "CCC"], "image_b64": "AAA"}
            ],
        },
    )
    body = captured[-1].content
    # El primer frame solo aparece una vez: con el bug previo se duplicaba.
    assert body.count(b'"AAA"') == 1


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


def test_http_validate_document_sends_multipart_and_uses_dedicated_timeout():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"is_valid": True, "issues": [], "checks": {"legible": True}}
        )

    provider = HttpKycProvider(
        settings=_settings(timeout_seconds=3.0, document_timeout_seconds=15.0),
        client=_client(handler, captured),
    )
    result = provider.validate_document(session_id=SESSION, image_b64=PNG_B64)
    assert result.is_valid is True
    assert result.issues == []
    assert result.checks == {"legible": True}

    request = captured[-1]
    assert request.url.path == "/api/v1/document/validate"
    assert str(request.url) == "http://kyc.local/api/v1/document/validate"
    assert provider.DOCUMENT_VALIDATE_PATH == "/api/v1/document/validate"
    assert request.headers["X-API-Key"] == API_KEY
    assert API_KEY not in request.content.decode("latin-1")
    assert b'name="file"' in request.content
    timeout = request.extensions.get("timeout")
    assert timeout is not None
    assert set(timeout.values()) == {15.0}


def test_http_validate_document_normalizes_wrapped_data():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "is_valid": False,
                    "issues": ["blurry", "glare"],
                    "checks": {"resolution": "low"},
                }
            },
        )

    provider = HttpKycProvider(settings=_settings(), client=_client(handler, captured))
    result = provider.validate_document(session_id=SESSION, image_b64=PNG_B64)
    assert result.is_valid is False
    assert result.issues == ["blurry", "glare"]
    assert result.checks == {"resolution": "low"}


def test_http_validate_document_tolerates_unexpected_issue_and_check_shapes():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"is_valid": False, "issues": "blurry", "checks": [1, 2]})

    provider = HttpKycProvider(settings=_settings(), client=_client(handler, captured))
    result = provider.validate_document(session_id=SESSION, image_b64=PNG_B64)
    assert result.is_valid is False
    assert result.issues == ["blurry"]
    assert result.checks == {}


def test_http_validate_document_4xx_raises_kyc_invalid_without_retry():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "not an image"})

    provider = HttpKycProvider(settings=_settings(), client=_client(handler, captured))
    with pytest.raises(KycInvalidError) as exc:
        provider.validate_document(session_id=SESSION, image_b64=PNG_B64)
    assert exc.value.code == KYC_INVALID
    assert exc.value.reason == "not an image"
    assert len(captured) == 1


def test_http_validate_document_5xx_raises_unavailable():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    provider = HttpKycProvider(settings=_settings(max_retries=0), client=_client(handler, captured))
    with pytest.raises(KycUnavailableError):
        provider.validate_document(session_id=SESSION, image_b64=PNG_B64)


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
        "KYC_VERIFY_TIMEOUT_SECONDS",
        "KYC_DOCUMENT_TIMEOUT_SECONDS",
        "KYC_MAX_RETRIES",
        "KYC_BACKOFF_BASE_SECONDS",
        "KYC_BREAKER_FAILURES",
        "KYC_BREAKER_COOLDOWN_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = KycSettings.from_env()
    assert (settings.base_url, settings.max_retries) == ("http://localhost:8000", 2)
    assert settings.timeout_seconds == 3.0
    assert settings.verify_timeout_seconds == 60.0
    assert settings.document_timeout_seconds == 15.0


def test_settings_verify_timeout_from_env(monkeypatch):
    monkeypatch.setenv("KYC_VERIFY_TIMEOUT_SECONDS", "45")
    assert KycSettings.from_env().verify_timeout_seconds == 45.0


def test_settings_document_timeout_from_env(monkeypatch):
    monkeypatch.setenv("KYC_DOCUMENT_TIMEOUT_SECONDS", "20")
    assert KycSettings.from_env().document_timeout_seconds == 20.0
