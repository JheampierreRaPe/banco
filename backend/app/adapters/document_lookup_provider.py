"""Adaptador DocumentLookupProvider (E1-T35, modulo identity).

Proxy de **consulta del titular** por documento contra la API externa
`https://app.apiinti.dev/api/v1` (`GET /dni/{numero}` y `GET /ruc/{numero}`),
distinto del proxy de **legibilidad** (`document/validate`, E1-T30).

Patron `kyc_provider.py`: interfaz comun + implementacion real
(`HttpDocumentLookupProvider`) + simulada (`MockDocumentLookupProvider`) +
fabrica por entorno (`DOC_LOOKUP_PROVIDER=mock|http|apiinti`, default `mock`).

Autenticacion fijada (decision del dueno): la llamada real envia
`Authorization: Bearer <APIINTI_API_KEY>` + `Content-Type: application/json`.
La key vive solo en el entorno del backend (`.env` de la raiz, cableada por
`docker-compose.yml` al servicio `backend`); jamas se expone al cliente ni
se registra en logs.

La forma exacta del JSON del proveedor NO esta confirmada: el parseo vive en
un UNICO punto (`_normalize_holder`), que tolera la forma comun
`{"data": {...}}` o plana y claves alternativas (`nombres`/`apellidos`/
`first_name`/`last_name`/`razon_social`/`business_name`, mas `apellido_paterno`/
`apellido_materno` como composicion del apellido). La forma exacta se fija al
probar contra el proveedor real.

Reglas (docs/02#9, docs/16 reglas de oro 7 y 10):
- Sin PII en logs: el numero de documento solo se correlaciona hasheado
  (`hash_document`); los nombres/apellidos/razon social, la key y la
  respuesta cruda NUNCA se loguean.
- Timeouts cortos, reintentos con backoff exponencial y circuit breaker.
- Errores neutros: `DocumentNotFoundError` (404) sin eco del numero;
  `DocumentLookupUnavailableError` / `DocumentLookupTimeoutError` sin
  filtrar el cuerpo del proveedor.

Configuracion por entorno via `Settings` (`backend/.env`, fuente unica):
- `DOC_LOOKUP_PROVIDER=mock|http|apiinti` (default `mock`; valor no reconocido -> mock).
- `DOC_LOOKUP_MOCK_MODE=success|not_found|down|timeout` (default `success`).
- `APIINTI_BASE_URL` (default `https://app.apiinti.dev/api/v1`).
- `APIINTI_API_KEY` (default vacio; solo cabecera `Authorization: Bearer`).
- `DOC_LOOKUP_TIMEOUT_SECONDS` (default `5.0`).
- `DOC_LOOKUP_MAX_RETRIES` (default `2`).
- `DOC_LOOKUP_BACKOFF_BASE_SECONDS` (default `0.1`).
- `DOC_LOOKUP_BREAKER_FAILURES` (default `3` fallos consecutivos para abrir).
- `DOC_LOOKUP_BREAKER_COOLDOWN_SECONDS` (default `30.0`).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _silence_httpx_url_logs():
    """Evita que httpx registre la URL en sus logs INFO.

    El path (`/dni/{numero}`, `/ruc/{numero}`) contiene el numero de
    documento en claro y httpx lo incluiria en su linea `HTTP Request`
    (regla de oro 7: sin PII en logs). Solo eleva el nivel durante la
    llamada y lo restaura despues; no afecta a otros loggers.
    """
    httpx_logger = logging.getLogger("httpx")
    previous = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        httpx_logger.setLevel(previous)


# ---------------------------------------------------------------- Errores tipados
DOC_LOOKUP_NOT_FOUND = "DOCUMENT_NOT_FOUND"
DOC_LOOKUP_UNAVAILABLE = "DOC_LOOKUP_UNAVAILABLE"
DOC_LOOKUP_TIMEOUT = "DOC_LOOKUP_TIMEOUT"


class DocLookupError(Exception):
    """Error base del adaptador; expone `code` para propagacion tipada."""

    code: str = DOC_LOOKUP_UNAVAILABLE

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DocumentNotFoundError(DocLookupError):
    """Sin datos para el documento (mensaje neutro, sin eco del numero)."""

    def __init__(self, message: str = "sin datos para el documento indicado") -> None:
        super().__init__(DOC_LOOKUP_NOT_FOUND, message)


class DocumentLookupUnavailableError(DocLookupError):
    def __init__(self, message: str = "servicio de consulta no disponible") -> None:
        super().__init__(DOC_LOOKUP_UNAVAILABLE, message)


class DocumentLookupTimeoutError(DocLookupError):
    def __init__(self, message: str = "timeout contra el servicio de consulta") -> None:
        super().__init__(DOC_LOOKUP_TIMEOUT, message)


# ---------------------------------------------------------------- Resultado
@dataclass(frozen=True)
class DocumentHolder:
    """Titular normalizado (sin PII mas alla de lo devuelto al cliente).

    Persona natural: `first_name`/`last_name` con valores y `business_name`
    vacio. RUC de persona juridica: `business_name` con la razon social y
    `first_name`/`last_name` vacios.
    """

    session_id: str
    document_type: str
    first_name: str = ""
    last_name: str = ""
    business_name: str = ""


def hash_document(number: str) -> str:
    """Hash corto del numero de documento para correlacion en logs (sin PII)."""
    return hashlib.sha256(str(number or "").encode("utf-8")).hexdigest()[:16]


def clean_text(value: object) -> str:
    """Normaliza texto: `trim` + colapso de espacios; no-strings -> vacio."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


#: Alias historico de `clean_text` (compatibilidad con imports previos).
_clean = clean_text


#: Claves alternativas aceptadas por el punto unico de parseo (forma exacta
#: del JSON de apiinti aun sin confirmar; se fija contra el proveedor real).
_FIRST_NAME_KEYS: tuple[str, ...] = ("first_name", "firstname", "nombres", "nombre", "given_name")
_LAST_NAME_KEYS: tuple[str, ...] = ("last_name", "lastname", "apellidos", "apellido", "family_name")
_BUSINESS_NAME_KEYS: tuple[str, ...] = (
    "business_name",
    "razon_social",
    "razonsocial",
    "company_name",
    "denominacion",
)


def _first_present(payload: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        cleaned = clean_text(payload.get(key))
        if cleaned:
            return cleaned
    return ""


def _normalize_holder(data: object, session_id: str, doc_type: str) -> DocumentHolder:
    """UNICO punto de parseo de la respuesta del proveedor.

    Acepta la forma envuelta `{"data": {...}}` o plana; mapea las claves
    alternativas a `first_name`/`last_name`/`business_name` (persona juridica
    = razon social). El apellido tambien se compone desde `apellido_paterno`
    + `apellido_materno` cuando no hay clave directa. Sin datos utilizables
    lanza `DocumentNotFoundError` (neutro, sin eco del numero).
    """
    payload: dict = data if isinstance(data, dict) else {}
    nested = payload.get("data")
    if isinstance(nested, dict):
        payload = nested
    first_name = _first_present(payload, _FIRST_NAME_KEYS)
    last_name = _first_present(payload, _LAST_NAME_KEYS)
    if not last_name:
        composed = " ".join(
            part
            for part in (
                clean_text(payload.get("apellido_paterno")),
                clean_text(payload.get("apellido_materno")),
            )
            if part
        )
        last_name = composed
    business_name = _first_present(payload, _BUSINESS_NAME_KEYS)
    if not first_name and not last_name and not business_name:
        raise DocumentNotFoundError()
    return DocumentHolder(
        session_id=session_id,
        document_type=doc_type,
        first_name=first_name,
        last_name=last_name,
        business_name=business_name,
    )


# ---------------------------------------------------------------- Interfaz
class DocumentLookupProvider(ABC):
    """Interfaz comun del adaptador de consulta del titular."""

    @abstractmethod
    def lookup_document(self, *, session_id: str, doc_type: str, number: str) -> DocumentHolder:
        """`GET /dni/{numero}` o `GET /ruc/{numero}` segun `doc_type`."""


# ---------------------------------------------------------------- Config
@dataclass(frozen=True)
class DocLookupSettings:
    base_url: str = "https://app.apiinti.dev/api/v1"
    api_key: str = ""
    timeout_seconds: float = 5.0
    max_retries: int = 2
    backoff_base_seconds: float = 0.1
    breaker_failures: int = 3
    breaker_cooldown_seconds: float = 30.0

    @classmethod
    def from_settings(cls, settings) -> DocLookupSettings:
        """Construye desde `Settings` (fuente unica: `backend/.env`)."""
        return cls(
            base_url=settings.apiinti_base_url,
            api_key=settings.apiinti_api_key,
            timeout_seconds=float(settings.doc_lookup_timeout_seconds),
            max_retries=int(settings.doc_lookup_max_retries),
            backoff_base_seconds=float(settings.doc_lookup_backoff_base_seconds),
            breaker_failures=int(settings.doc_lookup_breaker_failures),
            breaker_cooldown_seconds=float(settings.doc_lookup_breaker_cooldown_seconds),
        )

    @classmethod
    def from_env(cls) -> DocLookupSettings:
        """Compatibilidad: deriva de `Settings` (no lee `os.environ` crudo)."""
        from app.core.config import Settings

        return cls.from_settings(Settings())


# ---------------------------------------------------------------- Implementacion real
class HttpDocumentLookupProvider(DocumentLookupProvider):
    """Implementacion real sobre `httpx` (API externa apiinti)."""

    DNI_PATH = "/dni/{number}"
    RUC_PATH = "/ruc/{number}"

    def __init__(
        self,
        settings: DocLookupSettings | None = None,
        client: httpx.Client | None = None,
        **overrides,
    ) -> None:
        base = settings or DocLookupSettings.from_env()
        if overrides:
            data = {f: getattr(base, f) for f in base.__dataclass_fields__}
            data.update({k: v for k, v in overrides.items() if k in data})
            base = DocLookupSettings(**data)
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
            logger.warning("doc_lookup circuit_open session_id=%s", session_id)
            raise DocumentLookupUnavailableError("circuito de consulta abierto tras caidas")

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_opened_at = None

    def _record_failure(self, session_id: str, code: str) -> None:
        self._consecutive_failures += 1
        logger.warning(
            "doc_lookup failure code=%s session_id=%s consecutive=%d",
            code,
            session_id,
            self._consecutive_failures,
        )
        if self._consecutive_failures >= self.settings.breaker_failures:
            self._circuit_opened_at = time.monotonic()
            logger.error("doc_lookup circuit opened session_id=%s", session_id)

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
        # Se envian por request (no solo al crear el cliente) para que
        # apliquen tambien con un cliente inyectado (pruebas).
        return {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, *, session_id: str, doc_hash: str) -> dict:
        self._check_circuit(session_id)
        client = self._client_or_default()
        owns = self._owns_client and self._client is None
        last_error: Exception | None = None
        try:
            with _silence_httpx_url_logs():
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
                            "doc_lookup timeout session_id=%s doc_hash=%s attempt=%d",
                            session_id,
                            doc_hash,
                            attempt,
                        )
                        self._sleep(attempt)
                        continue
                    except httpx.HTTPError as exc:  # red / conexion: reintentable
                        last_error = exc
                        logger.warning(
                            "doc_lookup transport_error session_id=%s doc_hash=%s "
                            "attempt=%d err=%s",
                            session_id,
                            doc_hash,
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
                        # 404 neutro: sin eco del numero ni del cuerpo del proveedor.
                        logger.info(
                            "doc_lookup not_found session_id=%s doc_hash=%s",
                            session_id,
                            doc_hash,
                        )
                        raise DocumentNotFoundError()
                    # Otros 4xx/5xx: no se filtra el cuerpo del proveedor.
                    last_error = DocumentLookupUnavailableError(
                        f"proveedor respondio {response.status_code}"
                    )
                    logger.warning(
                        "doc_lookup server_error session_id=%s doc_hash=%s status=%d attempt=%d",
                        session_id,
                        doc_hash,
                        response.status_code,
                        attempt,
                    )
                    self._sleep(attempt)
        finally:
            if owns:
                client.close()
        if isinstance(last_error, (httpx.TimeoutException, TimeoutError)):
            self._record_failure(session_id, DOC_LOOKUP_TIMEOUT)
            raise DocumentLookupTimeoutError(
                "timeout contra el servicio de consulta"
            ) from last_error
        self._record_failure(session_id, DOC_LOOKUP_UNAVAILABLE)
        raise DocumentLookupUnavailableError("servicio de consulta no disponible") from last_error

    def _sleep(self, attempt: int) -> None:
        delay = self.settings.backoff_base_seconds * (2**attempt)
        if delay > 0:
            time.sleep(delay)

    # -- interfaz -------------------------------------------------------
    def lookup_document(self, *, session_id: str, doc_type: str, number: str) -> DocumentHolder:
        kind = (doc_type or "").strip().upper()
        path = (
            self.RUC_PATH.format(number=number)
            if kind == "RUC"
            else self.DNI_PATH.format(number=number)
        )
        doc_hash = hash_document(number)
        data = self._get(path, session_id=session_id, doc_hash=doc_hash)
        holder = _normalize_holder(data, session_id, kind)
        # Solo tipo + hash en logs: nunca nombres, numero ni respuesta cruda.
        logger.info(
            "doc_lookup ok session_id=%s doc_type=%s doc_hash=%s has_business=%s",
            session_id,
            kind,
            doc_hash,
            bool(holder.business_name),
        )
        return holder


# ---------------------------------------------------------------- Mock determinista
class MockDocumentLookupProvider(DocumentLookupProvider):
    """Mock determinista configurable: `success` | `not_found` | `down` | `timeout`.

    `success` responde sin red: DNI -> persona natural (`first_name`/
    `last_name`); RUC -> persona juridica (`business_name`, razon social).
    `not_found` lanza `DocumentNotFoundError`; `down` lanza
    `DocumentLookupUnavailableError`; `timeout` lanza
    `DocumentLookupTimeoutError`.
    """

    MODES = ("success", "not_found", "down", "timeout")

    def __init__(self, mode: str = "success") -> None:
        if mode not in self.MODES:
            raise ValueError(f"mode debe ser uno de {self.MODES}")
        self.mode = mode

    def lookup_document(self, *, session_id: str, doc_type: str, number: str) -> DocumentHolder:
        doc_hash = hash_document(number)
        kind = (doc_type or "").strip().upper()
        if self.mode == "timeout":
            logger.warning(
                "doc_lookup mock_timeout session_id=%s doc_hash=%s", session_id, doc_hash
            )
            raise DocumentLookupTimeoutError("timeout simulado de la consulta")
        if self.mode == "down":
            logger.warning("doc_lookup mock_down session_id=%s doc_hash=%s", session_id, doc_hash)
            raise DocumentLookupUnavailableError("caida simulada de la consulta")
        if self.mode == "not_found":
            logger.info("doc_lookup mock_not_found session_id=%s doc_hash=%s", session_id, doc_hash)
            raise DocumentNotFoundError()
        logger.info(
            "doc_lookup mock_ok session_id=%s doc_type=%s doc_hash=%s mode=%s",
            session_id,
            kind,
            doc_hash,
            self.mode,
        )
        if kind == "RUC":
            return DocumentHolder(
                session_id=session_id,
                document_type="RUC",
                first_name="",
                last_name="",
                business_name="EMPRESA EJEMPLO S.A.C.",
            )
        return DocumentHolder(
            session_id=session_id,
            document_type="DNI",
            first_name="Juan",
            last_name="Perez Gomez",
            business_name="",
        )


# ---------------------------------------------------------------- Fabrica por entorno
def create_document_lookup_provider(kind: str | None = None, **overrides) -> DocumentLookupProvider:
    """Fabrica: `DOC_LOOKUP_PROVIDER=mock|http` (default `mock`).

    Fuente unica: `Settings` (`backend/.env`); `http`/`real`/`remote`/`apiinti`
    devuelven `HttpDocumentLookupProvider`; cualquier otro valor devuelve
    `MockDocumentLookupProvider` (`doc_lookup_mock_mode` o `success`).
    """
    from app.core.config import Settings

    settings = Settings()
    selected = (kind if kind is not None else settings.doc_lookup_provider).strip().lower()
    if selected in ("http", "real", "remote", "apiinti"):
        return HttpDocumentLookupProvider(
            settings=DocLookupSettings.from_settings(settings), **overrides
        )
    return MockDocumentLookupProvider(mode=settings.doc_lookup_mock_mode or "success")


__all__ = [
    "DOC_LOOKUP_NOT_FOUND",
    "DOC_LOOKUP_TIMEOUT",
    "DOC_LOOKUP_UNAVAILABLE",
    "DocLookupError",
    "DocLookupSettings",
    "DocumentHolder",
    "DocumentLookupProvider",
    "DocumentLookupTimeoutError",
    "DocumentLookupUnavailableError",
    "DocumentNotFoundError",
    "HttpDocumentLookupProvider",
    "MockDocumentLookupProvider",
    "clean_text",
    "create_document_lookup_provider",
    "hash_document",
]
