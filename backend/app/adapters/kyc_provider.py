"""Adaptador KycProvider (E1-T01, modulo identity).

Cliente hacia el microservicio KYC existente (solo onboarding HU01), con
interfaz comun e implementaciones real (`HttpKycProvider`) y simulada
(`MockKycProvider`).

Contrato consumido (`docs/05-contratos-api.md#8`, prefijo `/api/v1`):
- `POST /api/v1/liveness/challenge`    -> token + secuencia de pasos.
- `POST /api/v1/liveness/evaluate`     -> validacion de una tarea.
- `POST /api/v1/identity/verify-full`  -> resultado integral del KYC.
- `POST /api/v1/document/validate`            -> legibilidad/aceptacion del documento
  (multipart `file`) -> `{is_valid, issues, checks}` (E1-T30).

Contrato REAL del microservicio (verificado 2026-09-18 contra
`http://localhost:8001`, rama `eliminar` del repo KYC):
- challenge: request con cuerpo VACIO (el endpoint no acepta JSON; enviar
  `{"session_id": ...}` devuelve 422) -> `{token, steps, expires_in}`.
- evaluate: JSON `{token, step, frames_base64[]}` (solo MediaPipe, <1s)
  -> `{step, passed, reason, frames_analyzed, details}`.
- verify-full: `multipart/form-data` con `document_image` (bytes del
  documento) + `liveness_frames` (string JSON `{token, segments}` donde
  `segments` es `{paso: [b64, ...]}`; el desafio debe estar completado)
  -> `{document_validation, liveness, identity_consistency, face_match,
  overall_result, overall_reason}` (se mapean `distance` desde
  `face_match.distance` y `detail_code` desde `overall_reason`).

Compatibilidad: se aceptan con fallback los nombres legacy de los tests
(`challenge_token`/`expires_in_seconds`, `score`, `distance`/`detail_code`
planos y `verify_full` JSON sin imagenes) para no romper E1-T01/E1-T02.

Reglas (docs/02-arquitectura.md#9, docs/16 reglas de oro):
- La `X-API-Key` vive solo en el backend; nunca se expone al movil ni se
  incluye en los retornos del adaptador.
- No se persisten frames ni imagenes: solo resultados + hash del token.
- Sin PII en logs: solo IDs y codigos (el token se registra hasheado).
- Timeouts cortos, 2 reintentos con backoff exponencial y circuit breaker.

Los errores 4xx del microservicio ya no se colapsan: se parsea
`response.json()["detail"]` (string o dict) y se propaga `step`/`reason` en
`KycInvalidError` para que el proxy exponga el paso/motivo exacto.

Configuracion por entorno (defaults documentados):
- `KYC_PROVIDER=mock|http` (default `mock`).
- `KYC_BASE_URL` (default `http://localhost:8000`).
- `KYC_API_KEY` (default `change-me`; solo cabecera `X-API-Key`).
- `KYC_TIMEOUT_SECONDS` (default `3.0`; challenge/evaluate, MediaPipe <1s).
- `KYC_VERIFY_TIMEOUT_SECONDS` (default `60.0`; `verify-full` procesa muchos
  frames con DeepFace y necesita mas margen que el challenge).
- `KYC_DOCUMENT_TIMEOUT_SECONDS` (default `15.0`; `/api/v1/document/validate` analiza
  la imagen del documento, mas lento que el challenge/evaluate).
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
    """4xx del microservicio; conserva `step`/`reason` del `detail`."""

    def __init__(
        self,
        message: str = "solicitud KYC invalida",
        *,
        step: str = "",
        reason: str = "",
    ) -> None:
        super().__init__(KYC_INVALID, message)
        self.step = step
        self.reason = reason


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
    step: str = ""
    reason: str = ""
    frames_analyzed: int = 0
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FullVerification:
    session_id: str
    overall_result: bool
    distance: float = 0.0
    detail_code: str = ""
    steps_verified: list[str] = field(default_factory=list)
    steps_total: list[str] = field(default_factory=list)
    failed_step: str | None = None
    step_results: dict = field(default_factory=dict)
    overall_reason: str = ""


@dataclass(frozen=True)
class DocumentValidation:
    """Resultado de `/api/v1/document/validate` (E1-T30).

    `is_valid=False` NO es un error: viaja con los `issues` para que la app
    decida bloquear/reintentar. `checks` es tolerante a formas inesperadas
    (siempre `dict`). No contiene imagenes ni PII.
    """

    session_id: str
    is_valid: bool
    issues: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)


def _as_str_list(raw: object) -> list[str]:
    """Normaliza `steps_verified`/`steps_total` a lista de nombres de paso.

    El microservicio (`liveness_service.py`) devuelve LISTAS de nombres
    (`steps_total=list(required_steps)`, `steps_verified=passed_steps`), no
    conteos. Nunca se hace `int()` sobre ellas; una forma inesperada (p.ej. un
    int legacy) se ignora sin romper el submit.
    """
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    return []


def _normalize_step_results(raw: object) -> dict:
    """Normaliza `step_results` (dict `{paso: {...}}` o lista de pasos)."""
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    if isinstance(raw, list):
        normalized: dict = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("step", item.get("task", ""))).strip()
            if name:
                normalized[name] = item
        return normalized
    return {}


def _first_failed_step(step_results: dict) -> str | None:
    for name, result in step_results.items():
        if isinstance(result, dict) and not result.get("passed", True):
            return str(name)
        if result is False:
            return str(name)
    return None


def _extract_invalid_detail(response: httpx.Response) -> tuple[str, str]:
    """Extrae `(step, reason)` del `detail` de un 4xx, sin filtrar internos."""
    try:
        payload = response.json()
    except ValueError:
        return "", ""
    detail = payload.get("detail") if isinstance(payload, dict) else payload
    if isinstance(detail, dict):
        step = str(detail.get("step", detail.get("task", "")) or "").strip()
        reason = str(
            detail.get("reason", detail.get("message", detail.get("detail", ""))) or ""
        ).strip()
        return step, reason
    if isinstance(detail, str):
        return "", detail.strip()
    return "", ""


def _normalize_document_validation(data: object, session_id: str) -> DocumentValidation:
    """Normaliza la respuesta de `/api/v1/document/validate`.

    Acepta la forma plana `{is_valid, issues, checks}` o envuelta en
    `{"data": {...}}`; tolera `issues`/`checks` con formas inesperadas sin
    romper (`list[str]` / `dict`).
    """
    payload: dict = data if isinstance(data, dict) else {}
    nested = payload.get("data")
    if isinstance(nested, dict):
        payload = nested
    checks = payload.get("checks")
    return DocumentValidation(
        session_id=session_id,
        is_valid=bool(payload.get("is_valid", payload.get("valid", False))),
        issues=_as_str_list(payload.get("issues")),
        checks=checks if isinstance(checks, dict) else {},
    )


def _image_file(raw: bytes) -> tuple[str, bytes, str]:
    """Nombre + MIME segun magic bytes (JPEG/PNG) para el `file` multipart."""
    if raw.startswith(b"\xff\xd8\xff"):
        return "document.jpg", raw, "image/jpeg"
    return "document.png", raw, "image/png"


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

    @abstractmethod
    def validate_document(self, *, session_id: str, image_b64: str = "") -> DocumentValidation:
        """`POST /api/v1/document/validate`: legibilidad/aceptacion del documento."""


# ---------------------------------------------------------------- Config
@dataclass(frozen=True)
class KycSettings:
    base_url: str = "http://localhost:8000"
    api_key: str = "change-me"
    timeout_seconds: float = 3.0
    verify_timeout_seconds: float = 60.0
    document_timeout_seconds: float = 15.0
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
            verify_timeout_seconds=_float("KYC_VERIFY_TIMEOUT_SECONDS", 60.0),
            document_timeout_seconds=_float("KYC_DOCUMENT_TIMEOUT_SECONDS", 15.0),
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
    DOCUMENT_VALIDATE_PATH = "/api/v1/document/validate"

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

    def _post(
        self,
        path: str,
        *,
        session_id: str,
        body: dict | None = None,
        files: dict | None = None,
        data: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        self._check_circuit(session_id)
        client = self._client_or_default()
        request_timeout = timeout if timeout is not None else self.settings.timeout_seconds
        owns = self._owns_client and self._client is None
        last_error: Exception | None = None
        try:
            for attempt in range(self.settings.max_retries + 1):
                try:
                    # La key viaja solo en cabecera; el body lleva IDs y datos de tarea.
                    # `challenge` real exige cuerpo vacio (sin `json=`); `verify-full`
                    # real usa multipart (`files` + `data`).
                    if files is not None:
                        response = client.post(
                            path, files=files, data=data, timeout=request_timeout
                        )
                    elif body is not None:
                        response = client.post(path, json=body, timeout=request_timeout)
                    else:
                        response = client.post(path, timeout=request_timeout)
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
                    step, reason = _extract_invalid_detail(response)
                    logger.warning(
                        "kyc invalid code=%s session_id=%s status=%d step=%s has_reason=%s",
                        KYC_INVALID,
                        session_id,
                        response.status_code,
                        step or "-",
                        bool(reason),
                    )
                    raise KycInvalidError(
                        f"KYC rechazo la solicitud ({response.status_code})",
                        step=step,
                        reason=reason,
                    )
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
        # Microservicio real: POST sin cuerpo -> {token, steps, expires_in}.
        # (Fallback legacy: {challenge_token, steps, expires_in_seconds}.)
        data = self._post(self.CHALLENGE_PATH, session_id=session_id, body=None)
        token = str(data.get("token", data.get("challenge_token", "")))
        logger.info("kyc challenge_ok session_id=%s token_hash=%s", session_id, hash_token(token))
        return Challenge(
            challenge_token_hash=hash_token(token),
            steps=tuple(data.get("steps", [])),
            expires_in_seconds=int(data.get("expires_in", data.get("expires_in_seconds", 0))),
            raw_token=token,
        )

    def evaluate_liveness(
        self, *, session_id: str, task: str, payload: dict | None = None
    ) -> LivenessEvaluation:
        # Microservicio real: {token, step, frames_base64[]} -> {passed, ...}.
        # El token viaja en `payload["token"]` o, por compatibilidad con el
        # proxy (`verify_full(session_id=<token>)`), se reusa `session_id`.
        extra = payload or {}
        frames = extra.get("frames_base64", extra.get("frames", extra.get("frames_b64", [])))
        token = str(extra.get("token", extra.get("challenge_token", session_id)))
        body = {"token": token, "step": task, "frames_base64": list(frames)}
        data = self._post(self.EVALUATE_PATH, session_id=session_id, body=body)
        details = data.get("details")
        result = LivenessEvaluation(
            session_id=session_id,
            passed=bool(data.get("passed", data.get("is_live", False))),
            score=float(data.get("score", 0.0)),
            step=str(data.get("step", task) or task),
            reason=str(data.get("reason", "") or ""),
            frames_analyzed=int(data.get("frames_analyzed", 0) or 0),
            details=details if isinstance(details, dict) else {},
        )
        logger.info(
            "kyc evaluate session_id=%s step=%s passed=%s frames=%d",
            session_id,
            result.step,
            result.passed,
            result.frames_analyzed,
        )
        return result

    def verify_full(self, *, session_id: str, payload: dict | None = None) -> FullVerification:
        # Microservicio real: multipart document_image + liveness_frames JSON
        # {token, segments:{paso:[b64]}} (desafio ya completado via /evaluate).
        # Sin imagenes (tests/mock): JSON legacy {session_id, ...}.
        extra = dict(payload or {})
        doc_b64 = str(
            extra.pop("document_image_b64", extra.pop("document_image", "")) or ""
        ).strip()
        segments = extra.pop("segments", [])
        data: dict
        if doc_b64 and segments:
            import base64 as _b64
            import json as _json

            seg_map: dict[str, list[str]] = {}
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                name = str(segment.get("task", segment.get("step", ""))).strip()
                if not name:
                    continue
                # Rafaga (E1-T29): varios frames por tarea, en orden. Si
                # `frames_b64` viene, es la fuente unica (no se concatena
                # `image_b64`, que duplicaria el primer frame); `image_b64`/
                # `frame_b64` solo se usan como fallback de un frame unico.
                burst = segment.get("frames_b64", segment.get("frames"))
                frames: list[str] = []
                if isinstance(burst, list):
                    for frame in burst:
                        value = str(frame or "").strip()
                        if value:
                            frames.append(value)
                if not frames:
                    image = str(
                        segment.get("image_b64", segment.get("frame_b64", "")) or ""
                    ).strip()
                    if image:
                        frames.append(image)
                if frames:
                    seg_map.setdefault(name, []).extend(frames)
            try:
                doc_bytes = _b64.b64decode(doc_b64, validate=True)
            except ValueError:
                doc_bytes = _b64.b64decode(doc_b64)
            data = self._post(
                self.VERIFY_FULL_PATH,
                session_id=session_id,
                files={"document_image": ("document.png", doc_bytes, "image/png")},
                data={"liveness_frames": _json.dumps({"token": session_id, "segments": seg_map})},
                timeout=self.settings.verify_timeout_seconds,
            )
        else:
            body = {"session_id": session_id, **extra}
            data = self._post(
                self.VERIFY_FULL_PATH,
                session_id=session_id,
                body=body,
                timeout=self.settings.verify_timeout_seconds,
            )
        face_match = data.get("face_match")
        if not isinstance(face_match, dict):
            face_match = {}
        liveness = data.get("liveness")
        if not isinstance(liveness, dict):
            liveness = {}
        step_results = _normalize_step_results(liveness.get("step_results"))
        overall_reason = str(data.get("overall_reason", "") or "")
        failed_step = data.get("failed_step")
        if failed_step is not None:
            failed_step = str(failed_step) or None
        if failed_step is None:
            failed_step = _first_failed_step(step_results)
        result = FullVerification(
            session_id=session_id,
            overall_result=bool(data.get("overall_result", False)),
            distance=float(data.get("distance", face_match.get("distance", 0.0))),
            detail_code=str(data.get("detail_code", data.get("overall_reason", ""))),
            steps_verified=_as_str_list(liveness.get("steps_verified")),
            steps_total=_as_str_list(liveness.get("steps_total")),
            failed_step=failed_step,
            step_results=step_results,
            overall_reason=overall_reason,
        )
        logger.info(
            "kyc verify_full session_id=%s overall=%s code=%s steps=%d/%d failed=%s",
            session_id,
            result.overall_result,
            result.detail_code,
            len(result.steps_verified),
            len(result.steps_total),
            result.failed_step or "-",
        )
        return result

    def validate_document(self, *, session_id: str, image_b64: str = "") -> DocumentValidation:
        # Decodifica base64 y reenvia como `file` multipart al microservicio.
        # Los bytes viven solo como variable local; no se persisten ni loguean.
        import base64 as _b64

        try:
            raw = _b64.b64decode(str(image_b64 or "").strip(), validate=True)
        except ValueError:
            raw = _b64.b64decode(str(image_b64 or "").strip())
        filename, content, content_type = _image_file(raw)
        data = self._post(
            self.DOCUMENT_VALIDATE_PATH,
            session_id=session_id,
            files={"file": (filename, content, content_type)},
            timeout=self.settings.document_timeout_seconds,
        )
        result = _normalize_document_validation(data, session_id)
        logger.info(
            "kyc validate_document session_id=%s is_valid=%s issues=%d bytes=%d",
            session_id,
            result.is_valid,
            len(result.issues),
            len(raw),
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
        frames = (payload or {}).get("frames_base64", [])
        frames_analyzed = len(frames) if isinstance(frames, list) else 0
        logger.info("kyc mock_evaluate session_id=%s passed=%s", session_id, passed)
        return LivenessEvaluation(
            session_id=session_id,
            passed=passed,
            score=0.98 if passed else 0.12,
            step=task,
            reason="ok" if passed else "liveness_failed",
            frames_analyzed=frames_analyzed,
            details={},
        )

    def verify_full(self, *, session_id: str, payload: dict | None = None) -> FullVerification:
        self._guard(session_id)
        overall = self.mode == "success"
        steps = ("blink", "turn-left", "smile")
        step_results = {
            step: {
                "passed": overall,
                "reason": "ok" if overall else "liveness_failed",
                "details": {},
            }
            for step in steps
        }
        logger.info("kyc mock_verify session_id=%s overall=%s", session_id, overall)
        return FullVerification(
            session_id=session_id,
            overall_result=overall,
            distance=0.21 if overall else 0.87,
            detail_code="OK" if overall else "LIVENESS_FAILED",
            steps_verified=list(steps) if overall else [],
            steps_total=list(steps),
            failed_step=None if overall else steps[0],
            step_results=step_results,
            overall_reason="OK" if overall else "LIVENESS_FAILED",
        )

    def validate_document(self, *, session_id: str, image_b64: str = "") -> DocumentValidation:
        self._guard(session_id)
        valid = self.mode == "success"
        logger.info("kyc mock_validate_document session_id=%s is_valid=%s", session_id, valid)
        return DocumentValidation(
            session_id=session_id,
            is_valid=valid,
            issues=[] if valid else ["document_unreadable", "low_resolution"],
            checks={"legible": valid, "glare": not valid},
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
