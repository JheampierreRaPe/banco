"""Dominio puro del QR firmado (E4-T06, HU14 CA-01/CA-03; `docs/18` G68).

Empaquetado canonico + `payload_hash` (SHA-256) + firma con secreto de
servidor + caducidad. La generacion **no mueve dinero** (`docs/04#7.4`:
si el QR expiro o la firma no valida, en el pago `HU15` se rechaza
antes, sin retencion).

Decision del dueno (G68/D06, formato propio firmado + adaptador):
firma **HMAC-SHA256** determinista en el MVP (simple, sin
dependencias), encapsulada tras la interfaz `QrSigner` para migrar a
`Ed25519` despues sin tocar el caso de uso. El payload es canonico
(JSON ordenado, claves fijas) e interoperable: el sobre de transporte
`BO-QR1` se (de)codifica con el adaptador de `E4-T03`
(`wallet_interop.encode_payload`/`decode_payload`; la firma viaja como
`sig` opaco y se verifica en local contra la fila `qr_charges`).

Puro y determinista: sin BD, sin red, sin reloj (el `now` se inyecta).
Dinero entero en centimos; nunca `float` (regla de oro 3). Sin PII ni
secretos en logs (regla 7): aqui no se loguea el secreto, el RUC ni el
contenido firmable (solo longitudes/tipos/resultado booleano).

Modos (`docs/03b#93`): `DYNAMIC` (monto fijo por cobro, con
`expires_at = now + ttl`) y `STATIC` (cobro reutilizable del
comercio, sin caducidad estricta en el MVP: `expires_at = NULL`;
`is_expired` con `expires_at` nulo retorna `False`; el job de `E4-T07`
podra caducar `STATIC` rancios por politica sin romper la firma).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Version del formato propio firmado (D06; mismo valor que el sobre
#: `BO-QR1` del adaptador `E4-T03`).
QR_FORMAT_VERSION = "BO-QR1"

#: Modos de cobro (`docs/03b#93`).
QR_MODES = ("DYNAMIC", "STATIC")

#: Contexto del HMAC: separa este uso del secreto de otros (p. ej. el
#: digest del PIN de `transactions`, G58).
_QR_HMAC_CONTEXT = "wallet-qr/v1:"

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_CONCEPT_MAX_LEN = 120


# ------------------------------------------------------------------ validacion
def _require_minor(name: str, value: object) -> int:
    """Entero de centimos `> 0` (`bool`/`float`/texto -> error)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} debe ser entero de centimos, recibido: {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{name} debe ser > 0, recibido: {value}")
    return value


def _require_mode(mode: object) -> str:
    if not isinstance(mode, str) or mode not in QR_MODES:
        raise ValueError(f"mode debe ser uno de {list(QR_MODES)}, recibido: {mode!r}")
    return mode


def _require_currency(currency: object) -> str:
    if not isinstance(currency, str) or _CURRENCY_RE.match(currency.strip().upper()) is None:
        raise ValueError(f"currency debe ser ISO-4217 (3 letras), recibido: {currency!r}")
    return currency.strip().upper()


def _require_concept(concept: object) -> str | None:
    if concept is None:
        return None
    if not isinstance(concept, str):
        raise TypeError("concept debe ser texto o None")
    text = concept.strip()
    if len(text) > _CONCEPT_MAX_LEN:
        raise ValueError(f"concept supera {_CONCEPT_MAX_LEN} caracteres")
    return text or None


def _epoch(expires_at: datetime | None) -> int:
    """`expires_at` aware -> epoch (`None` = sin caducidad -> `0`)."""
    if expires_at is None:
        return 0
    if not isinstance(expires_at, datetime):
        raise TypeError("expires_at debe ser datetime o None")
    moment = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=UTC)
    return int(moment.timestamp())


# ------------------------------------------------------------------ payload
def _require_optional_minor(name: str, value: object) -> int | None:
    """Centimos `None` (monto variable) o entero `> 0` (nunca `float`)."""
    if value is None:
        return None
    return _require_minor(name, value)


def build_payload(
    *,
    charge_id: object,
    merchant_id: object | None = None,
    owner_user_id: object | None = None,
    amount_minor: int | None = None,
    currency: str,
    concept: object = None,
    mode: str = "DYNAMIC",
    expires_at: datetime | None,
) -> dict[str, Any]:
    """Construye el payload canonico del QR (dict JSON-serializable).

    Comercio (`merchant_id`, E4-T06): campos fijos (orden canonico al
    serializar) `v` (version del formato), `charge_id`,
    `merchant_id`, `amount_minor` (`> 0`), `currency`, `concept` (o
    `None`), `mode`, `exp` (epoch, `0` = sin caducidad). El dict de
    comercio es byte-identico al historico (la firma vigente no
    cambia).

    Personal (`owner_user_id`, E4-T21): QR estatico sin monto
    (`amount_minor = None`; el pagador lo digita) y sin caducidad
    (`expires_at = None`); el payload lleva `owner_user_id` en lugar
    de `merchant_id` y `mode = "STATIC"`. Exactamente uno de
    `merchant_id`/`owner_user_id` es obligatorio; el personal exige
    `amount_minor = None` y `mode = "STATIC"`.

    Manipular cualquier campo invalida la firma.
    """
    charge = str(charge_id)
    if not charge.strip():
        raise ValueError("charge_id es obligatorio")
    merchant = str(merchant_id).strip() if merchant_id is not None else ""
    owner = str(owner_user_id).strip() if owner_user_id is not None else ""
    if bool(merchant) == bool(owner):
        raise ValueError("exactamente uno de merchant_id / owner_user_id es obligatorio")
    if owner:
        if amount_minor is not None:
            raise ValueError("el QR personal es sin monto (amount_minor debe ser None)")
        if mode != "STATIC":
            raise ValueError('el QR personal es STATIC (mode debe ser "STATIC")')
        return {
            "v": QR_FORMAT_VERSION,
            "charge_id": charge,
            "owner_user_id": owner,
            "amount_minor": None,
            "currency": _require_currency(currency),
            "concept": _require_concept(concept),
            "mode": "STATIC",
            "exp": _epoch(expires_at),
        }
    return {
        "v": QR_FORMAT_VERSION,
        "charge_id": charge,
        "merchant_id": merchant,
        "amount_minor": _require_minor("amount_minor", amount_minor),
        "currency": _require_currency(currency),
        "concept": _require_concept(concept),
        "mode": _require_mode(mode),
        "exp": _epoch(expires_at),
    }


def is_personal_payload(payload: dict[str, Any]) -> bool:
    """Indica si el payload es de un QR personal (`owner_user_id`)."""
    return isinstance(payload, dict) and bool(payload.get("owner_user_id"))


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Serializacion canonica (claves ordenadas, sin espacios)."""
    if not isinstance(payload, dict):
        raise TypeError("payload debe ser dict")
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def payload_hash_for(payload: dict[str, Any]) -> str:
    """`payload_hash` estable: SHA-256 hex del canonico (64 chars)."""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


# ------------------------------------------------------------------ firma
class QrSigner(Protocol):
    """Interfaz de firma del QR (MVP: HMAC; futuro: `Ed25519`)."""

    def sign(self, payload: dict[str, Any]) -> str: ...
    def verify(self, payload: dict[str, Any], signature: object) -> bool: ...


@dataclass(frozen=True)
class HmacQrSigner:
    """Firma HMAC-SHA256 con secreto de servidor (MVP, gap G68).

    El secreto nunca se loguea ni se serializa (el `repr` lo oculta).
    `verify` usa `hmac.compare_digest` (tiempo constante).
    """

    secret: str

    def __post_init__(self) -> None:
        if not isinstance(self.secret, str) or not self.secret:
            raise ValueError("el secreto de firma es obligatorio")

    def __repr__(self) -> str:  # sin exponer el secreto (regla 7)
        return "HmacQrSigner(secret=***)"

    @property
    def _key(self) -> bytes:
        return (_QR_HMAC_CONTEXT + self.secret).encode("utf-8")

    def sign(self, payload: dict[str, Any]) -> str:
        digest = hmac.new(self._key, canonical_bytes(payload), hashlib.sha256).hexdigest()
        logger.debug("qr firmado bytes=%d", len(canonical_bytes(payload)))
        return digest

    def verify(self, payload: dict[str, Any], signature: object) -> bool:
        if not isinstance(signature, str) or not signature:
            return False
        try:
            expected = self.sign(payload)
        except (TypeError, ValueError):
            return False
        ok = hmac.compare_digest(expected, signature)
        logger.debug("qr verificado ok=%s", ok)
        return ok


def resolve_secret(explicit: str | None = None) -> str:
    """Resuelve el secreto de firma (explicito > settings > entorno).

    Fuente vigente: `jwt_secret` de `app.core.config` (mismo criterio
    que el digest del PIN del motor, G58). Nunca se loguea el valor.
    """
    if isinstance(explicit, str) and explicit:
        return explicit
    try:
        from app.core.config import get_settings

        candidate = str(get_settings().jwt_secret or "")
    except Exception:  # noqa: BLE001 - sin settings rige el entorno
        candidate = ""
    if candidate:
        return candidate
    candidate = str(os.environ.get("QR_SIGNING_SECRET", "") or "")
    if candidate:
        return candidate
    raise ValueError("sin secreto de firma (configurar `jwt_secret`/`QR_SIGNING_SECRET`)")


def default_signer(secret: str | None = None) -> HmacQrSigner:
    """Signer vigente del MVP (HMAC; `Ed25519` enchufable aqui)."""
    return HmacQrSigner(secret=resolve_secret(secret))


def sign_payload(payload: dict[str, Any], *, secret: str | None = None) -> str:
    """Firma el payload canonico (determinista: mismo input, misma firma)."""
    return default_signer(secret).sign(payload)


def verify_signature(
    payload: dict[str, Any], signature: object, *, secret: str | None = None
) -> bool:
    """Verifica la firma (puro y determinista; alteracion -> `False`)."""
    try:
        return default_signer(secret).verify(payload, signature)
    except ValueError:
        return False


# ------------------------------------------------------------------ RUC hasheado
def hash_tax_id(tax_id: object) -> str:
    """Hashea el RUC del comercio (nunca se persiste en claro, regla 7).

    HMAC-SHA256 con pepper de entorno (`TAX_ID_HASH_PEPPER`, con
    fallback constante de desarrollo como `DOC_HASH_PEPPER`, R25; el
    valor nunca se loguea). El mismo RUC da el mismo hash.
    """
    if not isinstance(tax_id, str) or not tax_id.strip():
        raise ValueError("tax_id es obligatorio")
    digits = "".join(ch for ch in tax_id.strip() if ch.isdigit())
    if len(digits) not in (8, 11):
        raise ValueError("tax_id con formato invalido (8 u 11 digitos)")
    pepper = str(os.environ.get("TAX_ID_HASH_PEPPER", "") or "dev-tax-id-pepper")
    return hmac.new(pepper.encode("utf-8"), digits.encode("utf-8"), hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ caducidad
def is_expired(charge: Any, now: datetime) -> bool:
    """Indica si el cargo expiro (`now` inyectable; `None` = sin caducidad).

    Acepta la fila ORM o un dict con `expires_at`. Naive se asume UTC.
    """
    if not isinstance(now, datetime):
        raise TypeError("now debe ser datetime")
    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    expires_at = (
        charge.get("expires_at")
        if isinstance(charge, dict)
        else getattr(charge, "expires_at", None)
    )
    if expires_at is None:
        return False
    if not isinstance(expires_at, datetime):
        raise TypeError("expires_at debe ser datetime o None")
    limit = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=UTC)
    return moment >= limit


# ------------------------------------------------------------------ sobre BO-QR1
def to_interop_token(payload: dict[str, Any], signature: str) -> str:
    """Envuelve el payload firmado en el sobre `BO-QR1` (`E4-T03`).

    Mapeo al sobre del adaptador (`issuer`/`peer` fijos del ecosistema
    local; `ref` = `charge_id`; `exp` = epoch; `sig` opaco con la firma
    local). El token resultante lo decodifica `wallet_interop`
    (`decode_payload`) sin conocer este dominio (CA-03). Un QR
    personal sin monto (`amount_minor = None`, E4-T21) viaja como
    `amount_minor = 0` en el sobre ("lo define el pagador"; el
    adaptador admite `>= 0` y el pago verifica contra la fila, nunca
    se confia en el cliente).
    """
    if not isinstance(payload, dict):
        raise TypeError("payload debe ser dict")
    if not isinstance(signature, str) or not signature:
        raise ValueError("signature es obligatoria")
    from app.adapters import wallet_interop as interop

    return interop.encode_payload(
        issuer="CUYCASH",
        peer="CUYCASH",
        ref=str(payload.get("charge_id") or ""),
        amount_minor=payload.get("amount_minor") or 0,
        currency=str(payload.get("currency") or ""),
        expires_at=int(payload.get("exp") or 0),
        sig=signature,
    )


def envelope_fields(token: str) -> dict[str, Any]:
    """Decodifica el sobre `BO-QR1` via el adaptador (CA-03)."""
    from app.adapters import wallet_interop as interop

    decoded = interop.decode_payload(token)
    return {
        "issuer": decoded.issuer,
        "peer": decoded.peer,
        "ref": decoded.ref,
        "amount_minor": decoded.amount_minor,
        "currency": decoded.currency,
        "exp": decoded.expires_at,
        "sig": decoded.sig,
    }


__all__ = [
    "QR_FORMAT_VERSION",
    "QR_MODES",
    "HmacQrSigner",
    "QrSigner",
    "build_payload",
    "canonical_bytes",
    "default_signer",
    "envelope_fields",
    "hash_tax_id",
    "is_expired",
    "is_personal_payload",
    "payload_hash_for",
    "resolve_secret",
    "sign_payload",
    "to_interop_token",
    "verify_signature",
]
