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
    """Maximo de segmentos/tareas por submit (`KYC_MAX_SEGMENTS`, default 10)."""
    return _env_int("KYC_MAX_SEGMENTS", 10)


def max_frames_per_segment() -> int:
    """Maximo de frames por tarea en una rafaga.

    `KYC_MAX_FRAMES_PER_SEGMENT` (default 30) debe permitir >=15 frames por
    tarea (el microservicio exige >=5 por segmento y >=8 en total para la
    selfie). `image_b64` cuenta como un unico frame.
    """
    return _env_int("KYC_MAX_FRAMES_PER_SEGMENT", 30)


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


def _validate_frames(image_b64: str, *, field: str) -> str:
    """Valida un frame base64 (tamano + magic bytes) y devuelve su forma limpia."""
    validate_image_b64(image_b64, field=field)
    return image_b64.strip()


def _validate_segment(index: int, segment: dict) -> tuple[str, list[str], str | None]:
    """Valida un segmento (tarea + rafaga y/o imagen) y normaliza sus frames.

    `frames_b64` es aditivo: si viene, se validan todos y se conserva el orden;
    `image_b64` (compatibilidad) se agrega como un frame mas. Se exige al menos
    uno de los dos. Devuelve `(task, frames_b64, image_b64)`.
    """
    if not isinstance(segment, dict):
        raise KycProxyValidationError(f"segments[{index}]: objeto invalido")
    task = segment.get("task")
    if not isinstance(task, str) or not task.strip() or len(task) > 64:
        raise KycProxyValidationError(f"segments[{index}].task es obligatorio")

    raw_frames = segment.get("frames_b64")
    frames: list[str] = []
    if raw_frames is not None:
        if not isinstance(raw_frames, list) or not raw_frames:
            raise KycProxyValidationError(
                f"segments[{index}].frames_b64 debe ser una lista no vacia"
            )
        if len(raw_frames) > max_frames_per_segment():
            raise KycProxyValidationError(
                f"segments[{index}].frames_b64: maximo {max_frames_per_segment()}"
            )
        for f_index, frame in enumerate(raw_frames):
            if not isinstance(frame, str) or not frame.strip():
                raise KycProxyValidationError(
                    f"segments[{index}].frames_b64[{f_index}] es obligatorio"
                )
            frames.append(_validate_frames(frame, field=f"segments[{index}].frames_b64[{f_index}]"))

    image_b64 = segment.get("image_b64")
    clean_image: str | None = None
    if image_b64 is not None:
        if not isinstance(image_b64, str) or not image_b64.strip():
            raise KycProxyValidationError(f"segments[{index}].image_b64 debe ser texto no vacio")
        clean_image = _validate_frames(image_b64, field=f"segments[{index}].image_b64")

    if not frames and clean_image is None:
        raise KycProxyValidationError(f"segments[{index}]: se requiere frames_b64 o image_b64")
    return task.strip(), frames, clean_image


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
    conocido, 1..`max_segments()` segmentos, cada uno con `task` y al menos
    `frames_b64` (rafaga, en orden) o `image_b64` (compatibilidad); cada frame
    se valida (base64, tamano, JPEG/PNG). Reenvio via
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
    seg_frames: list[dict] = []
    for index, segment in enumerate(segments):
        task, frames, image = _validate_segment(index, segment)
        entry: dict = {"task": task}
        if frames:
            entry["frames_b64"] = frames
        if image is not None:
            entry["image_b64"] = image
        seg_frames.append(entry)
    logger.info(
        "kyc submit_validated token_hash=%s doc_type=%s segments=%d doc_bytes=%d",
        hash_token(token),
        document_type,
        len(seg_frames),
        len(doc_bytes),
    )
    try:
        result = provider.verify_full(
            session_id=token,
            payload={
                "document_type": document_type,
                "document_image_b64": document_image_b64.strip(),
                "segments": seg_frames,
            },
        )
    finally:
        # Reenvia y descarta: sin persistencia de frames/imagenes.
        del doc_bytes, seg_frames
    logger.info(
        "kyc submit_result token_hash=%s overall=%s code=%s steps=%d/%d failed=%s",
        hash_token(token),
        result.overall_result,
        result.detail_code,
        len(result.steps_verified),
        len(result.steps_total),
        result.failed_step or "-",
    )
    return {
        "overall_result": bool(result.overall_result),
        "detail_code": str(result.detail_code or ""),
        "distance": round(float(result.distance), 4),
        "steps_verified": list(result.steps_verified),
        "steps_total": list(result.steps_total),
        "failed_step": result.failed_step,
        "step_results": dict(result.step_results),
        "overall_reason": str(result.overall_reason or ""),
    }


def evaluate_step(
    provider: KycProvider,
    *,
    challenge_token: str,
    step: str,
    frames_b64: list[str],
) -> dict:
    """Evalua un paso de liveness en vivo (rafaga de frames) sin persistir.

    Valida token/paso y cada frame; delega en
    `provider.evaluate_liveness` (`/liveness/evaluate`) y devuelve
    `step`/`passed`/`reason`/`frames_analyzed`/`details`. Los frames viajan
    como variables locales y jamas se loguean ni se persisten.
    """
    token = challenge_token.strip() if isinstance(challenge_token, str) else ""
    if not token:
        raise KycProxyValidationError("challenge_token es obligatorio")
    if not isinstance(step, str) or not step.strip() or len(step) > 64:
        raise KycProxyValidationError("step es obligatorio")
    if not isinstance(frames_b64, list) or not frames_b64:
        raise KycProxyValidationError("frames_b64: se requiere al menos 1 frame")
    if len(frames_b64) > max_frames_per_segment():
        raise KycProxyValidationError(f"frames_b64: maximo {max_frames_per_segment()}")
    normalized: list[str] = []
    for index, frame in enumerate(frames_b64):
        if not isinstance(frame, str) or not frame.strip():
            raise KycProxyValidationError(f"frames_b64[{index}] es obligatorio")
        normalized.append(_validate_frames(frame, field=f"frames_b64[{index}]"))
    clean_step = step.strip()
    logger.info(
        "kyc evaluate_validated token_hash=%s step=%s frames=%d",
        hash_token(token),
        clean_step,
        len(normalized),
    )
    try:
        result = provider.evaluate_liveness(
            session_id=token,
            task=clean_step,
            payload={"token": token, "frames_base64": normalized},
        )
    finally:
        del normalized
    logger.info(
        "kyc evaluate_result token_hash=%s step=%s passed=%s frames=%d",
        hash_token(token),
        result.step or clean_step,
        result.passed,
        result.frames_analyzed,
    )
    return {
        "step": str(result.step or clean_step),
        "passed": bool(result.passed),
        "reason": str(result.reason or ""),
        "frames_analyzed": int(result.frames_analyzed),
        "details": dict(result.details),
    }


def validate_document(provider: KycProvider, *, image_b64: str) -> dict:
    """Valida la legibilidad de un documento (E1-T30) sin persistirlo.

    Validacion previa (422): base64 valido, tamano (`KYC_MAX_IMAGE_BYTES`) y
    magic bytes JPEG/PNG via `validate_image_b64`. Reenvia al adaptador
    (`provider.validate_document`) con un `session_id` propio (uuid4 hex, sin
    PII) y descarta la imagen (`del`). `is_valid=false` NO es error: se
    devuelve como exito con los `issues`; los fallos del microservicio los
    traduce el router.
    """
    raw = validate_image_b64(image_b64, field="image_b64")
    session_id = uuid.uuid4().hex
    logger.info("kyc document_validated session_id=%s bytes=%d", session_id, len(raw))
    try:
        result = provider.validate_document(session_id=session_id, image_b64=image_b64.strip())
    finally:
        # Reenvia y descarta: sin persistencia de la imagen.
        del raw
    logger.info(
        "kyc document_result session_id=%s is_valid=%s issues=%d",
        session_id,
        result.is_valid,
        len(result.issues),
    )
    return {
        "is_valid": bool(result.is_valid),
        "issues": list(result.issues),
        "checks": dict(result.checks),
    }


__all__ = [
    "DOC_TYPES",
    "KycProxyValidationError",
    "KycRateLimitedError",
    "build_rate_key",
    "check_rate_limit",
    "evaluate_step",
    "max_frames_per_segment",
    "max_image_bytes",
    "max_segments",
    "rate_limit_cfg",
    "request_challenge",
    "reset_rate_limits",
    "submit_kyc",
    "validate_document",
    "validate_image_b64",
]
