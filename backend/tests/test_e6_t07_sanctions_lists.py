"""Adaptador `SanctionsLists` (E6-T07, `risk`, HU21).

Sin BD ni red real (mock determinista + `httpx.MockTransport`):
- Normalizacion pura: acentos, mayusculas, puntuacion, espacios.
- Camino feliz: coincidencia exacta (`1.0`) y difusa (typo, orden de
  nombres) con `match_score` y `matched_fields` (solo nombres de campos).
- Sin coincidencia: `matched=false` en todos los hits (falso positivo
  controlado). Documento y pais refuerzan sin crear hits por si solos.
- Umbral: el limite cuenta como coincidencia; por debajo no.
- `http`: timeout/503/circuito -> errores tipados; la key solo viaja en
  cabecera y jamas se filtra (ni en URL ni en logs, sin PII en logs).
- Determinismo: misma entrada -> mismo score. Sin persistencia: las
  semillas JSON no cambian tras los cotejos.
"""

from __future__ import annotations

import hashlib
import json
import logging

import httpx
import pytest

from app.adapters.sanctions_lists import (
    SANCTIONS_TIMEOUT,
    SANCTIONS_UNAVAILABLE,
    SANCTIONS_VALIDATION_ERROR,
    HttpSanctionsLists,
    MockSanctionsLists,
    SanctionsListsSettings,
    SanctionsTimeoutError,
    SanctionsUnavailableError,
    SanctionsValidationError,
    ScreeningHit,
    clamp_threshold,
    create_sanctions_lists,
    evaluate_entry,
    normalize_country,
    normalize_document,
    normalize_name,
    normalize_subject_type,
    parse_hit_payload,
    parse_list_version_payload,
)
from app.modules.risk.domain.screening import (
    DEFAULT_SCREENING_THRESHOLD,
    is_match,
    jaro_winkler,
    name_similarity,
    normalize_text,
)

EXACT_NAME = "Aurelio Mendoza Quispe"
EXACT_DOC = "FIC-OFAC-0001"


# ------------------------------------------------------- normalizacion pura
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("áéíóú ñ ü", "AEIOU N U"),
        ("  Aurelio   MENDOZA\tQuispe\n", "AURELIO MENDOZA QUISPE"),
        ("Corp. Andina-Ficticia S.A.C.", "CORP ANDINA FICTICIA S A C"),
        ("TALÍA  HUAMÁN-CONDORI", "TALIA HUAMAN CONDORI"),
        ("", ""),
        (None, ""),
        (123, ""),
    ],
)
def test_normalize_text_accents_case_punct_spaces(raw, expected):
    assert normalize_text(raw) == expected


def test_name_similarity_is_deterministic_and_bounded():
    first = name_similarity("Aurelio Mendoza Quispe", "Aurelio Mendoza Quispi")
    second = name_similarity("Aurelio Mendoza Quispe", "Aurelio Mendoza Quispi")
    assert first == second
    assert 0.0 <= first <= 1.0
    assert name_similarity("Aurelio Mendoza Quispe", "Aurelio Mendoza Quispe") == 1.0
    assert name_similarity("", "Aurelio Mendoza Quispe") == 0.0
    assert name_similarity("", "") == 0.0


def test_jaro_winkler_identical_and_empty():
    assert jaro_winkler("ABC", "ABC") == 1.0
    assert jaro_winkler("", "ABC") == 0.0
    assert 0.0 <= jaro_winkler("MENDOZA", "MENDOZA QUISPE") <= 1.0


# ------------------------------------------------------- validaciones puras
def test_normalize_subject_type_accepts_both_cases():
    assert normalize_subject_type("PERSON") == "person"
    assert normalize_subject_type("Company") == "company"
    with pytest.raises(SanctionsValidationError) as exc:
        normalize_subject_type("alien")
    assert exc.value.code == SANCTIONS_VALIDATION_ERROR


@pytest.mark.parametrize("bad", ["", "   ", None, 123, "x" * 201])
def test_normalize_name_rejects_empty_or_huge(bad):
    with pytest.raises(SanctionsValidationError) as exc:
        normalize_name(bad)
    assert exc.value.code == SANCTIONS_VALIDATION_ERROR


def test_normalize_document_and_country():
    assert normalize_document(None) is None
    assert normalize_document("  fic-ofac-0001 ") == "FICOFAC0001"
    assert normalize_country(None) is None
    assert normalize_country("pe") == "PE"
    with pytest.raises(SanctionsValidationError):
        normalize_country("PER")


def test_clamp_threshold_out_of_range_falls_back_to_default():
    assert clamp_threshold(0.85) == 0.85
    assert clamp_threshold(1.0) == 1.0
    assert clamp_threshold(0.0) == DEFAULT_SCREENING_THRESHOLD
    assert clamp_threshold(2.0) == DEFAULT_SCREENING_THRESHOLD
    assert clamp_threshold("basura") == DEFAULT_SCREENING_THRESHOLD
    assert clamp_threshold(True) == DEFAULT_SCREENING_THRESHOLD


def test_is_match_boundary_includes_limit():
    assert is_match(0.85, 0.85) is True
    assert is_match(0.8499, 0.85) is False
    assert is_match("basura", 0.85) is False


# ------------------------------------------------------- mock: camino feliz
def test_mock_exact_match_scores_one_with_fields():
    gateway = MockSanctionsLists()
    hits = gateway.screen(
        subject_type="person",
        name=EXACT_NAME,
        document_number=EXACT_DOC,
        country="PE",
    )
    assert {hit.list_code for hit in hits} == {"OFAC", "PEP", "WORLDCHECK"}
    ofac = next(hit for hit in hits if hit.list_code == "OFAC")
    assert ofac.matched is True
    assert ofac.match_score == 1.0
    assert set(ofac.matched_fields) == {"name", "document_number", "country"}
    assert all(isinstance(hit, ScreeningHit) for hit in hits)


def test_mock_fuzzy_typo_matches_with_high_score():
    gateway = MockSanctionsLists()
    hits = gateway.screen(subject_type="person", name="Aurelio Mendoza Quispi")
    ofac = next(hit for hit in hits if hit.list_code == "OFAC")
    assert ofac.matched is True
    assert ofac.match_score >= DEFAULT_SCREENING_THRESHOLD
    assert ofac.match_score < 1.0
    assert ofac.matched_fields == ("name",)


def test_mock_name_order_matches_exact():
    gateway = MockSanctionsLists()
    hits = gateway.screen(subject_type="person", name="Quispe Mendoza Aurelio")
    ofac = next(hit for hit in hits if hit.list_code == "OFAC")
    assert ofac.matched is True
    assert ofac.match_score == 1.0
    assert "name" in ofac.matched_fields


def test_mock_distinct_name_returns_no_false_positive():
    gateway = MockSanctionsLists()
    hits = gateway.screen(subject_type="person", name="Juan Perez Garcia")
    assert hits, "siempre hay un hit por lista (el mejor candidato)"
    assert all(hit.matched is False for hit in hits)
    assert all(hit.matched_fields == () for hit in hits)
    assert all(hit.match_score < DEFAULT_SCREENING_THRESHOLD for hit in hits)


def test_mock_document_match_scores_one_without_name():
    gateway = MockSanctionsLists()
    hits = gateway.screen(
        subject_type="person",
        name="Nombre Totalmente Distinto",
        document_number="FIC-OFAC-0001",
    )
    ofac = next(hit for hit in hits if hit.list_code == "OFAC")
    assert ofac.matched is True
    assert ofac.match_score == 1.0
    assert "document_number" in ofac.matched_fields
    assert "name" not in ofac.matched_fields


def test_mock_country_reinforces_but_never_creates_hit_alone():
    gateway = MockSanctionsLists()
    # Nombre exacto + pais correcto -> `country` refuerza.
    hits = gateway.screen(subject_type="person", name=EXACT_NAME, country="PE")
    ofac = next(hit for hit in hits if hit.list_code == "OFAC")
    assert ofac.matched is True
    assert set(ofac.matched_fields) == {"name", "country"}
    # Nombre exacto + pais distinto -> el nombre igual coincide, sin `country`.
    hits = gateway.screen(subject_type="person", name=EXACT_NAME, country="CL")
    ofac = next(hit for hit in hits if hit.list_code == "OFAC")
    assert ofac.matched is True
    assert ofac.matched_fields == ("name",)
    # Nombre distinto + pais coincidente -> sin hit (el pais solo no basta).
    hits = gateway.screen(subject_type="person", name="Nombre Totalmente Distinto", country="PE")
    assert all(hit.matched is False for hit in hits)


def test_mock_company_subject_type_supported():
    gateway = MockSanctionsLists()
    hits = gateway.screen(subject_type="company", name="Corporacion Andina Ficticia S.A.C.")
    ofac = next(hit for hit in hits if hit.list_code == "OFAC")
    assert ofac.matched is True
    assert ofac.match_score == 1.0


def test_mock_invalid_subject_raises_validation_error():
    gateway = MockSanctionsLists()
    with pytest.raises(SanctionsValidationError) as exc:
        gateway.screen(subject_type="alien", name=EXACT_NAME)
    assert exc.value.code == SANCTIONS_VALIDATION_ERROR
    with pytest.raises(SanctionsValidationError):
        gateway.screen(subject_type="person", name="   ")


def test_mock_modes_down_and_timeout_are_typed():
    with pytest.raises(SanctionsUnavailableError) as exc:
        MockSanctionsLists(mode="down").screen(subject_type="person", name=EXACT_NAME)
    assert exc.value.code == SANCTIONS_UNAVAILABLE
    with pytest.raises(SanctionsTimeoutError) as exc:
        MockSanctionsLists(mode="timeout").screen(subject_type="person", name=EXACT_NAME)
    assert exc.value.code == SANCTIONS_TIMEOUT
    with pytest.raises(ValueError):
        MockSanctionsLists(mode="basura")


def test_mock_list_versions_are_versioned():
    versions = MockSanctionsLists().list_versions()
    assert {item.list_code for item in versions} == {"OFAC", "PEP", "WORLDCHECK"}
    for item in versions:
        assert item.version
        assert item.effective_from


def test_mock_is_deterministic_across_calls():
    gateway = MockSanctionsLists()
    first = gateway.screen(subject_type="person", name="Aurelio Mendoza Quispi")
    second = gateway.screen(subject_type="person", name="Aurelio Mendoza Quispi")
    assert first == second


def test_mock_strict_threshold_only_keeps_exact():
    strict = MockSanctionsLists(threshold=1.0)
    exact = strict.screen(subject_type="person", name=EXACT_NAME)
    ofac_exact = next(hit for hit in exact if hit.list_code == "OFAC")
    assert ofac_exact.matched is True
    fuzzy = strict.screen(subject_type="person", name="Aurelio Mendoza Quispi")
    ofac_fuzzy = next(hit for hit in fuzzy if hit.list_code == "OFAC")
    assert ofac_fuzzy.matched is False


def test_evaluate_entry_country_alone_never_matches():
    from app.adapters.sanctions_lists import SeedEntry

    entry = SeedEntry(
        name="AURELIO MENDOZA QUISPE",
        document_number="FIC-OFAC-0001",
        country="PE",
    )
    score, fields = evaluate_entry(
        name="Nombre Totalmente Distinto",
        document_number=None,
        country="PE",
        entry=entry,
        threshold=DEFAULT_SCREENING_THRESHOLD,
    )
    assert not is_match(score, DEFAULT_SCREENING_THRESHOLD)
    assert fields == ()


# ------------------------------------------------------- fabrica por entorno
def test_factory_selects_by_env_and_garbage_falls_back_to_mock(monkeypatch):
    monkeypatch.setenv("SANCTIONS_PROVIDER", "mock")
    assert isinstance(create_sanctions_lists(), MockSanctionsLists)
    monkeypatch.setenv("SANCTIONS_PROVIDER", "http")
    assert isinstance(create_sanctions_lists(), HttpSanctionsLists)
    monkeypatch.setenv("SANCTIONS_PROVIDER", "basura-no-existe")
    assert isinstance(create_sanctions_lists(), MockSanctionsLists)
    assert isinstance(create_sanctions_lists("http"), HttpSanctionsLists)
    assert isinstance(create_sanctions_lists("mock"), MockSanctionsLists)


def test_settings_defaults_are_safe():
    settings = SanctionsListsSettings.from_env({})
    assert settings.provider == "mock"
    assert settings.mock_mode == "success"
    assert settings.api_key == ""
    assert settings.screening_threshold == DEFAULT_SCREENING_THRESHOLD


def test_settings_invalid_threshold_falls_back_to_default():
    settings = SanctionsListsSettings.from_env({"SANCTIONS_SCREENING_THRESHOLD": "99"})
    assert settings.screening_threshold == DEFAULT_SCREENING_THRESHOLD


# ------------------------------------------------------- parseo de payloads
def test_parse_hit_payload_accepts_aliases_and_recalibrates_missing_matched():
    hit = parse_hit_payload({"list_code": "ofac", "score": 0.92, "fields": ["name"]})
    assert hit.list_code == "OFAC"
    assert hit.match_score == 0.92
    assert hit.matched_fields == ("name",)
    assert hit.matched is False  # fail-closed: el `http` recalibra con su umbral.


@pytest.mark.parametrize(
    "bad",
    [
        {"list_code": "", "match_score": 0.9},
        {"list_code": "OFAC", "match_score": 1.5},
        {"list_code": "OFAC", "match_score": "alta"},
        {"list_code": "OFAC", "match_score": 0.9, "matched_fields": ["nombre"]},
        {"list_code": "OFAC", "match_score": 0.9, "matched_fields": "name"},
        ["no-dict"],
    ],
)
def test_parse_hit_payload_rejects_invalid(bad):
    with pytest.raises(SanctionsValidationError) as exc:
        parse_hit_payload(bad)
    assert exc.value.code == SANCTIONS_VALIDATION_ERROR


def test_parse_list_version_payload_roundtrip():
    version = parse_list_version_payload(
        {"list_code": "OFAC", "version": "2026.01", "effective_from": "2026-01-05"}
    )
    assert version.list_code == "OFAC"
    assert version.version == "2026.01"
    with pytest.raises(SanctionsValidationError):
        parse_list_version_payload({"list_code": "OFAC"})


# ------------------------------------------------------- http sin red real
def _http(handler, **overrides) -> HttpSanctionsLists:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://lists.test")
    base = {
        "provider": "http",
        "base_url": "http://lists.test",
        "api_key": "k-secreta",
        "timeout_seconds": 5.0,
        "max_retries": 1,
        "backoff_base_seconds": 0.0,
        "breaker_failures": 5,
        "breaker_cooldown_seconds": 30.0,
        "screening_threshold": 0.85,
    }
    base.update(overrides)
    return HttpSanctionsLists(settings=SanctionsListsSettings(**base), client=client)


def _hit_dict(**overrides):
    base = {
        "list_code": "OFAC",
        "matched": True,
        "match_score": 1.0,
        "matched_fields": ["name"],
    }
    base.update(overrides)
    return base


def test_http_screen_and_lists_success_recalibrates_matched():
    def _ok(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer k-secreta"
        assert "k-secreta" not in str(request.url)
        if request.url.path == "/screen":
            assert request.method == "POST"
            return httpx.Response(200, json={"hits": [_hit_dict(matched=False)]})
        assert request.url.path == "/lists"
        return httpx.Response(
            200,
            json={
                "lists": [
                    {
                        "list_code": "OFAC",
                        "version": "2026.01",
                        "effective_from": "2026-01-05",
                    }
                ]
            },
        )

    gateway = _http(_ok)
    hits = gateway.screen(subject_type="person", name=EXACT_NAME)
    # El servidor dijo `matched=false` pero el score `1.0` supera el umbral
    # propio: el cliente recalibra en local (determinista).
    assert hits[0].matched is True
    assert hits[0].matched_fields == ("name",)
    versions = gateway.list_versions()
    assert versions[0].list_code == "OFAC"


def test_http_screen_accepts_bare_list_and_applies_threshold():
    def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_hit_dict(match_score=0.5)])

    hits = _http(_ok).screen(subject_type="person", name=EXACT_NAME)
    assert hits[0].matched is False
    assert hits[0].matched_fields == ()


def test_http_validates_subject_without_calling_provider():
    def _must_not_run(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no debe llamar a la red con sujeto invalido")

    with pytest.raises(SanctionsValidationError):
        _http(_must_not_run).screen(subject_type="person", name="   ")
    with pytest.raises(SanctionsValidationError):
        _http(_must_not_run).screen(subject_type="alien", name=EXACT_NAME)


def test_http_timeout_becomes_typed_error():
    def _slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulado", request=request)

    with pytest.raises(SanctionsTimeoutError) as exc:
        _http(_slow).screen(subject_type="person", name=EXACT_NAME)
    assert exc.value.code == SANCTIONS_TIMEOUT


def test_http_server_error_becomes_unavailable():
    def _down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "caido"})

    with pytest.raises(SanctionsUnavailableError) as exc:
        _http(_down).screen(subject_type="person", name=EXACT_NAME)
    assert exc.value.code == SANCTIONS_UNAVAILABLE


def test_http_malformed_body_becomes_unavailable():
    def _weird(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rara": True})

    with pytest.raises(SanctionsUnavailableError):
        _http(_weird).screen(subject_type="person", name=EXACT_NAME)

    def _weird_hit(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": [_hit_dict(match_score="alta")]})

    with pytest.raises(SanctionsUnavailableError):
        _http(_weird_hit).screen(subject_type="person", name=EXACT_NAME)


def test_http_retries_then_succeeds():
    calls = {"n": 0}

    def _flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"hits": [_hit_dict()]})

    hits = _http(_flaky).screen(subject_type="person", name=EXACT_NAME)
    assert hits[0].matched is True
    assert calls["n"] == 2


def test_circuit_opens_after_consecutive_failures():
    def _down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    gateway = _http(_down, max_retries=0, breaker_failures=1, breaker_cooldown_seconds=60.0)
    with pytest.raises(SanctionsUnavailableError):
        gateway.screen(subject_type="person", name=EXACT_NAME)
    assert gateway.circuit_open is True
    with pytest.raises(SanctionsUnavailableError):
        gateway.screen(subject_type="person", name=EXACT_NAME)


# ------------------------------------------------------- sin PII / secretos ni persistencia
def test_secret_and_subject_never_leak_in_logs(caplog):
    gateway = MockSanctionsLists()
    with caplog.at_level(logging.INFO, logger="app.adapters.sanctions_lists"):
        gateway.screen(
            subject_type="person",
            name=EXACT_NAME,
            document_number=EXACT_DOC,
            country="PE",
        )
        gateway.list_versions()
    for record in caplog.records:
        message = record.getMessage()
        assert "k-secreta" not in message
        assert EXACT_NAME not in message
        assert EXACT_DOC not in message


def test_http_never_sends_key_in_url_or_body():
    seen: list[tuple[str, str]] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.content.decode("utf-8")))
        if request.url.path == "/lists":
            return httpx.Response(200, json={"lists": []})
        return httpx.Response(200, json={"hits": []})

    gateway = _http(_ok)
    gateway.screen(subject_type="person", name=EXACT_NAME)
    gateway.list_versions()
    assert seen and all("k-secreta" not in url and "k-secreta" not in body for url, body in seen)


def test_screen_does_not_persist_or_mutate_seeds(tmp_path):
    from app.adapters import sanctions_lists as module

    seed_dir = module._SEED_DIR
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(seed_dir.glob("*.json"))
    }
    assert len(before) == 3
    gateway = MockSanctionsLists()
    for _ in range(3):
        gateway.screen(
            subject_type="person",
            name=EXACT_NAME,
            document_number=EXACT_DOC,
            country="PE",
        )
        gateway.list_versions()
    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(seed_dir.glob("*.json"))
    }
    assert before == after
    payload = json.loads((seed_dir / "ofac.json").read_text(encoding="utf-8"))
    assert payload["list_code"] == "OFAC"
