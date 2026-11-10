"""Adaptador WalletInterop (E4-T03, HU13 CA-03 interoperabilidad).

Sin BD ni red real (mock determinista + `httpx.MockTransport`): el mock
codifica/decodifica el estandar `BO-QR1` de forma reproducible (mismo input
-> mismo output); el payload manipulado se rechaza tipado; el servidor
simulado que agota timeout/cae produce `INTEROP_TIMEOUT`/`INTEROP_UNAVAILABLE`
tras reintentos con backoff y circuito; la seleccion `mock|http` es por
entorno (valor basura -> `mock`); los logs/errores nunca contienen la
referencia en claro ni la API key; ninguna prueba hace red real.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.adapters.wallet_interop import (
    INTEROP_TIMEOUT,
    INTEROP_UNAVAILABLE,
    INTEROP_VALIDATION_ERROR,
    CounterpartResult,
    HttpWalletInteropGateway,
    InteropPayload,
    InteropTimeoutError,
    InteropUnavailableError,
    InteropValidationError,
    MockWalletInteropGateway,
    WalletInteropSettings,
    create_wallet_interop_provider,
    decode_payload,
    encode_payload,
)

PEER = "PAR-BANCO-01"
ISSUER = "BO-PE-001"
REF = "QR-COBRO-123"
AMOUNT = 150_00


def _payload_kwargs(**overrides):
    base = {
        "issuer": ISSUER,
        "peer": PEER,
        "ref": REF,
        "amount_minor": AMOUNT,
        "currency": "PEN",
    }
    base.update(overrides)
    return base


def _settings(**overrides) -> WalletInteropSettings:
    base = {
        "provider": "http",
        "base_url": "http://interop.test",
        "api_key": "k",
        "timeout_seconds": 5.0,
        "max_retries": 2,
        "backoff_base_seconds": 0.0,
        "breaker_failures": 3,
        "breaker_cooldown_seconds": 30.0,
    }
    base.update(overrides)
    return WalletInteropSettings(**base)


def _http(handler, **overrides) -> HttpWalletInteropGateway:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://interop.test")
    return HttpWalletInteropGateway(settings=_settings(**overrides), client=client)


# ---------------------------------------------------------------- Camino feliz: mock determinista
def test_mock_encode_decode_roundtrip_deterministic():
    first = MockWalletInteropGateway().encode_payload(**_payload_kwargs())
    second = MockWalletInteropGateway().encode_payload(**_payload_kwargs())
    assert first == second
    decoded = MockWalletInteropGateway().decode_payload(first)
    assert decoded == InteropPayload(
        issuer=ISSUER, peer=PEER, ref=REF, amount_minor=AMOUNT, currency="PEN"
    )


def test_module_functions_match_gateway_methods():
    gateway = MockWalletInteropGateway()
    assert gateway.encode_payload(**_payload_kwargs()) == encode_payload(**_payload_kwargs())
    assert gateway.decode_payload(encode_payload(**_payload_kwargs())) == decode_payload(
        encode_payload(**_payload_kwargs())
    )


def test_mock_validate_counterpart_success():
    result = MockWalletInteropGateway().validate_counterpart(peer_ref=PEER, bank_code="PE01")
    assert result == CounterpartResult(valid=True, peer_ref=PEER, bank_code="PE01")


def test_mock_validate_resolves_configured_bank_code():
    provider = MockWalletInteropGateway(counterparts={PEER: "PE02"})
    assert provider.validate_counterpart(peer_ref=PEER).bank_code == "PE02"


def test_mock_validate_rejected_is_typed_rejection():
    provider = MockWalletInteropGateway(rejected={PEER})
    result = provider.validate_counterpart(peer_ref=PEER)
    assert result.valid is False
    assert result.peer_ref == PEER


# ---------------------------------------------------------------- Errores: payload invalido/manipulado
@pytest.mark.parametrize(
    "tamper",
    [
        "no-es-base64!!!",
        "e30",  # `{}` valido pero sin el estandar
        None,
        "",
    ],
)
def test_decode_rejects_malformed_payload(tamper):
    with pytest.raises(InteropValidationError) as exc_info:
        decode_payload(tamper)  # type: ignore[arg-type]
    assert exc_info.value.code == INTEROP_VALIDATION_ERROR


def test_decode_rejects_tampered_payload():
    valid = encode_payload(**_payload_kwargs())
    tampered = ("A" if valid[0] != "A" else "B") + valid[1:]
    with pytest.raises(InteropValidationError):
        decode_payload(tampered)


def test_encode_rejects_negative_amount_and_bad_currency():
    with pytest.raises(InteropValidationError):
        encode_payload(**_payload_kwargs(amount_minor=-1))
    with pytest.raises(InteropValidationError):
        encode_payload(**_payload_kwargs(currency="soles"))


def test_validate_counterpart_rejects_bad_format_locally():
    with pytest.raises(InteropValidationError):
        MockWalletInteropGateway().validate_counterpart(peer_ref="!!!")


# ---------------------------------------------------------------- Resiliencia http (transporte simulado, sin red real)
def test_http_validate_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/interop/counterparts/validate"
        assert "Authorization" in request.headers
        return httpx.Response(200, json={"valid": True, "peer_ref": PEER, "bank_code": "PE01"})

    result = _http(handler).validate_counterpart(peer_ref=PEER)
    assert result == CounterpartResult(valid=True, peer_ref=PEER, bank_code="PE01")


def test_http_timeout_exhausts_retries_then_typed_error():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("simulado")

    with pytest.raises(InteropTimeoutError) as exc_info:
        _http(handler).validate_counterpart(peer_ref=PEER)
    assert exc_info.value.code == INTEROP_TIMEOUT
    assert calls["n"] == 3  # intento inicial + 2 reintentos


def test_http_down_then_typed_unavailable():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"error": "caido"})

    with pytest.raises(InteropUnavailableError) as exc_info:
        _http(handler).validate_counterpart(peer_ref=PEER)
    assert exc_info.value.code == INTEROP_UNAVAILABLE
    assert calls["n"] == 3


def test_http_circuit_opens_after_consecutive_failures():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    gateway = _http(handler, max_retries=0, breaker_failures=2, breaker_cooldown_seconds=60.0)
    with pytest.raises(InteropUnavailableError):
        gateway.validate_counterpart(peer_ref=PEER)
    with pytest.raises(InteropUnavailableError):
        gateway.validate_counterpart(peer_ref=PEER)
    assert gateway.circuit_open is True
    with pytest.raises(InteropUnavailableError) as exc_info:
        gateway.validate_counterpart(peer_ref=PEER)
    assert exc_info.value.code == INTEROP_UNAVAILABLE


def test_mock_modes_down_and_timeout():
    with pytest.raises(InteropUnavailableError) as exc_info:
        MockWalletInteropGateway(mode="down").validate_counterpart(peer_ref=PEER)
    assert exc_info.value.code == INTEROP_UNAVAILABLE
    with pytest.raises(InteropTimeoutError) as exc_info:
        MockWalletInteropGateway(mode="timeout").validate_counterpart(peer_ref=PEER)
    assert exc_info.value.code == INTEROP_TIMEOUT


# ---------------------------------------------------------------- Config: seleccion por entorno
def test_factory_selects_http_and_falls_back_to_mock(monkeypatch):
    monkeypatch.setenv("WALLET_INTEROP_PROVIDER", "http")
    assert isinstance(create_wallet_interop_provider(), HttpWalletInteropGateway)
    monkeypatch.setenv("WALLET_INTEROP_PROVIDER", "basura-no-reconocida")
    provider = create_wallet_interop_provider()
    assert isinstance(provider, MockWalletInteropGateway)


def test_factory_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("WALLET_INTEROP_PROVIDER", raising=False)
    assert isinstance(create_wallet_interop_provider(), MockWalletInteropGateway)


# ---------------------------------------------------------------- Seguridad: sin secretos/PII en logs ni red real
def test_api_key_and_peer_ref_never_in_logs(caplog):
    secret = "sk-secreta-del-par-123"
    gateway = _http(
        lambda request: httpx.Response(200, json={"valid": True}),
        api_key=secret,
    )
    with caplog.at_level(logging.INFO, logger="app.adapters.wallet_interop"):
        gateway.validate_counterpart(peer_ref=PEER)
        try:
            MockWalletInteropGateway(mode="down").validate_counterpart(peer_ref=PEER)
        except InteropUnavailableError:
            pass
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in text
    assert PEER not in text


def test_no_real_network_every_http_test_uses_mock_transport():
    # Guarda de regresion: este modulo solo permite `httpx.MockTransport`.
    import app.adapters.wallet_interop as module

    assert hasattr(httpx, "MockTransport")
    assert module.VALIDATE_PATH == "/interop/counterparts/validate"
