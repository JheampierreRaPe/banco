"""Tokenizacion de fuentes de fondos (E4-T01, HU13 logica 1; `docs/18` G67).

Recibe el dato crudo (PAN) y devuelve (`token_ref`, `last4`, `brand`),
**descartando** el crudo: jamas se persiste, se responde ni se loguea
(regla de oro 7; enmascarar por defecto, regla 9).

Decision del dueno (MVP): el proveedor es un **mock determinista
local** (`MockWalletTokenizationProvider`); la interfaz
`WalletTokenizationProvider` queda desacoplada para enchufar un
proveedor real (`http`) sin tocar el caso de uso. Sin PII/secretos en
logs: aqui no se loguea el PAN (solo longitudes/tipos).

Dominio puro: sin BD, sin red, sin reloj.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

#: Longitud aceptada de un PAN (ISO/IEC 7812: 13..19 digitos).
PAN_MIN_LEN = 13
PAN_MAX_LEN = 19

#: Prefijo del `token_ref` emitido por el mock (determinista por PAN).
MOCK_TOKEN_PREFIX = "tok_mock_"

_PAN_CLEAN_RE = re.compile(r"[\s\-]+")


@dataclass(frozen=True)
class TokenizedCard:
    """Salida de la tokenizacion: referencia opaca + ultimos 4 + marca."""

    token_ref: str
    last4: str
    brand: str


def normalize_pan(pan: object) -> str:
    """Normaliza el PAN (sin espacios/guiones); exige 13..19 digitos."""
    if not isinstance(pan, str):
        raise TypeError("pan debe ser texto")
    digits = _PAN_CLEAN_RE.sub("", pan.strip())
    if not digits.isdigit():
        raise ValueError("pan con formato invalido: solo digitos")
    if not PAN_MIN_LEN <= len(digits) <= PAN_MAX_LEN:
        raise ValueError(f"pan con longitud invalida ({PAN_MIN_LEN}..{PAN_MAX_LEN} digitos)")
    if not _luhn_ok(digits):
        raise ValueError("pan con digito de control invalido")
    return digits


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def detect_brand(pan_digits: str) -> str:
    """Marca por BIN (heuristica local del mock; el proveedor real la
    resuelve contra la red)."""
    if pan_digits.startswith("4"):
        return "VISA"
    if pan_digits[:2].isdigit() and 51 <= int(pan_digits[:2]) <= 55:
        return "MASTERCARD"
    if pan_digits[:2] in ("34", "37"):
        return "AMEX"
    if pan_digits[:4].isdigit() and 2221 <= int(pan_digits[:4]) <= 2720:
        return "MASTERCARD"
    return "UNKNOWN"


def mask_account_number(number: object) -> str:
    """Enmascara un numero de cuenta/tarjeta (`****` + ultimos 4)."""
    if not isinstance(number, str) or len(number.strip()) < 4:
        raise ValueError("numero con formato invalido para enmascarar")
    tail = re.sub(r"[\s\-]+", "", number.strip())[-4:]
    if not tail:
        raise ValueError("numero con formato invalido para enmascarar")
    return f"****{tail}"


class WalletTokenizationProvider(Protocol):
    """Interfaz del proveedor de tokenizacion (G67).

    El caso de uso solo depende de esta interfaz; el `mock` es la
    implementacion del MVP y un adaptador `http` futuro la implementa
    sin cambios en `service/` ni en el esquema.
    """

    def tokenize_card(self, pan: str) -> TokenizedCard:
        """Tokeniza el PAN crudo (-> `token_ref`/`last4`/`brand`)."""
        ...  # pragma: no cover - interfaz


class MockWalletTokenizationProvider:
    """Proveedor local determinista (MVP, decision del dueno).

    Mismo PAN -> mismo `token_ref` (`tok_mock_` + SHA-256 truncado, 41
    caracteres, dentro del `VARCHAR(120)`); el crudo se descarta tras
    derivar `last4`/`brand`. Sin red, sin secretos, sin estado.
    """

    def tokenize_card(self, pan: str) -> TokenizedCard:
        digits = normalize_pan(pan)
        digest = hashlib.sha256(f"wallet-card/v1:{digits}".encode()).hexdigest()[:32]
        token = TokenizedCard(
            token_ref=f"{MOCK_TOKEN_PREFIX}{digest}",
            last4=digits[-4:],
            brand=detect_brand(digits),
        )
        logger.debug("tarjeta tokenizada brand=%s last4_len=%d", token.brand, len(token.last4))
        return token
