"""Cifrado en reposo e indices ciegos de beneficiarios (E2-T13, HU07).

Fuente: `docs/02-arquitectura.md#10-seguridad-arquitectonica` (AES-256 en
reposo para datos sensibles), gap `G11` (`docs/18-gestion-de-cambios.md`:
no habia utilidad/clave de cifrado definida; se gestiona por entorno como
`DOC_HASH_PEPPER`, nunca en el repo).

Diseno (dominio puro, sin BD ni logs con PII):

- Clave simetrica de 256 bits desde `BENEFICIARY_CRYPTO_KEY` (hex de 64,
  base64 de 32 bytes, o texto libre que se deriva con SHA-256). Sin la
  variable se usa un default constante SOLO en `local`/desarrollo/pruebas
  (mismo patron aceptado que `_DEV_DOC_HASH_PEPPER` en
  `identity.service.kyc_onboarding`; fuera de `local` es fail-fast:
  `RuntimeError`, nunca default). La clave jamas se loguea.
- AES-256-GCM (`cryptography`, dependencia declarada en
  `backend/pyproject.toml`: el sobre es versionado). Si la libreria no
  estuviera disponible se usa un fallback stdlib documentado (stream CTR
  con HMAC-SHA256 como PRF + encrypt-then-MAC) SOLO en `local`/tests y
  con warning; fuera de `local` el fallback esta prohibido (fail-fast:
  `assert_production_ready`, validado tambien en `app.core.config`).
  Ambos sobres llevan marcador de version y el descifrado los distingue.
  Interfaz estable `encrypt_value` / `decrypt_value` para migrar sin
  tocar el resto del modulo.
- Indice ciego: HMAC-SHA256 con pepper propio (`BENEFICIARY_INDEX_PEPPER`,
  mismo manejo que la clave) sobre el termino normalizado
  (`normalize_search_term`: sin tildes, UPPER, espacios colapsados, en
  paridad con `holder_lookup.normalize_name`). Determinista por pepper:
  permite buscar por igualdad indexada SIN descifrar (el servidor nunca
  barre la tabla descifrando).
- Tokens de busqueda (`search_tokens`): por cada palabra (len >= 2) del
  alias/nombre normalizados se indexa la palabra completa + sus prefijos
  (2..10 caracteres). Asi `q=mar` encuentra `MARIA` por igualdad de hash
  del prefijo, sin LIKE sobre claro ni descifrado masivo.

Sin PII en logs (regla de oro 7): aqui no se loguea nada; los llamantes
solo correlacionan por `id` o hashes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import secrets
import unicodedata

logger = logging.getLogger(__name__)

try:  # AES-256-GCM real cuando `cryptography` esta instalada.
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _AES_AVAILABLE = True
except ImportError:  # pragma: no cover - entorno sin la dependencia
    AESGCM = None  # type: ignore[assignment,misc]
    InvalidTag = Exception  # type: ignore[assignment,misc]
    _AES_AVAILABLE = False

#: Variables de entorno (G11: gestionadas como `DOC_HASH_PEPPER`).
CRYPTO_KEY_ENV = "BENEFICIARY_CRYPTO_KEY"
INDEX_PEPPER_ENV = "BENEFICIARY_INDEX_PEPPER"

#: Defaults SOLO desarrollo/pruebas (nunca en produccion; cambiarlos
#: invalida cifrados/indices previos). No son secretos reales.
_DEV_CRYPTO_KEY = "banca-online-dev-beneficiary-crypto-key"
_DEV_INDEX_PEPPER = "banca-online-dev-beneficiary-index-pepper"

#: Marcadores de version del sobre (migracion sin re-cifrado masivo).
_ENVELOPE_AES = "B1"
_ENVELOPE_FALLBACK = "B0"

#: AAD/contextos fijos (separan dominios del HMAC/AEAD).
_AES_AAD = b"BENEF-v1"
_FALLBACK_CTX_KS = b"BENEF-B0-KS"
_FALLBACK_CTX_TAG = b"BENEF-B0-TAG"
_INDEX_CTX = b"ben-idx-v1"

#: Busqueda: longitud minima de palabra/prefijo indexado y tope de prefijo.
MIN_TOKEN_LEN = 2
MAX_PREFIX_LEN = 10


class CryptoError(ValueError):
    """Sobre invalido o clave incorrecta (el router lo traduce a 400/500)."""


def _raw_key_bytes() -> bytes:
    """Materia prima de la clave (entorno o default de desarrollo).

    Fuera de `local` el default jamas se usa: fail-fast (`RuntimeError`).
    En `local`/tests se conserva con warning (sin secretos reales).
    """
    configured = os.environ.get(CRYPTO_KEY_ENV)
    if configured:
        return configured.encode("utf-8")
    if _env_is_nonlocal():
        raise RuntimeError(
            f"{CRYPTO_KEY_ENV} es obligatoria cuando APP_ENV/ENVIRONMENT no es 'local'"
        )
    logger.warning(
        "%s ausente: usando clave de desarrollo (solo local/tests, sin secretos reales)",
        CRYPTO_KEY_ENV,
    )
    return _DEV_CRYPTO_KEY.encode("utf-8")


def crypto_key_bytes() -> bytes:
    """Clave simetrica de 32 bytes (AES-256).

    Acepta: hex de 64 chars (32 bytes), base64 de 32 bytes, o texto libre
    (se deriva con SHA-256, determinista). Nunca se loguea ni se expone.
    """
    raw = _raw_key_bytes().strip()
    if len(raw) == 64:
        try:
            decoded = bytes.fromhex(raw.decode("ascii"))
            if len(decoded) == 32:
                return decoded
        except (ValueError, UnicodeDecodeError):
            pass
    try:
        decoded_b64 = base64.b64decode(raw, validate=True)
        if len(decoded_b64) == 32:
            return decoded_b64
    except (binascii.Error, ValueError):
        pass
    return hashlib.sha256(raw).digest()


def _index_pepper() -> bytes:
    """Pepper del indice ciego (entorno o default de desarrollo).

    Mismo fail-fast fuera de `local` que la clave de cifrado.
    """
    configured = os.environ.get(INDEX_PEPPER_ENV)
    if configured:
        return configured.encode("utf-8")
    if _env_is_nonlocal():
        raise RuntimeError(
            f"{INDEX_PEPPER_ENV} es obligatoria cuando APP_ENV/ENVIRONMENT no es 'local'"
        )
    logger.warning(
        "%s ausente: usando pepper de desarrollo (solo local/tests, sin secretos reales)",
        INDEX_PEPPER_ENV,
    )
    return _DEV_INDEX_PEPPER.encode("utf-8")


def _env_is_nonlocal() -> bool:
    """`True` si `APP_ENV` o `ENVIRONMENT` indican un entorno no local."""
    for var in ("APP_ENV", "ENVIRONMENT"):
        value = (os.environ.get(var) or "").strip().lower()
        if value and value != "local":
            return True
    return False


def assert_production_ready() -> None:
    """Fail-fast fuera de `local`: exige AES + clave y pepper reales.

    No-op en `local`/tests (ahi se permite el fallback `B0` con warning).
    Fuera de `local` lanza `RuntimeError` si falta `cryptography` o si
    `BENEFICIARY_CRYPTO_KEY`/`BENEFICIARY_INDEX_PEPPER` no estan
    configuradas: nada de degradacion silenciosa en produccion.
    """
    if not _env_is_nonlocal():
        return
    if not _AES_AVAILABLE:
        raise RuntimeError(
            "cryptography es obligatoria cuando APP_ENV/ENVIRONMENT no es 'local' "
            "(el fallback B0 no esta permitido en produccion)"
        )
    if not (os.environ.get(CRYPTO_KEY_ENV) or "").strip():
        raise RuntimeError(
            f"{CRYPTO_KEY_ENV} es obligatoria cuando APP_ENV/ENVIRONMENT no es 'local'"
        )
    if not (os.environ.get(INDEX_PEPPER_ENV) or "").strip():
        raise RuntimeError(
            f"{INDEX_PEPPER_ENV} es obligatoria cuando APP_ENV/ENVIRONMENT no es 'local'"
        )


def using_aes() -> bool:
    """`True` si el cifrado activo es AES-256-GCM (no el fallback)."""
    return _AES_AVAILABLE


def encrypt_value(plaintext: str) -> str:
    """Cifra un texto (sobres `B1` AES-GCM o `B0` fallback). Nonce aleatorio."""
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError("plaintext debe ser texto no vacio")
    data = plaintext.encode("utf-8")
    key = crypto_key_bytes()
    if _AES_AVAILABLE:
        nonce = secrets.token_bytes(12)
        assert AESGCM is not None
        sealed = AESGCM(key).encrypt(nonce, data, _AES_AAD)
        return f"{_ENVELOPE_AES}." + base64.urlsafe_b64encode(nonce + sealed).decode("ascii")
    if _env_is_nonlocal():
        raise RuntimeError(
            "sobre B0 (fallback sin AES) no permitido cuando APP_ENV/ENVIRONMENT no es 'local'"
        )
    logger.warning("cifrando con fallback B0 sin AES (solo local/tests)")
    nonce = secrets.token_bytes(16)
    keystream = _fallback_keystream(key, nonce, len(data))
    cipher = bytes(a ^ b for a, b in zip(data, keystream))
    tag = hmac.new(key, _FALLBACK_CTX_TAG + nonce + cipher, hashlib.sha256).digest()
    return f"{_ENVELOPE_FALLBACK}." + base64.urlsafe_b64encode(nonce + cipher + tag).decode("ascii")


def _fallback_keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """Keystream CTR con HMAC-SHA256 como PRF (solo fallback sin AES)."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(
            hmac.new(
                key, _FALLBACK_CTX_KS + nonce + counter.to_bytes(4, "big"), hashlib.sha256
            ).digest()
        )
        counter += 1
    return bytes(out[:length])


def decrypt_value(token: str) -> str:
    """Descifra un sobre `B1`/`B0`; `CryptoError` si es invalido o la clave no coincide."""
    if not isinstance(token, str) or "." not in token:
        raise CryptoError("sobre de cifrado invalido")
    version, _, payload = token.partition(".")
    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise CryptoError("sobre de cifrado invalido") from exc
    key = crypto_key_bytes()
    try:
        if version == _ENVELOPE_AES:
            if not _AES_AVAILABLE:
                raise CryptoError("sobre AES sin libreria disponible")
            assert AESGCM is not None
            nonce, sealed = raw[:12], raw[12:]
            if len(nonce) != 12 or not sealed:
                raise CryptoError("sobre de cifrado invalido")
            return AESGCM(key).decrypt(nonce, sealed, _AES_AAD).decode("utf-8")
        if version == _ENVELOPE_FALLBACK:
            if len(raw) < 16 + 32 + 1:
                raise CryptoError("sobre de cifrado invalido")
            nonce, cipher, tag = raw[:16], raw[16:-32], raw[-32:]
            expected = hmac.new(key, _FALLBACK_CTX_TAG + nonce + cipher, hashlib.sha256).digest()
            if not hmac.compare_digest(tag, expected):
                raise CryptoError("sobre de cifrado invalido o clave incorrecta")
            keystream = _fallback_keystream(key, nonce, len(cipher))
            return bytes(a ^ b for a, b in zip(cipher, keystream)).decode("utf-8")
    except CryptoError:
        raise
    except (InvalidTag, UnicodeDecodeError, ValueError) as exc:
        raise CryptoError("sobre de cifrado invalido o clave incorrecta") from exc
    raise CryptoError("version de sobre desconocida")


def normalize_search_term(value: object) -> str:
    """Normaliza para indice/busqueda: sin tildes, UPPER, 1 espacio."""
    if not isinstance(value, str):
        return ""
    folded = unicodedata.normalize("NFKD", value)
    ascii_only = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(ascii_only.upper().split())


def blind_index(term: str, *, domain: str) -> str:
    """Indice ciego HMAC-SHA256 hex del termino normalizado (dominio separado)."""
    normalized = normalize_search_term(term)
    if not normalized:
        raise ValueError("termino es obligatorio para el indice")
    if not domain or not str(domain).strip():
        raise ValueError("domain es obligatorio")
    msg = _INDEX_CTX + b"|" + str(domain).encode("utf-8") + b"|" + normalized.encode("utf-8")
    return hmac.new(_index_pepper(), msg, hashlib.sha256).hexdigest()


def alias_index(alias: str) -> str:
    """Hash determinista del alias normalizado (exacto; dedup + busqueda)."""
    return blind_index(alias, domain="alias")


def name_index(holder_name: str) -> str:
    """Hash determinista del nombre normalizado (exacto)."""
    return blind_index(holder_name, domain="name")


def account_index(account_or_cci: str) -> str:
    """Hash determinista del CCI/cuenta normalizado (dedup, sin claro)."""
    return blind_index(account_or_cci, domain="acct")


def token_hashes_for(alias: str, holder_name: str | None) -> set[str]:
    """Hashes de tokens (palabra + prefijos) del alias y el nombre.

    Dominio puro: alimenta `beneficiary_search_tokens`. Cada palabra
    (len >= 2) aporta su hash completo + prefijos 2..10: la busqueda por
    prefijo (`q=mar` -> `MARIA`) se resuelve por igualdad indexada.
    """
    words: set[str] = set()
    for text in (alias, holder_name or ""):
        for word in normalize_search_term(text).split():
            if len(word) < MIN_TOKEN_LEN:
                continue
            words.add(word)
            for size in range(MIN_TOKEN_LEN, min(len(word), MAX_PREFIX_LEN) + 1):
                words.add(word[:size])
    pepper = _index_pepper()
    out: set[str] = set()
    for word in words:
        msg = _INDEX_CTX + b"|tok|" + word.encode("utf-8")
        out.add(hmac.new(pepper, msg, hashlib.sha256).hexdigest())
    return out


def query_token_hashes(query: str) -> set[str]:
    """Hashes de los terminos de `q` (AND entre palabras; `[]` si nada util)."""
    pepper = _index_pepper()
    out: set[str] = set()
    for word in normalize_search_term(query).split():
        if len(word) < MIN_TOKEN_LEN:
            continue
        msg = _INDEX_CTX + b"|tok|" + word.encode("utf-8")
        out.add(hmac.new(pepper, msg, hashlib.sha256).hexdigest())
    return out


def mask_account_or_cci(last4: str) -> str:
    """Enmascara: solo ultimos 4 visibles (`****1234`, regla de oro 9)."""
    if not isinstance(last4, str) or len(last4) != 4:
        raise ValueError("last4 debe ser texto de 4 caracteres")
    return f"****{last4}"


__all__ = [
    "CRYPTO_KEY_ENV",
    "INDEX_PEPPER_ENV",
    "MAX_PREFIX_LEN",
    "MIN_TOKEN_LEN",
    "CryptoError",
    "account_index",
    "alias_index",
    "assert_production_ready",
    "blind_index",
    "crypto_key_bytes",
    "decrypt_value",
    "encrypt_value",
    "mask_account_or_cci",
    "name_index",
    "normalize_search_term",
    "query_token_hashes",
    "token_hashes_for",
    "using_aes",
]
