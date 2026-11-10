"""Adaptador `ClearingSource` (E5-T16, reconciliation, HU19).

Sin BD ni red real (mock determinista + `httpx.MockTransport`):
- Camino feliz: el mock entrega lote + items; `checksum` estable entre
  llamadas; misma entrada -> mismo lote.
- Errores: item con monto <= 0 / moneda invalida / `item_count`
  inconsistente -> `VALIDATION_ERROR`; `http` con timeout / 503 /
  circuito abierto -> errores tipados; la key nunca se filtra.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.adapters.clearing_source import (
    CLEARING_SOURCE_TIMEOUT,
    CLEARING_SOURCE_UNAVAILABLE,
    CLEARING_SOURCE_VALIDATION_ERROR,
    ClearingFilePayload,
    ClearingItemPayload,
    ClearingSourceSettings,
    ClearingSourceTimeoutError,
    ClearingSourceUnavailableError,
    ClearingSourceValidationError,
    HttpClearingSource,
    MockClearingSource,
    build_deterministic_batch,
    canonical_checksum,
    check_batch_consistency,
    create_clearing_source,
    parse_file_payload,
    parse_item_payload,
)


def _item_dict(**overrides):
    base = {
        "external_ref": "EXT-ABCDEF-0001",
        "amount_minor": 5200,
        "currency": "PEN",
        "value_date": "2026-01-15",
        "direction": "INBOUND",
        "raw": {"seq": 1},
    }
    base.update(overrides)
    return base


def _file_dict(**overrides):
    _file, _items = build_deterministic_batch(
        seed="clearing-demo", source="NETWORK", day="2026-01-15"
    )
    base = {
        "source": _file.source,
        "file_ref": _file.file_ref,
        "checksum": _file.checksum,
        "item_count": _file.item_count,
    }
    base.update(overrides)
    return base


# ------------------------------------------------------- mock: camino feliz
def test_mock_fetch_files_returns_valid_batch():
    gateway = MockClearingSource()
    files = gateway.fetch_files()
    assert len(files) == 1
    file = files[0]
    assert isinstance(file, ClearingFilePayload)
    assert file.file_ref.startswith("CLR-")
    assert len(file.checksum) == 64
    assert file.item_count == 3

    items = gateway.fetch_items(file.file_ref)
    assert len(items) == file.item_count
    for item in items:
        assert isinstance(item, ClearingItemPayload)
        assert isinstance(item.amount_minor, int) and item.amount_minor > 0
        assert item.currency in ("PEN", "USD")
        assert item.direction in ("INBOUND", "OUTBOUND")
        assert item.external_ref


def test_mock_is_deterministic_across_calls():
    gateway = MockClearingSource()
    first = gateway.fetch_files()[0]
    second = gateway.fetch_files()[0]
    assert first == second
    assert gateway.fetch_items(first.file_ref) == gateway.fetch_items(second.file_ref)
    assert first.checksum == canonical_checksum(gateway.fetch_items(first.file_ref))


def test_mock_same_input_same_batch_other_day_other_ref():
    gateway = MockClearingSource()
    default = gateway.fetch_files()[0]
    other = gateway.fetch_files(since="2026-01-16")[0]
    assert default.file_ref != other.file_ref
    assert default.checksum != other.checksum
    again = gateway.fetch_files(since="2026-01-16", source="network")[0]
    assert again == other


def test_mock_fetch_batch_verifies_count_and_checksum():
    gateway = MockClearingSource()
    file = gateway.fetch_files()[0]
    batch_file, items = gateway.fetch_batch(file.file_ref)
    assert batch_file == file
    assert len(items) == file.item_count


def test_mock_modes_down_and_timeout_are_typed():
    with pytest.raises(ClearingSourceUnavailableError) as exc:
        MockClearingSource(mode="down").fetch_files()
    assert exc.value.code == CLEARING_SOURCE_UNAVAILABLE
    with pytest.raises(ClearingSourceTimeoutError) as exc:
        MockClearingSource(mode="timeout").fetch_files()
    assert exc.value.code == CLEARING_SOURCE_TIMEOUT
    with pytest.raises(ValueError):
        MockClearingSource(mode="basura")


def test_mock_unknown_file_ref_is_validation_error():
    gateway = MockClearingSource()
    gateway.fetch_files()
    with pytest.raises(ClearingSourceValidationError) as exc:
        gateway.fetch_items("CLR-20990101-DEADBEEF")
    assert exc.value.code == CLEARING_SOURCE_VALIDATION_ERROR


# ------------------------------------------------------- validaciones puras
@pytest.mark.parametrize(
    "override",
    [
        {"amount_minor": 0},
        {"amount_minor": -100},
        {"amount_minor": 52.5},
        {"amount_minor": True},
        {"currency": "PE"},
        {"currency": "PENN"},
        {"currency": "pen "[:2]},
        {"direction": "SIDEWAYS"},
        {"direction": ""},
        {"external_ref": ""},
        {"value_date": "no-fecha"},
        {"raw": "no-dict"},
    ],
)
def test_parse_item_rejects_invalid_payload(override):
    with pytest.raises(ClearingSourceValidationError) as exc:
        parse_item_payload(_item_dict(**override))
    assert exc.value.code == CLEARING_SOURCE_VALIDATION_ERROR


def test_parse_item_rejects_non_dict():
    with pytest.raises(ClearingSourceValidationError):
        parse_item_payload([1, 2, 3])


@pytest.mark.parametrize(
    "override",
    [
        {"checksum": "abc"},
        {"checksum": "g" * 64},
        {"item_count": -1},
        {"item_count": 2.5},
        {"file_ref": ""},
        {"file_ref": "!!"},
    ],
)
def test_parse_file_rejects_invalid_payload(override):
    with pytest.raises(ClearingSourceValidationError) as exc:
        parse_file_payload(_file_dict(**override))
    assert exc.value.code == CLEARING_SOURCE_VALIDATION_ERROR


def test_check_batch_rejects_count_mismatch():
    file, items = build_deterministic_batch(
        seed="clearing-demo", source="NETWORK", day="2026-01-15"
    )
    tampered = ClearingFilePayload(
        source=file.source,
        file_ref=file.file_ref,
        checksum=file.checksum,
        item_count=file.item_count + 1,
    )
    with pytest.raises(ClearingSourceValidationError):
        check_batch_consistency(tampered, items)


def test_check_batch_rejects_checksum_mismatch():
    file, items = build_deterministic_batch(
        seed="clearing-demo", source="NETWORK", day="2026-01-15"
    )
    tampered = ClearingFilePayload(
        source=file.source,
        file_ref=file.file_ref,
        checksum="0" * 64,
        item_count=file.item_count,
    )
    with pytest.raises(ClearingSourceValidationError):
        check_batch_consistency(tampered, items)


# ------------------------------------------------------- fabrica por entorno
def test_factory_selects_by_env_and_garbage_falls_back_to_mock(monkeypatch):
    monkeypatch.setenv("CLEARING_SOURCE_PROVIDER", "mock")
    assert isinstance(create_clearing_source(), MockClearingSource)
    monkeypatch.setenv("CLEARING_SOURCE_PROVIDER", "http")
    assert isinstance(create_clearing_source(), HttpClearingSource)
    monkeypatch.setenv("CLEARING_SOURCE_PROVIDER", "basura-no-existe")
    assert isinstance(create_clearing_source(), MockClearingSource)
    assert isinstance(create_clearing_source("http"), HttpClearingSource)
    assert isinstance(create_clearing_source("mock"), MockClearingSource)


def test_settings_defaults_are_safe():
    settings = ClearingSourceSettings.from_env({})
    assert settings.provider == "mock"
    assert settings.mock_mode == "success"
    assert settings.api_key == ""


# ------------------------------------------------------- http sin red real
def _http(handler, **overrides) -> HttpClearingSource:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://clearing.test")
    base = {
        "provider": "http",
        "base_url": "http://clearing.test",
        "api_key": "k-secreta",
        "timeout_seconds": 5.0,
        "max_retries": 1,
        "backoff_base_seconds": 0.0,
        "breaker_failures": 5,
        "breaker_cooldown_seconds": 30.0,
    }
    base.update(overrides)
    return HttpClearingSource(settings=ClearingSourceSettings(**base), client=client)


def test_http_fetch_files_and_items_success():
    file_entry = _file_dict()
    item_entry = _item_dict()

    def _ok(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer k-secreta"
        assert "k-secreta" not in str(request.url)
        if request.url.path == "/clearing/files":
            return httpx.Response(200, json={"files": [file_entry]})
        assert request.url.path == f"/clearing/files/{file_entry['file_ref']}/items"
        return httpx.Response(200, json={"items": [item_entry]})

    gateway = _http(_ok)
    files = gateway.fetch_files()
    assert files == [parse_file_payload(file_entry)]
    items = gateway.fetch_items(files[0].file_ref)
    assert items == [parse_item_payload(item_entry)]


def test_http_fetch_files_accepts_bare_list():
    file_entry = _file_dict()

    def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[file_entry])

    assert _http(_ok).fetch_files() == [parse_file_payload(file_entry)]


def test_http_timeout_becomes_typed_error():
    def _slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulado", request=request)

    with pytest.raises(ClearingSourceTimeoutError) as exc:
        _http(_slow).fetch_items("CLR-20260115-ABCDEF01")
    assert exc.value.code == CLEARING_SOURCE_TIMEOUT


def test_http_server_error_becomes_unavailable():
    def _down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "caido"})

    with pytest.raises(ClearingSourceUnavailableError) as exc:
        _http(_down).fetch_files()
    assert exc.value.code == CLEARING_SOURCE_UNAVAILABLE


def test_http_malformed_body_becomes_unavailable():
    def _weird(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rara": True})

    with pytest.raises(ClearingSourceUnavailableError):
        _http(_weird).fetch_files()

    def _weird_item(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [_item_dict(amount_minor=0)]})

    with pytest.raises(ClearingSourceUnavailableError):
        _http(_weird_item).fetch_items("CLR-20260115-ABCDEF01")


def test_http_retries_then_succeeds():
    calls = {"n": 0}
    file_entry = _file_dict()

    def _flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"files": [file_entry]})

    files = _http(_flaky).fetch_files()
    assert files == [parse_file_payload(file_entry)]
    assert calls["n"] == 2


def test_http_validates_file_ref_without_calling_provider():
    def _must_not_run(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no debe llamar a la red con ref invalida")

    with pytest.raises(ClearingSourceValidationError):
        _http(_must_not_run).fetch_items("!!")


def test_circuit_opens_after_consecutive_failures():
    def _down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    gateway = _http(_down, max_retries=0, breaker_failures=1, breaker_cooldown_seconds=60.0)
    with pytest.raises(ClearingSourceUnavailableError):
        gateway.fetch_files()
    assert gateway.circuit_open is True
    with pytest.raises(ClearingSourceUnavailableError):
        gateway.fetch_files()


# ------------------------------------------------------- sin PII / secretos
def test_secret_and_refs_never_leak_in_logs(caplog):
    gateway = MockClearingSource()
    file = gateway.fetch_files()[0]
    gateway.fetch_items(file.file_ref)
    with caplog.at_level(logging.INFO, logger="app.adapters.clearing_source"):
        ref = gateway.fetch_files(since="2026-01-16")[0].file_ref
        gateway.fetch_batch(ref, since="2026-01-16")
    for record in caplog.records:
        assert "k-secreta" not in record.getMessage()
        assert file.file_ref not in record.getMessage()


def test_http_never_sends_key_in_url():
    seen: list[str] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"files": []})

    _http(_ok).fetch_files(since="2026-01-15")
    assert seen and all("k-secreta" not in url for url in seen)
