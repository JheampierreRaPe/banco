"""Adaptador del proveedor de servicios (E4-T16, modulo wallet, HU16 CA-01).

Consulta de empresas de servicios por nombre o numero de suministro y
validacion determinista del suministro, sin acoplarse a un proveedor
real (el pago es `E4-T17`; este adaptador **no mueve dinero**).

Patron `wallet_interop.py` / `credit_bureau.py` (`docs/02#8/#9`):
interfaz comun (`ServiceProviderGateway`) + implementacion real
(`HttpServiceProviderGateway`, `POST
{base_url}/services/supply/validate`) + simulada
(`MockServiceProviderGateway`, determinista, sin red) + fabrica por
entorno (`SERVICE_PROVIDER=mock|http`, default `mock`; valor no
reconocido -> `mock`).

Alcance del adaptador (sin persistencia: es un cliente, no escribe
tablas de negocio):
- `lookup_biller(query)`: empresas del catalogo demo que coinciden
  por nombre (subcadena) o por numero de suministro (prefijo del
  `supply_ref`; cada demo fija sus prefijos).
- `validate_supply(*, biller_id, supply_ref)`: validacion del
  suministro (formato en local + consulta simulada en `mock`).

Reglas (`docs/02#8/#10`, `docs/16` reglas 7/10):
- Sin PII ni secretos en logs: el suministro/telefono solo se
  correlaciona hasheado (`hash_supply_ref`); la API key NUNCA se
  loguea (solo cabecera `Authorization`). Mensajes neutros sin eco.
- Timeouts cortos, reintentos con backoff exponencial y circuit
  breaker; el proveedor nunca bloquea el hilo indefinidamente.
- Dinero entero en centimos en `amount_due_minor`; nunca `float`.

Configuracion por entorno (default `mock`, sin red):
- `SERVICE_PROVIDER=mock|http` (no reconocido -> mock).
- `SERVICE_PROVIDER_MOCK_MODE=success|down|timeout` (default `success`).
- `SERVICE_PROVIDER_BASE_URL` (default `http://localhost:8000`).
- `SERVICE_PROVIDER_API_KEY` (default vacio; solo cabecera `Authorization`).
- `SERVICE_PROVIDER_TIMEOUT_SECONDS` (default `5.0`).
- `SERVICE_PROVIDER_MAX_RETRIES` (default `2`).
- `SERVICE_PROVIDER_BACKOFF_BASE_SECONDS` (default `0.1`).
- `SERVICE_PROVIDER_BREAKER_FAILURES` (default `3` fallos consecutivos).
- `SERVICE_PROVIDER_BREAKER_COOLDOWN_SECONDS` (default `30.0`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- Codigos de negocio
SERVICE_PROVIDER_UNAVAILABLE = "SERVICE_PROVIDER_UNAVAILABLE"
SERVICE_PROVIDER_TIMEOUT = "SERVICE_PROVIDER_TIMEOUT"
SERVICE_PROVIDER_VALIDATION_ERROR = "VALIDATION_ERROR"

#: Ruta del contrato con el proveedor (`docs/05#6.5`; mock hasta el proveedor real).
VALIDATE_PATH = "/services/supply/validate"

_SUPPLY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-. ]{2,79}$")
_PHONE_RE = re.compile(r"^[0-9]{6,15}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MAX_QUERY_LEN = 80


class ServiceProviderError(Exception):
    """Error base del adaptador; expone `code` para propagacion tipada."""

    code: str = SERVICE_PROVIDER_UNAVAILABLE

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ServiceProviderUnavailableError(ServiceProviderError):
    """Proveedor caido o con error tras agotar reintentos (`503` en el caso de uso)."""

    def __init__(self, message: str = "proveedor de servicios no disponible") -> None:
        super().__init__(SERVICE_PROVIDER_UNAVAILABLE, message)


class ServiceProviderTimeoutError(ServiceProviderError):
    """Timeout contra el proveedor tras agotar reintentos (`502` en el caso de uso)."""

    def __init__(self, message: str = "timeout contra el proveedor de servicios") -> None:
        super().__init__(SERVICE_PROVIDER_TIMEOUT, message)


class ServiceProviderValidationError(ServiceProviderError):
    """Suministro/empresa con formato invalido -> 422 (sin llamar al proveedor)."""

    def __init__(self, message: str = "suministro invalido") -> None:
        super().__init__(SERVICE_PROVIDER_VALIDATION_ERROR, message)


# ---------------------------------------------------------------- Utilidades puras
def hash_supply_ref(value: object) -> str:
    """Hash corto del suministro/telefono para correlacion en logs (sin PII)."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def normalize_supply_ref(value: object) -> str:
    """Normaliza y valida el numero de suministro (opaco, sin eco en errores)."""
    text = str(value or "").strip()
    if not text or len(text) > 80 or not _SUPPLY_RE.match(text):
        raise ServiceProviderValidationError("numero de suministro invalido")
    return text


def normalize_phone(value: object) -> str:
    """Normaliza y valida el telefono de recarga (solo digitos, 6..15)."""
    text = re.sub(r"[\s\-()]+", "", str(value or "").strip())
    if not _PHONE_RE.match(text):
        raise ServiceProviderValidationError("telefono de recarga invalido")
    return text


def normalize_biller_id(value: object) -> str:
    """Normaliza el id del `biller` (UUID en texto; el detalle vive en `wallet`)."""
    text = str(value or "").strip()
    if not text or len(text) > 64:
        raise ServiceProviderValidationError("empresa de servicios invalida")
    return text


# ---------------------------------------------------------------- Catalogo demo del mock
@dataclass(frozen=True)
class DemoBiller:
    """Empresa demo del mock (espejo del seed `0042_wallet_services`)."""

    biller_id: str
    category: str
    name: str
    required_fields: tuple[str, ...]
    supply_prefixes: tuple[str, ...]


#: Espejo de la siembra `0042_wallet_services` + `wallet.repository.services.DEMO_BILLERS`.
DEMO_BILLERS: tuple[DemoBiller, ...] = (
    DemoBiller(
        "c1111111-1111-4111-8111-111111111111",
        "ELECTRICIDAD",
        "Luz del Hogar Demo",
        ("supply_ref",),
        ("10", "11"),
    ),
    DemoBiller(
        "c2222222-2222-4222-8222-222222222222",
        "AGUA",
        "Agua Potable Demo",
        ("supply_ref",),
        ("20", "21"),
    ),
    DemoBiller(
        "c3333333-3333-4333-8333-333333333333",
        "GAS",
        "Gas Domiciliario Demo",
        ("supply_ref",),
        ("30",),
    ),
    DemoBiller(
        "c4444444-4444-4444-8444-444444444444",
        "INTERNET",
        "Internet Fibra Demo",
        ("supply_ref",),
        ("40", "41"),
    ),
    DemoBiller(
        "c5555555-5555-4555-8555-555555555555",
        "TELEFONIA",
        "Recarga Movil Sol Demo",
        ("phone",),
        ("99", "98"),
    ),
    DemoBiller(
        "c6666666-6666-4666-8666-666666666666",
        "TELEFONIA",
        "Recarga Movil Luna Demo",
        ("phone",),
        ("97", "96"),
    ),
)

_DEMO_BY_ID = {demo.biller_id: demo for demo in DEMO_BILLERS}


def get_demo_biller(biller_id: str) -> DemoBiller | None:
    """Demo por id o `None` (solo lectura, sin red)."""
    return _DEMO_BY_ID.get(str(biller_id or "").strip())


# ---------------------------------------------------------------- Resultados
@dataclass(frozen=True)
class BillerMatch:
    """Coincidencia de `lookup_biller` (sin PII: solo el catalogo publico)."""

    biller_id: str
    category: str
    name: str
    required_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class SupplyValidation:
    """Resultado de `validate_supply` (montos enteros; titular enmascarado)."""

    valid: bool
    biller_id: str = ""
    holder_masked: str = ""
    amount_due_minor: int = 0
    currency: str = "PEN"


def _deterministic_amount_due(supply_ref: str) -> int:
    """Deuda simulada determinista en centimos (mismo suministro -> mismo monto)."""
    digest = hashlib.sha256(supply_ref.encode("utf-8")).hexdigest()
    return 2_000 + (int(digest[:8], 16) % 98_000)


def _masked_holder(supply_ref: str) -> str:
    """Titular enmascarado determinista (sin PII: solo 2 ultimos visibles)."""
    tail = re.sub(r"\s+", "", supply_ref)[-2:]
    return f"******{tail}"


def parse_supply_payload(data: Any, *, biller_id: str = "") -> SupplyValidation:
    """Normaliza la respuesta del proveedor a `SupplyValidation` (tolerante).

    Acepta `{valid, holder_masked?, amount_due_minor?, currency?}` (y
    `amount_due`/`holder` como alias). Sin el minimo exigible ->
    `ServiceProviderUnavailableError` neutro (sin eco).
    """
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        raise ServiceProviderUnavailableError("respuesta invalida del proveedor de servicios")
    valid = data.get("valid", data.get("is_valid"))
    if not isinstance(valid, bool):
        raise ServiceProviderUnavailableError("respuesta invalida del proveedor de servicios")
    if not valid:
        return SupplyValidation(valid=False, biller_id=biller_id)
    amount = data.get("amount_due_minor", data.get("amount_due", 0))
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        raise ServiceProviderUnavailableError("respuesta invalida del proveedor de servicios")
    currency = str(data.get("currency") or "PEN").strip().upper()
    if not _CURRENCY_RE.match(currency):
        raise ServiceProviderUnavailableError("respuesta invalida del proveedor de servicios")
    holder = str(data.get("holder_masked", data.get("holder", ""))).strip()[:64]
    return SupplyValidation(
        valid=True,
        biller_id=biller_id,
        holder_masked=holder,
        amount_due_minor=amount,
        currency=currency,
    )


# ---------------------------------------------------------------- Interfaz
class ServiceProviderGateway(ABC):
    """Interfaz comun del adaptador de servicios (E4-T16)."""

    @abstractmethod
    def lookup_biller(self, query: str) -> list[BillerMatch]:
        """Empresas que coinciden por nombre o numero de suministro (sin red en `mock`)."""

    @abstractmethod
    def validate_supply(self, *, biller_id: str, supply_ref: str) -> SupplyValidation:
        """Valida el suministro contra el proveedor (puede usar red en `http`)."""


# ---------------------------------------------------------------- Config
@dataclass(frozen=True)
class ServiceProviderSettings:
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
    def from_env(cls, environ: dict[str, str] | None = None) -> ServiceProviderSettings:
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
            provider=str(env.get("SERVICE_PROVIDER", "mock")),
            mock_mode=str(env.get("SERVICE_PROVIDER_MOCK_MODE", "success")),
            base_url=str(env.get("SERVICE_PROVIDER_BASE_URL", "http://localhost:8000")),
            api_key=str(env.get("SERVICE_PROVIDER_API_KEY", "")),
            timeout_seconds=_float("SERVICE_PROVIDER_TIMEOUT_SECONDS", 5.0),
            max_retries=_int("SERVICE_PROVIDER_MAX_RETRIES", 2),
            backoff_base_seconds=_float("SERVICE_PROVIDER_BACKOFF_BASE_SECONDS", 0.1),
            breaker_failures=_int("SERVICE_PROVIDER_BREAKER_FAILURES", 3),
            breaker_cooldown_seconds=_float("SERVICE_PROVIDER_BREAKER_COOLDOWN_SECONDS", 30.0),
        )


# ---------------------------------------------------------------- Implementacion real
class HttpServiceProviderGateway(ServiceProviderGateway):
    """Implementacion real sobre `httpx`: `POST {base_url}/services/supply/validate`.

    Request `{biller_id, supply_ref}` (referencias opacas); response
    `{valid, holder_masked?, amount_due_minor?, currency?}` normalizada
    por `parse_supply_payload`. `lookup_biller` es local (filtro sobre
    el catalogo demo conocido; el proveedor real lo ampliara sin tocar
    el dominio). Cualquier estado no-2xx o fallo de red es un error
    tipado neutro (sin filtrar el cuerpo del proveedor ni la API key).
    """

    def __init__(
        self,
        settings: ServiceProviderSettings | None = None,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
        **overrides: Any,
    ) -> None:
        base = settings or ServiceProviderSettings.from_env()
        if overrides:
            data = {f: getattr(base, f) for f in base.__dataclass_fields__}
            data.update({k: v for k, v in overrides.items() if k in data})
            base = ServiceProviderSettings(**data)
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
            logger.warning("service_provider circuit_open")
            raise ServiceProviderUnavailableError("circuito del proveedor abierto tras caidas")

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_opened_at = None

    def _record_failure(self, code: str) -> None:
        self._consecutive_failures += 1
        logger.warning(
            "service_provider failure code=%s consecutive=%d",
            code,
            self._consecutive_failures,
        )
        if self._consecutive_failures >= self.settings.breaker_failures:
            self._circuit_opened_at = time.monotonic()
            logger.error("service_provider circuit opened")

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
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        return headers

    def _post(self, payload: dict[str, Any], *, supply_hash: str) -> dict[str, Any]:
        self._check_circuit()
        client = self._client_or_default()
        owns = self._owns_client and self._client is None
        last_error: Exception | None = None
        try:
            for attempt in range(self.settings.max_retries + 1):
                try:
                    response = client.post(
                        VALIDATE_PATH,
                        json=payload,
                        headers=self._auth_headers(),
                        timeout=self.settings.timeout_seconds,
                    )
                except (httpx.TimeoutException, TimeoutError) as exc:
                    last_error = exc
                    logger.warning(
                        "service_provider timeout supply_hash=%s attempt=%d",
                        supply_hash,
                        attempt,
                    )
                    self._sleep(attempt)
                    continue
                except httpx.HTTPError as exc:  # red / conexion: reintentable
                    last_error = exc
                    logger.warning(
                        "service_provider transport_error supply_hash=%s attempt=%d err=%s",
                        supply_hash,
                        attempt,
                        type(exc).__name__,
                    )
                    self._sleep(attempt)
                    continue
                if 200 <= response.status_code < 300:
                    self._record_success()
                    try:
                        body = response.json()
                    except ValueError:
                        body = {}
                    return body if isinstance(body, dict) else {}
                last_error = ServiceProviderUnavailableError(
                    f"proveedor respondio {response.status_code}"
                )
                logger.warning(
                    "service_provider server_error supply_hash=%s status=%d attempt=%d",
                    supply_hash,
                    response.status_code,
                    attempt,
                )
                self._sleep(attempt)
        finally:
            if owns:
                client.close()
        if isinstance(last_error, (httpx.TimeoutException, TimeoutError)):
            self._record_failure(SERVICE_PROVIDER_TIMEOUT)
            raise ServiceProviderTimeoutError(
                "timeout contra el proveedor de servicios"
            ) from last_error
        self._record_failure(SERVICE_PROVIDER_UNAVAILABLE)
        raise ServiceProviderUnavailableError(
            "proveedor de servicios no disponible"
        ) from last_error

    def _sleep(self, attempt: int) -> None:
        delay = self.settings.backoff_base_seconds * (2**attempt)
        if delay > 0:
            self._sleeper(delay)

    # -- interfaz -------------------------------------------------------
    def lookup_biller(self, query: str) -> list[BillerMatch]:
        return lookup_demo_billers(query)

    def validate_supply(self, *, biller_id: str, supply_ref: str) -> SupplyValidation:
        code = normalize_biller_id(biller_id)
        supply = normalize_supply_ref(supply_ref)
        supply_hash = hash_supply_ref(supply)
        body: dict[str, Any] = {"biller_id": code, "supply_ref": supply}
        # Solo hash en logs: nunca el suministro en claro ni la respuesta cruda.
        data = self._post(body, supply_hash=supply_hash)
        result = parse_supply_payload(data, biller_id=code)
        logger.info("service_provider ok supply_hash=%s valid=%s", supply_hash, result.valid)
        return result


# ---------------------------------------------------------------- Mock determinista
class MockServiceProviderGateway(ServiceProviderGateway):
    """Mock determinista sin red: `success` | `down` | `timeout`.

    `lookup_biller` filtra el catalogo demo en local (por nombre o por
    prefijo de suministro). `validate_supply` valida el formato en
    local (`ServiceProviderValidationError` sin tocar el "proveedor");
    con formato valido: `timeout` lanza `ServiceProviderTimeoutError`,
    `down` lanza `ServiceProviderUnavailableError`, y `success`
    responde sin red (valido + deuda determinista derivada del hash
    del suministro; `valid=False` si el suministro esta en `rejected`).
    """

    MODES = ("success", "down", "timeout")

    def __init__(
        self,
        mode: str = "success",
        rejected: set[str] | frozenset[str] | list[str] | tuple[str, ...] | None = None,
    ) -> None:
        if mode not in self.MODES:
            raise ValueError(f"mode debe ser uno de {self.MODES}")
        self.mode = mode
        self.rejected: frozenset[str] = frozenset(
            str(r or "").strip() for r in (rejected or []) if str(r or "").strip()
        )

    def lookup_biller(self, query: str) -> list[BillerMatch]:
        return lookup_demo_billers(query)

    def validate_supply(self, *, biller_id: str, supply_ref: str) -> SupplyValidation:
        code = normalize_biller_id(biller_id)
        supply = normalize_supply_ref(supply_ref)
        supply_hash = hash_supply_ref(supply)
        if self.mode == "timeout":
            logger.warning("service_provider mock_timeout supply_hash=%s", supply_hash)
            raise ServiceProviderTimeoutError("timeout simulado del proveedor de servicios")
        if self.mode == "down":
            logger.warning("service_provider mock_down supply_hash=%s", supply_hash)
            raise ServiceProviderUnavailableError("caida simulada del proveedor de servicios")
        demo = get_demo_biller(code)
        if demo is None:
            raise ServiceProviderValidationError("empresa de servicios desconocida")
        if supply in self.rejected:
            logger.info("service_provider mock_rejected supply_hash=%s", supply_hash)
            return SupplyValidation(valid=False, biller_id=code)
        logger.info("service_provider mock_ok supply_hash=%s mode=%s", supply_hash, self.mode)
        return SupplyValidation(
            valid=True,
            biller_id=code,
            holder_masked=_masked_holder(supply),
            amount_due_minor=_deterministic_amount_due(supply),
            currency="PEN",
        )


def lookup_demo_billers(query: str) -> list[BillerMatch]:
    """Filtro local del catalogo demo por nombre o numero de suministro.

    Puro y determinista (mismo input -> mismo output); sin red ni
    secretos. `query` vacia -> todo el catalogo; por nombre =
    subcadena insensible a mayusculas; por suministro = prefijo de
    alguno de los `supply_prefixes` del demo (solo digitos).
    """
    text = str(query or "").strip()
    if not text or len(text) > _MAX_QUERY_LEN:
        if len(text) > _MAX_QUERY_LEN:
            raise ServiceProviderValidationError("busqueda demasiado larga")
        return [
            BillerMatch(
                biller_id=demo.biller_id,
                category=demo.category,
                name=demo.name,
                required_fields=demo.required_fields,
            )
            for demo in DEMO_BILLERS
        ]
    folded = text.casefold()
    digits = re.sub(r"\D", "", text)
    matches: list[BillerMatch] = []
    for demo in DEMO_BILLERS:
        by_name = folded in demo.name.casefold() or folded in demo.category.casefold()
        by_supply = bool(digits) and any(digits.startswith(p) for p in demo.supply_prefixes)
        if by_name or by_supply:
            matches.append(
                BillerMatch(
                    biller_id=demo.biller_id,
                    category=demo.category,
                    name=demo.name,
                    required_fields=demo.required_fields,
                )
            )
    return matches


# ---------------------------------------------------------------- Fabrica por entorno
def create_service_provider(kind: str | None = None, **overrides: Any) -> ServiceProviderGateway:
    """Fabrica: `SERVICE_PROVIDER=mock|http` (default `mock`).

    Fuente: variables de entorno (`ServiceProviderSettings.from_env`);
    `kind` permite forzar la seleccion en pruebas. `http`/`real`/
    `remote` devuelven `HttpServiceProviderGateway`; cualquier otro
    valor devuelve `MockServiceProviderGateway`
    (`SERVICE_PROVIDER_MOCK_MODE` o `success`).
    """
    settings = ServiceProviderSettings.from_env()
    selected = (kind if kind is not None else settings.provider).strip().lower()
    if selected in ("http", "real", "remote"):
        return HttpServiceProviderGateway(settings=settings, **overrides)
    return MockServiceProviderGateway(mode=settings.mock_mode or "success")


__all__ = [
    "DEMO_BILLERS",
    "SERVICE_PROVIDER_TIMEOUT",
    "SERVICE_PROVIDER_UNAVAILABLE",
    "SERVICE_PROVIDER_VALIDATION_ERROR",
    "VALIDATE_PATH",
    "BillerMatch",
    "DemoBiller",
    "HttpServiceProviderGateway",
    "MockServiceProviderGateway",
    "ServiceProviderError",
    "ServiceProviderGateway",
    "ServiceProviderSettings",
    "ServiceProviderTimeoutError",
    "ServiceProviderUnavailableError",
    "ServiceProviderValidationError",
    "SupplyValidation",
    "create_service_provider",
    "get_demo_biller",
    "hash_supply_ref",
    "lookup_demo_billers",
    "normalize_biller_id",
    "normalize_phone",
    "normalize_supply_ref",
    "parse_supply_payload",
]
