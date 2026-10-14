"""Adaptador WalletInterop (E4-T03, modulo wallet, HU13 CA-03).

Interoperabilidad de la billetera con el sistema par (equipo del curso):
encapsula el **estandar de QR (formato propio firmado, D06)** y el contrato
acordado con el par (D01: el contrato definitivo puede llegar tarde; el mock
es la fuente del contrato hasta entonces, `docs/05#7`).

Patron `credit_bureau.py` / `holder_lookup.py` (`docs/02#8/#9`): interfaz
comun (`WalletInteropGateway`) + implementacion real (`HttpWalletInteropGateway`,
`POST {base_url}/interop/counterparts/validate`) + simulada
(`MockWalletInteropGateway`, determinista, sin red) + fabrica por entorno
(`WALLET_INTEROP_PROVIDER=mock|http`, default `mock`).

Alcance del adaptador (no mueve dinero por si mismo en el MVP):
- `encode_payload(...)` / `decode_payload(...)`: (de)codificacion **local**
  (pura, sin red) del sobre de transporte del estandar `BO-QR1` (canonico
  JSON ordenado -> base64url sin padding). La firma/validacion local del QR
  vive en `E4-T06` (`wallet.qr_charges`): aqui `sig` es un campo opaco de
  passthrough reservado, nunca verificado.
- `validate_counterpart(...)`: validacion de la contraparte del par
  (operacion de red en `http`, simulada en `mock`).

Reglas (`docs/02#8/#10`, `docs/16` reglas 7/10):
- Sin PII ni secretos en logs: la referencia del par solo se correlaciona
  hasheada (`hash_peer_ref`); la API key NUNCA se loguea (solo cabecera
  `Authorization`). Los mensajes de error son neutros y sin eco del dato.
- Timeouts cortos, reintentos con backoff exponencial y circuit breaker;
  el par nunca bloquea el hilo indefinidamente.
- Sin persistencia: el adaptador es un cliente (no escribe tablas de
  negocio) y no mueve dinero.

Configuracion por entorno (default `mock`, sin red):
- `WALLET_INTEROP_PROVIDER=mock|http` (no reconocido -> mock).
- `WALLET_INTEROP_MOCK_MODE=success|down|timeout` (default `success`).
- `WALLET_INTEROP_BASE_URL` (default `http://localhost:8000`).
- `WALLET_INTEROP_API_KEY` (default vacio; solo cabecera `Authorization`).
- `WALLET_INTEROP_TIMEOUT_SECONDS` (default `5.0`).
- `WALLET_INTEROP_MAX_RETRIES` (default `2`).
- `WALLET_INTEROP_BACKOFF_BASE_SECONDS` (default `0.1`).
- `WALLET_INTEROP_BREAKER_FAILURES` (default `3` fallos consecutivos).
- `WALLET_INTEROP_BREAKER_COOLDOWN_SECONDS` (default `30.0`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
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
INTEROP_UNAVAILABLE = "INTEROP_UNAVAILABLE"
INTEROP_TIMEOUT = "INTEROP_TIMEOUT"
INTEROP_VALIDATION_ERROR = "VALIDATION_ERROR"

#: Version del estandar propio firmado (D06). La firma la fija/verifica
#: `E4-T06`; este adaptador solo transporta el sobre.
INTEROP_FORMAT_VERSION = "BO-QR1"

#: Ruta del contrato con el par (`docs/05#7`; mock hasta el contrato definitivo D01).
VALIDATE_PATH = "/interop/counterparts/validate"

_PEER_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{2,63}$")
_BANK_CODE_RE = re.compile(r"^[A-Z0-9]{2,11}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MAX_REF_LEN = 64
_MAX_SIG_LEN = 512


class WalletInteropError(Exception):
    """Error base del adaptador; expone `code` para propagacion tipada."""

    code: str = INTEROP_UNAVAILABLE

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InteropUnavailableError(WalletInteropError):
    """Par caido o con error tras agotar reintentos (`503` en el caso de uso)."""

    def __init__(self, message: str = "red interoperable no disponible") -> None:
        super().__init__(INTEROP_UNAVAILABLE, message)


class InteropTimeoutError(WalletInteropError):
    """Timeout contra el par tras agotar reintentos (`502` en el caso de uso)."""

    def __init__(self, message: str = "timeout contra la red interoperable") -> None:
        super().__init__(INTEROP_TIMEOUT, message)


class InteropValidationError(WalletInteropError):
    """Payload/referencia con formato invalido -> 422 (sin llamar al par)."""

    def __init__(self, message: str = "payload interoperable invalido") -> None:
        super().__init__(INTEROP_VALIDATION_ERROR, message)


# ---------------------------------------------------------------- Utilidades puras
def hash_peer_ref(value: object) -> str:
    """Hash corto de la referencia del par para correlacion en logs (sin PII)."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def validate_amount_minor(value: object, *, field_name: str = "amount_minor") -> int:
    """Valida un monto en centimos: entero `>= 0` (regla de oro 3)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InteropValidationError(f"{field_name} debe ser entero en centimos")
    if value < 0:
        raise InteropValidationError(f"{field_name} debe ser entero en centimos")
    return value


def normalize_peer_ref(value: object) -> str:
    """Normaliza y valida la referencia de contraparte del par (opaca)."""
    text = str(value or "").strip()
    if not text or len(text) > _MAX_REF_LEN or not _PEER_REF_RE.match(text):
        raise InteropValidationError("referencia de contraparte invalida")
    return text


def normalize_bank_code(value: object) -> str:
    """Normaliza y valida el codigo de entidad del par (vacio = no informado)."""
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if not _BANK_CODE_RE.match(text):
        raise InteropValidationError("codigo de entidad invalido")
    return text


# ---------------------------------------------------------------- Resultado
@dataclass(frozen=True)
class InteropPayload:
    """Sobre canonico del estandar `BO-QR1` (D06, formato propio firmado).

    `sig` es opaco: lo fija/verifica `E4-T06` (firma local); el adaptador
    lo transporta sin interpretarlo. Campos extra del par se ignoran
    (compatibilidad hacia adelante), pero los conocidos se validan.
    """

    issuer: str
    peer: str
    ref: str
    amount_minor: int
    currency: str
    expires_at: int = 0
    sig: str = ""


@dataclass(frozen=True)
class CounterpartResult:
    """Resultado de `validate_counterpart` (sin PII: solo refs opacas)."""

    valid: bool
    peer_ref: str = ""
    bank_code: str = ""


def encode_payload(
    *,
    issuer: str,
    peer: str,
    ref: str,
    amount_minor: int,
    currency: str,
    expires_at: int = 0,
    sig: str = "",
) -> str:
    """Codifica el sobre `BO-QR1`: JSON canonico (claves ordenadas) -> base64url.

    Pura y determinista (mismo input -> mismo output); sin red ni secretos.
    """
    issuer_text = str(issuer or "").strip()
    peer_text = str(peer or "").strip()
    ref_text = str(ref or "").strip()
    if not issuer_text or len(issuer_text) > _MAX_REF_LEN:
        raise InteropValidationError("emisor invalido")
    normalize_peer_ref(peer_text)
    if not ref_text or len(ref_text) > _MAX_REF_LEN:
        raise InteropValidationError("referencia de cobro invalida")
    amount = validate_amount_minor(amount_minor)
    currency_text = str(currency or "").strip().upper()
    if not _CURRENCY_RE.match(currency_text):
        raise InteropValidationError("moneda invalida")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at < 0:
        raise InteropValidationError("expiracion invalida")
    sig_text = str(sig or "")
    if len(sig_text) > _MAX_SIG_LEN:
        raise InteropValidationError("firma invalida")
    canonical = {
        "amount_minor": amount,
        "currency": currency_text,
        "exp": expires_at,
        "issuer": issuer_text,
        "peer": peer_text,
        "ref": ref_text,
        "sig": sig_text,
        "v": INTEROP_FORMAT_VERSION,
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_payload(payload: str) -> InteropPayload:
    """Decodifica y valida un sobre `BO-QR1` (puro, sin red).

    Cualquier manipulacion (base64/JSON roto, version distinta, campos
    ausentes o fuera de rango) -> `InteropValidationError` neutro.
    """
    text = str(payload or "").strip()
    if not text or len(text) > 8192:
        raise InteropValidationError("payload interoperable invalido")
    padded = text + "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeError):
        raise InteropValidationError("payload interoperable invalido") from None
    if not isinstance(data, dict):
        raise InteropValidationError("payload interoperable invalido")
    if data.get("v") != INTEROP_FORMAT_VERSION:
        raise InteropValidationError("version de estandar no soportada")
    try:
        issuer = str(data.get("issuer") or "").strip()
        peer = normalize_peer_ref(data.get("peer"))
        ref = str(data.get("ref") or "").strip()
        if not issuer or len(issuer) > _MAX_REF_LEN:
            raise InteropValidationError("emisor invalido")
        if not ref or len(ref) > _MAX_REF_LEN:
            raise InteropValidationError("referencia de cobro invalida")
        amount = validate_amount_minor(data.get("amount_minor"))
        currency = str(data.get("currency") or "").strip().upper()
        if not _CURRENCY_RE.match(currency):
            raise InteropValidationError("moneda invalida")
        expires_at = data.get("exp", 0)
        if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at < 0:
            raise InteropValidationError("expiracion invalida")
        sig = str(data.get("sig") or "")
        if len(sig) > _MAX_SIG_LEN:
            raise InteropValidationError("firma invalida")
    except InteropValidationError:
        raise
    except (TypeError, ValueError):
        raise InteropValidationError("payload interoperable invalido") from None
    return InteropPayload(
        issuer=issuer,
        peer=peer,
        ref=ref,
        amount_minor=amount,
        currency=currency,
        expires_at=expires_at,
        sig=sig,
    )


def parse_counterpart_payload(data: Any, *, peer_ref: str = "") -> CounterpartResult:
    """Normaliza la respuesta del par a `CounterpartResult` (tolerante).

    Acepta `{valid, peer_ref?, bank_code?}` (y `peer`/`bank` como alias).
    Sin el minimo exigible -> `InteropUnavailableError` neutro (sin eco).
    """
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        raise InteropUnavailableError("respuesta invalida de la red interoperable")
    valid = data.get("valid", data.get("is_valid"))
    if not isinstance(valid, bool):
        raise InteropUnavailableError("respuesta invalida de la red interoperable")
    ref = data.get("peer_ref", data.get("peer", peer_ref))
    bank = data.get("bank_code", data.get("bank", ""))
    try:
        peer_text = normalize_peer_ref(ref) if str(ref or "").strip() else ""
        bank_text = normalize_bank_code(bank)
    except InteropValidationError:
        raise InteropUnavailableError("respuesta invalida de la red interoperable") from None
    return CounterpartResult(valid=valid, peer_ref=peer_text, bank_code=bank_text)


# ---------------------------------------------------------------- Interfaz
class WalletInteropGateway(ABC):
    """Interfaz comun del adaptador de interoperabilidad (E4-T03)."""

    @abstractmethod
    def encode_payload(
        self,
        *,
        issuer: str,
        peer: str,
        ref: str,
        amount_minor: int,
        currency: str,
        expires_at: int = 0,
        sig: str = "",
    ) -> str:
        """Codifica el sobre `BO-QR1` (local, sin red)."""

    @abstractmethod
    def decode_payload(self, payload: str) -> InteropPayload:
        """Decodifica y valida un sobre `BO-QR1` (local, sin red)."""

    @abstractmethod
    def validate_counterpart(self, *, peer_ref: str, bank_code: str = "") -> CounterpartResult:
        """Valida la contraparte del par (puede usar red en `http`)."""


# ---------------------------------------------------------------- Config
@dataclass(frozen=True)
class WalletInteropSettings:
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
    def from_env(cls, environ: dict[str, str] | None = None) -> WalletInteropSettings:
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
            provider=str(env.get("WALLET_INTEROP_PROVIDER", "mock")),
            mock_mode=str(env.get("WALLET_INTEROP_MOCK_MODE", "success")),
            base_url=str(env.get("WALLET_INTEROP_BASE_URL", "http://localhost:8000")),
            api_key=str(env.get("WALLET_INTEROP_API_KEY", "")),
            timeout_seconds=_float("WALLET_INTEROP_TIMEOUT_SECONDS", 5.0),
            max_retries=_int("WALLET_INTEROP_MAX_RETRIES", 2),
            backoff_base_seconds=_float("WALLET_INTEROP_BACKOFF_BASE_SECONDS", 0.1),
            breaker_failures=_int("WALLET_INTEROP_BREAKER_FAILURES", 3),
            breaker_cooldown_seconds=_float("WALLET_INTEROP_BREAKER_COOLDOWN_SECONDS", 30.0),
        )


# ---------------------------------------------------------------- Implementacion real
class HttpWalletInteropGateway(WalletInteropGateway):
    """Implementacion real sobre `httpx`: `POST {base_url}/interop/counterparts/validate`.

    Request `{peer_ref, bank_code?}` (referencias opacas); response
    `{valid, peer_ref?, bank_code?}` normalizada por
    `parse_counterpart_payload`. `(de)codificar` es local (funciones puras).
    Cualquier estado no-2xx o fallo de red es un error tipado neutro (sin
    filtrar el cuerpo del par ni la API key).
    """

    def __init__(
        self,
        settings: WalletInteropSettings | None = None,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
        **overrides: Any,
    ) -> None:
        base = settings or WalletInteropSettings.from_env()
        if overrides:
            data = {f: getattr(base, f) for f in base.__dataclass_fields__}
            data.update({k: v for k, v in overrides.items() if k in data})
            base = WalletInteropSettings(**data)
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
            logger.warning("wallet_interop circuit_open")
            raise InteropUnavailableError("circuito interoperable abierto tras caidas")

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_opened_at = None

    def _record_failure(self, code: str) -> None:
        self._consecutive_failures += 1
        logger.warning(
            "wallet_interop failure code=%s consecutive=%d",
            code,
            self._consecutive_failures,
        )
        if self._consecutive_failures >= self.settings.breaker_failures:
            self._circuit_opened_at = time.monotonic()
            logger.error("wallet_interop circuit opened")

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

    def _post(self, payload: dict[str, Any], *, peer_hash: str) -> dict[str, Any]:
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
                        "wallet_interop timeout peer_hash=%s attempt=%d",
                        peer_hash,
                        attempt,
                    )
                    self._sleep(attempt)
                    continue
                except httpx.HTTPError as exc:  # red / conexion: reintentable
                    last_error = exc
                    logger.warning(
                        "wallet_interop transport_error peer_hash=%s attempt=%d err=%s",
                        peer_hash,
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
                last_error = InteropUnavailableError(f"par respondio {response.status_code}")
                logger.warning(
                    "wallet_interop server_error peer_hash=%s status=%d attempt=%d",
                    peer_hash,
                    response.status_code,
                    attempt,
                )
                self._sleep(attempt)
        finally:
            if owns:
                client.close()
        if isinstance(last_error, (httpx.TimeoutException, TimeoutError)):
            self._record_failure(INTEROP_TIMEOUT)
            raise InteropTimeoutError("timeout contra la red interoperable") from last_error
        self._record_failure(INTEROP_UNAVAILABLE)
        raise InteropUnavailableError("red interoperable no disponible") from last_error

    def _sleep(self, attempt: int) -> None:
        delay = self.settings.backoff_base_seconds * (2**attempt)
        if delay > 0:
            self._sleeper(delay)

    # -- interfaz -------------------------------------------------------
    def encode_payload(self, **kwargs: Any) -> str:
        return encode_payload(**kwargs)  # type: ignore[arg-type]

    def decode_payload(self, payload: str) -> InteropPayload:
        return decode_payload(payload)

    def validate_counterpart(self, *, peer_ref: str, bank_code: str = "") -> CounterpartResult:
        peer = normalize_peer_ref(peer_ref)
        bank = normalize_bank_code(bank_code)
        peer_hash = hash_peer_ref(peer)
        body: dict[str, Any] = {"peer_ref": peer}
        if bank:
            body["bank_code"] = bank
        # Solo hash en logs: nunca la referencia en claro ni la respuesta cruda.
        data = self._post(body, peer_hash=peer_hash)
        result = parse_counterpart_payload(data, peer_ref=peer)
        logger.info("wallet_interop ok peer_hash=%s valid=%s", peer_hash, result.valid)
        return result


# ---------------------------------------------------------------- Mock determinista
class MockWalletInteropGateway(WalletInteropGateway):
    """Mock determinista sin red: `success` | `down` | `timeout`.

    `(de)codificar` es local (funciones puras, igual que en `http`).
    `validate_counterpart` valida el formato en local (`InteropValidationError`
    sin tocar el "par"); con formato valido: `timeout` lanza
    `InteropTimeoutError`, `down` lanza `InteropUnavailableError`, y
    `success` responde sin red (`valid=False` si el `peer_ref` esta en
    `rejected`; si no, `valid=True` con el `bank_code` dado o el fijado en
    `counterparts`).
    """

    MODES = ("success", "down", "timeout")

    def __init__(
        self,
        mode: str = "success",
        counterparts: dict[str, str] | None = None,
        rejected: set[str] | frozenset[str] | list[str] | tuple[str, ...] | None = None,
    ) -> None:
        if mode not in self.MODES:
            raise ValueError(f"mode debe ser uno de {self.MODES}")
        self.mode = mode
        self.counterparts: dict[str, str] = {
            normalize_peer_ref(k): normalize_bank_code(v)
            for k, v in dict(counterparts or {}).items()
        }
        self.rejected: frozenset[str] = frozenset(normalize_peer_ref(r) for r in (rejected or []))

    def encode_payload(self, **kwargs: Any) -> str:
        return encode_payload(**kwargs)  # type: ignore[arg-type]

    def decode_payload(self, payload: str) -> InteropPayload:
        return decode_payload(payload)

    def validate_counterpart(self, *, peer_ref: str, bank_code: str = "") -> CounterpartResult:
        peer = normalize_peer_ref(peer_ref)
        bank = normalize_bank_code(bank_code)
        peer_hash = hash_peer_ref(peer)
        if self.mode == "timeout":
            logger.warning("wallet_interop mock_timeout peer_hash=%s", peer_hash)
            raise InteropTimeoutError("timeout simulado de la red interoperable")
        if self.mode == "down":
            logger.warning("wallet_interop mock_down peer_hash=%s", peer_hash)
            raise InteropUnavailableError("caida simulada de la red interoperable")
        if peer in self.rejected:
            logger.info("wallet_interop mock_rejected peer_hash=%s", peer_hash)
            return CounterpartResult(valid=False, peer_ref=peer, bank_code=bank)
        resolved_bank = bank or self.counterparts.get(peer, "")
        logger.info("wallet_interop mock_ok peer_hash=%s mode=%s", peer_hash, self.mode)
        return CounterpartResult(valid=True, peer_ref=peer, bank_code=resolved_bank)


# ---------------------------------------------------------------- Fabrica por entorno
def create_wallet_interop_provider(
    kind: str | None = None, **overrides: Any
) -> WalletInteropGateway:
    """Fabrica: `WALLET_INTEROP_PROVIDER=mock|http` (default `mock`).

    Fuente: variables de entorno (`WalletInteropSettings.from_env`); `kind`
    permite forzar la seleccion en pruebas. `http`/`real`/`remote`
    devuelven `HttpWalletInteropGateway`; cualquier otro valor devuelve
    `MockWalletInteropGateway` (`WALLET_INTEROP_MOCK_MODE` o `success`).
    """
    settings = WalletInteropSettings.from_env()
    selected = (kind if kind is not None else settings.provider).strip().lower()
    if selected in ("http", "real", "remote"):
        return HttpWalletInteropGateway(settings=settings, **overrides)
    return MockWalletInteropGateway(mode=settings.mock_mode or "success")


__all__ = [
    "INTEROP_FORMAT_VERSION",
    "INTEROP_TIMEOUT",
    "INTEROP_UNAVAILABLE",
    "INTEROP_VALIDATION_ERROR",
    "VALIDATE_PATH",
    "CounterpartResult",
    "HttpWalletInteropGateway",
    "InteropPayload",
    "InteropTimeoutError",
    "InteropUnavailableError",
    "InteropValidationError",
    "MockWalletInteropGateway",
    "WalletInteropError",
    "WalletInteropGateway",
    "WalletInteropSettings",
    "create_wallet_interop_provider",
    "decode_payload",
    "encode_payload",
    "hash_peer_ref",
    "normalize_bank_code",
    "normalize_peer_ref",
    "parse_counterpart_payload",
    "validate_amount_minor",
]
