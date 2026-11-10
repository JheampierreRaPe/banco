"""Adaptador HolderLookup + fachada `validate_holder` (E2-T08, HU06 CA-02).

Sin BD ni red real (mock determinista + `httpx.MockTransport`): el mismo
CCI produce el mismo resultado; los errores son neutros (sin eco del CCI
ni del titular) y sin PII en logs; el proveedor caido/timeout colapsa a
`HOLDER_LOOKUP_UNAVAILABLE`; reintentos y circuit breaker verificados en
el `HttpHolderLookupProvider`. Sin endpoint nuevo (D-07 retirado).
"""

from __future__ import annotations

import logging
import time

import httpx
import pytest

from app.adapters.holder_lookup import (
    HOLDER_LOOKUP_UNAVAILABLE,
    HOLDER_NOT_FOUND,
    HOLDER_VALIDATION_ERROR,
    HolderLookupSettings,
    HolderLookupTimeoutError,
    HolderLookupUnavailableError,
    HolderNotFoundError,
    HolderRateLimitedError,
    HolderValidationError,
    HttpHolderLookupProvider,
    MockHolderLookupProvider,
    create_holder_lookup_provider,
    hash_cci,
    mask_holder_name,
    mock_holder_name_for,
    normalize_name,
    validate_cci_format,
)
from app.modules.transactions import service as transfers_service

CCI_OK = "00110001111111111111"
CCI_OTHER = "00220002222222222222"
EXPECTED_OK = "TITULAR DEMO 1111"


@pytest.fixture(autouse=True)
def _clean_holder_cache():
    transfers_service.clear_holder_cache()
    yield
    transfers_service.clear_holder_cache()


class RecordingProvider(MockHolderLookupProvider):
    def __init__(self, mode: str = "success") -> None:
        super().__init__(mode=mode)
        self.calls = 0

    def lookup_holder(self, *, cci, bank_code=""):
        self.calls += 1
        return super().lookup_holder(cci=cci, bank_code=bank_code)


# ---------------------------------------------------------------- Camino feliz
def test_validate_holder_ok_with_matching_name():
    result = transfers_service.validate_holder(
        CCI_OK,
        holder_name=EXPECTED_OK,
        provider=MockHolderLookupProvider(),
    )
    assert result["valid"] is True
    assert result["holder_name_masked"] == "T*** D*** 1***"
    assert EXPECTED_OK not in result["holder_name_masked"]


def test_validate_holder_ok_case_and_accent_insensitive():
    result = transfers_service.validate_holder(
        CCI_OK,
        holder_name="  titular démo 1111 ",
        provider=MockHolderLookupProvider(),
    )
    assert result["valid"] is True


def test_validate_holder_ok_without_name_precheck_mode():
    """Sin `holder_name`: E2-T09 solo pre-chequea existencia del CCI."""
    result = transfers_service.validate_holder(CCI_OK, provider=MockHolderLookupProvider())
    assert result == {"valid": True, "holder_name_masked": "T*** D*** 1***"}


def test_mock_deterministic_same_cci_same_result():
    first = transfers_service.validate_holder(
        CCI_OK, holder_name=EXPECTED_OK, provider=MockHolderLookupProvider()
    )
    second = transfers_service.validate_holder(
        CCI_OK, holder_name=EXPECTED_OK, provider=MockHolderLookupProvider()
    )
    assert first == second
    assert mock_holder_name_for(CCI_OK) == mock_holder_name_for(CCI_OK) == EXPECTED_OK


def test_mock_known_map_overrides_derivation():
    provider = MockHolderLookupProvider(known={CCI_OK: "Empresa Ejemplo S.A.C."})
    result = transfers_service.validate_holder(
        CCI_OK, holder_name="empresa ejemplo s.a.c.", provider=provider
    )
    assert result == {"valid": True, "holder_name_masked": "E*** E*** S***"}


# ---------------------------------------------------------------- Formato (422 sin proveedor)
@pytest.mark.parametrize(
    "bad_cci", ["123", "abcdefghij1234567890", "", "  ", None, 123, "0011-00xx-1111"]
)
def test_validate_holder_invalid_format_never_hits_provider(bad_cci):
    provider = RecordingProvider()
    with pytest.raises(HolderValidationError) as exc_info:
        transfers_service.validate_holder(bad_cci, provider=provider)
    assert exc_info.value.code == HOLDER_VALIDATION_ERROR == "VALIDATION_ERROR"
    assert provider.calls == 0


def test_validate_cci_strips_spaces_and_dashes():
    assert validate_cci_format("0011 0001-1111 1111 1111") == CCI_OK


def test_validate_holder_invalid_bank_code():
    with pytest.raises(HolderValidationError):
        transfers_service.validate_holder(CCI_OK, bank_code="!!")


# ---------------------------------------------------------------- Anti-enumeracion (404 neutro)
def test_holder_mismatch_is_neutral_not_found():
    with pytest.raises(HolderNotFoundError) as exc_info:
        transfers_service.validate_holder(
            CCI_OK, holder_name="OTRA PERSONA", provider=MockHolderLookupProvider()
        )
    assert exc_info.value.code == HOLDER_NOT_FOUND
    assert CCI_OK not in str(exc_info.value)
    assert "OTRA PERSONA" not in str(exc_info.value)


def test_unknown_cci_is_neutral_not_found():
    provider = MockHolderLookupProvider(mode="not_found")
    with pytest.raises(HolderNotFoundError) as exc_info:
        transfers_service.validate_holder(CCI_OTHER, provider=provider)
    assert exc_info.value.code == HOLDER_NOT_FOUND
    assert CCI_OTHER not in str(exc_info.value)


def test_mismatch_and_unknown_are_indistinguishable():
    with pytest.raises(HolderNotFoundError) as mismatch_err:
        transfers_service.validate_holder(
            CCI_OK, holder_name="OTRA PERSONA", provider=MockHolderLookupProvider()
        )
    with pytest.raises(HolderNotFoundError) as unknown_err:
        transfers_service.validate_holder(
            CCI_OTHER, provider=MockHolderLookupProvider(mode="not_found")
        )
    assert type(mismatch_err.value) is type(unknown_err.value)
    assert str(mismatch_err.value) == str(unknown_err.value)
    assert mismatch_err.value.code == unknown_err.value.code == HOLDER_NOT_FOUND


def test_same_unknown_cci_always_same_error():
    provider = MockHolderLookupProvider(mode="not_found")
    errors = []
    for _ in range(2):
        with pytest.raises(HolderNotFoundError) as exc_info:
            transfers_service.validate_holder(CCI_OTHER, provider=provider)
        errors.append((exc_info.value.code, str(exc_info.value)))
    assert errors[0] == errors[1]


# ---------------------------------------------------------------- Proveedor caido / timeout (503 neutro)
def test_provider_down_maps_to_unavailable():
    with pytest.raises(HolderLookupUnavailableError) as exc_info:
        transfers_service.validate_holder(CCI_OK, provider=MockHolderLookupProvider(mode="down"))
    assert exc_info.value.code == HOLDER_LOOKUP_UNAVAILABLE
    assert CCI_OK not in str(exc_info.value)


def test_provider_timeout_collapses_to_unavailable():
    """El timeout es indistinguible de 'no disponible' (determinista)."""
    with pytest.raises(HolderLookupUnavailableError) as exc_info:
        transfers_service.validate_holder(CCI_OK, provider=MockHolderLookupProvider(mode="timeout"))
    assert exc_info.value.code == HOLDER_LOOKUP_UNAVAILABLE


def test_same_down_cci_always_same_error():
    provider = MockHolderLookupProvider(mode="down")
    first = second = None
    for slot in ("first", "second"):
        with pytest.raises(HolderLookupUnavailableError) as exc_info:
            transfers_service.validate_holder(CCI_OK, provider=provider)
        if slot == "first":
            first = (exc_info.value.code, str(exc_info.value))
        else:
            second = (exc_info.value.code, str(exc_info.value))
    assert first == second


# ---------------------------------------------------------------- Sin PII en logs
def test_no_pii_in_logs(caplog):
    with caplog.at_level(logging.INFO, logger="app.adapters.holder_lookup"):
        transfers_service.validate_holder(
            CCI_OK, holder_name=EXPECTED_OK, provider=MockHolderLookupProvider()
        )
        with pytest.raises(HolderNotFoundError):
            transfers_service.validate_holder(
                CCI_OTHER, provider=MockHolderLookupProvider(mode="not_found")
            )
        with pytest.raises(HolderLookupUnavailableError):
            transfers_service.validate_holder(
                CCI_OK, provider=MockHolderLookupProvider(mode="down")
            )
    assert hash_cci(CCI_OK)
    for record in caplog.records:
        message = record.getMessage()
        assert CCI_OK not in message
        assert CCI_OTHER not in message
        assert EXPECTED_OK not in message


# ---------------------------------------------------------------- HTTP: reintentos y recuperacion
def _http_provider(handler, **overrides) -> HttpHolderLookupProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://d01.test")
    params = {
        "base_url": "http://d01.test",
        "backoff_base_seconds": 0.0,
        "breaker_cooldown_seconds": 30.0,
    }
    params.update(overrides)
    settings = HolderLookupSettings(**params)
    return HttpHolderLookupProvider(settings=settings, client=client)


def test_http_retries_then_succeeds():
    attempts = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        assert request.headers["Authorization"] == "Bearer "
        if attempts["n"] < 3:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"holder_name": "Ana Quispe"})

    provider = _http_provider(_handler, max_retries=2)
    record = provider.lookup_holder(cci=CCI_OK)
    assert record.holder_name == "Ana Quispe"
    assert attempts["n"] == 3


def test_http_retries_exhausted_maps_to_unavailable():
    attempts = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, json={"error": "boom"})

    provider = _http_provider(_handler, max_retries=1)
    with pytest.raises(HolderLookupUnavailableError):
        provider.lookup_holder(cci=CCI_OK)
    assert attempts["n"] == 2  # intento inicial + 1 reintento


def test_http_transport_timeout_raises_timeout():
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    provider = _http_provider(_handler, max_retries=1)
    with pytest.raises(HolderLookupTimeoutError):
        provider.lookup_holder(cci=CCI_OK)


def test_http_404_is_neutral_not_found():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "nope"})

    with pytest.raises(HolderNotFoundError) as exc_info:
        _http_provider(_handler).lookup_holder(cci=CCI_OK)
    assert CCI_OK not in str(exc_info.value)


def test_http_429_maps_to_rate_limited():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "slow"})

    with pytest.raises(HolderRateLimitedError):
        _http_provider(_handler).lookup_holder(cci=CCI_OK)


def test_http_circuit_opens_and_recovers():
    state = {"mode": "fail"}
    attempts = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if state["mode"] == "fail":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"holder_name": "Ana Quispe"})

    provider = _http_provider(
        _handler, max_retries=0, breaker_failures=2, breaker_cooldown_seconds=60.0
    )
    with pytest.raises(HolderLookupUnavailableError):
        provider.lookup_holder(cci=CCI_OK)
    with pytest.raises(HolderLookupUnavailableError):
        provider.lookup_holder(cci=CCI_OK)
    assert provider.circuit_open
    before = attempts["n"]
    with pytest.raises(HolderLookupUnavailableError):
        provider.lookup_holder(cci=CCI_OK)
    assert attempts["n"] == before  # circuito abierto: sin llamada de red
    # Tras el cooldown el circuito se cierra y el proveedor se recupera.
    provider._circuit_opened_at = time.monotonic() - 61.0
    state["mode"] = "ok"
    assert provider.lookup_holder(cci=CCI_OK).holder_name == "Ana Quispe"
    assert not provider.circuit_open


# ---------------------------------------------------------------- Fabrica, cache y frontera
def test_factory_defaults_to_mock_and_unknown_falls_back(monkeypatch):
    monkeypatch.delenv("HOLDER_LOOKUP_PROVIDER", raising=False)
    monkeypatch.delenv("HOLDER_LOOKUP_MOCK_MODE", raising=False)
    assert isinstance(create_holder_lookup_provider(), MockHolderLookupProvider)
    assert isinstance(create_holder_lookup_provider("bogus"), MockHolderLookupProvider)


def test_factory_http_selection(monkeypatch):
    monkeypatch.setenv("HOLDER_LOOKUP_PROVIDER", "http")
    assert isinstance(create_holder_lookup_provider(), HttpHolderLookupProvider)


def test_facade_uses_env_mock_by_default(monkeypatch):
    monkeypatch.setenv("HOLDER_LOOKUP_PROVIDER", "mock")
    monkeypatch.setenv("HOLDER_LOOKUP_MOCK_MODE", "success")
    result = transfers_service.validate_holder(CCI_OK, holder_name=EXPECTED_OK)
    assert result["valid"] is True


def test_success_is_cached_without_hitting_provider_twice():
    provider = RecordingProvider()
    first = transfers_service.validate_holder(CCI_OK, holder_name=EXPECTED_OK, provider=provider)
    second = transfers_service.validate_holder(CCI_OK, holder_name=EXPECTED_OK, provider=provider)
    assert first == second
    assert provider.calls == 1


def test_no_public_validate_endpoint_invented():
    from app.main import app

    paths = set()
    for route in app.routes:
        paths.add(getattr(route, "path", ""))
    assert not any("interbank/validate" in path for path in paths)


# ---------------------------------------------------------------- Utilidades puras
def test_mask_holder_name_never_returns_clear_text():
    assert mask_holder_name("Juan Perez Gomez") == "J*** P*** G***"
    assert mask_holder_name("") == ""


def test_normalize_name_strips_accents_for_matching():
    assert normalize_name("  José  Pérez ") == "JOSE PEREZ"
