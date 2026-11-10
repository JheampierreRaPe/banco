"""Adaptador de la fuente de clearing (E5-T16, modulo reconciliation, HU19).

Entrega **archivos de compensacion simulados** (lote de operaciones de la
red/equipo par) con una interfaz comun y una implementacion `mock`
determinista, sin persistir nada y sin mover dinero. Es el cimiento de la
ingesta de HU19 (la ingesta/matching es `E5-T17`..`E5-T21`).

Patron `service_provider.py` / `wallet_interop.py` (`docs/02#9`):
interfaz comun (`ClearingSource`) + implementacion real
(`HttpClearingSource`, `GET {base_url}/clearing/files` y
`GET {base_url}/clearing/files/{ref}/items`) + simulada
(`MockClearingSource`, determinista, sin red) + fabrica por entorno
(`CLEARING_SOURCE_PROVIDER=mock|http`, default `mock`; valor no
reconocido -> `mock`).

Alcance del adaptador (sin persistencia: es un cliente, no escribe
`clearing_files`/`clearing_items`; esas tablas las puebla `E5-T17`):
- `fetch_files(*, since=None, source=None)`: lotes disponibles.
- `fetch_items(file_ref)`: items del lote (`external_ref`, `amount_minor`,
  `currency`, `value_date`, `direction`, `raw`).
- `fetch_batch(file_ref, ...)` (conveniencia): archivo + items con
  verificacion de `item_count` coherente.

Reglas (`docs/02#9`, `docs/16` reglas 7/9/10):
- Sin PII ni secretos en logs: las referencias solo se correlacionan
  hasheadas (`hash_file_ref`/`hash_external_ref`); la API key NUNCA se
  loguea (solo cabecera `Authorization`, jamas en URL/query). Mensajes
  neutros sin eco del dato.
- Timeouts cortos, reintentos con backoff exponencial y circuit breaker;
  la red nunca bloquea el hilo indefinidamente.
- Dinero entero en centimos (`amount_minor: int`, nunca `float`); el
  adaptador no mueve dinero.

Configuracion por entorno (default `mock`, sin red):
- `CLEARING_SOURCE_PROVIDER=mock|http` (no reconocido -> mock).
- `CLEARING_SOURCE_MOCK_MODE=success|down|timeout` (default `success`).
- `CLEARING_SOURCE_BASE_URL` (default `http://localhost:8000`).
- `CLEARING_SOURCE_API_KEY` (default vacio; solo cabecera `Authorization`).
- `CLEARING_SOURCE_TIMEOUT_SECONDS` (default `5.0`).
- `CLEARING_SOURCE_MAX_RETRIES` (default `2`).
- `CLEARING_SOURCE_BACKOFF_BASE_SECONDS` (default `0.1`).
- `CLEARING_SOURCE_BREAKER_FAILURES` (default `3` fallos consecutivos).
- `CLEARING_SOURCE_BREAKER_COOLDOWN_SECONDS` (default `30.0`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- Codigos de negocio
CLEARING_SOURCE_UNAVAILABLE = "CLEARING_SOURCE_UNAVAILABLE"
CLEARING_SOURCE_TIMEOUT = "CLEARING_SOURCE_TIMEOUT"
CLEARING_SOURCE_VALIDATION_ERROR = "VALIDATION_ERROR"

#: Estado HTTP que el caso de uso debe traducir por codigo (el adaptador
#: nunca construye respuestas HTTP: solo expone `code`).
HTTP_STATUS_FOR_CODE = {
    CLEARING_SOURCE_UNAVAILABLE: 503,
    CLEARING_SOURCE_TIMEOUT: 502,
    CLEARING_SOURCE_VALIDATION_ERROR: 422,
}

#: Rutas del contrato con la red/equipo par (D01: el contrato definitivo
#: puede llegar tarde; el mock es la fuente del contrato hasta entonces).
FILES_PATH = "/clearing/files"
FILE_ITEMS_PATH_TEMPLATE = "/clearing/files/{ref}/items"

#: Direcciones validas de un item de compensacion (`docs/03b#12.1`).
DIRECTIONS = ("INBOUND", "OUTBOUND")

#: Monedas aceptadas en el MVP (ISO-4217 de 3 letras; se valida formato).
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_FILE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,63}$")
_EXTERNAL_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,127}$")
_MAX_SOURCE_LEN = 64
_DEFAULT_SOURCE = "NETWORK"
_DEFAULT_ITEMS_PER_FILE = 3


class ClearingSourceError(Exception):
    """Error base del adaptador; expone `code` para propagacion tipada."""

    code: str = CLEARING_SOURCE_UNAVAILABLE

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ClearingSourceUnavailableError(ClearingSourceError):
    """Red/par caido o respuesta invalida tras agotar reintentos (`503`)."""

    def __init__(self, message: str = "fuente de clearing no disponible") -> None:
        super().__init__(CLEARING_SOURCE_UNAVAILABLE, message)


class ClearingSourceTimeoutError(ClearingSourceError):
    """Timeout contra la red/par tras agotar reintentos (`502`)."""

    def __init__(self, message: str = "timeout contra la fuente de clearing") -> None:
        super().__init__(CLEARING_SOURCE_TIMEOUT, message)


class ClearingSourceValidationError(ClearingSourceError):
    """Payload o argumento invalido -> `VALIDATION_ERROR` (sin red)."""

    def __init__(self, message: str = "payload de clearing invalido") -> None:
        super().__init__(CLEARING_SOURCE_VALIDATION_ERROR, message)


# ---------------------------------------------------------------- Utilidades puras
def hash_file_ref(value: object) -> str:
    """Hash corto de `file_ref` para correlacion en logs (sin PII)."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def hash_external_ref(value: object) -> str:
    """Hash corto de `external_ref` para correlacion en logs (sin PII)."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def normalize_file_ref(value: object) -> str:
    """Normaliza y valida `file_ref` (opaco, sin eco en errores)."""
    text = str(value or "").strip()
    if not text or len(text) > 64 or not _FILE_REF_RE.match(text):
        raise ClearingSourceValidationError("referencia de archivo invalida")
    return text


def normalize_source(value: object | None) -> str:
    """Normaliza la fuente/red (`NETWORK` por defecto)."""
    text = str(value or _DEFAULT_SOURCE).strip().upper()
    if not text or len(text) > _MAX_SOURCE_LEN or not _FILE_REF_RE.match(text):
        raise ClearingSourceValidationError("fuente de clearing invalida")
    return text


def normalize_since(value: object | None) -> str | None:
    """Normaliza `since` a fecha ISO (`YYYY-MM-DD`) o `None`."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ClearingSourceValidationError("fecha `since` invalida") from None


def normalize_value_date(value: object) -> str:
    """Normaliza `value_date` a fecha ISO (exige fecha valida)."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ClearingSourceValidationError("fecha de valor invalida") from None


# ---------------------------------------------------------------- Payloads tipados
@dataclass(frozen=True)
class ClearingItemPayload:
    """Item de compensacion (espejo de `clearing_items`, `docs/03b#12.1`).

    `amount_minor` en centimos (`int > 0`, nunca `float`); `currency`
    ISO-4217; `direction` solo `INBOUND`/`OUTBOUND`; `raw` opaco con el
    contenido original del proveedor (sin PII en logs).
    """

    external_ref: str
    amount_minor: int
    currency: str
    value_date: str
    direction: str
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_canonical(self) -> dict[str, Any]:
        """Vista canonica del item (base del `checksum`; sin `raw`)."""
        return {
            "external_ref": self.external_ref,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "value_date": self.value_date,
            "direction": self.direction,
        }


@dataclass(frozen=True)
class ClearingFilePayload:
    """Archivo/lote de compensacion (espejo de `clearing_files`).

    `checksum` SHA-256 hex del contenido canonico de sus items;
    `item_count` debe coincidir con los items entregados.
    """

    source: str
    file_ref: str
    checksum: str
    item_count: int


def canonical_checksum(items: list[ClearingItemPayload]) -> str:
    """SHA-256 hex del contenido canonico (ordenado por `external_ref`)."""
    canonical = sorted(
        (item.to_canonical() for item in items),
        key=lambda entry: entry["external_ref"],
    )
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def parse_item_payload(data: Any) -> ClearingItemPayload:
    """Normaliza un item del proveedor a `ClearingItemPayload` (tolerante).

    Acepta `{external_ref, amount_minor, currency, value_date, direction,
    raw?}` (y `amount`/`date`/`type` como alias menores). Cualquier campo
    invalido -> `ClearingSourceValidationError` neutro (sin eco).
    """
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        raise ClearingSourceValidationError("item de clearing invalido")
    external_ref = str(data.get("external_ref", "") or "").strip()
    if not external_ref or not _EXTERNAL_REF_RE.match(external_ref):
        raise ClearingSourceValidationError("item de clearing invalido")
    amount = data.get("amount_minor", data.get("amount"))
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ClearingSourceValidationError("item de clearing invalido")
    currency = str(data.get("currency") or "").strip().upper()
    if not _CURRENCY_RE.match(currency):
        raise ClearingSourceValidationError("item de clearing invalido")
    value_date = normalize_value_date(data.get("value_date", data.get("date")))
    direction = str(data.get("direction", data.get("type")) or "").strip().upper()
    if direction not in DIRECTIONS:
        raise ClearingSourceValidationError("item de clearing invalido")
    raw = data.get("raw", {})
    if not isinstance(raw, Mapping):
        raise ClearingSourceValidationError("item de clearing invalido")
    return ClearingItemPayload(
        external_ref=external_ref,
        amount_minor=amount,
        currency=currency,
        value_date=value_date,
        direction=direction,
        raw=dict(raw),
    )


def parse_file_payload(data: Any) -> ClearingFilePayload:
    """Normaliza un archivo del proveedor a `ClearingFilePayload`.

    Exige `{source?, file_ref, checksum (64 hex), item_count >= 0}`; lo
    demas -> `ClearingSourceValidationError` neutro (sin eco).
    """
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        raise ClearingSourceValidationError("archivo de clearing invalido")
    file_ref = normalize_file_ref(data.get("file_ref"))
    source = normalize_source(data.get("source"))
    checksum = str(data.get("checksum") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ClearingSourceValidationError("archivo de clearing invalido")
    item_count = data.get("item_count")
    if isinstance(item_count, bool) or not isinstance(item_count, int) or item_count < 0:
        raise ClearingSourceValidationError("archivo de clearing invalido")
    return ClearingFilePayload(
        source=source, file_ref=file_ref, checksum=checksum, item_count=item_count
    )


def check_batch_consistency(file: ClearingFilePayload, items: list[ClearingItemPayload]) -> None:
    """Verifica `item_count` y `checksum` contra los items entregados."""
    if file.item_count != len(items):
        raise ClearingSourceValidationError("conteo de items inconsistente")
    if file.checksum != canonical_checksum(items):
        raise ClearingSourceValidationError("checksum del lote inconsistente")


# ---------------------------------------------------------------- Interfaz
class ClearingSource(ABC):
    """Interfaz comun de la fuente de clearing (E5-T16)."""

    @abstractmethod
    def fetch_files(
        self, *, since: object = None, source: object = None
    ) -> list[ClearingFilePayload]:
        """Lotes disponibles (filtrables por fecha/fuente, sin mover dinero)."""

    @abstractmethod
    def fetch_items(self, file_ref: str) -> list[ClearingItemPayload]:
        """Items del lote `file_ref` (validados; `VALIDATION_ERROR` sin red)."""

    def fetch_batch(
        self, file_ref: str, *, since: object = None, source: object = None
    ) -> tuple[ClearingFilePayload, list[ClearingItemPayload]]:
        """Archivo + items con verificacion de `item_count`/`checksum`."""
        code = normalize_file_ref(file_ref)
        files = {
            candidate.file_ref: candidate
            for candidate in self.fetch_files(since=since, source=source)
        }
        if code not in files:
            raise ClearingSourceValidationError("archivo de clearing desconocido")
        items = self.fetch_items(code)
        check_batch_consistency(files[code], items)
        return files[code], items


# ---------------------------------------------------------------- Config
@dataclass(frozen=True)
class ClearingSourceSettings:
    provider: str = "mock"
    mock_mode: str = "success"
    base_url: str = "http://localhost:8000"
    api_key: str = ""
    timeout_seconds: float = 5.0
    max_retries: int = 2
    backoff_base_seconds: float = 0.1
    breaker_failures: int = 3
    breaker_cooldown_seconds: float = 30.0

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> ClearingSourceSettings:
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
            provider=str(env.get("CLEARING_SOURCE_PROVIDER", "mock")),
            mock_mode=str(env.get("CLEARING_SOURCE_MOCK_MODE", "success")),
            base_url=str(env.get("CLEARING_SOURCE_BASE_URL", "http://localhost:8000")),
            api_key=str(env.get("CLEARING_SOURCE_API_KEY", "")),
            timeout_seconds=_float("CLEARING_SOURCE_TIMEOUT_SECONDS", 5.0),
            max_retries=_int("CLEARING_SOURCE_MAX_RETRIES", 2),
            backoff_base_seconds=_float("CLEARING_SOURCE_BACKOFF_BASE_SECONDS", 0.1),
            breaker_failures=_int("CLEARING_SOURCE_BREAKER_FAILURES", 3),
            breaker_cooldown_seconds=_float("CLEARING_SOURCE_BREAKER_COOLDOWN_SECONDS", 30.0),
        )


# ---------------------------------------------------------------- Lote determinista (mock + base del http)
def build_deterministic_batch(
    *,
    seed: str,
    source: str,
    day: str,
    items_per_file: int = _DEFAULT_ITEMS_PER_FILE,
) -> tuple[ClearingFilePayload, list[ClearingItemPayload]]:
    """Construye el lote determinista (misma entrada -> mismo lote/checksum).

    Puro y sin red: `file_ref`/`external_ref`/`amount_minor` derivan del
    SHA-256 de `(seed, source, day, indice)`; `checksum` es el canonico de
    los items. Base del mock y referencia del contrato `http`.
    """
    if items_per_file <= 0:
        raise ClearingSourceValidationError("lote de clearing invalido")
    digest = hashlib.sha256(f"{seed}|{source}|{day}".encode()).hexdigest()
    file_ref = f"CLR-{day.replace('-', '')}-{digest[:8].upper()}"
    currencies = ("PEN", "PEN", "USD")
    items: list[ClearingItemPayload] = []
    for index in range(items_per_file):
        item_digest = hashlib.sha256(f"{digest}:{index}".encode()).hexdigest()
        amount = 1_000 + (int(item_digest[:8], 16) % 99_000)
        items.append(
            ClearingItemPayload(
                external_ref=f"EXT-{digest[:6].upper()}-{index + 1:04d}",
                amount_minor=amount,
                currency=currencies[index % len(currencies)],
                value_date=day,
                direction="INBOUND" if index % 2 == 0 else "OUTBOUND",
                raw={"index": index, "file_ref": file_ref},
            )
        )
    file = ClearingFilePayload(
        source=source,
        file_ref=file_ref,
        checksum=canonical_checksum(items),
        item_count=len(items),
    )
    return file, items


# ---------------------------------------------------------------- Implementacion real
class HttpClearingSource(ClearingSource):
    """Implementacion real sobre `httpx`.

    - `fetch_files`: `GET {base_url}/clearing/files?since=&source=` ->
      `{files: [...]}` (o lista directa).
    - `fetch_items`: `GET {base_url}/clearing/files/{ref}/items` ->
      `{items: [...]}` (o lista directa); valida cada item en local
      (`VALIDATION_ERROR` sin reintento ante item corrupto del proveedor).
    La key viaja solo en cabecera `Authorization`; jamas en URL ni logs.
    Cualquier estado no-2xx o fallo de red es un error tipado neutro (sin
    filtrar el cuerpo del proveedor ni la API key).
    """

    def __init__(
        self,
        settings: ClearingSourceSettings | None = None,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
        **overrides: Any,
    ) -> None:
        base = settings or ClearingSourceSettings.from_env()
        if overrides:
            data = {f: getattr(base, f) for f in base.__dataclass_fields__}
            data.update({k: v for k, v in overrides.items() if k in data})
            base = ClearingSourceSettings(**data)
        self.settings = base
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
            logger.warning("clearing_source circuit_open")
            raise ClearingSourceUnavailableError(
                "circuito de la fuente de clearing abierto tras caidas"
            )

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_opened_at = None

    def _record_failure(self, code: str) -> None:
        self._consecutive_failures += 1
        logger.warning(
            "clearing_source failure code=%s consecutive=%d",
            code,
            self._consecutive_failures,
        )
        if self._consecutive_failures >= self.settings.breaker_failures:
            self._circuit_opened_at = time.monotonic()
            logger.error("clearing_source circuit opened")

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
        headers = {"Accept": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        return headers

    def _get(self, path: str, *, file_hash: str) -> Any:
        self._check_circuit()
        client = self._client_or_default()
        owns = self._owns_client and self._client is None
        last_error: Exception | None = None
        try:
            for attempt in range(self.settings.max_retries + 1):
                try:
                    response = client.get(
                        path,
                        headers=self._auth_headers(),
                        timeout=self.settings.timeout_seconds,
                    )
                except (httpx.TimeoutException, TimeoutError) as exc:
                    last_error = exc
                    logger.warning(
                        "clearing_source timeout file_hash=%s attempt=%d",
                        file_hash,
                        attempt,
                    )
                    self._sleep(attempt)
                    continue
                except httpx.HTTPError as exc:  # red / conexion: reintentable
                    last_error = exc
                    logger.warning(
                        "clearing_source transport_error file_hash=%s attempt=%d err=%s",
                        file_hash,
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
                last_error = ClearingSourceUnavailableError(
                    f"fuente respondio {response.status_code}"
                )
                logger.warning(
                    "clearing_source server_error file_hash=%s status=%d attempt=%d",
                    file_hash,
                    response.status_code,
                    attempt,
                )
                self._sleep(attempt)
        finally:
            if owns:
                client.close()
        if isinstance(last_error, (httpx.TimeoutException, TimeoutError)):
            self._record_failure(CLEARING_SOURCE_TIMEOUT)
            raise ClearingSourceTimeoutError("timeout contra la fuente de clearing") from last_error
        self._record_failure(CLEARING_SOURCE_UNAVAILABLE)
        raise ClearingSourceUnavailableError("fuente de clearing no disponible") from last_error

    def _sleep(self, attempt: int) -> None:
        delay = self.settings.backoff_base_seconds * (2**attempt)
        if delay > 0:
            self._sleeper(delay)

    # -- interfaz -------------------------------------------------------
    def fetch_files(
        self, *, since: object = None, source: object = None
    ) -> list[ClearingFilePayload]:
        day = normalize_since(since)
        origin = normalize_source(source) if source is not None else _DEFAULT_SOURCE
        query = f"{FILES_PATH}?source={origin}"
        if day is not None:
            query += f"&since={day}"
        # Solo hashes en logs: nunca fechas/fuentes en claro ni el cuerpo crudo.
        body = self._get(query, file_hash=hash_file_ref(query))
        if isinstance(body, dict) and isinstance(body.get("files"), list):
            entries = body["files"]
        elif isinstance(body, list):
            entries = body
        else:
            raise ClearingSourceUnavailableError("respuesta invalida de clearing")
        try:
            return [parse_file_payload(entry) for entry in entries]
        except ClearingSourceValidationError as exc:
            raise ClearingSourceUnavailableError("respuesta invalida de clearing") from exc

    def fetch_items(self, file_ref: str) -> list[ClearingItemPayload]:
        code = normalize_file_ref(file_ref)  # `VALIDATION_ERROR` sin red.
        path = FILE_ITEMS_PATH_TEMPLATE.format(ref=code)
        body = self._get(path, file_hash=hash_file_ref(code))
        if isinstance(body, dict) and isinstance(body.get("items"), list):
            entries = body["items"]
        elif isinstance(body, list):
            entries = body
        else:
            raise ClearingSourceUnavailableError("respuesta invalida de clearing")
        try:
            return [parse_item_payload(entry) for entry in entries]
        except ClearingSourceValidationError as exc:
            raise ClearingSourceUnavailableError("respuesta invalida de clearing") from exc


# ---------------------------------------------------------------- Mock determinista
class MockClearingSource(ClearingSource):
    """Mock determinista sin red: `success` | `down` | `timeout`.

    Misma entrada (`since`, `source`) -> mismo lote (`file_ref`,
    `checksum`, items); el registro en memoria permite verificar
    `item_count`/`checksum` en `fetch_batch`. `down`/`timeout` simulan la
    caida con errores tipados (sin red).
    """

    MODES = ("success", "down", "timeout")

    def __init__(
        self,
        mode: str = "success",
        seed: str = "clearing-demo",
        items_per_file: int = _DEFAULT_ITEMS_PER_FILE,
        day: str = "2026-01-15",
    ) -> None:
        if mode not in self.MODES:
            raise ValueError(f"mode debe ser uno de {self.MODES}")
        if items_per_file <= 0:
            raise ValueError("items_per_file debe ser > 0")
        self.mode = mode
        self.seed = seed
        self.items_per_file = items_per_file
        self.day = normalize_value_date(day)
        self._registry: dict[str, tuple[ClearingFilePayload, list[ClearingItemPayload]]] = {}

    def _remember(
        self, file: ClearingFilePayload, items: list[ClearingItemPayload]
    ) -> ClearingFilePayload:
        self._registry[file.file_ref] = (file, list(items))
        return file

    def fetch_files(
        self, *, since: object = None, source: object = None
    ) -> list[ClearingFilePayload]:
        if self.mode == "timeout":
            logger.warning("clearing_source mock_timeout")
            raise ClearingSourceTimeoutError("timeout simulado de clearing")
        if self.mode == "down":
            logger.warning("clearing_source mock_down")
            raise ClearingSourceUnavailableError("caida simulada de clearing")
        day = normalize_since(since) or self.day
        origin = normalize_source(source) if source is not None else _DEFAULT_SOURCE
        file, items = build_deterministic_batch(
            seed=self.seed,
            source=origin,
            day=day,
            items_per_file=self.items_per_file,
        )
        logger.info(
            "clearing_source mock_ok file_hash=%s items=%d",
            hash_file_ref(file.file_ref),
            len(items),
        )
        return [self._remember(file, items)]

    def fetch_items(self, file_ref: str) -> list[ClearingItemPayload]:
        code = normalize_file_ref(file_ref)  # `VALIDATION_ERROR` sin red.
        if self.mode == "timeout":
            logger.warning("clearing_source mock_timeout file_hash=%s", hash_file_ref(code))
            raise ClearingSourceTimeoutError("timeout simulado de clearing")
        if self.mode == "down":
            logger.warning("clearing_source mock_down file_hash=%s", hash_file_ref(code))
            raise ClearingSourceUnavailableError("caida simulada de clearing")
        if code in self._registry:
            return list(self._registry[code][1])
        # Lote nunca listado por este mock: desconocido (`VALIDATION_ERROR`
        # sin red; el flujo canonico es `fetch_files` -> `fetch_items`).
        raise ClearingSourceValidationError("archivo de clearing desconocido")


# ---------------------------------------------------------------- Fabrica por entorno
def create_clearing_source(kind: str | None = None, **overrides: Any) -> ClearingSource:
    """Fabrica: `CLEARING_SOURCE_PROVIDER=mock|http` (default `mock`).

    Fuente: variables de entorno (`ClearingSourceSettings.from_env`);
    `kind` permite forzar la seleccion en pruebas. `http`/`real`/
    `remote` devuelven `HttpClearingSource`; cualquier otro valor
    devuelve `MockClearingSource` (`CLEARING_SOURCE_MOCK_MODE` o
    `success`).
    """
    settings = ClearingSourceSettings.from_env()
    selected = (kind if kind is not None else settings.provider).strip().lower()
    if selected in ("http", "real", "remote"):
        return HttpClearingSource(settings=settings, **overrides)
    return MockClearingSource(mode=settings.mock_mode or "success")


__all__ = [
    "CLEARING_SOURCE_TIMEOUT",
    "CLEARING_SOURCE_UNAVAILABLE",
    "CLEARING_SOURCE_VALIDATION_ERROR",
    "DIRECTIONS",
    "FILES_PATH",
    "FILE_ITEMS_PATH_TEMPLATE",
    "HTTP_STATUS_FOR_CODE",
    "ClearingFilePayload",
    "ClearingItemPayload",
    "ClearingSource",
    "ClearingSourceError",
    "ClearingSourceSettings",
    "ClearingSourceTimeoutError",
    "ClearingSourceUnavailableError",
    "ClearingSourceValidationError",
    "HttpClearingSource",
    "MockClearingSource",
    "build_deterministic_batch",
    "canonical_checksum",
    "check_batch_consistency",
    "create_clearing_source",
    "hash_external_ref",
    "hash_file_ref",
    "normalize_file_ref",
    "normalize_since",
    "normalize_source",
    "normalize_value_date",
    "parse_file_payload",
    "parse_item_payload",
]
