"""Adaptador KycProvider (E1-T01, modulo identity).

Cliente hacia el microservicio KYC existente (solo onboarding HU01), con
interfaz comun e implementaciones real (`HttpKycProvider`) y simulada
(`MockKycProvider`).

Contrato consumido (`docs/05-contratos-api.md#8`, prefijo `/api/v1`):
- `POST /api/v1/liveness/challenge`    -> token + secuencia de pasos.
- `POST /api/v1/liveness/evaluate`     -> validacion de una tarea.
- `POST /api/v1/identity/verify-full`  -> resultado integral del KYC.

Reglas (docs/02-arquitectura.md#9, docs/16 reglas de oro):
- La `X-API-Key` vive solo en el backend; nunca se expone al movil ni se
  incluye en los retornos del adaptador.
- No se persisten frames ni imagenes: solo resultados + hash del token.
- Sin PII en logs: solo IDs y codigos (el token se registra hasheado).
- Timeouts cortos, 2 reintentos con backoff exponencial y circuit breaker.

Configuracion por entorno (defaults documentados):
- `KYC_PROVIDER=mock|http` (default `mock`).
- `KYC_BASE_URL` (default `http://localhost:8000`).
- `KYC_API_KEY` (default `change-me`; solo cabecera `X-API-Key`).
- `KYC_TIMEOUT_SECONDS` (default `3.0`).
- `KYC_MAX_RETRIES` (default `2`).
- `KYC_BACKOFF_BASE_SECONDS` (default `0.1`).
- `KYC_BREAKER_FAILURES` (default `3` fallos consecutivos para abrir).
- `KYC_BREAKER_COOLDOWN_SECONDS` (default `30.0`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- Errores tipados
KYC_UNAVAILABLE = "KYC_UNAVAILABLE"
KYC_INVALID = "KYC_INVALID"
KYC_TIMEOUT = "KYC_TIMEOUT"


class KycError(Exception):
    """Error base del adaptador; expone `code` para propagacion tipada."""

    code: str = KYC_UNAVAILABLE

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class KycUnavailableError(KycError):
    def __init__(self, message: str = "servicio KYC no disponible") -> None:
        super().__init__(KYC_UNAVAILABLE, message)


class KycInvalidError(KycError):
    def __init__(self, message: str = "solicitud KYC invalida") -> None:
        super().__init__(KYC_INVALID, message)


class KycTimeoutError(KycError):
    def __init__(self, message: str = "timeout contra el servicio KYC") -> None:
        super().__init__(KYC_TIMEOUT, message)


# ---------------------------------------------------------------- Resultados
@dataclass(frozen=True)
class Challenge:
    """Resultado de `challenge`: token + pasos (sin frames ni PII)."""

    challenge_token_hash: str
    steps: tuple[str, ...] = ()
    expires_in_seconds: int = 0
    raw_token: str = field(default="", repr=False, compare=False)

    def expose_token(self) -> str:
        """Token plano solo para el backend (jamas serializar al movil)."""
        return self.raw_token


@dataclass(frozen=True)
class LivenessEvaluation:
    session_id: str
    passed: bool
    score: float = 0.0


@dataclass(frozen=True)
class FullVerification:
    session_id: str
    overall_result: bool
    distance: float = 0.0
    detail_code: str = ""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- Interfaz
class KycProvider(ABC):
    """Interfaz comun del adaptador KYC."""

    @abstractmethod
    def challenge(self, *, session_id: str) -> Challenge:
        """`POST /api/v1/liveness/challenge`: obtiene token y pasos."""

    @abstractmethod
    def evaluate_liveness(
        self, *, session_id: str, task: str, payload: dict | None = None
    ) -> LivenessEvaluation:
        """`POST /api/v1/liveness/evaluate`: valida una tarea por vez."""

    @abstractmethod
    def verify_full(self, *, session_id: str, payload: dict | None = None) -> FullVerification:
        """`POST /api/v1/identity/verify-full`: resultado integral."""


# ---------------------------------------------------------------- Config
@dataclass(frozen=True)
class KycSettings:
    base_url: str = "http://localhost:8000"
    api_key: str = "change-me"
    timeout_seconds: float = 3.0
    max_retries: int = 2
    backoff_base_seconds: float = 0.1
    breaker_failures: int = 3
    breaker_cooldown_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> KycSettings:
        def _float(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default

        def _int(name: str, default: int) -> int:
            try:
                return int(float(os.environ.get(name, default)))
            except (TypeError, ValueError):
                return default

        return cls(
            base_url=os.environ.get("KYC_BASE_URL", "http://localhost:8000"),
            api_key=os.environ.get("KYC_API_KEY", "change-me"),
            timeout_seconds=_float("KYC_TIMEOUT_SECONDS", 3.0),
            max_retries=_int("KYC_MAX_RETRIES", 2),
            backoff_base_seconds=_float("KYC_BACKOFF_BASE_SECONDS", 0.1),
            breaker_failures=_int("KYC_BREAKER_FAILURES", 3),
            breaker_cooldown_seconds=_float("KYC_BREAKER_COOLDOWN_SECONDS", 30.0),
        )


# ---------------------------------------------------------------- Implementacion real
class HttpKycProvider(KycProvider):
    """Implementacion real sobre `httpx` (microservicio FastAPI existente)."""

    CHALLENGE_PATH = "/api/v1/liveness/challenge"
    EVALUATE_PATH = "/api/v1/liveness/evaluate"
    VERIFY_FULL_PATH = "/api/v1/identity/verify-full"

    def __init__(
        self,
        settings: KycSettings | None = None,
        client: httpx.Client | None = None,
        **overrides,
    ) -> None:
        base = settings or KycSettings.from_env()
        if overrides:
            data = {f: getattr(base, f) for f in base.__dataclass_fields__}
            data.update({k: v for k, v in overrides.items() if k in data})
            base = KycSettings(**data)
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

    def _check_circuit(self, session_id: str) -> None:
        if self.circuit_open:
            logger.warning("kyc circuit_open session_id=%s", session_id)
            raise KycUnavailableError("circuito KYC abierto tras caidas")

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_opened_at = None

    def _record_failure(self, session_id: str, code: str) -> None:
        self._consecutive_failures += 1
        logger.warning(
            "kyc failure code=%s session_id=%s consecutive=%d",
            code,
            session_id,
            self._consecutive_failures,
        )
        if self._consecutive_failures >= self.settings.breaker_failures:
            self._circuit_opened_at = time.monotonic()
            logger.error("kyc circuit opened session_id=%s", session_id)

    # -- http -----------------------------------------------------------
    def _client_or_default(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(
            base_url=self.settings.base_url,
            timeout=self.settings.timeout_seconds,
            headers={"X-API-Key": self.settings.api_key},
        )

    def _post(self, path: str, *, session_id: str, body: dict) -> dict:
        self._check_circuit(session_id)
        client = self._client_or_default()
        owns = self._owns_client and self._client is None
        last_error: Exception | None = None
        try:
            for attempt in range(self.settings.max_retries + 1):
                try:
                    # La key viaja solo en cabecera; el body lleva IDs y datos de tarea.
                    response = client.post(path, json=body)
                except (httpx.TimeoutException, TimeoutError) as exc:
                    last_error = exc
                    logger.warning("kyc timeout session_id=%s attempt=%d", session_id, attempt)
                    self._sleep(attempt)
                    continue
                except httpx.HTTPError as exc:  # red / conexion: reintentable
                    last_error = exc
                    logger.warning(
                        "kyc transport_error session_id=%s attempt=%d err=%s",
                        session_id,
                        attempt,
                        type(exc).__name__,
                    )
                    self._sleep(attempt)
                    continue
                if 200 <= response.status_code < 300:
                    self._record_success()
                    return response.json()
                if 400 <= response.status_code < 500:
                    logger.warning(
                        "kyc invalid code=%s session_id=%s status=%d",
                        KYC_INVALID,
                        session_id,
                        response.status_code,
                    )
                    raise KycInvalidError(f"KYC rechazo la solicitud ({response.status_code})")
                last_error = KycUnavailableError(f"KYC respondio {response.status_code}")
                logger.warning(
                    "kyc server_error session_id=%s status=%d attempt=%d",
                    session_id,
                    response.status_code,
                    attempt,
                )
                self._sleep(attempt)
        finally:
            if owns:
                client.close()
        if isinstance(last_error, (httpx.TimeoutException, TimeoutError)):
            self._record_failure(session_id, KYC_TIMEOUT)
            raise KycTimeoutError("timeout contra el servicio KYC") from last_error
        self._record_failure(session_id, KYC_UNAVAILABLE)
        raise KycUnavailableError("servicio KYC no disponible") from last_error

    def _sleep(self, attempt: int) -> None:
        delay = self.settings.backoff_base_seconds * (2**attempt)
        if delay > 0:
            time.sleep(delay)

    # -- interfaz -------------------------------------------------------
    def challenge(self, *, session_id: str) -> Challenge:
        data = self._post(
            self.CHALLENGE_PATH, session_id=session_id, body={"session_id": session_id}
        )
        token = str(data.get("challenge_token", ""))
        logger.info("kyc challenge_ok session_id=%s token_hash=%s", session_id, hash_token(token))
        return Challenge(
            challenge_token_hash=hash_token(token),
            steps=tuple(data.get("steps", [])),
            expires_in_seconds=int(data.get("expires_in_seconds", 0)),
            raw_token=token,
        )

    def evaluate_liveness(
        self, *, session_id: str, task: str, payload: dict | None = None
    ) -> LivenessEvaluation:
        body = {"session_id": session_id, "task": task, **(payload or {})}
        data = self._post(self.EVALUATE_PATH, session_id=session_id, body=body)
        result = LivenessEvaluation(
            session_id=session_id,
            passed=bool(data.get("passed", False)),
            score=float(data.get("score", 0.0)),
        )
        logger.info("kyc evaluate session_id=%s passed=%s", session_id, result.passed)
        return result

    def verify_full(self, *, session_id: str, payload: dict | None = None) -> FullVerification:
        body = {"session_id": session_id, **(payload or {})}
        data = self._post(self.VERIFY_FULL_PATH, session_id=session_id, body=body)
        result = FullVerification(
            session_id=session_id,
            overall_result=bool(data.get("overall_result", False)),
            distance=float(data.get("distance", 0.0)),
            detail_code=str(data.get("detail_code", "")),
        )
        logger.info(
            "kyc verify_full session_id=%s overall=%s code=%s",
            session_id,
            result.overall_result,
            result.detail_code,
        )
        return result


# ---------------------------------------------------------------- Mock determinista
class MockKycProvider(KycProvider):
    """Mock determinista configurable: `success` | `failure` | `timeout` | `down`.

    `success`/`failure` responden sin red; `timeout` lanza `KYC_TIMEOUT`;
    `down` lanza `KYC_UNAVAILABLE` (simula caida + circuito abierto).
    """

    MODES = ("success", "failure", "timeout", "down")

    def __init__(self, mode: str = "success") -> None:
        if mode not in self.MODES:
            raise ValueError(f"mode debe ser uno de {self.MODES}")
        self.mode = mode

    def _guard(self, session_id: str) -> None:
        if self.mode == "timeout":
            logger.warning("kyc mock_timeout session_id=%s", session_id)
            raise KycTimeoutError("timeout simulado del mock KYC")
        if self.mode == "down":
            logger.warning("kyc mock_down session_id=%s", session_id)
            raise KycUnavailableError("caida simulada del mock KYC")

    def challenge(self, *, session_id: str) -> Challenge:
        self._guard(session_id)
        token = f"mock-challenge-{session_id}"
        logger.info(
            "kyc mock_challenge session_id=%s token_hash=%s mode=%s",
            session_id,
            hash_token(token),
            self.mode,
        )
        return Challenge(
            challenge_token_hash=hash_token(token),
            steps=("blink", "turn-left", "smile"),
            expires_in_seconds=120,
            raw_token=token,
        )

    def evaluate_liveness(
        self, *, session_id: str, task: str, payload: dict | None = None
    ) -> LivenessEvaluation:
        self._guard(session_id)
        passed = self.mode == "success"
        logger.info("kyc mock_evaluate session_id=%s passed=%s", session_id, passed)
        return LivenessEvaluation(
            session_id=session_id, passed=passed, score=0.98 if passed else 0.12
        )

    def verify_full(self, *, session_id: str, payload: dict | None = None) -> FullVerification:
        self._guard(session_id)
        overall = self.mode == "success"
        logger.info("kyc mock_verify session_id=%s overall=%s", session_id, overall)
        return FullVerification(
            session_id=session_id,
            overall_result=overall,
            distance=0.21 if overall else 0.87,
            detail_code="OK" if overall else "LIVENESS_FAILED",
        )


# ---------------------------------------------------------------- Fabrica por entorno
def create_kyc_provider(kind: str | None = None, **overrides) -> KycProvider:
    """Fabrica: `KYC_PROVIDER=mock|http` (default `mock`).

    `http`/`real` devuelven `HttpKycProvider` (lee `KycSettings.from_env()`);
    cualquier otro valor devuelve `MockKycProvider` (`KYC_MOCK_MODE` o `success`).
    """
    selected = (kind or os.environ.get("KYC_PROVIDER", "mock")).strip().lower()
    if selected in ("http", "real", "remote"):
        return HttpKycProvider(settings=KycSettings.from_env(), **overrides)
    return MockKycProvider(mode=os.environ.get("KYC_MOCK_MODE", "success"))
