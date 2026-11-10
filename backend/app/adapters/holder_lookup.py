"""Adaptador HolderLookup (E2-T08, modulo transactions).

Valida el **titular y el CCI** de una cuenta destino para transferencias a
tercero e interbancarias (HU06), de modo que `E2-T09` no invente el titular
ni confie en datos enviados por el cliente.

Patron `document_lookup_provider.py` (docs/02#9): interfaz comun +
implementacion real (`HttpHolderLookupProvider`, simulador interbancario
`D01` con contrato propio) + simulada (`MockHolderLookupProvider`,
determinista) + fabrica por entorno
(`HOLDER_LOOKUP_PROVIDER=mock|http`, default `mock`).

Reglas (docs/02#8/#10, docs/05#6.1, docs/15#3, docs/16 reglas 7/9/10):
- Sin PII en logs: el CCI solo se correlaciona hasheado (`hash_cci`); el
  titular (canonico o consultado), el CCI en claro y la respuesta cruda
  NUNCA se loguean. Los mensajes de error son neutros y sin eco del dato.
- Anti-enumeracion: "titular no coincide" y "CCI inexistente" devuelven el
  MISMO error neutro (`HolderNotFoundError`); el llamante no distingue si
  el CCI existe.
- Timeouts cortos, reintentos con backoff exponencial y circuit breaker;
  el proveedor nunca bloquea el hilo indefinidamente.
- Sin persistencia ORM: el cache opcional es en memoria y guarda solo el
  resultado booleano + titular enmascarado, con clave = hash del CCI.

Configuracion por entorno via `Settings` (`backend/.env`, fuente unica):
- `HOLDER_LOOKUP_PROVIDER=mock|http` (default `mock`; no reconocido -> mock).
- `HOLDER_LOOKUP_MOCK_MODE=success|not_found|down|timeout` (default `success`).
- `HOLDER_LOOKUP_BASE_URL` (default `http://localhost:8000`; simulador D01).
- `HOLDER_LOOKUP_API_KEY` (default vacio; solo cabecera `Authorization`).
- `HOLDER_LOOKUP_TIMEOUT_SECONDS` (default `5.0`).
- `HOLDER_LOOKUP_MAX_RETRIES` (default `2`).
- `HOLDER_LOOKUP_BACKOFF_BASE_SECONDS` (default `0.1`).
- `HOLDER_LOOKUP_BREAKER_FAILURES` (default `3` fallos consecutivos).
- `HOLDER_LOOKUP_BREAKER_COOLDOWN_SECONDS` (default `30.0`).
- `HOLDER_LOOKUP_CACHE_TTL_SECONDS` (default `300.0`; `0` desactiva).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import time
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _silence_httpx_url_logs():
    """Evita que httpx registre la URL en sus logs INFO.

    El path (`/holders/{cci}`) contiene el CCI en claro y httpx lo
    incluiria en su linea `HTTP Request` (regla de oro 7: sin PII en
    logs). Solo eleva el nivel durante la llamada y lo restaura despues.
    """
    httpx_logger = logging.getLogger("httpx")
    previous = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        httpx_logger.setLevel(previous)


# ---------------------------------------------------------------- Codigos de negocio
HOLDER_NOT_FOUND = "HOLDER_NOT_FOUND"
HOLDER_LOOKUP_UNAVAILABLE = "HOLDER_LOOKUP_UNAVAILABLE"
HOLDER_LOOKUP_TIMEOUT = "HOLDER_LOOKUP_TIMEOUT"
HOLDER_VALIDATION_ERROR = "VALIDATION_ERROR"
HOLDER_RATE_LIMITED = "RATE_LIMITED"

#: Longitud exacta del CCI (docs/05: 20 digitos numericos).
CCI_LENGTH = 20
_CCI_RE = re.compile(r"^[0-9]{20}$")
_BANK_CODE_RE = re.compile(r"^[A-Z0-9]{2,6}$")


class HolderLookupError(Exception):
    """Error base del adaptador; expone `code` para propagacion tipada."""

    code: str = HOLDER_LOOKUP_UNAVAILABLE

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HolderNotFoundError(HolderLookupError):
    """CCI inexistente o titular no coincidente (neutro, sin eco del dato)."""

    def __init__(self, message: str = "sin datos para la cuenta indicada") -> None:
        super().__init__(HOLDER_NOT_FOUND, message)


class HolderLookupUnavailableError(HolderLookupError):
    def __init__(self, message: str = "servicio de validacion no disponible") -> None:
        super().__init__(HOLDER_LOOKUP_UNAVAILABLE, message)


class HolderLookupTimeoutError(HolderLookupError):
    def __init__(self, message: str = "timeout contra el servicio de validacion") -> None:
        super().__init__(HOLDER_LOOKUP_TIMEOUT, message)


class HolderValidationError(HolderLookupError):
    """Formato de CCI/`bank_code` invalido -> 422 (sin llamar al proveedor)."""

    def __init__(self, message: str = "CCI con formato invalido") -> None:
        super().__init__(HOLDER_VALIDATION_ERROR, message)


class HolderRateLimitedError(HolderLookupError):
    def __init__(self, message: str = "limite de consultas excedido") -> None:
        super().__init__(HOLDER_RATE_LIMITED, message)


# ---------------------------------------------------------------- Resultado
@dataclass(frozen=True)
class HolderRecord:
    """Titular canonico del proveedor (interno; jamas se expone en claro).

    `holder_name` es el nombre/razon social tal como lo conoce el banco
    destino. La fachada solo expone su forma enmascarada.
    """

    holder_name: str
    bank_code: str = ""


# ---------------------------------------------------------------- Utilidades puras
def hash_cci(cci: str) -> str:
    """Hash corto del CCI para correlacion en logs (sin PII)."""
    return hashlib.sha256(str(cci or "").encode("utf-8")).hexdigest()[:16]


def normalize_cci(value: object) -> str:
    """Normaliza el CCI: `str`, sin espacios ni guiones intermedios."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\s\-]+", "", value.strip())


def validate_cci_format(value: object) -> str:
    """Valida `CCI = 20 digitos`; retorna los digitos o lanza `422` neutro."""
    digits = normalize_cci(value)
    if _CCI_RE.fullmatch(digits) is None:
        raise HolderValidationError("CCI con formato invalido: debe tener 20 digitos")
    return digits


def validate_bank_code(value: object) -> str:
    """Valida `bank_code` opcional (2-6 alfanumericos); vacio -> `""`."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HolderValidationError("bank_code con formato invalido")
    cleaned = value.strip().upper()
    if cleaned == "":
        return ""
    if _BANK_CODE_RE.fullmatch(cleaned) is None:
        raise HolderValidationError("bank_code con formato invalido")
    return cleaned


def normalize_name(value: object) -> str:
    """Normaliza un nombre para comparacion: sin tildes, UPPER, 1 espacio."""
    if not isinstance(value, str):
        return ""
    folded = unicodedata.normalize("NFKD", value)
    ascii_only = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(ascii_only.upper().split())


def mask_holder_name(holder_name: str) -> str:
    """Enmascara el titular: primera letra de cada palabra + `***`.

    `mask_holder_name("Juan Perez Gomez") == "J*** P*** G***"`. Nunca
    devuelve el nombre en claro; vacio -> `""`.
    """
    words = str(holder_name or "").split()
    if not words:
        return ""
    return " ".join(f"{word[0].upper()}***" for word in words if word)


def mock_holder_name_for(cci: str) -> str:
    """Nombre canonico determinista del mock para un CCI (mismo CCI -> mismo)."""
    digits = normalize_cci(cci)
    return f"TITULAR DEMO {digits[-4:]}" if digits else "TITULAR DEMO"


# ---------------------------------------------------------------- Interfaz
class HolderLookupProvider(ABC):
    """Interfaz comun del adaptador de validacion de titular/CCI."""

    @abstractmethod
    def lookup_holder(self, *, cci: str, bank_code: str = "") -> HolderRecord:
        """Retorna el titular canonico del CCI (`bank_code` solo enruta)."""


# ---------------------------------------------------------------- Config
@dataclass(frozen=True)
class HolderLookupSettings:
    base_url: str = "http://localhost:8000"
    api_key: str = ""
    timeout_seconds: float = 5.0
    max_retries: int = 2
    backoff_base_seconds: float = 0.1
    breaker_failures: int = 3
    breaker_cooldown_seconds: float = 30.0
    cache_ttl_seconds: float = 300.0

    @classmethod
    def from_settings(cls, settings) -> HolderLookupSettings:
        """Construye desde `Settings` (fuente unica: `backend/.env`)."""
        return cls(
            base_url=settings.holder_lookup_base_url,
            api_key=settings.holder_lookup_api_key,
            timeout_seconds=float(settings.holder_lookup_timeout_seconds),
            max_retries=int(settings.holder_lookup_max_retries),
            backoff_base_seconds=float(settings.holder_lookup_backoff_base_seconds),
            breaker_failures=int(settings.holder_lookup_breaker_failures),
            breaker_cooldown_seconds=float(settings.holder_lookup_breaker_cooldown_seconds),
            cache_ttl_seconds=float(settings.holder_lookup_cache_ttl_seconds),
        )

    @classmethod
    def from_env(cls) -> HolderLookupSettings:
        """Compatibilidad: deriva de `Settings` (no lee `os.environ` crudo)."""
        from app.core.config import Settings

        return cls.from_settings(Settings())


# ---------------------------------------------------------------- Implementacion real (simulador D01)
class HttpHolderLookupProvider(HolderLookupProvider):
    """Implementacion real sobre `httpx` (simulador interbancario D01).

    `GET {base_url}/holders/{cci}[?bank_code=]` -> `200 {"holder_name": ...}`,
    `404` sin datos, `429` limite. Cualquier otro estado o fallo de red es
    `HOLDER_LOOKUP_UNAVAILABLE` neutro (sin filtrar el cuerpo del proveedor).
    """

    HOLDER_PATH = "/holders/{cci}"

    def __init__(
        self,
        settings: HolderLookupSettings | None = None,
        client: httpx.Client | None = None,
        **overrides,
    ) -> None:
        base = settings or HolderLookupSettings.from_env()
        if overrides:
            data = {f: getattr(base, f) for f in base.__dataclass_fields__}
            data.update({k: v for k, v in overrides.items() if k in data})
            base = HolderLookupSettings(**data)
        self.settings = base
        self._client = client
        self._owns_client = client is None
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
            logger.warning("holder_lookup circuit_open")
            raise HolderLookupUnavailableError("circuito de validacion abierto tras caidas")

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_opened_at = None

    def _record_failure(self, code: str) -> None:
        self._consecutive_failures += 1
        logger.warning(
            "holder_lookup failure code=%s consecutive=%d",
            code,
            self._consecutive_failures,
        )
        if self._consecutive_failures >= self.settings.breaker_failures:
            self._circuit_opened_at = time.monotonic()
            logger.error("holder_lookup circuit opened")

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
        return {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, *, params: dict, cci_hash: str) -> dict:
        self._check_circuit()
        client = self._client_or_default()
        owns = self._owns_client and self._client is None
        last_error: Exception | None = None
        try:
            with _silence_httpx_url_logs():
                for attempt in range(self.settings.max_retries + 1):
                    try:
                        response = client.get(
                            path,
                            params=params,
                            headers=self._auth_headers(),
                            timeout=self.settings.timeout_seconds,
                        )
                    except (httpx.TimeoutException, TimeoutError) as exc:
                        last_error = exc
                        logger.warning(
                            "holder_lookup timeout cci_hash=%s attempt=%d",
                            cci_hash,
                            attempt,
                        )
                        self._sleep(attempt)
                        continue
                    except httpx.HTTPError as exc:  # red / conexion: reintentable
                        last_error = exc
                        logger.warning(
                            "holder_lookup transport_error cci_hash=%s attempt=%d err=%s",
                            cci_hash,
                            attempt,
                            type(exc).__name__,
                        )
                        self._sleep(attempt)
                        continue
                    if 200 <= response.status_code < 300:
                        self._record_success()
                        try:
                            payload = response.json()
                        except ValueError:
                            payload = {}
                        return payload if isinstance(payload, dict) else {}
                    if response.status_code == 404:
                        logger.info("holder_lookup not_found cci_hash=%s", cci_hash)
                        raise HolderNotFoundError()
                    if response.status_code == 429:
                        logger.warning("holder_lookup rate_limited cci_hash=%s", cci_hash)
                        raise HolderRateLimitedError()
                    last_error = HolderLookupUnavailableError(
                        f"proveedor respondio {response.status_code}"
                    )
                    logger.warning(
                        "holder_lookup server_error cci_hash=%s status=%d attempt=%d",
                        cci_hash,
                        response.status_code,
                        attempt,
                    )
                    self._sleep(attempt)
        finally:
            if owns:
                client.close()
        if isinstance(last_error, (httpx.TimeoutException, TimeoutError)):
            self._record_failure(HOLDER_LOOKUP_TIMEOUT)
            raise HolderLookupTimeoutError(
                "timeout contra el servicio de validacion"
            ) from last_error
        self._record_failure(HOLDER_LOOKUP_UNAVAILABLE)
        raise HolderLookupUnavailableError("servicio de validacion no disponible") from last_error

    def _sleep(self, attempt: int) -> None:
        delay = self.settings.backoff_base_seconds * (2**attempt)
        if delay > 0:
            time.sleep(delay)

    # -- interfaz -------------------------------------------------------
    def lookup_holder(self, *, cci: str, bank_code: str = "") -> HolderRecord:
        digits = validate_cci_format(cci)
        code = validate_bank_code(bank_code)
        cci_hash = hash_cci(digits)
        params = {"bank_code": code} if code else {}
        data = self._get(self.HOLDER_PATH.format(cci=digits), params=params, cci_hash=cci_hash)
        nested = data.get("data")
        if isinstance(nested, dict):
            data = nested
        holder_name = str(data.get("holder_name") or data.get("holderName") or "").strip()
        if not holder_name:
            raise HolderNotFoundError()
        # Solo hash/tipo en logs: nunca titular, CCI ni respuesta cruda.
        logger.info("holder_lookup ok cci_hash=%s has_bank=%s", cci_hash, bool(code))
        return HolderRecord(holder_name=holder_name, bank_code=code)


# ---------------------------------------------------------------- Mock determinista
class MockHolderLookupProvider(HolderLookupProvider):
    """Mock determinista: `success` | `not_found` | `down` | `timeout`.

    `success` responde sin red con `mock_holder_name_for(cci)` (mismo CCI ->
    mismo titular), salvo los CCI del mapa `known` (fixtures de demo).
    `not_found` lanza `HolderNotFoundError`; `down` lanza
    `HolderLookupUnavailableError`; `timeout` lanza
    `HolderLookupTimeoutError`.
    """

    MODES = ("success", "not_found", "down", "timeout")

    def __init__(
        self,
        mode: str = "success",
        known: dict[str, str] | None = None,
    ) -> None:
        if mode not in self.MODES:
            raise ValueError(f"mode debe ser uno de {self.MODES}")
        self.mode = mode
        self.known: dict[str, str] = dict(known or {})

    def lookup_holder(self, *, cci: str, bank_code: str = "") -> HolderRecord:
        digits = validate_cci_format(cci)
        code = validate_bank_code(bank_code)
        cci_hash = hash_cci(digits)
        if self.mode == "timeout":
            logger.warning("holder_lookup mock_timeout cci_hash=%s", cci_hash)
            raise HolderLookupTimeoutError("timeout simulado de la validacion")
        if self.mode == "down":
            logger.warning("holder_lookup mock_down cci_hash=%s", cci_hash)
            raise HolderLookupUnavailableError("caida simulada de la validacion")
        if self.mode == "not_found":
            logger.info("holder_lookup mock_not_found cci_hash=%s", cci_hash)
            raise HolderNotFoundError()
        holder_name = self.known.get(digits) or mock_holder_name_for(digits)
        logger.info(
            "holder_lookup mock_ok cci_hash=%s has_bank=%s mode=%s",
            cci_hash,
            bool(code),
            self.mode,
        )
        return HolderRecord(holder_name=holder_name, bank_code=code)


# ---------------------------------------------------------------- Fabrica por entorno
def resolve_cache_ttl_seconds(default: float = 300.0) -> float:
    """TTL del cache de validaciones desde `Settings` (regla de oro 6).

    Fuente unica `backend/.env` (`HOLDER_LOOKUP_CACHE_TTL_SECONDS`); ante
    cualquier fallo de lectura rige `default`. Son segundos de cache, no
    dinero (el dinero siempre es entero en centimos).
    """
    try:
        from app.core.config import get_settings

        return float(get_settings().holder_lookup_cache_ttl_seconds)
    except Exception:  # noqa: BLE001 - sin config rige el default
        return default


def create_holder_lookup_provider(kind: str | None = None, **overrides) -> HolderLookupProvider:
    """Fabrica: `HOLDER_LOOKUP_PROVIDER=mock|http` (default `mock`).

    Fuente unica: `Settings` (`backend/.env`); `http`/`real`/`remote`
    devuelven `HttpHolderLookupProvider`; cualquier otro valor devuelve
    `MockHolderLookupProvider` (`holder_lookup_mock_mode` o `success`).
    """
    from app.core.config import Settings

    settings = Settings()
    selected = (kind if kind is not None else settings.holder_lookup_provider).strip().lower()
    if selected in ("http", "real", "remote"):
        return HttpHolderLookupProvider(
            settings=HolderLookupSettings.from_settings(settings), **overrides
        )
    return MockHolderLookupProvider(mode=settings.holder_lookup_mock_mode or "success")


__all__ = [
    "CCI_LENGTH",
    "HOLDER_LOOKUP_TIMEOUT",
    "HOLDER_LOOKUP_UNAVAILABLE",
    "HOLDER_NOT_FOUND",
    "HOLDER_RATE_LIMITED",
    "HOLDER_VALIDATION_ERROR",
    "HolderLookupError",
    "HolderLookupProvider",
    "HolderLookupSettings",
    "HolderLookupTimeoutError",
    "HolderLookupUnavailableError",
    "HolderNotFoundError",
    "HolderRateLimitedError",
    "HolderRecord",
    "HolderValidationError",
    "HttpHolderLookupProvider",
    "MockHolderLookupProvider",
    "create_holder_lookup_provider",
    "hash_cci",
    "mask_holder_name",
    "mock_holder_name_for",
    "normalize_cci",
    "normalize_name",
    "resolve_cache_ttl_seconds",
    "validate_bank_code",
    "validate_cci_format",
]
