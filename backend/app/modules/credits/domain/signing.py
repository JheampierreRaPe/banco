"""Sello de firma del contrato (E3-T13, HU11 CA-03).

Dominio puro: sin BD, sin I/O, sin red, testeable sin Postgres. El sello
es la combinacion determinista `doc_hash` + `signed_at` + token
biometrico (no repudio): mismas entradas -> mismo sello (reproducible
para verificar integridad); cualquier alteracion lo invalida.

Decision del dueno (sello derivado, sin migracion): no hay columna
`sellar` propia; el sello se deriva y se expone en la respuesta de
firma, mientras en `contracts` persisten `doc_hash` (E3-T12) +
`signed_at` + `signature_token_hash` (E3-T13).

El token biometrico jamas se persiste ni se loguea en claro: solo su
SHA-256 (`signature_token_hash`, `docs/03b#86`). En la via dispositivo
el hash liga `firma + nonce + device_id + application_id` (el `nonce`
es de un solo uso via `identity.domain.nonce`, asi que cada firma da
un hash unico y reutilizar el token se detecta por igualdad en BD).
En la via PIN (fallback, patron E2-T31: el PIN es reutilizable por
diseno) el hash liga `application_id + signed_at + sal` aleatoria del
servidor: unico por firma sin exponer el PIN (un SHA-256 de un PIN de
4-6 digitos seria invertible por fuerza bruta). La no reutilizacion en
la via PIN la garantiza el estado (`GENERATED` -> `SIGNED`, sin
re-firma).

Sin `float`, sin secretos/PII en logs (este modulo no loguea).
"""

from __future__ import annotations

import hashlib
import hmac
import json

#: Version del sello (cambia el hash si cambia el formato canonico).
SEAL_VERSION = "credit-contract-seal/v1"

#: Version del hash del token (idem).
TOKEN_VERSION = "credit-signature-token/v1"


def _canonical(payload: dict[str, object]) -> bytes:
    """JSON canonico (claves ordenadas, sin espacios, UTF-8)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def token_hash_for(
    *,
    signature: str,
    nonce: str,
    device_id: str,
    application_id: str,
) -> str:
    """SHA-256 del token biometrico de la via dispositivo (64 hex).

    Liga la firma del `nonce` al dispositivo y al expediente: el mismo
    material no sella otro expediente ni otra firma (el `nonce` de un
    solo uso ya lo hace unico; la igualdad en BD es la segunda capa).
    Entradas no textuales/vacias -> `ValueError` (el servicio lo mapea
    al generico, sin oraculo).
    """
    for name, value in (
        ("signature", signature),
        ("nonce", nonce),
        ("device_id", device_id),
        ("application_id", application_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} debe ser texto no vacio")
    raw = _canonical(
        {
            "v": TOKEN_VERSION,
            "via": "device",
            "signature": signature.strip(),
            "nonce": nonce.strip(),
            "device_id": device_id.strip(),
            "application_id": application_id.strip(),
        }
    )
    return hashlib.sha256(raw).hexdigest()


def token_hash_for_pin(
    *,
    application_id: str,
    signed_at_iso: str,
    salt_hex: str,
) -> str:
    """SHA-256 del token de la via PIN (64 hex, fallback E2-T31).

    El PIN en claro jamas entra al hash (seria invertible): el hash
    liga el expediente + el instante del servidor + una sal aleatoria
    de 16+ bytes generada por el servicio en cada firma (unicidad por
    firma; la verificacion del PIN ya ocurrio por fachada antes de
    sellar). Entradas invalidas -> `ValueError`.
    """
    if not isinstance(application_id, str) or not application_id.strip():
        raise ValueError("application_id debe ser texto no vacio")
    if not isinstance(signed_at_iso, str) or not signed_at_iso.strip():
        raise ValueError("signed_at_iso debe ser texto no vacio")
    if not isinstance(salt_hex, str) or not salt_hex.strip():
        raise ValueError("salt_hex debe ser texto no vacio")
    try:
        salt = bytes.fromhex(salt_hex.strip())
    except ValueError as exc:
        raise ValueError("salt_hex debe ser hexadecimal") from exc
    if len(salt) < 16:
        raise ValueError("salt_hex debe aportar 16+ bytes")
    raw = _canonical(
        {
            "v": TOKEN_VERSION,
            "via": "pin",
            "application_id": application_id.strip(),
            "signed_at": signed_at_iso.strip(),
            "salt": salt_hex.strip().lower(),
        }
    )
    return hashlib.sha256(raw).hexdigest()


def seal_for(
    *,
    doc_hash: str,
    signed_at_iso: str,
    signature_token_hash: str,
) -> str:
    """Sello de no repudio de un documento (SHA-256, 64 hex).

    Combina el hash canonico del documento (E3-T12) + el instante del
    servidor + el hash del token. Determinista y reproducible: verificar
    integridad = recomputar y comparar. Entradas invalidas ->
    `ValueError`.
    """
    if not isinstance(doc_hash, str) or len(doc_hash.strip()) != 64:
        raise ValueError("doc_hash debe ser SHA-256 en hex (64 caracteres)")
    if not isinstance(signed_at_iso, str) or not signed_at_iso.strip():
        raise ValueError("signed_at_iso debe ser texto no vacio")
    if not isinstance(signature_token_hash, str) or not signature_token_hash.strip():
        raise ValueError("signature_token_hash debe ser texto no vacio")
    raw = _canonical(
        {
            "v": SEAL_VERSION,
            "doc_hash": doc_hash.strip().lower(),
            "signed_at": signed_at_iso.strip(),
            "signature_token_hash": signature_token_hash.strip().lower(),
        }
    )
    return hashlib.sha256(raw).hexdigest()


def verify_seal(
    *,
    doc_hash: str,
    signed_at_iso: str,
    signature_token_hash: str,
    seal: str,
) -> bool:
    """Verifica un sello (`True` solo si recomputa identico).

    Comparacion en tiempo constante; cualquier entrada malformada ->
    `False` (sin excepciones, sin oraculo).
    """
    if not isinstance(seal, str) or not seal.strip():
        return False
    try:
        expected = seal_for(
            doc_hash=doc_hash,
            signed_at_iso=signed_at_iso,
            signature_token_hash=signature_token_hash,
        )
    except ValueError:
        return False
    return hmac.compare_digest(expected, seal.strip().lower())


__all__ = [
    "SEAL_VERSION",
    "TOKEN_VERSION",
    "seal_for",
    "token_hash_for",
    "token_hash_for_pin",
    "verify_seal",
]
