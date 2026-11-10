"""Adaptador CreditBureau (E3-T06, modulo credits).

Central de riesgo para la evaluacion crediticia (HU10): el motor de reglas
(`E3-T07`) consulta el bureau y recibe `{score, deudas, morosidad}`; el
adaptador no decide, solo devuelve el dato.

Patron `holder_lookup.py` (docs/02#8/#9/#10): interfaz comun +
implementacion real (`HttpCreditBureau`, `POST {base_url}/score`) +
simulada (`MockCreditBureau`, determinista por perfil) + fabrica por
entorno (`CREDIT_BUREAU_PROVIDER=mock|http`, default `mock`).

Reglas (docs/02#8/#10, docs/16 reglas 7/10):
- Sin PII ni secretos en logs: el documento/usuario solo se correlaciona
  hasheado (`hash_subject`); ingresos, deuda, respuesta cruda y la API key
  NUNCA se loguean. Los mensajes de error son neutros y sin eco del dato.
- Timeouts cortos, reintentos con backoff exponencial y circuit breaker;
  el proveedor nunca bloquea el hilo indefinidamente.
- Sin persistencia: el adaptador es un cliente (el caso de uso decide si
  persiste el score y emite `bureau.score.received`, E3-T08).

Configuracion por entorno (default `mock`, sin red):
- `CREDIT_BUREAU_PROVIDER=mock|http` (no reconocido -> mock).
- `CREDIT_BUREAU_MOCK_MODE=success|down|timeout` (default `success`).
- `CREDIT_BUREAU_BASE_URL` (default `http://localhost:8000`).
- `CREDIT_BUREAU_API_KEY` (default vacio; solo cabecera `Authorization`).
- `CREDIT_BUREAU_TIMEOUT_SECONDS` (default `5.0`).
- `CREDIT_BUREAU_MAX_RETRIES` (default `2`).
- `CREDIT_BUREAU_BACKOFF_BASE_SECONDS` (default `0.1`).
- `CREDIT_BUREAU_BREAKER_FAILURES` (default `3` fallos consecutivos).
- `CREDIT_BUREAU_BREAKER_COOLDOWN_SECONDS` (default `30.0`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- Codigos de negocio
BUREAU_UNAVAILABLE = "BUREAU_UNAVAILABLE"
BUREAU_TIMEOUT = "BUREAU_TIMEOUT"
BUREAU_VALIDATION_ERROR = "VALIDATION_ERROR"

#: Ruta del contrato real (docs/02#9: `POST /score -> {score, deudas, morosidad}`).
SCORE_PATH = "/score"


class CreditBureauError(Exception):
    """Error base del adaptador; expone `code` para propagacion tipada."""

    code: str = BUREAU_UNAVAILABLE

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BureauUnavailableError(CreditBureauError):
    """Proveedor caido o con error tras agotar reintentos (`503` en el caso de uso)."""

    def __init__(self, message: str = "central de riesgo no disponible") -> None:
        super().__init__(BUREAU_UNAVAILABLE, message)


class BureauTimeoutError(CreditBureauError):
    """Timeout contra el proveedor tras agotar reintentos (`502` en el caso de uso)."""

    def __init__(self, message: str = "timeout contra la central de riesgo") -> None:
        super().__init__(BUREAU_TIMEOUT, message)


class BureauValidationError(CreditBureauError):
    """Entradas con formato invalido -> 422 (sin llamar al proveedor)."""

    def __init__(self, message: str = "parametros de consulta invalidos") -> None:
        super().__init__(BUREAU_VALIDATION_ERROR, message)


# ---------------------------------------------------------------- Resultado
@dataclass(frozen=True)
class BureauScore:
    """Dato normalizado del bureau (montos en centimos, nunca `float`).

    `morosidad` se normaliza a `bool` aunque el proveedor responda texto
    (`"SI"`, `"alta"`, ...); el adaptador no decide, solo normaliza.
    """

    score: int
    deudas_minor: int
    morosidad: bool


# ---------------------------------------------------------------- Utilidades puras
def hash_subject(value: object) -> str:
    """Hash corto del sujeto para correlacion en logs (sin PII)."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def validate_amount_minor(value: object, *, field_name: str = "monto") -> int:
    """Valida un monto en centimos: entero `>= 0` (regla de oro 3)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise BureauValidationError(f"{field_name} debe ser entero en centimos")
    if value < 0:
        raise BureauValidationError(f"{field_name} debe ser entero en centimos")
    return value


def normalize_morosidad(value: object) -> bool:
    """Normaliza `morosidad` (`bool|str`) a `bool`."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in {"true", "1", "si", "sí", "s", "alta", "mora", "con_mora", "delinquent", "yes"}


def parse_bureau_payload(data: Any) -> BureauScore:
    """Normaliza la respuesta del proveedor a `BureauScore` (tolerante).

    Acepta `score` (`score`/`puntaje`), deudas (`deudas`/`deudas_minor`/
    `debt_minor`/`deuda`) y `morosidad` (`morosidad`/`mora`/`delinquency`).
    Sin el minimo exigible -> `BureauUnavailableError` neutro (sin eco).
    """
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        raise BureauUnavailableError("respuesta invalida de la central de riesgo")
    raw_score = data.get("score", data.get("puntaje"))
    raw_debts = data.get(
        "deudas", data.get("deudas_minor", data.get("debt_minor", data.get("deuda", 0)))
    )
    raw_mora = data.get("morosidad", data.get("mora", data.get("delinquency", False)))
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
        raise BureauUnavailableError("respuesta invalida de la central de riesgo")
    score = int(raw_score)
    if isinstance(raw_debts, bool) or not isinstance(raw_debts, (int, float)) or int(raw_debts) < 0:
        raise BureauUnavailableError("respuesta invalida de la central de riesgo")
    return BureauScore(
        score=score,
        deudas_minor=int(raw_debts),
        morosidad=normalize_morosidad(raw_mora),
    )


# ---------------------------------------------------------------- Interfaz
class CreditBureauProvider(ABC):
    """Interfaz comun del adaptador de central de riesgo."""

    @abstractmethod
    def get_score(
        self,
        *,
        subject_ref: str = "",
        document_hash: str = "",
        user_id: str = "",
        income_minor: int = 0,
        debt_minor: int = 0,
    ) -> BureauScore:
        """Retorna el dato del bureau para el sujeto (sin decidir)."""


def _subject_key(*, subject_ref: str = "", document_hash: str = "", user_id: str = "") -> str:
    """Clave del sujeto: primer identificador no vacio (hash o id opaco)."""
    for candidate in (subject_ref, document_hash, user_id):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


# ---------------------------------------------------------------- Config
@dataclass(frozen=True)
class CreditBureauSettings:
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
    def from_env(cls, environ: dict[str, str] | None = None) -> CreditBureauSettings:
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
            provider=str(env.get("CREDIT_BUREAU_PROVIDER", "mock")),
            mock_mode=str(env.get("CREDIT_BUREAU_MOCK_MODE", "success")),
            base_url=str(env.get("CREDIT_BUREAU_BASE_URL", "http://localhost:8000")),
            api_key=str(env.get("CREDIT_BUREAU_API_KEY", "")),
            timeout_seconds=_float("CREDIT_BUREAU_TIMEOUT_SECONDS", 5.0),
            max_retries=_int("CREDIT_BUREAU_MAX_RETRIES", 2),
            backoff_base_seconds=_float("CREDIT_BUREAU_BACKOFF_BASE_SECONDS", 0.1),
            breaker_failures=_int("CREDIT_BUREAU_BREAKER_FAILURES", 3),
            breaker_cooldown_seconds=_float("CREDIT_BUREAU_BREAKER_COOLDOWN_SECONDS", 30.0),
        )


# ---------------------------------------------------------------- Implementacion real
class HttpCreditBureau(CreditBureauProvider):
    """Implementacion real sobre `httpx`: `POST {base_url}/score`.

    Request `{subject_ref?, document_hash?, user_id?, income_minor,
    debt_minor}` (identificadores opacos + montos en centimos); response
    `{score, deudas, morosidad}` normalizada por `parse_bureau_payload`.
    `404`/`429` y cualquier otro estado o fallo de red son errores tipados
    neutros (sin filtrar el cuerpo del proveedor ni la API key).
    """

    def __init__(
        self,
        settings: CreditBureauSettings | None = None,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
        **overrides: Any,
    ) -> None:
        base = settings or CreditBureauSettings.from_env()
        if overrides:
            data = {f: getattr(base, f) for f in base.__dataclass_fields__}
            data.update({k: v for k, v in overrides.items() if k in data})
            base = CreditBureauSettings(**data)
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
            logger.warning("credit_bureau circuit_open")
            raise BureauUnavailableError("circuito de la central de riesgo abierto tras caidas")

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_opened_at = None

    def _record_failure(self, code: str) -> None:
        self._consecutive_failures += 1
        logger.warning(
            "credit_bureau failure code=%s consecutive=%d",
            code,
            self._consecutive_failures,
        )
        if self._consecutive_failures >= self.settings.breaker_failures:
            self._circuit_opened_at = time.monotonic()
            logger.error("credit_bureau circuit opened")

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

    def _post(self, payload: dict[str, Any], *, subject_hash: str) -> dict[str, Any]:
        self._check_circuit()
        client = self._client_or_default()
        owns = self._owns_client and self._client is None
        last_error: Exception | None = None
        try:
            for attempt in range(self.settings.max_retries + 1):
                try:
                    response = client.post(
                        SCORE_PATH,
                        json=payload,
                        headers=self._auth_headers(),
                        timeout=self.settings.timeout_seconds,
                    )
                except (httpx.TimeoutException, TimeoutError) as exc:
                    last_error = exc
                    logger.warning(
                        "credit_bureau timeout subject_hash=%s attempt=%d",
                        subject_hash,
                        attempt,
                    )
                    self._sleep(attempt)
                    continue
                except httpx.HTTPError as exc:  # red / conexion: reintentable
                    last_error = exc
                    logger.warning(
                        "credit_bureau transport_error subject_hash=%s attempt=%d err=%s",
                        subject_hash,
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
                last_error = BureauUnavailableError(f"proveedor respondio {response.status_code}")
                logger.warning(
                    "credit_bureau server_error subject_hash=%s status=%d attempt=%d",
                    subject_hash,
                    response.status_code,
                    attempt,
                )
                self._sleep(attempt)
        finally:
            if owns:
                client.close()
        if isinstance(last_error, (httpx.TimeoutException, TimeoutError)):
            self._record_failure(BUREAU_TIMEOUT)
            raise BureauTimeoutError("timeout contra la central de riesgo") from last_error
        self._record_failure(BUREAU_UNAVAILABLE)
        raise BureauUnavailableError("central de riesgo no disponible") from last_error

    def _sleep(self, attempt: int) -> None:
        delay = self.settings.backoff_base_seconds * (2**attempt)
        if delay > 0:
            self._sleeper(delay)

    # -- interfaz -------------------------------------------------------
    def get_score(
        self,
        *,
        subject_ref: str = "",
        document_hash: str = "",
        user_id: str = "",
        income_minor: int = 0,
        debt_minor: int = 0,
    ) -> BureauScore:
        key = _subject_key(subject_ref=subject_ref, document_hash=document_hash, user_id=user_id)
        income = validate_amount_minor(income_minor, field_name="income_minor")
        debt = validate_amount_minor(debt_minor, field_name="debt_minor")
        subject_hash = hash_subject(key)
        payload: dict[str, Any] = {"income_minor": income, "debt_minor": debt}
        if isinstance(subject_ref, str) and subject_ref.strip():
            payload["subject_ref"] = subject_ref.strip()
        if isinstance(document_hash, str) and document_hash.strip():
            payload["document_hash"] = document_hash.strip()
        if isinstance(user_id, str) and user_id.strip():
            payload["user_id"] = user_id.strip()
        # Solo hash en logs: nunca identificadores, montos ni respuesta cruda.
        data = self._post(payload, subject_hash=subject_hash)
        result = parse_bureau_payload(data)
        logger.info("credit_bureau ok subject_hash=%s", subject_hash)
        return result


# ---------------------------------------------------------------- Mock determinista
#: Perfiles fijos del mock (misma entrada -> misma salida; sin azar).
MOCK_PROFILES: dict[str, BureauScore] = {
    "prime": BureauScore(score=780, deudas_minor=0, morosidad=False),
    "standard": BureauScore(score=650, deudas_minor=500_00, morosidad=False),
    "subprime": BureauScore(score=520, deudas_minor=1500_00, morosidad=True),
}

_MOCK_BUCKETS = ("prime", "standard", "subprime")


def mock_profile_for(key: str) -> str:
    """Perfil determinista para una clave de sujeto (por hash)."""
    digest = hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()
    return _MOCK_BUCKETS[int(digest, 16) % len(_MOCK_BUCKETS)]


class MockCreditBureau(CreditBureauProvider):
    """Mock determinista por perfil: `success` | `down` | `timeout`.

    `success` responde sin red segun reglas fijas por perfil: el `profiles`
    opcional fija el `BureauScore` por clave de sujeto (fixtures de demo) y
    el resto deriva del hash (`prime`/`standard`/`subprime`). `down` lanza
    `BureauUnavailableError`; `timeout` lanza `BureauTimeoutError`.
    """

    MODES = ("success", "down", "timeout")

    def __init__(
        self,
        mode: str = "success",
        profiles: dict[str, BureauScore | dict[str, Any]] | None = None,
    ) -> None:
        if mode not in self.MODES:
            raise ValueError(f"mode debe ser uno de {self.MODES}")
        self.mode = mode
        self.profiles: dict[str, BureauScore] = {}
        for key, value in dict(profiles or {}).items():
            if isinstance(value, BureauScore):
                self.profiles[str(key)] = value
            elif isinstance(value, dict):
                self.profiles[str(key)] = parse_bureau_payload(value)
            else:
                raise TypeError("profiles debe mapear a BureauScore o dict")

    def get_score(
        self,
        *,
        subject_ref: str = "",
        document_hash: str = "",
        user_id: str = "",
        income_minor: int = 0,
        debt_minor: int = 0,
    ) -> BureauScore:
        key = _subject_key(subject_ref=subject_ref, document_hash=document_hash, user_id=user_id)
        validate_amount_minor(income_minor, field_name="income_minor")
        validate_amount_minor(debt_minor, field_name="debt_minor")
        subject_hash = hash_subject(key)
        if self.mode == "timeout":
            logger.warning("credit_bureau mock_timeout subject_hash=%s", subject_hash)
            raise BureauTimeoutError("timeout simulado de la central de riesgo")
        if self.mode == "down":
            logger.warning("credit_bureau mock_down subject_hash=%s", subject_hash)
            raise BureauUnavailableError("caida simulada de la central de riesgo")
        if key in self.profiles:
            result = self.profiles[key]
        else:
            result = MOCK_PROFILES[mock_profile_for(key)]
        logger.info(
            "credit_bureau mock_ok subject_hash=%s profile=%s mode=%s",
            subject_hash,
            mock_profile_for(key) if key not in self.profiles else "fixture",
            self.mode,
        )
        return result


# ---------------------------------------------------------------- Fabrica por entorno
def create_credit_bureau_provider(
    kind: str | None = None, **overrides: Any
) -> CreditBureauProvider:
    """Fabrica: `CREDIT_BUREAU_PROVIDER=mock|http` (default `mock`).

    Fuente: variables de entorno (`CreditBureauSettings.from_env`); `kind`
    permite forzar la seleccion en pruebas. `http`/`real`/`remote`
    devuelven `HttpCreditBureau`; cualquier otro valor devuelve
    `MockCreditBureau` (`CREDIT_BUREAU_MOCK_MODE` o `success`).
    """
    settings = CreditBureauSettings.from_env()
    selected = (kind if kind is not None else settings.provider).strip().lower()
    if selected in ("http", "real", "remote"):
        return HttpCreditBureau(settings=settings, **overrides)
    return MockCreditBureau(mode=settings.mock_mode or "success")


__all__ = [
    "BUREAU_TIMEOUT",
    "BUREAU_UNAVAILABLE",
    "BUREAU_VALIDATION_ERROR",
    "MOCK_PROFILES",
    "SCORE_PATH",
    "BureauScore",
    "BureauTimeoutError",
    "BureauUnavailableError",
    "BureauValidationError",
    "CreditBureauError",
    "CreditBureauProvider",
    "CreditBureauSettings",
    "HttpCreditBureau",
    "MockCreditBureau",
    "create_credit_bureau_provider",
    "hash_subject",
    "mock_profile_for",
    "normalize_morosidad",
    "parse_bureau_payload",
    "validate_amount_minor",
]
