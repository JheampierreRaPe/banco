"""Adaptador de tipos de cambio (E7-T08, modulo `fx`, HU25).

Entrega **tasas de cambio vigentes** (`rate`, `source`, `ttl`) con una
implementacion `mock` determinista (tasas semilla, sin red) y una `http`
con timeout/reintentos/circuito, mas cache en proceso por par con TTL
y fallback a tasa semilla (D04) si el proveedor real falla.

Patron `clearing_source.py` / `sanctions_lists.py` (`docs/02#9`): interfaz
comun (`FxRateProvider`) + implementacion real (`HttpFxRateProvider`,
`GET {base_url}/rates?base=PEN`) + simulada (`MockFxRateProvider`,
determinista, sin red) + decoradores (`CachedFxRateProvider`,
`SeedFallbackFxRateProvider`) + fabrica por entorno
(`FX_RATE_PROVIDER=mock|http`, default `mock`; valor no reconocido
-> `mock`).

Reglas (`docs/02#9`, `docs/16` reglas 7/9):
- `rate` es `Decimal` exacto, nunca `float` (el wire debe traerlo como
  `string`; un `float` se rechaza con `VALIDATION_ERROR`).
- `base != quote`; ambas ISO-4217 (3 letras); tasa `> 0`.
- Cache por `(base, quote)` con TTL; expirado -> refresco; nunca se
  cachean errores ni se sirve una tasa vencida como vigente.
- Sin PII/secretos en logs: la key viaja solo en cabecera
  `Authorization`, jamas en URL/query/cuerpo. Solo se loguean los
  codigos de moneda (`base`/`quote`), nunca montos ni llaves.
- No persiste nada ni mueve dinero (la persistencia de `fx_rates` y las
  cotizaciones son `E7-T09`; el spread `E7-T10`; la ejecucion `E7-T11`).
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- Codigos de negocio
FX_RATE_UNAVAILABLE = "FX_RATE_UNAVAILABLE"
FX_RATE_TIMEOUT = "FX_RATE_TIMEOUT"
FX_RATE_VALIDATION_ERROR = "VALIDATION_ERROR"

#: Estado HTTP que el caso de uso debe traducir por codigo (el adaptador
#: nunca construye respuestas HTTP: solo expone `code`).
HTTP_STATUS_FOR_CODE = {
    FX_RATE_UNAVAILABLE: 503,
    FX_RATE_TIMEOUT: 502,
    FX_RATE_VALIDATION_ERROR: 422,
}

#: Ruta del contrato con el proveedor de tipos de cambio (`docs/02#9`).
RATES_PATH = "/rates"

#: Tasas semilla directas (D04): si no hay proveedor real se usan estas.
#: Espejo operativo en `data/fx_rates.json` (el test de coherencia las
#: compara; la canonica es esta para no depender de lectura de disco).
SEED_RATES: dict[tuple[str, str], Decimal] = {
    ("PEN", "USD"): Decimal("3.80"),
    ("PEN", "EUR"): Decimal("4.10"),
    ("USD", "EUR"): Decimal("0.92"),
}

#: Precision de las inversas coherentes (`1/rate` a 8 decimales).
INVERSE_QUANTUM = Decimal("0.00000001")

#: Fuentes declaradas en `FxRatePayload.source`.
SOURCE_MOCK = "mock"
SOURCE_HTTP = "http"
SOURCE_SEED = "seed"


class FxRateError(Exception):
    """Error base del adaptador; expone `code` para propagacion tipada."""

    code: str = FX_RATE_UNAVAILABLE

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FxRateUnavailableError(FxRateError):
    """Proveedor caido o respuesta invalida tras agotar reintentos (`503`)."""

    def __init__(self, message: str = "tipo de cambio no disponible") -> None:
        super().__init__(FX_RATE_UNAVAILABLE, message)


class FxRateTimeoutError(FxRateError):
    """Timeout contra el proveedor tras agotar reintentos (`502`)."""

    def __init__(self, message: str = "timeout contra el proveedor de tipo de cambio") -> None:
        super().__init__(FX_RATE_TIMEOUT, message)


class FxRateValidationError(FxRateError):
    """Par no soportado o payload invalido -> `VALIDATION_ERROR` (sin red)."""

    def __init__(self, message: str = "par de monedas o tasa invalida") -> None:
        super().__init__(FX_RATE_VALIDATION_ERROR, message)


# ---------------------------------------------------------------- Utilidades puras
def normalize_currency(value: object) -> str:
    """Normaliza un codigo de moneda a ISO-4217 (3 letras; otro -> error tipado)."""
    text = str(value).strip().upper() if isinstance(value, str) else ""
    if len(text) != 3 or not text.isalpha():
        raise FxRateValidationError("moneda invalida (se espera ISO-4217)")
    return text


def normalize_pair(base: object, quote: object) -> tuple[str, str]:
    """Normaliza `(base, quote)`; `base == quote` -> error tipado (sin red)."""
    clean_base = normalize_currency(base)
    clean_quote = normalize_currency(quote)
    if clean_base == clean_quote:
        raise FxRateValidationError("base y quote deben diferir")
    return (clean_base, clean_quote)


def coerce_rate(value: object) -> Decimal:
    """Convierte `value` a `Decimal` exacto `> 0` (nunca `float`).

    Acepta `Decimal`/`int`/`str` numerico; `float`/`bool`/no numerico/
    `<= 0`/no finito -> `FxRateValidationError`. El wire debe traer la
    tasa como `string` exacto (`docs/02#9`).
    """
    if isinstance(value, bool):
        raise FxRateValidationError("tasa invalida")
    if isinstance(value, Decimal):
        rate = value
    elif isinstance(value, int):
        rate = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise FxRateValidationError("tasa invalida")
        try:
            rate = Decimal(text)
        except InvalidOperation:
            raise FxRateValidationError("tasa invalida") from None
    else:
        # `float` incluido: el binario no es exacto para dinero; el
        # proveedor debe serializar la tasa como string.
        raise FxRateValidationError("tasa invalida")
    if not rate.is_finite() or rate <= 0:
        raise FxRateValidationError("tasa invalida")
    return rate


def inverse_rate(rate: Decimal) -> Decimal:
    """Inversa coherente (`1/rate` redondeada a 8 decimales)."""
    return (Decimal(1) / rate).quantize(INVERSE_QUANTUM, rounding=ROUND_HALF_UP)


def supported_quotes(base: str) -> list[str]:
    """Quotes soportados para `base` (directos + inversos), ordenados."""
    quotes = {quote for (seed_base, quote) in SEED_RATES if seed_base == base} | {
        seed_base for (seed_base, quote) in SEED_RATES if quote == base
    }
    return sorted(quotes)


def seed_rate(base: str, quote: str) -> Decimal:
    """Tasa semilla determinista para el par (directa o inversa coherente).

    Par no soportado -> `FxRateValidationError` (sin red).
    """
    pair = normalize_pair(base, quote)
    direct = SEED_RATES.get(pair)
    if direct is not None:
        return direct
    flipped = SEED_RATES.get((pair[1], pair[0]))
    if flipped is not None:
        return inverse_rate(flipped)
    raise FxRateValidationError(f"par no soportado: {pair[0]}/{pair[1]}")


# ---------------------------------------------------------------- Payload tipado
@dataclass(frozen=True)
class FxRatePayload:
    """Tasa vigente de un par (`rate` exacto, nunca `float`).

    `source`: `mock` (tasas semilla del mock) | `http` (proveedor real)
    | `seed` (fallback D04 ante caida del proveedor real).
    `ttl_seconds`: vigencia en segundos de esta tasa para la cache.
    """

    base: str
    quote: str
    rate: Decimal
    source: str
    ttl_seconds: int


def parse_rate_payload(
    data: Any,
    *,
    base: str,
    source: str = SOURCE_HTTP,
    default_ttl: int = 60,
) -> FxRatePayload:
    """Normaliza una tasa del proveedor a `FxRatePayload` (tolerante).

    Acepta `{quote, rate, ttl_seconds?}` (y `ttl` como alias; `data` como
    envoltorio). `rate` como `float` se rechaza (`VALIDATION_ERROR`:
    el wire debe traer string exacto). Cualquier campo invalido ->
    `FxRateValidationError` neutro (sin eco de secretos).
    """
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        raise FxRateValidationError("tasa del proveedor invalida")
    clean_base = normalize_currency(base)
    try:
        clean_quote = normalize_currency(data.get("quote"))
    except FxRateValidationError as exc:
        raise FxRateValidationError("tasa del proveedor invalida") from exc
    if clean_base == clean_quote:
        raise FxRateValidationError("tasa del proveedor invalida")
    try:
        rate = coerce_rate(data.get("rate"))
    except FxRateValidationError as exc:
        raise FxRateValidationError("tasa del proveedor invalida") from exc
    raw_ttl = data.get("ttl_seconds", data.get("ttl", default_ttl))
    try:
        ttl = int(raw_ttl)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise FxRateValidationError("tasa del proveedor invalida") from None
    if isinstance(raw_ttl, bool) or ttl <= 0:
        raise FxRateValidationError("tasa del proveedor invalida")
    return FxRatePayload(
        base=clean_base,
        quote=clean_quote,
        rate=rate,
        source=source,
        ttl_seconds=ttl,
    )


def parse_rates_body(
    body: Any,
    *,
    base: str,
    source: str = SOURCE_HTTP,
    default_ttl: int = 60,
) -> list[FxRatePayload]:
    """Normaliza el cuerpo de `GET /rates?base=` a lista de tasas.

    Acepta `{rates: [...]}` (o `data`/`items` como envoltorio/lista
    directa). Cuerpo invalido o vacio -> `FxRateValidationError`.
    """
    if isinstance(body, dict):
        for key in ("rates", "data", "items"):
            candidate = body.get(key)
            if isinstance(candidate, list):
                entries = candidate
                break
            if isinstance(candidate, dict) and isinstance(candidate.get("rates"), list):
                entries = candidate["rates"]
                break
        else:
            raise FxRateValidationError("respuesta de tasas invalida")
    elif isinstance(body, list):
        entries = body
    else:
        raise FxRateValidationError("respuesta de tasas invalida")
    if not entries:
        raise FxRateValidationError("respuesta de tasas invalida")
    try:
        parsed = [
            parse_rate_payload(entry, base=base, source=source, default_ttl=default_ttl)
            for entry in entries
        ]
    except FxRateValidationError as exc:
        raise FxRateValidationError("respuesta de tasas invalida") from exc
    return sorted(parsed, key=lambda item: item.quote)


# ---------------------------------------------------------------- Interfaz
class FxRateProvider(ABC):
    """Interfaz comun del proveedor de tipos de cambio (E7-T08)."""

    @abstractmethod
    def get_rate(self, *, base: str, quote: str) -> FxRatePayload:
        """Tasa vigente del par (cacheada si aplica; errores no cacheados)."""

    @abstractmethod
    def get_rates(self, *, base: str) -> list[FxRatePayload]:
        """Tasas vigentes de `base` contra todos sus quotes (ordenadas)."""


# ---------------------------------------------------------------- Config
@dataclass(frozen=True)
class FxRateSettings:
    provider: str = "mock"
    mock_mode: str = "success"
    base_url: str = "http://localhost:8000"
    api_key: str = ""
    timeout_seconds: float = 5.0
    max_retries: int = 2
    backoff_base_seconds: float = 0.1
    breaker_failures: int = 3
    breaker_cooldown_seconds: float = 30.0
    cache_ttl_seconds: int = 60
    fallback_seed: bool = True

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> FxRateSettings:
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

        raw_fallback = str(env.get("FX_RATE_FALLBACK_SEED", "1")).strip().lower()

        return cls(
            provider=str(env.get("FX_RATE_PROVIDER", "mock")),
            mock_mode=str(env.get("FX_RATE_MOCK_MODE", "success")),
            base_url=str(env.get("FX_RATE_BASE_URL", "http://localhost:8000")),
            api_key=str(env.get("FX_RATE_API_KEY", "")),
            timeout_seconds=_float("FX_RATE_TIMEOUT_SECONDS", 5.0),
            max_retries=_int("FX_RATE_MAX_RETRIES", 2),
            backoff_base_seconds=_float("FX_RATE_BACKOFF_BASE_SECONDS", 0.1),
            breaker_failures=_int("FX_RATE_BREAKER_FAILURES", 3),
            breaker_cooldown_seconds=_float("FX_RATE_BREAKER_COOLDOWN_SECONDS", 30.0),
            cache_ttl_seconds=_int("FX_RATE_CACHE_TTL_SECONDS", 60),
            fallback_seed=raw_fallback not in ("0", "false", "no", "off", ""),
        )


# ---------------------------------------------------------------- Implementacion real
class HttpFxRateProvider(FxRateProvider):
    """Implementacion real sobre `httpx`.

    - `get_rates`: `GET {base_url}/rates?base=PEN` -> `{rates:
      [{quote, rate, ttl_seconds?}]}` (o lista directa); la tasa viaja
      como string exacto (un `float` se rechaza con error tipado).
    - `get_rate`: una sola llamada via `get_rates` y filtro por `quote`
      (quote del proveedor ausente -> `VALIDATION_ERROR`).
    La key viaja solo en cabecera `Authorization`; jamas en URL, cuerpo
    ni logs. Cualquier estado no-2xx o fallo de red es un error tipado
    neutro (sin filtrar el cuerpo del proveedor ni la API key).
    """

    def __init__(
        self,
        settings: FxRateSettings | None = None,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
        **overrides: Any,
    ) -> None:
        base = settings or FxRateSettings.from_env()
        if overrides:
            data = {field: getattr(base, field) for field in base.__dataclass_fields__}
            data.update({k: v for k, v in overrides.items() if k in data})
            base = FxRateSettings(**data)
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
            logger.warning("fx_rate circuit_open")
            raise FxRateUnavailableError("circuito del proveedor de tipo de cambio abierto")

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_opened_at = None

    def _record_failure(self, code: str) -> None:
        self._consecutive_failures += 1
        logger.warning(
            "fx_rate failure code=%s consecutive=%d",
            code,
            self._consecutive_failures,
        )
        if self._consecutive_failures >= self.settings.breaker_failures:
            self._circuit_opened_at = time.monotonic()
            logger.error("fx_rate circuit opened")

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

    def _fetch_rates(self, base: str) -> list[FxRatePayload]:
        self._check_circuit()
        client = self._client_or_default()
        owns = self._owns_client and self._client is None
        last_error: Exception | None = None
        try:
            for attempt in range(self.settings.max_retries + 1):
                try:
                    response = client.get(
                        RATES_PATH,
                        params={"base": base},
                        headers=self._auth_headers(),
                        timeout=self.settings.timeout_seconds,
                    )
                except (httpx.TimeoutException, TimeoutError) as exc:
                    last_error = exc
                    logger.warning(
                        "fx_rate timeout base=%s attempt=%d",
                        base,
                        attempt,
                    )
                    self._sleep(attempt)
                    continue
                except httpx.HTTPError as exc:  # red / conexion: reintentable
                    last_error = exc
                    logger.warning(
                        "fx_rate transport_error base=%s attempt=%d err=%s",
                        base,
                        attempt,
                        type(exc).__name__,
                    )
                    self._sleep(attempt)
                    continue
                if 200 <= response.status_code < 300:
                    try:
                        body = response.json()
                    except ValueError as exc:
                        last_error = exc
                        logger.warning(
                            "fx_rate bad_json base=%s status=%d attempt=%d",
                            base,
                            response.status_code,
                            attempt,
                        )
                        self._sleep(attempt)
                        continue
                    try:
                        parsed = parse_rates_body(
                            body,
                            base=base,
                            source=SOURCE_HTTP,
                            default_ttl=self.settings.cache_ttl_seconds,
                        )
                    except FxRateValidationError as exc:
                        last_error = exc
                        logger.warning(
                            "fx_rate bad_body base=%s status=%d attempt=%d",
                            base,
                            response.status_code,
                            attempt,
                        )
                        self._sleep(attempt)
                        continue
                    self._record_success()
                    logger.info("fx_rate http_ok base=%s quotes=%d", base, len(parsed))
                    return parsed
                last_error = FxRateUnavailableError(f"proveedor respondio {response.status_code}")
                logger.warning(
                    "fx_rate server_error base=%s status=%d attempt=%d",
                    base,
                    response.status_code,
                    attempt,
                )
                self._sleep(attempt)
        finally:
            if owns:
                client.close()
        if isinstance(last_error, (httpx.TimeoutException, TimeoutError)):
            self._record_failure(FX_RATE_TIMEOUT)
            raise FxRateTimeoutError(
                "timeout contra el proveedor de tipo de cambio"
            ) from last_error
        self._record_failure(FX_RATE_UNAVAILABLE)
        raise FxRateUnavailableError("tipo de cambio no disponible") from last_error

    def _sleep(self, attempt: int) -> None:
        delay = self.settings.backoff_base_seconds * (2**attempt)
        if delay > 0:
            self._sleeper(delay)

    # -- interfaz -------------------------------------------------------
    def get_rates(self, *, base: str) -> list[FxRatePayload]:
        clean_base = normalize_currency(base)  # `VALIDATION_ERROR` sin red.
        return self._fetch_rates(clean_base)

    def get_rate(self, *, base: str, quote: str) -> FxRatePayload:
        pair = normalize_pair(base, quote)  # `VALIDATION_ERROR` sin red.
        for payload in self._fetch_rates(pair[0]):
            if payload.quote == pair[1]:
                return payload
        raise FxRateValidationError(f"par no soportado: {pair[0]}/{pair[1]}")


# ---------------------------------------------------------------- Mock determinista
class MockFxRateProvider(FxRateProvider):
    """Mock determinista sin red: `success` | `down` | `timeout`.

    Misma entrada (`base`, `quote`) -> misma tasa: directas de
    `SEED_RATES` (`PEN/USD=3.80`, `PEN/EUR=4.10`, `USD/EUR=0.92`) e
    inversas coherentes (`1/rate` a 8 decimales). `source="mock"` y
    `ttl_seconds` configurable (default `60`). `down`/`timeout` simulan
    la caida con errores tipados (sin red).
    """

    MODES = ("success", "down", "timeout")

    def __init__(self, mode: str = "success", ttl_seconds: int = 60) -> None:
        if mode not in self.MODES:
            raise ValueError(f"mode debe ser uno de {self.MODES}")
        if isinstance(ttl_seconds, bool) or int(ttl_seconds) <= 0:
            raise ValueError("ttl_seconds debe ser un entero > 0")
        self.mode = mode
        self.ttl_seconds = int(ttl_seconds)

    def get_rate(self, *, base: str, quote: str) -> FxRatePayload:
        if self.mode == "timeout":
            logger.warning("fx_rate mock_timeout")
            raise FxRateTimeoutError("timeout simulado de tipo de cambio")
        if self.mode == "down":
            logger.warning("fx_rate mock_down")
            raise FxRateUnavailableError("caida simulada de tipo de cambio")
        pair = normalize_pair(base, quote)  # `VALIDATION_ERROR` sin red.
        try:
            rate = seed_rate(pair[0], pair[1])
        except FxRateValidationError:
            logger.warning("fx_rate mock_unsupported base=%s quote=%s", pair[0], pair[1])
            raise
        logger.info("fx_rate mock_ok base=%s quote=%s", pair[0], pair[1])
        return FxRatePayload(
            base=pair[0],
            quote=pair[1],
            rate=rate,
            source=SOURCE_MOCK,
            ttl_seconds=self.ttl_seconds,
        )

    def get_rates(self, *, base: str) -> list[FxRatePayload]:
        if self.mode == "timeout":
            raise FxRateTimeoutError("timeout simulado de tipo de cambio")
        if self.mode == "down":
            raise FxRateUnavailableError("caida simulada de tipo de cambio")
        clean_base = normalize_currency(base)  # `VALIDATION_ERROR` sin red.
        quotes = supported_quotes(clean_base)
        if not quotes:
            raise FxRateValidationError(f"base no soportada: {clean_base}")
        return [self.get_rate(base=clean_base, quote=quote) for quote in quotes]


# ---------------------------------------------------------------- Cache en proceso
class FxRateCache:
    """Cache en proceso por `(base, quote)` con TTL (sin red ni persistencia).

    Solo guarda exitos (`set` con `payload.ttl_seconds`); los errores
    nunca se cachean. `get` devuelve `None` ante miss o entrada vencida
    (nunca se sirve una tasa vencida como vigente). Reloj monotonico
    inyectable para pruebas deterministas.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._entries: dict[tuple[str, str], tuple[FxRatePayload, float]] = {}

    def get(self, base: str, quote: str) -> FxRatePayload | None:
        entry = self._entries.get((base, quote))
        if entry is None:
            return None
        payload, expires_at = entry
        if self._clock() >= expires_at:
            del self._entries[(base, quote)]
            return None
        return payload

    def set(self, payload: FxRatePayload) -> None:
        self._entries[(payload.base, payload.quote)] = (
            payload,
            self._clock() + max(int(payload.ttl_seconds), 1),
        )

    def invalidate(self, base: str, quote: str) -> None:
        self._entries.pop((base, quote), None)

    def clear(self) -> None:
        self._entries.clear()


class CachedFxRateProvider(FxRateProvider):
    """Decorador con cache por par: hit vigente, refresco al expirar.

    `get_rates` resuelve lo vigente desde cache y llama al interior una
    sola vez solo por los quotes faltantes/vencidos. Los errores del
    interior se propagan sin cachearse.
    """

    def __init__(
        self,
        inner: FxRateProvider,
        cache: FxRateCache | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.inner = inner
        self.cache = cache or FxRateCache(clock=clock)

    def get_rate(self, *, base: str, quote: str) -> FxRatePayload:
        pair = normalize_pair(base, quote)  # `VALIDATION_ERROR` sin red.
        cached = self.cache.get(pair[0], pair[1])
        if cached is not None:
            logger.info("fx_rate cache_hit base=%s quote=%s", pair[0], pair[1])
            return cached
        payload = self.inner.get_rate(base=pair[0], quote=pair[1])
        self.cache.set(payload)
        return payload

    def get_rates(self, *, base: str) -> list[FxRatePayload]:
        clean_base = normalize_currency(base)  # `VALIDATION_ERROR` sin red.
        quotes = supported_quotes(clean_base)
        if not quotes:
            # Delega para que el interior decida el error tipado.
            return self.inner.get_rates(base=clean_base)
        fresh: dict[str, FxRatePayload] = {}
        missing: list[str] = []
        for quote in quotes:
            cached = self.cache.get(clean_base, quote)
            if cached is not None:
                fresh[quote] = cached
            else:
                missing.append(quote)
        if missing:
            for payload in self.inner.get_rates(base=clean_base):
                if payload.base == clean_base and payload.quote in missing:
                    self.cache.set(payload)
                    fresh[payload.quote] = payload
            still_missing = [quote for quote in missing if quote not in fresh]
            if still_missing:
                raise FxRateValidationError(f"par no soportado: {clean_base}/{still_missing[0]}")
        return [fresh[quote] for quote in sorted(fresh)]


# ---------------------------------------------------------------- Fallback semilla (D04)
class SeedFallbackFxRateProvider(FxRateProvider):
    """Fallback auditable a tasa semilla si el proveedor real falla (D04).

    Ante `FxRateUnavailableError`/`FxRateTimeoutError` del interior
    devuelve la semilla determinista con `source="seed"` y log `warning`
    (`fx_rate fallback ...`, solo codigos de moneda: sin secretos ni
    PII). Los `VALIDATION_ERROR` se propagan (el fallback no inventa
    pares no soportados).
    """

    def __init__(self, inner: FxRateProvider, ttl_seconds: int = 60) -> None:
        self.inner = inner
        self.ttl_seconds = int(ttl_seconds)

    def get_rate(self, *, base: str, quote: str) -> FxRatePayload:
        pair = normalize_pair(base, quote)  # `VALIDATION_ERROR` sin red.
        try:
            return self.inner.get_rate(base=pair[0], quote=pair[1])
        except (FxRateUnavailableError, FxRateTimeoutError) as exc:
            rate = seed_rate(pair[0], pair[1])  # no soportado -> se propaga el fallo real.
            logger.warning(
                "fx_rate fallback source=seed base=%s quote=%s reason=%s",
                pair[0],
                pair[1],
                exc.code,
            )
            return FxRatePayload(
                base=pair[0],
                quote=pair[1],
                rate=rate,
                source=SOURCE_SEED,
                ttl_seconds=self.ttl_seconds,
            )

    def get_rates(self, *, base: str) -> list[FxRatePayload]:
        clean_base = normalize_currency(base)  # `VALIDATION_ERROR` sin red.
        try:
            return self.inner.get_rates(base=clean_base)
        except (FxRateUnavailableError, FxRateTimeoutError) as exc:
            quotes = supported_quotes(clean_base)
            if not quotes:
                raise
            logger.warning(
                "fx_rate fallback source=seed base=%s quotes=%d reason=%s",
                clean_base,
                len(quotes),
                exc.code,
            )
            return [
                FxRatePayload(
                    base=clean_base,
                    quote=quote,
                    rate=seed_rate(clean_base, quote),
                    source=SOURCE_SEED,
                    ttl_seconds=self.ttl_seconds,
                )
                for quote in quotes
            ]


# ---------------------------------------------------------------- Fabrica por entorno
def create_fx_rate_provider(kind: str | None = None, **overrides: Any) -> FxRateProvider:
    """Fabrica: `FX_RATE_PROVIDER=mock|http` (default `mock`).

    Fuente: variables de entorno (`FxRateSettings.from_env`); `kind`
    permite forzar la seleccion en pruebas. `http`/`real`/`remote`
    devuelven `HttpFxRateProvider`; cualquier otro valor devuelve
    `MockFxRateProvider` (`FX_RATE_MOCK_MODE` o `success`). Ambos se
    envuelven en cache por par con TTL; el `http` ademas lleva fallback
    a semilla D04 (desactivable con `FX_RATE_FALLBACK_SEED=0`).
    """
    settings = FxRateSettings.from_env()
    selected = (kind if kind is not None else settings.provider).strip().lower()
    cache = FxRateCache()
    if selected in ("http", "real", "remote"):
        inner: FxRateProvider = HttpFxRateProvider(settings=settings, **overrides)
        if settings.fallback_seed:
            inner = SeedFallbackFxRateProvider(inner, ttl_seconds=settings.cache_ttl_seconds)
        return CachedFxRateProvider(inner, cache=cache)
    mock_mode = settings.mock_mode if settings.mock_mode in MockFxRateProvider.MODES else "success"
    return CachedFxRateProvider(
        MockFxRateProvider(mode=mock_mode, ttl_seconds=settings.cache_ttl_seconds),
        cache=cache,
    )


__all__ = [
    "FX_RATE_TIMEOUT",
    "FX_RATE_UNAVAILABLE",
    "FX_RATE_VALIDATION_ERROR",
    "HTTP_STATUS_FOR_CODE",
    "INVERSE_QUANTUM",
    "RATES_PATH",
    "SEED_RATES",
    "SOURCE_HTTP",
    "SOURCE_MOCK",
    "SOURCE_SEED",
    "CachedFxRateProvider",
    "FxRateCache",
    "FxRateError",
    "FxRatePayload",
    "FxRateProvider",
    "FxRateSettings",
    "FxRateTimeoutError",
    "FxRateUnavailableError",
    "FxRateValidationError",
    "HttpFxRateProvider",
    "MockFxRateProvider",
    "SeedFallbackFxRateProvider",
    "coerce_rate",
    "create_fx_rate_provider",
    "inverse_rate",
    "normalize_currency",
    "normalize_pair",
    "parse_rate_payload",
    "parse_rates_body",
    "seed_rate",
    "supported_quotes",
]
