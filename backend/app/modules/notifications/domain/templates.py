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
    "otp_code_email": {
        "code": "otp_code_email",
        "channel": "email",
        "subject": "Tu codigo de verificacion",
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
    # Seguridad de pagos sensibles (E2-T20, HU08 CA-04/CA-02): un canal
    # natural por codigo. Cuerpo generico sin PII (solo el instante `{at}`;
    # el `user_id` viaja en la fila, no en el texto).
    "auth_failed_attempt": {
        "code": "auth_failed_attempt",
        "channel": "push",
        "subject": None,
        "body_template": (
            "Detectamos un intento de autorizacion no reconocido en tu cuenta "
            "el {at}. Si no fuiste tu, revisa tu dispositivo."
        ),
    },
    "auth_blocked": {
        "code": "auth_blocked",
        "channel": "push",
        "subject": None,
        "body_template": (
            "Bloqueamos preventivamente tu operacion tras varios intentos "
            "fallidos el {at}. Si no fuiste tu, contacta a tu banco."
        ),
    },
    # Dictamen de credito (E3-T08, HU10 CA-03/CA-04): un canal natural por
    # codigo (push). Cuerpo generico sin PII ni cifras (el detalle vive en
    # el expediente, que solo consulta el titular).
    "credit_application_approved": {
        "code": "credit_application_approved",
        "channel": "push",
        "subject": None,
        "body_template": (
            "Tu solicitud de credito fue aprobada. Revisa el detalle en tu expediente."
        ),
    },
    "credit_application_rejected": {
        "code": "credit_application_rejected",
        "channel": "push",
        "subject": None,
        "body_template": (
            "Tu solicitud de credito fue evaluada y no fue aprobada. "
            "Revisa el detalle en tu expediente."
        ),
    },
    "credit_review_required": {
        "code": "credit_review_required",
        "channel": "push",
        "subject": None,
        "body_template": (
            "Tu solicitud de credito quedo en revision por un analista. "
            "Te avisaremos el dictamen final."
        ),
    },
    # Copia certificada del contrato firmado (E3-T14, HU11 CA-04): canal
    # `email` al correo del titular (`users.email`). Cuerpo generico sin
    # PII ni cifras (la referencia es el prefijo del expediente; el
    # detalle vive en el juego contractual, que solo descarga el
    # titular). El sender actual solo transporta `subject`/`body` (sin
    # adjuntos): el aviso remite al expediente para la descarga.
    "credit_contract_signed": {
        "code": "credit_contract_signed",
        "channel": "email",
        "subject": "Tu contrato de credito fue firmado",
        "body_template": (
            "Tu contrato de credito {application_ref} fue firmado el {signed_at}. "
            "La copia certificada queda disponible en tu expediente."
        ),
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
