"""Plantillas base y render puro del modulo `notifications` (E1-T09).

Dominio puro: sin BD, sin red, sin `float`, sin PII en logs. El render usa
`str.format` con validacion estricta (placeholder ausente o dato no escalar
-> `ValueError` antes de tocar persistencia o red).
"""

from __future__ import annotations

import logging
import string

logger = logging.getLogger(__name__)

# Plantillas semilla minimas (HU02): un canal natural por codigo. `code` es
# unico global (`03b#13`: UQ), por eso cada codigo vive en un solo canal.
BASE_TEMPLATES: dict[str, dict] = {
    "otp_code": {
        "code": "otp_code",
        "channel": "sms",
        "subject": None,
        "body_template": "Tu codigo de verificacion es {code}. Vence en {ttl_minutes} minutos.",
    },
    "account_activated": {
        "code": "account_activated",
        "channel": "email",
        "subject": "Tu cuenta esta activa",
        "body_template": "Tu cuenta {account_masked} fue activada. Bienvenido/a.",
    },
    "login_alert": {
        "code": "login_alert",
        "channel": "push",
        "subject": None,
        "body_template": "Nuevo inicio de sesion desde {device} el {at}.",
    },
}

_SCALAR_TYPES = (str, int, bool, type(None))


def render_template(body_template: str, data: dict) -> str:
    """Renderiza `body_template` con `data`. Puro y validado.

    - Todo `{placeholder}` debe existir en `data` (si falta -> `ValueError`).
    - Los valores deben ser escalares (`str`/`int`/`bool`/`None`): asi ningun
      objeto con PII estructurada se vuelca al mensaje por accidente.
    - El log solo incluye el codigo de plantilla, nunca los datos.
    """
    if not body_template or not str(body_template).strip():
        raise ValueError("body_template es obligatorio")
    if not isinstance(data, dict):
        raise TypeError("data debe ser dict")
    for key, value in data.items():
        if not isinstance(value, _SCALAR_TYPES):
            raise TypeError(f"dato no escalar para placeholder: {key!r}")
    placeholders = {name for _, name, _, _ in string.Formatter().parse(body_template) if name}
    missing = {name for name in placeholders if name.split(".")[0].split("[")[0] not in data}
    if missing:
        raise ValueError(f"placeholders sin dato: {sorted(missing)}")
    try:
        return body_template.format(**data)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"render de plantilla fallo: {exc}") from exc


def get_base_template(code: str) -> dict:
    """Devuelve la plantilla base `code` (copia). `KeyError` si no existe."""
    return dict(BASE_TEMPLATES[code])
