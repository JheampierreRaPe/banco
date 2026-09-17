"""Orquestacion del proxy KYC pre-registro (E1-T02, HU01 CA-01/CA-02).

Delega en el adaptador `KycProvider` (E1-T01): el backend genera un
`session_id` propio y pide el desafio al microservicio (o al mock); el
**token opaco lo emite el proveedor**, no este modulo. No se persiste el
token en claro (solo su hash en logs via `hash_token` del adaptador) y el
TTL corto lo dicta el proveedor (`expires_in_seconds`).

`submit`: valida documento + segmentos (base64 valido, tamano maximo y
magic bytes JPEG/PNG) ANTES de reenviar; reenvia al adaptador
(`verify_full`) y descarta las imagenes (variables locales, sin
persistencia, sin logs con PII/frames). La `X-API-Key` vive solo en el
adaptador/backend y jamas sale hacia el movil.

Rate limit en memoria (ventana + maximo por env): suficiente para el MVP
monolito; en produccion multirreplica va en Redis/middleware (ver
`check_rate_limit`).

No toca `onboard_customer` (E1-T03) ni otros modulos: sin imports de
`accounts`/`ledger` aqui.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import threading
import time
import uuid

from app.adapters.kyc_provider import KycProvider, hash_token

logger = logging.getLogger(__name__)

#: Tipos de documento aceptados (validacion temprana, espejo del schema).
DOC_TYPES: tuple[str, ...] = ("DNI", "CE", "PASSPORT")

#: Magic bytes aceptados: JPEG (FF D8 FF) y PNG (89 50 4E 47 0D 0A 1A 0A).
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def max_image_bytes() -> int:
    """Tamano maximo por imagen (bytes). Default 2 MiB (`KYC_MAX_IMAGE_BYTES`)."""
    return _env_int("KYC_MAX_IMAGE_BYTES", 2 * 1024 * 1024)


def max_segments() -> int:
    """Maximo de segmentos por submit (`KYC_MAX_SEGMENTS`, default 10)."""
    return _env_int("KYC_MAX_SEGMENTS", 10)


class KycProxyValidationError(ValueError):
    """Payload KYC invalido (tamano/formato/contenido) -> HTTP 422."""


class KycRateLimitedError(RuntimeError):
    """Ventana de rate limit excedida -> HTTP 429."""


def validate_image_b64(image_b64: str, *, field: str) -> bytes:
    """Decodifica y valida una imagen (tamano + magic bytes JPEG/PNG).

    Retorna los bytes crudos para reenviar al adaptador; el llamante los
    descarta tras el reenvio (sin persistencia). Sin PII en logs: solo
    campo y tamano.
    """
    if not isinstance(image_b64, str) or not image_b64.strip():
        raise KycProxyValidationError(f"{field}: imagen vacia")
    try:
        raw = base64.b64decode(image_b64.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KycProxyValidationError(f"{field}: base64 invalido") from exc
    limit = max_image_bytes()
    if len(raw) > limit:
        logger.warning("kyc image_rejected field=%s size=%d limit=%d", field, len(raw), limit)
        raise KycProxyValidationError(f"{field}: supera {limit} bytes")
    if not raw.startswith((_JPEG_MAGIC, _PNG_MAGIC)):
        logger.warning("kyc image_rejected field=%s size=%d reason=bad_magic", field, len(raw))
        raise KycProxyValidationError(f"{field}: formato no soportado (solo JPEG/PNG)")
    return raw


# ------------------------------------------------------- Rate limit en memoria
_BUCKETS: dict[str, list[float]] = {}
_BUCKETS_LOCK = threading.Lock()


def rate_limit_cfg() -> tuple[float, int]:
    """`(ventana_s, maximo)` desde env (defaults 60 s / 30 req)."""
    return (
        _env_float("KYC_RATE_LIMIT_WINDOW_SECONDS", 60.0),
        _env_int("KYC_RATE_LIMIT_MAX_REQUESTS", 30),
    )


def build_rate_key(client_ip: str, device_id: str | None) -> str:
    """Clave por IP/dispositivo (hash: sin PII en el mapa)."""
    raw = f"{client_ip or 'unknown'}|{(device_id or '').strip() or '-'}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def check_rate_limit(key: str) -> None:
    """Ventana deslizante en memoria; excederla lanza `KycRateLimitedError`.

    NOTA produccion: con multirreplica este mapa local no se comparte;
    mover a Redis o a un middleware de rate limiting (misma clave).
    """
    window_s, maximum = rate_limit_cfg()
    now = time.monotonic()
    with _BUCKETS_LOCK:
        hits = [t for t in _BUCKETS.get(key, []) if now - t < window_s]
        if len(hits) >= maximum:
            _BUCKETS[key] = hits
            logger.warning("kyc rate_limited key_hash=%s hits=%d", key[:8], len(hits))
            raise KycRateLimitedError("limite de intentos KYC excedido, reintente luego")
        hits.append(now)
        _BUCKETS[key] = hits


def reset_rate_limits() -> None:
    """Limpia las ventanas (uso en pruebas)."""
    with _BUCKETS_LOCK:
        _BUCKETS.clear()


# ------------------------------------------------------- Orquestacion
def request_challenge(provider: KycProvider, *, device_id: str | None = None) -> dict:
    """Pide un desafio al proveedor (token opaco + pasos + TTL).

    `session_id` propio (uuid4 hex, sin PII); el token lo emite el
    proveedor (delega: mock o microservicio HTTP). Solo se loguea el hash.
    """
    session_id = uuid.uuid4().hex
    challenge = provider.challenge(session_id=session_id)
    logger.info(
        "kyc challenge_issued session_id=%s token_hash=%s device=%s",
        session_id,
        challenge.challenge_token_hash,
        "set" if device_id else "unset",
    )
    return {
        "token": challenge.expose_token(),
        "steps": list(challenge.steps),
        "expires_in": int(challenge.expires_in_seconds),
    }


def submit_kyc(
    provider: KycProvider,
    *,
    challenge_token: str,
    document_type: str,
    document_image_b64: str,
    segments: list[dict],
) -> dict:
    """Valida documento + segmentos, reenvia al proveedor y descarta imagenes.

    Validacion previa (422): `challenge_token` no vacio, `document_type`
    conocido, 1..`max_segments()` segmentos con `task` + imagen valida
    (base64, tamano, JPEG/PNG). Reenvio via
    `verify_full(session_id=<token>, payload=...)`: el token opaco se reusa
    como id de correlacion (sin PII). Tras el reenvio se borran las
    referencias locales (`del`); nada se persiste ni se loguea.
    """
    token = challenge_token.strip() if isinstance(challenge_token, str) else ""
    if not token:
        raise KycProxyValidationError("challenge_token es obligatorio")
    if document_type not in DOC_TYPES:
        raise KycProxyValidationError(f"document.type debe ser uno de {DOC_TYPES}")
    if not isinstance(segments, list) or not segments:
        raise KycProxyValidationError("segments: se requiere al menos 1 segmento")
    if len(segments) > max_segments():
        raise KycProxyValidationError(f"segments: maximo {max_segments()}")

    doc_bytes = validate_image_b64(document_image_b64, field="document.image_b64")
    seg_images: list[tuple[str, str]] = []
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise KycProxyValidationError(f"segments[{index}]: objeto invalido")
        task = segment.get("task")
        if not isinstance(task, str) or not task.strip() or len(task) > 64:
            raise KycProxyValidationError(f"segments[{index}].task es obligatorio")
        image_b64 = segment.get("image_b64")
        if not isinstance(image_b64, str) or not image_b64.strip():
            raise KycProxyValidationError(f"segments[{index}].image_b64 es obligatorio")
        validate_image_b64(image_b64, field=f"segments[{index}].image_b64")
        seg_images.append((task.strip(), image_b64.strip()))
    logger.info(
        "kyc submit_validated token_hash=%s doc_type=%s segments=%d doc_bytes=%d",
        hash_token(token),
        document_type,
        len(seg_images),
        len(doc_bytes),
    )
    try:
        result = provider.verify_full(
            session_id=token,
            payload={
                "document_type": document_type,
                "document_image_b64": document_image_b64.strip(),
                "segments": [
                    {"task": task, "image_b64": image_b64} for task, image_b64 in seg_images
                ],
            },
        )
    finally:
        # Reenvia y descarta: sin persistencia de frames/imagenes.
        del doc_bytes, seg_images
    logger.info(
        "kyc submit_result token_hash=%s overall=%s code=%s",
        hash_token(token),
        result.overall_result,
        result.detail_code,
    )
    return {
        "overall_result": bool(result.overall_result),
        "detail_code": str(result.detail_code or ""),
        "distance": round(float(result.distance), 4),
    }


__all__ = [
    "DOC_TYPES",
    "KycProxyValidationError",
    "KycRateLimitedError",
    "build_rate_key",
    "check_rate_limit",
    "max_image_bytes",
    "max_segments",
    "rate_limit_cfg",
    "request_challenge",
    "reset_rate_limits",
    "submit_kyc",
    "validate_image_b64",
]
