"""Adaptador `FxRateProvider` (E7-T08, fx, HU25).

Sin BD ni red real (mock determinista + `httpx.MockTransport`):
- Camino feliz: el mock entrega las tasas semilla con `source`/`ttl`;
  directas exactas e inversas coherentes (`1/rate` a 8 decimales);
  `rate` siempre `Decimal` (nunca `float`); determinismo total.
- Errores: par no soportado / `base == quote` / moneda no ISO-4217 ->
  `VALIDATION_ERROR` (sin red); `http` con timeout / 503 / circuito
  abierto -> errores tipados; fallback semilla D04 auditable.
- Cache: hit dentro del TTL, refresco al expirar, errores nunca
  cacheados, nunca se sirve una tasa vencida como vigente.
- La key nunca viaja en URL/query ni se filtra en logs.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.adapters.fx_rate import (
    FX_RATE_TIMEOUT,
    FX_RATE_UNAVAILABLE,
    FX_RATE_VALIDATION_ERROR,
    SEED_RATES,
    CachedFxRateProvider,
    FxRateCache,
    FxRatePayload,
    FxRateProvider,
    FxRateSettings,
    FxRateTimeoutError,
    FxRateUnavailableError,
    FxRateValidationError,
    HttpFxRateProvider,
    MockFxRateProvider,
    SeedFallbackFxRateProvider,
    coerce_rate,
    create_fx_rate_provider,
    inverse_rate,
    parse_rate_payload,
    seed_rate,
)


class _Clock:
    """Reloj monotonico falso para pruebas deterministas de TTL."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _StubProvider(FxRateProvider):
    """Interior programable: cuenta llamadas, falla o cambia tasas a pedido."""

    def __init__(
        self,
        rates: dict[tuple[str, str], Decimal] | None = None,
        failures: list[Exception] | None = None,
        ttl_seconds: int = 60,
    ) -> None:
        self.rates = dict(rates or {})
        self.failures = list(failures or [])
        self.ttl_seconds = ttl_seconds
        self.calls = 0

    def _next(self, base: str, quote: str) -> FxRatePayload:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        rate = self.rates.get((base, quote))
        if rate is None:
            raise FxRateValidationError(f"par no soportado: {base}/{quote}")
        return FxRatePayload(
            base=base,
            quote=quote,
            rate=rate,
            source="http",
            ttl_seconds=self.ttl_seconds,
        )

    def get_rate(self, *, base: str, quote: str) -> FxRatePayload:
        return self._next(base, quote)

    def get_rates(self, *, base: str) -> list[FxRatePayload]:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        payloads = [
            FxRatePayload(
                base=base,
                quote=quote,
                rate=rate,
                source="http",
                ttl_seconds=self.ttl_seconds,
            )
            for (seed_base, quote), rate in sorted(self.rates.items())
            if seed_base == base
        ]
        if not payloads:
            raise FxRateValidationError(f"base no soportada: {base}")
        return payloads


# ------------------------------------------------------- mock: camino feliz
def test_mock_returns_seed_rates_with_source_and_ttl():
    mock = MockFxRateProvider()
    assert mock.get_rate(base="PEN", quote="USD") == FxRatePayload(
        base="PEN",
        quote="USD",
        rate=Decimal("3.80"),
        source="mock",
        ttl_seconds=60,
    )
    assert mock.get_rate(base="PEN", quote="EUR").rate == Decimal("4.10")
    assert mock.get_rate(base="USD", quote="EUR").rate == Decimal("0.92")


def test_mock_inverse_pairs_are_coherent():
    mock = MockFxRateProvider()
    assert mock.get_rate(base="USD", quote="PEN").rate == inverse_rate(Decimal("3.80"))
    assert mock.get_rate(base="EUR", quote="PEN").rate == inverse_rate(Decimal("4.10"))
    assert mock.get_rate(base="EUR", quote="USD").rate == inverse_rate(Decimal("0.92"))
    # Valores exactos documentados (1/rate a 8 decimales).
    assert mock.get_rate(base="USD", quote="PEN").rate == Decimal("0.26315789")
    assert mock.get_rate(base="EUR", quote="PEN").rate == Decimal("0.24390244")
    assert mock.get_rate(base="EUR", quote="USD").rate == Decimal("1.08695652")


def test_mock_rate_is_decimal_never_float():
    payload = MockFxRateProvider().get_rate(base="PEN", quote="USD")
    assert isinstance(payload.rate, Decimal)
    assert not isinstance(payload.rate, float)


def test_mock_is_deterministic_across_calls_and_instances():
    first = MockFxRateProvider().get_rate(base="PEN", quote="USD")
    second = MockFxRateProvider().get_rate(base="PEN", quote="USD")
    assert first == second
    assert MockFxRateProvider().get_rates(base="PEN") == MockFxRateProvider().get_rates(base="PEN")


def test_mock_get_rates_lists_supported_quotes_sorted():
    payloads = MockFxRateProvider().get_rates(base="PEN")
    assert [item.quote for item in payloads] == ["EUR", "USD"]
    assert all(item.base == "PEN" and item.source == "mock" for item in payloads)
    assert MockFxRateProvider().get_rates(base="USD")[0].quote == "EUR"
    assert [item.quote for item in MockFxRateProvider().get_rates(base="EUR")] == [
        "PEN",
        "USD",
    ]


def test_mock_modes_down_and_timeout_are_typed():
    with pytest.raises(FxRateUnavailableError) as exc:
        MockFxRateProvider(mode="down").get_rate(base="PEN", quote="USD")
    assert exc.value.code == FX_RATE_UNAVAILABLE
    with pytest.raises(FxRateTimeoutError) as exc:
        MockFxRateProvider(mode="timeout").get_rates(base="PEN")
    assert exc.value.code == FX_RATE_TIMEOUT
    with pytest.raises(ValueError):
        MockFxRateProvider(mode="basura")


# ------------------------------------------------------- validaciones puras
@pytest.mark.parametrize("base,quote", [("PEN", "PEN"), ("USD", "USD"), ("pen", "PEN")])
def test_same_currency_is_validation_error(base, quote):
    with pytest.raises(FxRateValidationError) as exc:
        MockFxRateProvider().get_rate(base=base, quote=quote)
    assert exc.value.code == FX_RATE_VALIDATION_ERROR


@pytest.mark.parametrize("base,quote", [("PEN", "GBP"), ("GBP", "USD"), ("JPY", "CLP")])
def test_unsupported_pair_is_validation_error(base, quote):
    with pytest.raises(FxRateValidationError) as exc:
        MockFxRateProvider().get_rate(base=base, quote=quote)
    assert exc.value.code == FX_RATE_VALIDATION_ERROR


@pytest.mark.parametrize("bad", ["PE", "PENN", "P3N", "", "  ", 123, None, True])
def test_invalid_iso_currency_is_validation_error(bad):
    with pytest.raises(FxRateValidationError) as exc:
        MockFxRateProvider().get_rate(base=bad, quote="USD")
    assert exc.value.code == FX_RATE_VALIDATION_ERROR
    with pytest.raises(FxRateValidationError):
        MockFxRateProvider().get_rates(base=bad)


def test_unsupported_base_get_rates_is_validation_error():
    with pytest.raises(FxRateValidationError) as exc:
        MockFxRateProvider().get_rates(base="GBP")
    assert exc.value.code == FX_RATE_VALIDATION_ERROR


@pytest.mark.parametrize("bad", [3.8, 0.92, True, False, 0, -1, "0", "-3.8", "abc", "", None, [1]])
def test_coerce_rate_rejects_float_and_non_positive(bad):
    with pytest.raises(FxRateValidationError):
        coerce_rate(bad)


@pytest.mark.parametrize(
    "good,expected",
    [("3.80", Decimal("3.80")), (" 4.10 ", Decimal("4.10")), (3, Decimal(3))],
)
def test_coerce_rate_accepts_exact_strings(good, expected):
    assert coerce_rate(good) == expected
    assert coerce_rate(Decimal("0.92")) == Decimal("0.92")


def test_seed_json_mirror_matches_canonical_seed_rates():
    path = Path(__file__).resolve().parent.parent / "app" / "adapters" / "data" / "fx_rates.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["ttl_seconds"] == 60
    mirrored = {(entry["base"], entry["quote"]): Decimal(entry["rate"]) for entry in body["rates"]}
    assert mirrored == SEED_RATES


def test_seed_rate_helpers_are_coherent():
    assert seed_rate("PEN", "USD") == Decimal("3.80")
    assert seed_rate("USD", "PEN") == inverse_rate(Decimal("3.80"))
    with pytest.raises(FxRateValidationError):
        seed_rate("PEN", "GBP")


def test_parse_rate_payload_rejects_float_wire_rate():
    with pytest.raises(FxRateValidationError):
        parse_rate_payload({"quote": "USD", "rate": 3.8}, base="PEN")


def test_parse_rate_payload_accepts_ttl_alias_and_defaults():
    assert parse_rate_payload({"quote": "USD", "rate": "3.80"}, base="PEN").ttl_seconds == 60
    assert (
        parse_rate_payload({"quote": "USD", "rate": "3.80", "ttl": 30}, base="PEN").ttl_seconds
        == 30
    )


# ------------------------------------------------------- cache en proceso
def test_cache_hit_within_ttl_calls_inner_once():
    inner = _StubProvider(rates={("PEN", "USD"): Decimal("3.80")})
    cached = CachedFxRateProvider(inner, cache=FxRateCache(clock=_Clock()))
    first = cached.get_rate(base="PEN", quote="USD")
    second = cached.get_rate(base="PEN", quote="USD")
    assert first == second
    assert inner.calls == 1


def test_cache_refreshes_after_expiry_and_never_serves_stale():
    clock = _Clock()
    inner = _StubProvider(rates={("PEN", "USD"): Decimal("3.80")})
    cached = CachedFxRateProvider(inner, cache=FxRateCache(clock=clock))
    assert cached.get_rate(base="PEN", quote="USD").rate == Decimal("3.80")
    clock.now += 59.0
    assert cached.get_rate(base="PEN", quote="USD").rate == Decimal("3.80")
    assert inner.calls == 1
    inner.rates[("PEN", "USD")] = Decimal("3.90")
    clock.now += 2.0  # TTL de 60 s expirado.
    refreshed = cached.get_rate(base="PEN", quote="USD")
    assert refreshed.rate == Decimal("3.90")
    assert inner.calls == 2


def test_cache_never_stores_errors():
    inner = _StubProvider(
        rates={("PEN", "USD"): Decimal("3.80")},
        failures=[FxRateUnavailableError("caido")],
    )
    cached = CachedFxRateProvider(inner, cache=FxRateCache(clock=_Clock()))
    with pytest.raises(FxRateUnavailableError):
        cached.get_rate(base="PEN", quote="USD")
    assert cached.get_rate(base="PEN", quote="USD").rate == Decimal("3.80")
    assert inner.calls == 2


def test_cache_get_rates_resolves_missing_once():
    inner = _StubProvider(rates={("PEN", "USD"): Decimal("3.80"), ("PEN", "EUR"): Decimal("4.10")})
    cached = CachedFxRateProvider(inner, cache=FxRateCache(clock=_Clock()))
    assert [item.quote for item in cached.get_rates(base="PEN")] == ["EUR", "USD"]
    assert inner.calls == 1
    # Segunda llamada: todo vigente en cache, sin tocar al interior.
    assert [item.quote for item in cached.get_rates(base="PEN")] == ["EUR", "USD"]
    assert inner.calls == 1


def test_mock_supports_custom_ttl():
    assert MockFxRateProvider(ttl_seconds=30).get_rate(base="PEN", quote="USD").ttl_seconds == 30


# ------------------------------------------------------- fabrica por entorno
def test_factory_selects_by_env_and_garbage_falls_back_to_mock(monkeypatch):
    monkeypatch.setenv("FX_RATE_PROVIDER", "mock")
    assert isinstance(create_fx_rate_provider().inner, MockFxRateProvider)
    monkeypatch.setenv("FX_RATE_PROVIDER", "http")
    assert isinstance(create_fx_rate_provider().inner, SeedFallbackFxRateProvider)
    monkeypatch.setenv("FX_RATE_PROVIDER", "basura-no-existe")
    assert isinstance(create_fx_rate_provider().inner, MockFxRateProvider)
    assert isinstance(create_fx_rate_provider("http").inner, SeedFallbackFxRateProvider)
    assert isinstance(create_fx_rate_provider("mock").inner, MockFxRateProvider)


def test_factory_applies_cache_and_fallback_flags(monkeypatch):
    monkeypatch.setenv("FX_RATE_PROVIDER", "mock")
    provider = create_fx_rate_provider()
    assert isinstance(provider, CachedFxRateProvider)
    monkeypatch.setenv("FX_RATE_PROVIDER", "http")
    monkeypatch.setenv("FX_RATE_FALLBACK_SEED", "0")
    assert isinstance(create_fx_rate_provider().inner, HttpFxRateProvider)


def test_factory_unknown_mock_mode_falls_back_to_success(monkeypatch):
    monkeypatch.setenv("FX_RATE_PROVIDER", "mock")
    monkeypatch.setenv("FX_RATE_MOCK_MODE", "basura")
    provider = create_fx_rate_provider()
    assert isinstance(provider.inner, MockFxRateProvider)
    assert provider.inner.mode == "success"


def test_settings_defaults_are_safe():
    settings = FxRateSettings.from_env({})
    assert settings.provider == "mock"
    assert settings.mock_mode == "success"
    assert settings.api_key == ""
    assert settings.cache_ttl_seconds == 60
    assert settings.fallback_seed is True


# ------------------------------------------------------- http sin red real
def _http(handler, **overrides) -> HttpFxRateProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://fx.test")
    base = {
        "provider": "http",
        "base_url": "http://fx.test",
        "api_key": "k-secreta",
        "timeout_seconds": 5.0,
        "max_retries": 1,
        "backoff_base_seconds": 0.0,
        "breaker_failures": 5,
        "breaker_cooldown_seconds": 30.0,
    }
    base.update(overrides)
    return HttpFxRateProvider(settings=FxRateSettings(**base), client=client)


def _rates_body():
    return {
        "rates": [
            {"quote": "USD", "rate": "3.80", "ttl_seconds": 60},
            {"quote": "EUR", "rate": "4.10", "ttl_seconds": 60},
        ]
    }


def test_http_get_rates_and_get_rate_success_with_auth_header():
    def _ok(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer k-secreta"
        assert "k-secreta" not in str(request.url)
        assert request.url.path == "/rates"
        assert request.url.params["base"] == "PEN"
        return httpx.Response(200, json=_rates_body())

    gateway = _http(_ok)
    payloads = gateway.get_rates(base="PEN")
    assert [item.quote for item in payloads] == ["EUR", "USD"]
    assert payloads[1] == FxRatePayload(
        base="PEN",
        quote="USD",
        rate=Decimal("3.80"),
        source="http",
        ttl_seconds=60,
    )
    assert isinstance(payloads[1].rate, Decimal)
    assert gateway.get_rate(base="PEN", quote="USD").rate == Decimal("3.80")


def test_http_get_rate_absent_quote_is_validation_error():
    def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_rates_body())

    with pytest.raises(FxRateValidationError) as exc:
        _http(_ok).get_rate(base="PEN", quote="GBP")
    assert exc.value.code == FX_RATE_VALIDATION_ERROR


def test_http_timeout_becomes_typed_error():
    def _slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulado", request=request)

    with pytest.raises(FxRateTimeoutError) as exc:
        _http(_slow).get_rates(base="PEN")
    assert exc.value.code == FX_RATE_TIMEOUT


def test_http_server_error_becomes_unavailable():
    def _down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "caido"})

    with pytest.raises(FxRateUnavailableError) as exc:
        _http(_down).get_rates(base="PEN")
    assert exc.value.code == FX_RATE_UNAVAILABLE


def test_http_malformed_body_becomes_unavailable():
    def _weird(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rara": True})

    with pytest.raises(FxRateUnavailableError):
        _http(_weird).get_rates(base="PEN")

    def _float_rate(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rates": [{"quote": "USD", "rate": 3.8}]})

    with pytest.raises(FxRateUnavailableError):
        _http(_float_rate).get_rates(base="PEN")


def test_http_retries_then_succeeds():
    calls = {"n": 0}

    def _flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={})
        return httpx.Response(200, json=_rates_body())

    payloads = _http(_flaky).get_rates(base="PEN")
    assert [item.quote for item in payloads] == ["EUR", "USD"]
    assert calls["n"] == 2


def test_http_validates_pair_without_calling_provider():
    def _must_not_run(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no debe llamar a la red con par invalido")

    with pytest.raises(FxRateValidationError):
        _http(_must_not_run).get_rate(base="PEN", quote="PEN")


def test_circuit_opens_after_consecutive_failures():
    def _down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    gateway = _http(_down, max_retries=0, breaker_failures=1, breaker_cooldown_seconds=60.0)
    with pytest.raises(FxRateUnavailableError):
        gateway.get_rates(base="PEN")
    assert gateway.circuit_open is True
    with pytest.raises(FxRateUnavailableError):
        gateway.get_rates(base="PEN")


# ------------------------------------------------------- fallback semilla (D04)
def test_fallback_returns_seed_when_primary_is_down(caplog):
    primary = MockFxRateProvider(mode="down")
    fallback = SeedFallbackFxRateProvider(primary)
    with caplog.at_level(logging.WARNING, logger="app.adapters.fx_rate"):
        payload = fallback.get_rate(base="PEN", quote="USD")
    assert payload == FxRatePayload(
        base="PEN",
        quote="USD",
        rate=Decimal("3.80"),
        source="seed",
        ttl_seconds=60,
    )
    assert any("fallback" in record.getMessage() for record in caplog.records)


def test_fallback_inverses_are_coherent_and_get_rates_supported():
    fallback = SeedFallbackFxRateProvider(MockFxRateProvider(mode="timeout"))
    assert fallback.get_rate(base="USD", quote="PEN").rate == Decimal("0.26315789")
    payloads = fallback.get_rates(base="PEN")
    assert [item.quote for item in payloads] == ["EUR", "USD"]
    assert all(item.source == "seed" for item in payloads)


def test_fallback_does_not_mask_validation_errors():
    fallback = SeedFallbackFxRateProvider(MockFxRateProvider(mode="down"))
    with pytest.raises(FxRateValidationError) as exc:
        fallback.get_rate(base="PEN", quote="GBP")
    assert exc.value.code == FX_RATE_VALIDATION_ERROR


def test_fallback_prefers_primary_when_healthy():
    primary = MockFxRateProvider(mode="success")
    assert SeedFallbackFxRateProvider(primary).get_rate(base="PEN", quote="USD").source == "mock"


# ------------------------------------------------------- sin PII / secretos
def test_secret_never_leaks_in_logs_or_url(caplog):
    seen_urls: list[str] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        assert "k-secreta" not in str(request.url)
        return httpx.Response(200, json=_rates_body())

    gateway = _http(_ok)
    with caplog.at_level(logging.INFO, logger="app.adapters.fx_rate"):
        gateway.get_rates(base="PEN")
        gateway.get_rate(base="PEN", quote="USD")
    assert seen_urls and all("k-secreta" not in url for url in seen_urls)
    for record in caplog.records:
        assert "k-secreta" not in record.getMessage()


def test_failure_logs_carry_no_secret(caplog):
    def _down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "caido"})

    with (
        caplog.at_level(logging.WARNING, logger="app.adapters.fx_rate"),
        pytest.raises(FxRateUnavailableError),
    ):
        _http(_down).get_rates(base="PEN")
    assert caplog.records
    for record in caplog.records:
        assert "k-secreta" not in record.getMessage()
