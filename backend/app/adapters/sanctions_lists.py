"""Adaptador de listas restrictivas (E6-T07, modulo `risk`, HU21).

Coteja un sujeto (persona/empresa) contra listas **simuladas**
(OFAC / World-Check / PEP) con **matching difuso determinista**, y una
implementacion `http` con timeout/reintentos/circuito. No persiste nada
(ni sujetos ni resultados: `screening_results` es `E6-T09`) y no mueve
dinero.

Patron `clearing_source.py` (`docs/02#9`): interfaz comun
(`SanctionsLists`) + implementacion real (`HttpSanctionsLists`,
`POST {base_url}/screen` y `GET {base_url}/lists`) + simulada
(`MockSanctionsLists`, determinista, sin red, con semillas JSON
versionadas de nombres ficticios) + fabrica por entorno
(`SANCTIONS_PROVIDER=mock|http`, default `mock`; valor no reconocido
-> `mock`).

Alcance del adaptador:
- `screen(*, subject_type, name, document_number=None, country=None)`:
  mejor candidato por lista (`ScreeningHit` con `list_code`, `matched`,
  `match_score` en `[0, 1]` y `matched_fields` con solo nombres de
  campos: `name` / `document_number` / `country`, sin valores crudos).
- `list_versions()`: versiones vigentes de las listas (`ListVersion`
  con `list_code`, `version`, `effective_from`).

Reglas (`docs/02#9`, `docs/16` reglas 7/9):
- Sin PII ni secretos en logs: el sujeto solo se correlaciona hasheado
  (`hash_subject`); la API key NUNCA se loguea (solo cabecera
  `Authorization`, jamas en URL/query/cuerpo). Mensajes neutros.
- Timeouts cortos, reintentos con backoff exponencial y circuit breaker.
- Nombres ficticios en las semillas (sin PII real); determinismo total:
  misma entrada + misma lista + mismo umbral -> mismo `match_score`.

Configuracion por entorno (default `mock`, sin red):
- `SANCTIONS_PROVIDER=mock|http` (no reconocido -> mock).
- `SANCTIONS_MOCK_MODE=success|down|timeout` (default `success`).
- `SANCTIONS_BASE_URL` (default `http://localhost:8000`).
- `SANCTIONS_API_KEY` (default vacio; solo cabecera `Authorization`).
- `SANCTIONS_TIMEOUT_SECONDS` (default `5.0`).
- `SANCTIONS_MAX_RETRIES` (default `2`).
- `SANCTIONS_BACKOFF_BASE_SECONDS` (default `0.1`).
- `SANCTIONS_BREAKER_FAILURES` (default `3` fallos consecutivos).
- `SANCTIONS_BREAKER_COOLDOWN_SECONDS` (default `30.0`).
- `SANCTIONS_SCREENING_THRESHOLD` (default `0.85`; espejo de la clave
  `risk.screening_threshold` de `config.parameters` que usara el caso
  de uso `E6-T09`). Fuera de `(0, 1]` -> default.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.modules.risk.domain.screening import (
    DEFAULT_SCREENING_THRESHOLD,
    is_match,
    name_similarity,
    normalize_text,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- Codigos de negocio
SANCTIONS_UNAVAILABLE = "SANCTIONS_UNAVAILABLE"
SANCTIONS_TIMEOUT = "SANCTIONS_TIMEOUT"
SANCTIONS_VALIDATION_ERROR = "VALIDATION_ERROR"

#: Estado HTTP que el caso de uso debe traducir por codigo (el adaptador
#: nunca construye respuestas HTTP: solo expone `code`).
HTTP_STATUS_FOR_CODE = {
    SANCTIONS_UNAVAILABLE: 503,
    SANCTIONS_TIMEOUT: 502,
    SANCTIONS_VALIDATION_ERROR: 422,
}

#: Rutas del contrato con el proveedor de listas.
SCREEN_PATH = "/screen"
LISTS_PATH = "/lists"

#: Tipos de sujeto aceptados (`subject_type`, insensible a mayusculas).
SUBJECT_TYPES = ("person", "company")

#: Campos declarables en `matched_fields` (nunca valores crudos).
MATCHED_FIELDS = ("name", "document_number", "country")

#: Directorio de las semillas JSON versionadas (nombres ficticios).
_SEED_DIR = Path(__file__).resolve().parent / "data" / "sanctions"


class SanctionsListsError(Exception):
    """Error base del adaptador; expone `code` para propagacion tipada."""

    code: str = SANCTIONS_UNAVAILABLE

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SanctionsUnavailableError(SanctionsListsError):
    """Proveedor caido o respuesta invalida tras agotar reintentos (`503`)."""

    def __init__(self, message: str = "listas restrictivas no disponibles") -> None:
        super().__init__(SANCTIONS_UNAVAILABLE, message)


class SanctionsTimeoutError(SanctionsListsError):
    """Timeout contra el proveedor tras agotar reintentos (`502`)."""

    def __init__(self, message: str = "timeout contra las listas restrictivas") -> None:
        super().__init__(SANCTIONS_TIMEOUT, message)


class SanctionsValidationError(SanctionsListsError):
    """Argumento o payload invalido -> `VALIDATION_ERROR` (sin red)."""

    def __init__(self, message: str = "sujeto o payload de cotejo invalido") -> None:
        super().__init__(SANCTIONS_VALIDATION_ERROR, message)


# ---------------------------------------------------------------- Utilidades puras
def hash_subject(value: object) -> str:
    """Hash corto del sujeto normalizado para correlacion en logs (sin PII)."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def normalize_subject_type(value: object) -> str:
    """Normaliza `subject_type` a `person`|`company` (otro -> error tipado)."""
    text = str(value or "").strip().lower()
    if text not in SUBJECT_TYPES:
        raise SanctionsValidationError("tipo de sujeto invalido")
    return text


def normalize_name(value: object) -> str:
    """Exige un nombre no vacio y lo devuelve recortado (sin eco en errores)."""
    text = str(value or "").strip() if isinstance(value, str) else ""
    if not text:
        raise SanctionsValidationError("nombre del sujeto invalido")
    if len(text) > 200:
        raise SanctionsValidationError("nombre del sujeto invalido")
    return text


def normalize_document(value: object | None) -> str | None:
    """Normaliza `document_number` (opcional; `None`/vacio -> `None`)."""
    if value is None:
        return None
    text = normalize_text(value)
    if not text:
        return None
    return text.replace(" ", "")


def normalize_country(value: object | None) -> str | None:
    """Normaliza `country` a ISO-3166 de 2 letras (opcional; `None` si ausente)."""
    if value is None:
        return None
    text = str(value).strip().upper() if isinstance(value, str) else ""
    if not text:
        return None
    if len(text) != 2 or not text.isalpha():
        raise SanctionsValidationError("pais del sujeto invalido")
    return text


def clamp_threshold(value: object) -> float:
    """Umbral en `(0, 1]` o el default (nunca lanza)."""
    try:
        candidate = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_SCREENING_THRESHOLD
    if isinstance(value, bool) or not (0.0 < candidate <= 1.0):
        return DEFAULT_SCREENING_THRESHOLD
    return candidate


# ---------------------------------------------------------------- Payloads tipados
@dataclass(frozen=True)
class ScreeningHit:
    """Mejor candidato de una lista (`docs/03b#11.4`, sin persistir aqui).

    `matched=true` si `match_score >= threshold`; `matched_fields`
    contiene solo nombres de campos (`name`/`document_number`/`country`),
    nunca valores crudos del sujeto ni de la entrada.
    """

    list_code: str
    matched: bool
    match_score: float
    matched_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ListVersion:
    """Version vigente de una lista restrictiva (actualizable en `E6-T08`)."""

    list_code: str
    version: str
    effective_from: str


@dataclass(frozen=True)
class SeedEntry:
    """Entrada ficticia de una lista (nombres inventados, sin PII real)."""

    name: str
    document_number: str | None = None
    country: str | None = None


@dataclass(frozen=True)
class SeedList:
    """Lista semilla versionada cargada desde `data/sanctions/*.json`."""

    list_code: str
    version: str
    effective_from: str
    entries: tuple[SeedEntry, ...] = ()


def parse_hit_payload(data: Any) -> ScreeningHit:
    """Normaliza un hit del proveedor a `ScreeningHit` (tolerante).

    Acepta `{list_code, matched?, match_score, matched_fields?}` (y
    `score`/`fields` como alias menores). Cualquier campo invalido ->
    `SanctionsValidationError` neutro (sin eco).
    """
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        raise SanctionsValidationError("hit de cotejo invalido")
    list_code = str(data.get("list_code") or "").strip().upper()
    if not list_code or len(list_code) > 32:
        raise SanctionsValidationError("hit de cotejo invalido")
    raw_score = data.get("match_score", data.get("score"))
    if isinstance(raw_score, bool):
        raise SanctionsValidationError("hit de cotejo invalido")
    try:
        score = float(raw_score)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise SanctionsValidationError("hit de cotejo invalido") from None
    if not (0.0 <= score <= 1.0):
        raise SanctionsValidationError("hit de cotejo invalido")
    raw_fields = data.get("matched_fields", data.get("fields", []))
    if not isinstance(raw_fields, (list, tuple)):
        raise SanctionsValidationError("hit de cotejo invalido")
    fields = tuple(str(item).strip().lower() for item in raw_fields if isinstance(item, str))
    if any(item not in MATCHED_FIELDS for item in fields):
        raise SanctionsValidationError("hit de cotejo invalido")
    matched = data.get("matched")
    # Sin `matched` del proveedor se asume `False` (fail-closed: el
    # llamante `http` lo recalibra con su umbral de todos modos).
    matched = bool(matched) if isinstance(matched, bool) else False
    return ScreeningHit(
        list_code=list_code,
        matched=matched,
        match_score=score,
        matched_fields=fields,
    )


def parse_list_version_payload(data: Any) -> ListVersion:
    """Normaliza una version de lista a `ListVersion` (tolerante)."""
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        raise SanctionsValidationError("version de lista invalida")
    list_code = str(data.get("list_code") or "").strip().upper()
    version = str(data.get("version") or "").strip()
    effective_from = str(data.get("effective_from") or "").strip()
    if not list_code or len(list_code) > 32 or not version or not effective_from:
        raise SanctionsValidationError("version de lista invalida")
    return ListVersion(list_code=list_code, version=version, effective_from=effective_from)


def load_seed_lists(directory: Path | None = None) -> list[SeedList]:
    """Carga las semillas JSON versionadas (puro; sin red ni persistencia)."""
    seed_dir = directory or _SEED_DIR
    if not seed_dir.is_dir():
        raise SanctionsUnavailableError("semillas de listas no disponibles")
    seed_lists: list[SeedList] = []
    for path in sorted(seed_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SanctionsUnavailableError("semillas de listas no disponibles") from exc
        if not isinstance(raw, dict):
            raise SanctionsUnavailableError("semillas de listas no disponibles")
        list_code = str(raw.get("list_code") or "").strip().upper()
        version = str(raw.get("version") or "").strip()
        effective_from = str(raw.get("effective_from") or "").strip()
        entries_raw = raw.get("entries")
        if not list_code or not version or not effective_from:
            raise SanctionsUnavailableError("semillas de listas no disponibles")
        if not isinstance(entries_raw, list) or not entries_raw:
            raise SanctionsUnavailableError("semillas de listas no disponibles")
        entries: list[SeedEntry] = []
        for item in entries_raw:
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                raise SanctionsUnavailableError("semillas de listas no disponibles")
            entries.append(
                SeedEntry(
                    name=str(item["name"]).strip(),
                    document_number=(
                        str(item["document_number"]).strip()
                        if item.get("document_number")
                        else None
                    ),
                    country=(str(item["country"]).strip().upper() if item.get("country") else None),
                )
            )
        seed_lists.append(
            SeedList(
                list_code=list_code,
                version=version,
                effective_from=effective_from,
                entries=tuple(entries),
            )
        )
    if not seed_lists:
        raise SanctionsUnavailableError("semillas de listas no disponibles")
    return seed_lists


def evaluate_entry(
    *,
    name: str,
    document_number: str | None,
    country: str | None,
    entry: SeedEntry,
    threshold: float,
) -> tuple[float, tuple[str, ...]]:
    """Puntua un sujeto contra una entrada semilla (puro y determinista).

    Devuelve `(match_score, matched_fields)`: `match_score` es el maximo
    entre la similitud difusa del nombre (`name_similarity`, tolerante al
    orden y a typos) y `1.0` ante documento normalizado identico;
    `matched_fields` lista los campos que aportaron (`name` solo si la
    similitud supera el umbral; `document_number`/`country` ante
    igualdad exacta normalizada). Sin valores crudos en la salida.
    """
    name_score = name_similarity(name, entry.name)
    fields: list[str] = []
    if is_match(name_score, threshold):
        fields.append("name")
    entry_doc = normalize_document(entry.document_number)
    if document_number and entry_doc and document_number == entry_doc:
        fields.append("document_number")
    if fields and country and entry.country and country == entry.country.upper():
        # El pais solo refuerza un cotejo por nombre/documento: no crea
        # un hit por si solo (evita falsos positivos por pais comun).
        fields.append("country")
    score = name_score
    if "document_number" in fields:
        score = 1.0
    return (min(1.0, max(0.0, score)), tuple(fields))


# ---------------------------------------------------------------- Interfaz
class SanctionsLists(ABC):
    """Interfaz comun de cotejo contra listas restrictivas (E6-T07)."""

    threshold: float = DEFAULT_SCREENING_THRESHOLD

    @abstractmethod
    def screen(
        self,
        *,
        subject_type: str,
        name: str,
        document_number: str | None = None,
        country: str | None = None,
    ) -> list[ScreeningHit]:
        """Mejor candidato por lista (siempre una entrada por lista).

        `matched=true` si `match_score >= threshold`; sin coincidencias
        el hit sale con `matched=false` (falso positivo controlado).
        """

    @abstractmethod
    def list_versions(self) -> list[ListVersion]:
        """Versiones vigentes de las listas (sin red en el mock)."""


# ---------------------------------------------------------------- Config
@dataclass(frozen=True)
class SanctionsListsSettings:
    provider: str = "mock"
    mock_mode: str = "success"
    base_url: str = "http://localhost:8000"
    api_key: str = ""
    timeout_seconds: float = 5.0
    max_retries: int = 2
    backoff_base_seconds: float = 0.1
    breaker_failures: int = 3
    breaker_cooldown_seconds: float = 30.0
    screening_threshold: float = DEFAULT_SCREENING_THRESHOLD

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> SanctionsListsSettings:
        """Construye desde variables de entorno (sin secretos en el repo)."""
        env = environ if environ is not None else os.environ

        def _float(key: str, default: float) -> float:
            try:
                return float(str(env.get(key, default)))
            except (TypeError, ValueError):
                return default

        def _int(key: str, default: int) -> int:
            try:
                return int(float(str(env.get(key, default))))
            except (TypeError, ValueError):
                return default

        return cls(
            provider=str(env.get("SANCTIONS_PROVIDER", "mock")),
            mock_mode=str(env.get("SANCTIONS_MOCK_MODE", "success")),
            base_url=str(env.get("SANCTIONS_BASE_URL", "http://localhost:8000")),
            api_key=str(env.get("SANCTIONS_API_KEY", "")),
            timeout_seconds=_float("SANCTIONS_TIMEOUT_SECONDS", 5.0),
            max_retries=_int("SANCTIONS_MAX_RETRIES", 2),
            backoff_base_seconds=_float("SANCTIONS_BACKOFF_BASE_SECONDS", 0.1),
            breaker_failures=_int("SANCTIONS_BREAKER_FAILURES", 3),
            breaker_cooldown_seconds=_float("SANCTIONS_BREAKER_COOLDOWN_SECONDS", 30.0),
            screening_threshold=clamp_threshold(
                env.get(
                    "SANCTIONS_SCREENING_THRESHOLD",
                    DEFAULT_SCREENING_THRESHOLD,
                )
            ),
        )


# ---------------------------------------------------------------- Implementacion real
class HttpSanctionsLists(SanctionsLists):
    """Implementacion real sobre `httpx`.

    - `screen`: `POST {base_url}/screen` con `{subject_type, name,
      document_number?, country?}` -> `{hits: [...]}` (o lista directa);
      el `matched` se recalcula en local contra el umbral propio
      (determinista aunque el proveedor calibre distinto).
    - `list_versions`: `GET {base_url}/lists` -> `{lists: [...]}` (o
      lista directa).
    La key viaja solo en cabecera `Authorization`; jamas en URL, cuerpo
    ni logs. Cualquier estado no-2xx o fallo de red es un error tipado
    neutro (sin filtrar el cuerpo del proveedor ni la API key).
    """

    def __init__(
        self,
        settings: SanctionsListsSettings | None = None,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
        **overrides: Any,
    ) -> None:
        base = settings or SanctionsListsSettings.from_env()
        if overrides:
            data = {f: getattr(base, f) for f in base.__dataclass_fields__}
            data.update({k: v for k, v in overrides.items() if k in data})
            data["screening_threshold"] = clamp_threshold(
                data.get("screening_threshold", DEFAULT_SCREENING_THRESHOLD)
            )
            base = SanctionsListsSettings(**data)
        self.settings = base
        self.threshold = base.screening_threshold
        self._client = client
        self._owns_client = client is None
        self._sleeper: Callable[[float], None] = sleeper or time.sleep
        self._consecutive_failures = 0
        self._circuit_opened_at: float | None = None

    # -- circuito ------------------------------------------------------
    @property
    def circuit_open(self) -> bool:
        if self._circuit_opened_at is None:
            return False
        elapsed = time.monotonic() - self._circuit_opened_at
        if elapsed >= self.settings.breaker_cooldown_seconds:
            self._circuit_opened_at = None
            self._consecutive_failures = 0
            return False
        return True

    def _check_circuit(self) -> None:
        if self.circuit_open:
            logger.warning("sanctions_lists circuit_open")
            raise SanctionsUnavailableError("circuito de listas restrictivas abierto tras caidas")

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_opened_at = None

    def _record_failure(self, code: str) -> None:
        self._consecutive_failures += 1
        logger.warning(
            "sanctions_lists failure code=%s consecutive=%d",
            code,
            self._consecutive_failures,
        )
        if self._consecutive_failures >= self.settings.breaker_failures:
            self._circuit_opened_at = time.monotonic()
            logger.error("sanctions_lists circuit opened")

    # -- http -----------------------------------------------------------
    def _client_or_default(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(
            base_url=self.settings.base_url.rstrip("/"),
            timeout=self.settings.timeout_seconds,
        )

    def _auth_headers(self) -> dict[str, str]:
        # La key viaja solo en cabecera; jamas en la URL ni en los logs.
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        return headers

    def _request(self, method: str, path: str, *, subject_hash: str, body: Any = None) -> Any:
        self._check_circuit()
        client = self._client_or_default()
        owns = self._owns_client and self._client is None
        last_error: Exception | None = None
        try:
            for attempt in range(self.settings.max_retries + 1):
                try:
                    if method == "POST":
                        response = client.post(
                            path,
                            json=body,
                            headers=self._auth_headers(),
                            timeout=self.settings.timeout_seconds,
                        )
                    else:
                        response = client.get(
                            path,
                            headers=self._auth_headers(),
                            timeout=self.settings.timeout_seconds,
                        )
                except (httpx.TimeoutException, TimeoutError) as exc:
                    last_error = exc
                    logger.warning(
                        "sanctions_lists timeout subject_hash=%s attempt=%d",
                        subject_hash,
                        attempt,
                    )
                    self._sleep(attempt)
                    continue
                except httpx.HTTPError as exc:  # red / conexion: reintentable
                    last_error = exc
                    logger.warning(
                        "sanctions_lists transport_error subject_hash=%s " "attempt=%d err=%s",
                        subject_hash,
                        attempt,
                        type(exc).__name__,
                    )
                    self._sleep(attempt)
                    continue
                if 200 <= response.status_code < 300:
                    self._record_success()
                    try:
                        return response.json()
                    except ValueError:
                        return {}
                last_error = SanctionsUnavailableError(
                    f"proveedor respondio {response.status_code}"
                )
                logger.warning(
                    "sanctions_lists server_error subject_hash=%s status=%d " "attempt=%d",
                    subject_hash,
                    response.status_code,
                    attempt,
                )
                self._sleep(attempt)
        finally:
            if owns:
                client.close()
        if isinstance(last_error, (httpx.TimeoutException, TimeoutError)):
            self._record_failure(SANCTIONS_TIMEOUT)
            raise SanctionsTimeoutError("timeout contra las listas restrictivas") from last_error
        self._record_failure(SANCTIONS_UNAVAILABLE)
        raise SanctionsUnavailableError("listas restrictivas no disponibles") from last_error

    def _sleep(self, attempt: int) -> None:
        delay = self.settings.backoff_base_seconds * (2**attempt)
        if delay > 0:
            self._sleeper(delay)

    # -- interfaz -------------------------------------------------------
    def screen(
        self,
        *,
        subject_type: str,
        name: str,
        document_number: str | None = None,
        country: str | None = None,
    ) -> list[ScreeningHit]:
        kind = normalize_subject_type(subject_type)  # `VALIDATION_ERROR` sin red.
        clean_name = normalize_name(name)
        clean_doc = normalize_document(document_number)
        clean_country = normalize_country(country)
        payload: dict[str, Any] = {"subject_type": kind, "name": clean_name}
        if clean_doc is not None:
            payload["document_number"] = clean_doc
        if clean_country is not None:
            payload["country"] = clean_country
        subject_hash = hash_subject(normalize_text(clean_name))
        body = self._request("POST", SCREEN_PATH, subject_hash=subject_hash, body=payload)
        if isinstance(body, dict) and isinstance(body.get("hits"), list):
            entries = body["hits"]
        elif isinstance(body, list):
            entries = body
        else:
            raise SanctionsUnavailableError("respuesta invalida de listas")
        try:
            parsed = [parse_hit_payload(entry) for entry in entries]
        except SanctionsValidationError as exc:
            raise SanctionsUnavailableError("respuesta invalida de listas") from exc
        # El `matched` se recalcula con el umbral propio (determinista).
        recalibrated: list[ScreeningHit] = []
        for hit in parsed:
            matched = is_match(hit.match_score, self.threshold)
            recalibrated.append(
                ScreeningHit(
                    list_code=hit.list_code,
                    matched=matched,
                    match_score=hit.match_score,
                    matched_fields=hit.matched_fields if matched else (),
                )
            )
        logger.info("sanctions_lists screen_ok subject_hash=%s hits=%d", subject_hash, len(parsed))
        return recalibrated

    def list_versions(self) -> list[ListVersion]:
        body = self._request("GET", LISTS_PATH, subject_hash="lists")
        if isinstance(body, dict) and isinstance(body.get("lists"), list):
            entries = body["lists"]
        elif isinstance(body, list):
            entries = body
        else:
            raise SanctionsUnavailableError("respuesta invalida de listas")
        try:
            return [parse_list_version_payload(entry) for entry in entries]
        except SanctionsValidationError as exc:
            raise SanctionsUnavailableError("respuesta invalida de listas") from exc


# ---------------------------------------------------------------- Mock determinista
class MockSanctionsLists(SanctionsLists):
    """Mock determinista sin red: `success` | `down` | `timeout`.

    Misma entrada (`subject_type`, `name`, `document_number`, `country`)
    + misma semilla + mismo umbral -> mismos hits (`match_score`
    reproducibles: exacto `1.0`, difuso por Jaro-Winkler con tolerancia
    al orden, no coincidencia `matched=false`). `down`/`timeout` simulan
    la caida con errores tipados (sin red). Las semillas viven en
    `data/sanctions/*.json` (nombres ficticios, sin PII real).
    """

    MODES = ("success", "down", "timeout")

    def __init__(
        self,
        mode: str = "success",
        seed_lists: list[SeedList] | None = None,
        threshold: float = DEFAULT_SCREENING_THRESHOLD,
    ) -> None:
        if mode not in self.MODES:
            raise ValueError(f"mode debe ser uno de {self.MODES}")
        self.mode = mode
        self.seed_lists = list(seed_lists) if seed_lists is not None else load_seed_lists()
        self.threshold = clamp_threshold(threshold)

    def screen(
        self,
        *,
        subject_type: str,
        name: str,
        document_number: str | None = None,
        country: str | None = None,
    ) -> list[ScreeningHit]:
        if self.mode == "timeout":
            logger.warning("sanctions_lists mock_timeout")
            raise SanctionsTimeoutError("timeout simulado de listas")
        if self.mode == "down":
            logger.warning("sanctions_lists mock_down")
            raise SanctionsUnavailableError("caida simulada de listas")
        kind = normalize_subject_type(subject_type)  # `VALIDATION_ERROR` sin red.
        clean_name = normalize_name(name)
        clean_doc = normalize_document(document_number)
        clean_country = normalize_country(country)
        del kind  # El mock no discrimina por tipo (documentado): el filtro
        # por `subject_type` llegara con el caso de uso (`E6-T09`).
        subject_hash = hash_subject(normalize_text(clean_name))
        hits: list[ScreeningHit] = []
        for seed in sorted(self.seed_lists, key=lambda item: item.list_code):
            best_score = 0.0
            best_fields: tuple[str, ...] = ()
            for entry in seed.entries:
                score, fields = evaluate_entry(
                    name=clean_name,
                    document_number=clean_doc,
                    country=clean_country,
                    entry=entry,
                    threshold=self.threshold,
                )
                if score > best_score:
                    best_score = score
                    best_fields = fields
            matched = is_match(best_score, self.threshold)
            hits.append(
                ScreeningHit(
                    list_code=seed.list_code,
                    matched=matched,
                    match_score=round(best_score, 4),
                    matched_fields=best_fields if matched else (),
                )
            )
        logger.info(
            "sanctions_lists mock_ok subject_hash=%s hits=%d matched=%d",
            subject_hash,
            len(hits),
            sum(1 for hit in hits if hit.matched),
        )
        return hits

    def list_versions(self) -> list[ListVersion]:
        if self.mode == "timeout":
            raise SanctionsTimeoutError("timeout simulado de listas")
        if self.mode == "down":
            raise SanctionsUnavailableError("caida simulada de listas")
        return [
            ListVersion(
                list_code=seed.list_code,
                version=seed.version,
                effective_from=seed.effective_from,
            )
            for seed in sorted(self.seed_lists, key=lambda item: item.list_code)
        ]


# ---------------------------------------------------------------- Fabrica por entorno
def create_sanctions_lists(kind: str | None = None, **overrides: Any) -> SanctionsLists:
    """Fabrica: `SANCTIONS_PROVIDER=mock|http` (default `mock`).

    Fuente: variables de entorno (`SanctionsListsSettings.from_env`);
    `kind` permite forzar la seleccion en pruebas. `http`/`real`/
    `remote` devuelven `HttpSanctionsLists`; cualquier otro valor
    devuelve `MockSanctionsLists` (`SANCTIONS_MOCK_MODE` o `success`).
    """
    settings = SanctionsListsSettings.from_env()
    selected = (kind if kind is not None else settings.provider).strip().lower()
    if selected in ("http", "real", "remote"):
        return HttpSanctionsLists(settings=settings, **overrides)
    return MockSanctionsLists(
        mode=settings.mock_mode or "success",
        threshold=settings.screening_threshold,
    )


__all__ = [
    "HTTP_STATUS_FOR_CODE",
    "LISTS_PATH",
    "MATCHED_FIELDS",
    "SANCTIONS_TIMEOUT",
    "SANCTIONS_UNAVAILABLE",
    "SANCTIONS_VALIDATION_ERROR",
    "SCREEN_PATH",
    "SUBJECT_TYPES",
    "HttpSanctionsLists",
    "ListVersion",
    "MockSanctionsLists",
    "SanctionsLists",
    "SanctionsListsError",
    "SanctionsListsSettings",
    "SanctionsTimeoutError",
    "SanctionsUnavailableError",
    "SanctionsValidationError",
    "ScreeningHit",
    "SeedEntry",
    "SeedList",
    "clamp_threshold",
    "create_sanctions_lists",
    "evaluate_entry",
    "hash_subject",
    "load_seed_lists",
    "normalize_country",
    "normalize_document",
    "normalize_name",
    "normalize_subject_type",
    "parse_hit_payload",
    "parse_list_version_payload",
]
