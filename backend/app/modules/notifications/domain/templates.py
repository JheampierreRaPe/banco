"""Plantillas base y render puro del modulo `notifications` (E1-T09).

Dominio puro: sin BD, sin red, sin `float`, sin PII en logs. El render usa
`str.format` con validacion estricta (placeholder ausente o dato no escalar
-> `ValueError` antes de tocar persistencia o red).
"""

from __future__ import annotations

import logging
import re
import string
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# Plantillas semilla minimas (HU02): un canal natural por codigo. `code` es
# unico global (`03b#13`: UQ), por eso cada codigo vive en un solo canal.
BASE_TEMPLATES: dict[str, dict] = {
    "otp_code": {
        "code": "otp_code",
        "channel": "sms",
        "subject": None,
        "body_template": "Tu código de verificación es {code}. Vence en {ttl_minutes} minutos.",
    },
    "otp_code_email": {
        "code": "otp_code_email",
        "channel": "email",
        "subject": "Tu código de verificación",
        "body_template": (
            "Tu código de verificación es {code}. Vence en {ttl_minutes} minutos. "
            "Si no solicitaste este código, ignora este mensaje."
        ),
    },
    "account_activated": {
        "code": "account_activated",
        "channel": "email",
        "subject": "Tu cuenta está activa",
        "body_template": (
            "Tu cuenta {account_masked} fue activada. "
            "Ya puedes ingresar a tu banca en línea. ¡Bienvenido/a!"
        ),
    },
    "login_alert": {
        "code": "login_alert",
        "channel": "push",
        "subject": None,
        "body_template": (
            "Nuevo inicio de sesión desde {device} el {at}. "
            "Si no fuiste tú, revisa tu actividad."
        ),
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
        "subject": "Tu contrato de crédito fue firmado",
        "body_template": (
            "Tu contrato de crédito {application_ref} fue firmado el {signed_at}. "
            "La copia certificada está disponible en tu expediente."
        ),
    },
    # Cuotas del prestamo (E3-T19, HU12 CA-03/CA-04): un canal natural
    # por codigo (push). Cuerpo generico sin PII ni cifras (solo la
    # referencia del prestamo —prefijo del `loan_id`— y la fecha; el
    # detalle vive en el cronograma, que solo consulta el titular).
    "loan_installment_reminder": {
        "code": "loan_installment_reminder",
        "channel": "push",
        "subject": None,
        "body_template": (
            "Tu prestamo {loan_ref} tiene una cuota que vence el {due_on}. " "Revisa tu cronograma."
        ),
    },
    "loan_auto_debit_failed": {
        "code": "loan_auto_debit_failed",
        "channel": "push",
        "subject": None,
        "body_template": (
            "No pudimos debitar automaticamente la cuota de tu prestamo "
            "{loan_ref} el {at}. Revisa tu cronograma."
        ),
    },
    # Aviso de abono QR al comercio (E4-T08, HU14 CA-04; `docs/18` G72):
    # canal push, invocado por la fachada `send(...)` desde el pago de
    # `HU15` (evento `qr.payment.confirmed`) via el helper
    # `wallet.service.qr.notify_merchant_charge_paid`. Cuerpo generico
    # sin PII ni cifras del pagador (solo la referencia del cargo y el
    # instante; el detalle vive en el panel del comercio, que solo
    # consulta el dueno). El aviso se emite dentro de la ventana < 5 s
    # (CA-04); `E4-T10` lo valida con un pago simulado y el E2E con el
    # pago real queda para la QA de HU15.
    "qr_charge_paid": {
        "code": "qr_charge_paid",
        "channel": "push",
        "subject": None,
        "body_template": (
            "Recibiste un pago con QR {charge_ref} el {at}. "
            "Revisa el detalle en tu panel de comercio."
        ),
    },
    # Recordatorio de vencimiento de servicios (E4-T18, HU16 CA-03):
    # canal push, invocado por la fachada `send(...)` desde el job
    # `wallet/jobs/service_reminders.py` (best-effort). Cuerpo
    # generico sin PII ni cifras (solo la referencia del servicio
    # —prefijo del `subscription_id`— y la fecha de vencimiento; el
    # detalle vive en los servicios registrados, que solo consulta
    # el titular).
    "service_reminder": {
        "code": "service_reminder",
        "channel": "push",
        "subject": None,
        "body_template": (
            "Tu servicio {service_ref} vence el {due_on}. " "Revisa tus servicios registrados."
        ),
    },
    # Alertas de seguridad multicanal (E6-T12, HU22 CA-01/CA-02):
    # las invoca la fachada `notify_security_event` (push inmediato +
    # correo de respaldo en paralelo, ventana < 5 s). Cuerpo generico
    # sin PII ni cifras: solo la operacion (**etiqueta humana**, no el
    # codigo tecnico crudo), el instante `{at}` (**fecha local legible
    # `DD/MM/YYYY HH:MM`**, no ISO crudo), la etiqueta de
    # dispositivo `{device}` (acotada) y la ubicacion aproximada
    # `{location}` (ciudad/region o "no disponible"; jamas
    # coordenadas, PAN, montos, firmas, `nonce` ni biometria). El
    # detalle completo vive en la app/centro, no en el push ni en el
    # correo. El `{reference}` (no PII) solo viaja en el correo para
    # que `E6-T14` correlacione el reporte "no reconozco". El codigo
    # tecnico crudo se conserva en el `payload` (`event_type`) para
    # correlacion, nunca en el texto visible.
    "security_operation_alert": {
        "code": "security_operation_alert",
        "channel": "push",
        "subject": None,
        "body_template": (
            "Alerta de seguridad: {operation} el {at}. "
            "Dispositivo: {device}. Ubicación: {location}. "
            "Si no fuiste tú, revisa tu actividad en la app."
        ),
    },
    "security_operation_email": {
        "code": "security_operation_email",
        "channel": "email",
        "subject": "Nueva operación en tu cuenta: {operation}",
        "body_template": (
            "Detectamos {operation} el {at}. "
            "Dispositivo: {device}. Ubicación: {location}. "
            "Referencia: {reference}. Si no reconoces esta operación, "
            "revisa tu actividad en la app."
        ),
    },
    # Acuse del reporte "no reconozco" (E6-T12 lo registra, E6-T14 lo
    # invoca): canal push, sin PII ni cifras (solo la referencia del
    # reporte y el instante).
    "security_report_received": {
        "code": "security_report_received",
        "channel": "push",
        "subject": None,
        "body_template": (
            "Recibimos tu reporte {reference} el {at}. Lo estamos " "revisando y te avisaremos."
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


# --------------------------------------- contenido humano (pulido de correos)
#
# Los emisores aportan el codigo tecnico del evento (`transfer.settled`,
# `auth.failed_attempt`, ...) y el instante ISO; la fachada
# `notify_security_event` los convierte a texto legible ANTES de renderizar
# (el codigo crudo se conserva en el `payload` como `event_type` para
# correlacion, nunca en el texto visible). Dominio puro: sin BD, sin red,
# sin PII en logs.

#: Etiqueta humana por codigo de evento de seguridad (los seis eventos de
#: `SENSITIVE_BUSINESS_EVENTS` + el intento de login + respaldo generico).
OPERATION_LABELS: dict[str, str] = {
    "transfer.settled": "Transferencia realizada",
    "credit.disbursed": "Desembolso de crédito",
    "loan.installment.paid": "Pago de cuota de crédito",
    "qr.payment.confirmed": "Pago con QR",
    "service.paid": "Pago de servicio",
    "topup.completed": "Recarga completada",
    "auth.failed_attempt": "Intento de inicio de sesión",
    "auth.blocked": "Bloqueo preventivo de seguridad",
    "sensitive.operation": "Operación sensible",
}

#: Largo maximo de la etiqueta sustituta de un codigo desconocido.
OPERATION_LABEL_MAX_LEN = 64


def humanize_operation(code: object) -> str:
    """Etiqueta humana para el codigo tecnico `code` (puro).

    Codigo conocido -> etiqueta fija (ver `OPERATION_LABELS`); codigo
    desconocido -> sustituto legible (separadores `.`/`_`/`-`/`/` por
    espacios, acotado); vacio -> generico. Jamas devuelve el codigo
    crudo con puntos cuando hay etiqueta conocida.
    """
    text = str(code or "").strip()
    if not text:
        return "Operación de tu cuenta"
    label = OPERATION_LABELS.get(text)
    if label:
        return label
    fallback = re.sub(r"[._\-/]+", " ", text).strip()
    collapsed = " ".join(fallback.split())
    if not collapsed:
        return "Operación de tu cuenta"
    return collapsed[:OPERATION_LABEL_MAX_LEN]


def _lima_tz():
    """Zona `America/Lima` (UTC-5 sin horario de verano); respaldo fijo."""
    try:
        return ZoneInfo("America/Lima")
    except (ZoneInfoNotFoundError, ValueError):
        return timezone(timedelta(hours=-5), "America/Lima")


def format_at_legible(value: object, now: datetime | None = None) -> str:
    """Fecha/hora local legible `DD/MM/YYYY HH:MM` (puro).

    Acepta `datetime` (naive = UTC, como SQLite) o texto ISO (con `Z`,
    offset o microsegundos); convierte a hora de Lima. Si no se puede
    interpretar, devuelve el texto saneado acotado (jamas vacio ni con
    microsegundos crudos cuando el parseo tiene exito).
    """
    moment = now or datetime.now(UTC)
    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text:
            candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
            try:
                parsed = datetime.fromisoformat(candidate[:64])
            except ValueError:
                return text[:64]
    if parsed is None:
        parsed = moment
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        local = parsed.astimezone(_lima_tz())
    except (ValueError, OverflowError):
        return str(value or "")[:64] or moment.isoformat()[:64]
    return local.strftime("%d/%m/%Y %H:%M")


def render_subject(subject: str | None, data: dict) -> str | None:
    """Renderiza el asunto con `data` cuando trae placeholders (puro).

    `None` (push/sms) o texto sin `{...}` se devuelve tal cual (contrato
    intacto para las plantillas de asunto estatico); con placeholders se
    aplica la misma validacion estricta que `render_template`.
    """
    if subject is None:
        return None
    text = str(subject)
    if "{" not in text or "}" not in text:
        return text
    if not isinstance(data, dict):
        raise TypeError("data debe ser dict")
    return render_template(text, data)
