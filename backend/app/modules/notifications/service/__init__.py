"""Fachada `send` del modulo `notifications` (E1-T09, HU02 CA-01).

Estrategia de envio (documentada):
- **Envio directo via sender inyectable + reintentos con backoff en la
  fachada** (no outbox perezoso): la fila `notifications` ES el registro
  durable. `send` registra `QUEUED` en la sesion del llamante (misma
  transaccion del negocio: si el negocio falla, no hay notificacion
  huerfana), intenta el envio inmediato y deja `SENT` o `FAILED`.
- **Tolerancia a caida de un canal sin perder el mensaje**: el fallo de un
  proveedor solo marca esa fila (`FAILED` con `error`, o `QUEUED` si aun
  quedan intentos en un reimpulso posterior); los demas canales no se ven
  afectados y `retry_notification` reintenta despues la fila pendiente. Un
  worker periodico puede barrer `list_pending` + reimpulsar `FAILED`.
- Backoff exponencial `base * 2**(intento-1)` (mismo patron que
  `shared/repository/outbox.py`), con `sleep_fn` inyectable (tests sin espera).
- Sin PII en logs: solo id de notificacion, canal y codigo de plantilla.
  El `payload_json` persistido contiene solo los datos de render (el
  llamante no debe incluir PII sensible: el render logea unicamente el
  codigo de plantilla).

Alertas de seguridad multicanal (E6-T12, HU22): `notify_security_event`
orquesta **push inmediato + correo de respaldo en paralelo** (ventana
`< 5 s` en el camino feliz) respetando `user_channel_preferences`, con
**email garantizado como respaldo** aunque el usuario lo desmotive
(decision del dueno) y aislamiento total por canal (el fallo de un
proveedor solo marca SU fila, nunca retrasa ni impide al otro). `sms`
no participa en la ruta de seguridad.

Contrato: `send(canal, destinatario, plantilla, datos)` == `send(channel,
recipient, template_code, data, ...)` (nombres en ingles por convencion del
repo). El `sender` se inyecta (por defecto `MockNotificationSender`); el
proveedor real solo implementa el Protocol sin tocar este modulo.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.adapters.notification_sender import (
    CHANNELS,
    GmailNotificationSender,
    MockNotificationSender,
    NotificationProviderError,
    NotificationSender,
    PushNotificationSender,
    TwilioNotificationSender,
)
from app.modules.notifications import repository as repo
from app.modules.notifications.domain.templates import (
    BASE_TEMPLATES,
    format_at_legible,
    humanize_operation,
    render_subject,
    render_template,
)
from app.modules.notifications.models import Notification
from app.modules.shared.config import parameters as config_params

logger = logging.getLogger(__name__)

# Reintentos: tras N intentos la fila pasa a FAILED (terminal hasta reimpulso).
DEFAULT_MAX_ATTEMPTS = 3
# Backoff exponencial base en segundos (CA-01 HU02: envio < 5 s en el caso feliz).
DEFAULT_BACKOFF_BASE_SECONDS = 1

# Ruta de seguridad (E6-T12, HU22; regla de oro 6: claves configurables
# fuera del codigo, leidas via `shared/config` best-effort con fallback;
# las siembra la migracion `0044_notifications_preferences`).
SECURITY_DELIVERY_WINDOW_SECONDS_KEY = "notifications.security_delivery_window_seconds"
DEFAULT_SECURITY_DELIVERY_WINDOW_SECONDS = 5
SECURITY_EMAIL_BACKUP_KEY = "notifications.security_alerts_email_backup"
DEFAULT_SECURITY_EMAIL_BACKUP = True

# Plantillas de la ruta de seguridad (`domain/templates.py`, E6-T12).
PUSH_SECURITY_TEMPLATE = "security_operation_alert"
EMAIL_SECURITY_TEMPLATE = "security_operation_email"

# Preferencias por defecto cuando el titular aun no tiene fila
# (`user_channel_preferences`, `03b#13`; espejo de los defaults de la
# migracion `0044`).
DEFAULT_PUSH_ENABLED = True
DEFAULT_EMAIL_ENABLED = True
DEFAULT_SMS_ENABLED = False

# Ubicacion aproximada cuando el emisor no la aporta (ciudad/region del
# servidor o derivada del evento; jamas coordenadas exactas).
UNKNOWN_LOCATION = "no disponible"
UNKNOWN_DEVICE = "no disponible"

# Resolutor del correo del titular para el respaldo `email` (A3, HU22).
# `service/` NO importa `identity` (frontera: `test_e6_t14` lo prohibe);
# el cableado (`app.composition.wire_security_email_resolver`, llamado en
# el `lifespan`) registra aqui la fachada de solo lectura
# `identity.service.holder_contact.get_holder_email` (D-03, mismo patron
# que la copia certificada de HU11 en `credits`). Sin resolver registrado
# (o si no resuelve) rige el fallback `str(user_id)`: con senders mock el
# respaldo se entrega igual; con `EMAIL_PROVIDER=gmail` el sender valida
# el formato y esa fila queda `FAILED` (reimpulsable) en lugar de enviar
# a un destinatario invalido. El `recipient_email` explicito del emisor
# siempre tiene prioridad sobre el resolutor.
_SECURITY_EMAIL_RESOLVER = None


def set_security_email_resolver(fn) -> None:
    """Registra el resolutor del correo del titular (cableado, A3).

    `fn(session, user_id) -> str | None` (best-effort, solo lectura).
    Idempotente (el ultimo registro gana); `None` lo retira (tests).
    Sin PII en logs (el valor jamas se registra).
    """
    global _SECURITY_EMAIL_RESOLVER
    _SECURITY_EMAIL_RESOLVER = fn


def _resolve_security_email(session: Session, uid: uuid.UUID, explicit: str | None) -> str:
    """Correo del respaldo: explicito > resolutor cableado > `str(uid)`."""
    if explicit and isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    resolver = _SECURITY_EMAIL_RESOLVER
    if resolver is not None:
        try:
            candidate = resolver(session, uid)
        except Exception:  # noqa: BLE001 - best-effort: sin correo rige el fallback
            candidate = None
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return str(uid)


# Proveedores SMS seleccionables por env `SMS_PROVIDER` (default `mock`).
SMS_PROVIDERS = ("mock", "twilio")
# Proveedores email seleccionables por env `EMAIL_PROVIDER` (default `mock`).
EMAIL_PROVIDERS = ("mock", "gmail")
# Proveedores push seleccionables por env `PUSH_PROVIDER` (default `mock`).
# Hoy solo `mock` (desacoplado por canal, E6-T13); el proveedor push real
# futuro se agrega aqui sin tocar el routing ni la fachada `send`.
PUSH_PROVIDERS = ("mock",)


def default_sender() -> NotificationSender:
    """Resuelve el sender por defecto segun env (aditivo E1-T09).

    - `SMS_PROVIDER=mock` (o ausente) -> `MockNotificationSender` (default,
      sin red, sin secretos).
    - `SMS_PROVIDER=twilio` -> `TwilioNotificationSender` con credenciales de
      `TWILIO_ACCOUNT_SID` / `TWILIO_API_KEY_SID` / `TWILIO_AUTH_TOKEN` /
      `TWILIO_FROM_NUMBER`.
    - Esquema separado: `TWILIO_ACCOUNT_SID` es el Account SID real (AC...,
      va en el path `/Accounts/{AC}/Messages.json`); `TWILIO_API_KEY_SID` es
      opcional (API Key SID SK... como usuario del Basic Auth, default = el
      Account SID para auth clasica AC+token); `TWILIO_AUTH_TOKEN` es el
      password (API Key secret o Auth Token). Ver `.env.example`.
    - Otro valor -> `ValueError` (falla rapido, sin reintento).
    """
    provider = (os.getenv("SMS_PROVIDER") or "mock").strip().lower() or "mock"
    if provider == "mock":
        return MockNotificationSender()
    if provider == "twilio":
        account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        # `or account_sid`: sin SK configurado se usa auth clasica AC+token.
        api_key_sid = (os.getenv("TWILIO_API_KEY_SID", "") or account_sid) or ""
        return TwilioNotificationSender(
            account_sid=account_sid,
            api_key_sid=api_key_sid,
            auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
            from_number=os.getenv("TWILIO_FROM_NUMBER", ""),
        )
    raise ValueError(f"SMS_PROVIDER debe ser uno de {SMS_PROVIDERS}, recibido: {provider!r}")


def email_sender() -> NotificationSender:
    """Resuelve el sender de email por env `EMAIL_PROVIDER` (E1-T25).

    - `EMAIL_PROVIDER=mock` (o ausente) -> `MockNotificationSender` (default,
      sin red, sin secretos).
    - `EMAIL_PROVIDER=gmail` -> `GmailNotificationSender` con credenciales de
      `GMAIL_USER` / `GMAIL_APP_PASSWORD` (solo `.env`; nunca en el repo).
    - Otro valor -> `ValueError` (falla rapido, sin reintento).
    """
    provider = (os.getenv("EMAIL_PROVIDER") or "mock").strip().lower() or "mock"
    if provider == "mock":
        return MockNotificationSender()
    if provider == "gmail":
        return GmailNotificationSender(
            gmail_user=os.getenv("GMAIL_USER", ""),
            app_password=os.getenv("GMAIL_APP_PASSWORD", ""),
        )
    raise ValueError(f"EMAIL_PROVIDER debe ser uno de {EMAIL_PROVIDERS}, recibido: {provider!r}")


def push_sender() -> NotificationSender:
    """Resuelve el sender push por env `PUSH_PROVIDER` (E6-T13, HU22).

    - `PUSH_PROVIDER=mock` (o ausente) -> `PushNotificationSender` (default,
      desacoplado por canal, sin red, sin secretos).
    - Otro valor -> `ValueError` (falla rapido, sin reintento; patron
      `SMS_PROVIDER`/`EMAIL_PROVIDER`).
    """
    provider = (os.getenv("PUSH_PROVIDER") or "mock").strip().lower() or "mock"
    if provider == "mock":
        return PushNotificationSender()
    raise ValueError(f"PUSH_PROVIDER debe ser uno de {PUSH_PROVIDERS}, recibido: {provider!r}")


def sender_for_channel(channel: str) -> NotificationSender:
    """Routing de sender por canal (E1-T25, E6-T13), sin alterar el contrato SMS.

    - `sms` -> `SMS_PROVIDER` via `default_sender()` (mock|twilio).
    - `email` -> `EMAIL_PROVIDER` via `email_sender()` (mock|gmail).
    - `push` -> `PUSH_PROVIDER` via `push_sender()` (mock desacoplado).
    - Otro canal -> mock generico (compatibilidad; `send` valida el canal
      antes de llegar aqui).

    Ningun canal comparte estado con otro: cada llamada construye un
    proveedor nuevo e independiente (un fallo tipificado en un canal no
    cruza al otro; lo consume la fachada `send`).
    """
    if channel == "sms":
        return default_sender()
    if channel == "email":
        return email_sender()
    if channel == "push":
        return push_sender()
    return MockNotificationSender()


def compute_backoff_seconds(attempts: int, base_seconds: int) -> int:
    """Backoff exponencial tras `attempts` fallos (>= 1). Dominio puro."""
    if attempts < 1:
        raise ValueError("attempts debe ser >= 1")
    if base_seconds < 0:
        raise ValueError("base_seconds debe ser >= 0")
    return base_seconds * (2 ** (attempts - 1))


def _resolve_template(session: Session, template_code: str) -> dict:
    """Plantilla desde BD (`notification_templates`) con fallback a semilla.

    Si la BD no tiene la plantilla pero es un codigo base conocido, se usa la
    semilla `BASE_TEMPLATES` (permite operar sin seed fisico). Plantilla
    deshabilitada o codigo desconocido -> `ValueError`.
    """
    if not template_code or not str(template_code).strip():
        raise ValueError("template_code es obligatorio")
    stored = repo.get_template(session, template_code)
    if stored is not None:
        if not stored.enabled:
            raise ValueError(f"plantilla deshabilitada: {template_code!r}")
        return {
            "code": stored.code,
            "channel": stored.channel,
            "subject": stored.subject,
            "body_template": stored.body_template,
        }
    if template_code in BASE_TEMPLATES:
        return dict(BASE_TEMPLATES[template_code])
    raise ValueError(f"plantilla desconocida: {template_code!r}")


def send(
    session: Session,
    *,
    channel: str,
    recipient: str,
    template_code: str,
    data: dict,
    user_id: uuid.UUID | str | None = None,
    sender: NotificationSender | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_seconds: int = DEFAULT_BACKOFF_BASE_SECONDS,
    sleep_fn=time.sleep,
    now: datetime | None = None,
) -> Notification:
    """Fachada `send(canal, destinatario, plantilla, datos)`.

    1. Resuelve y renderiza la plantilla (fallo de render -> `ValueError`
       ANTES de persistir: no deja filas residuales).
    2. Registra `QUEUED` (flush, sin commit: misma transaccion del llamante).
    3. Intenta el envio con `sender` (inyectable; mock por defecto) con
       reintentos + backoff; deja `SENT` (`provider_ref`, `sent_at`) o
       `FAILED` (`error` del proveedor, sin PII).
    """
    if channel not in CHANNELS:
        raise ValueError(f"channel debe ser uno de {CHANNELS}, recibido: {channel!r}")
    if not recipient or not str(recipient).strip():
        raise ValueError("recipient es obligatorio")
    if not isinstance(data, dict):
        raise TypeError("data debe ser dict")
    if max_attempts < 1:
        raise ValueError("max_attempts debe ser >= 1")
    sender = sender if sender is not None else sender_for_channel(channel)

    template = _resolve_template(session, template_code)
    if template["channel"] != channel:
        raise ValueError(
            f"plantilla {template_code!r} es de canal {template['channel']!r}, "
            f"recibido: {channel!r}"
        )
    body = render_template(template["body_template"], data)
    # Asunto dinamico cuando la plantilla lo trae (p. ej. el correo de
    # seguridad incluye la etiqueta humana); estatico si no hay `{...}`.
    subject = render_subject(template["subject"], data)
    logger.info("notifications.render channel=%s template=%s", channel, template_code)

    row = repo.save_notification(
        session,
        channel=channel,
        template_code=template_code,
        # `recipient` se persiste (dato de entrega necesario para el
        # reintento posterior); nunca se incluye en logs.
        payload={"template": template_code, "recipient": str(recipient), "data": dict(data)},
        user_id=user_id,
    )
    _attempt_delivery(
        session,
        row,
        sender=sender,
        subject=subject,
        body=body,
        recipient=str(recipient),
        max_attempts=max_attempts,
        backoff_base_seconds=backoff_base_seconds,
        sleep_fn=sleep_fn,
        now=now,
    )
    return row


def _attempt_delivery(
    session: Session,
    row: Notification,
    *,
    sender: NotificationSender,
    subject: str | None,
    body: str,
    recipient: str,
    max_attempts: int,
    backoff_base_seconds: int,
    sleep_fn,
    now: datetime | None,
) -> Notification:
    """Bucle de envio con backoff; actualiza `SENT`/`FAILED` (flush, sin commit)."""
    moment = now or datetime.now(UTC)
    last_error = "sin intentos"
    for attempt in range(1, max_attempts + 1):
        try:
            result = sender.send(
                channel=row.channel, recipient=recipient, subject=subject, body=body
            )
        except NotificationProviderError as exc:
            last_error = str(exc) or "provider caido"
        except (ValueError, TypeError):
            raise
        except Exception as exc:  # noqa: BLE001 - cualquier caida cuenta como intento
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if result.ok:
                return repo.mark_sent(
                    session, row.id, result.provider_ref or "mock-sin-ref", now=moment
                )
            last_error = result.error or "provider rechazo el envio"
        if attempt < max_attempts:
            delay = compute_backoff_seconds(attempt, backoff_base_seconds)
            if delay > 0:
                sleep_fn(delay)
    return repo.mark_failed(session, row.id, last_error)


def retry_notification(
    session: Session,
    notification_id: uuid.UUID,
    sender: NotificationSender | None = None,
    *,
    recipient: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_seconds: int = DEFAULT_BACKOFF_BASE_SECONDS,
    sleep_fn=time.sleep,
    now: datetime | None = None,
) -> Notification:
    """Reimpulsa una notificacion `QUEUED`/`FAILED` (reintento posterior).

    La fila nunca se pierde: tras la caida de un canal, este reintento la
    lleva a `SENT` cuando el proveedor se recupera. `SENT` ya enviada no se
    reenvia (idempotente por estado). Re-renderiza con el `payload` guardado;
    el destinatario sale del `payload` salvo override explicito.
    """
    row = repo.get_notification(session, notification_id)
    if row is None:
        raise ValueError(f"notificacion inexistente: {notification_id}")
    if row.status == "SENT":
        return row
    sender = sender if sender is not None else sender_for_channel(row.channel)
    if row.template_code is None:
        raise ValueError("notificacion sin plantilla: no reintentable")
    template = _resolve_template(session, row.template_code)
    stored = row.payload_json or {}
    data = stored.get("data", {}) if isinstance(stored, dict) else {}
    body = render_template(template["body_template"], data if isinstance(data, dict) else {})
    subject = render_subject(template["subject"], data if isinstance(data, dict) else {})
    to = recipient if recipient is not None else (stored.get("recipient", "") or "")
    if not str(to).strip():
        raise ValueError("reintento sin destinatario")
    row.status = "QUEUED"
    row.error = None
    session.flush()
    return _attempt_delivery(
        session,
        row,
        sender=sender,
        subject=subject,
        body=body,
        recipient=str(to),
        max_attempts=max_attempts,
        backoff_base_seconds=backoff_base_seconds,
        sleep_fn=sleep_fn,
        now=now,
    )


# --------------------------------------- ruta de seguridad (E6-T12, HU22)


def get_security_delivery_window_seconds(session: Session) -> int:
    """Ventana objetivo de entrega del push en segundos (E6-T12 CA-01).

    Lectura best-effort via `shared/config` (nunca lanza): clave ausente,
    ilegible o invalida -> `DEFAULT_SECURITY_DELIVERY_WINDOW_SECONDS`.
    """
    return config_params.get_int_parameter(
        session,
        SECURITY_DELIVERY_WINDOW_SECONDS_KEY,
        DEFAULT_SECURITY_DELIVERY_WINDOW_SECONDS,
        1,
    )


def _coerce_flag(raw: object, default: bool) -> bool:
    """Bandera booleana best-effort (acepta `bool`/`int`/`str`; resto -> default)."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw != 0
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in ("1", "true", "yes", "on", "si", "sí"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False if text else default
        return default
    return default


def is_security_email_backup_enabled(session: Session) -> bool:
    """`True` si el email es respaldo garantizado de seguridad (E6-T12).

    Decision del dueno: el correo de respaldo de eventos de seguridad se
    envia aunque el usuario haya desactivado `email` en sus preferencias
    (las preferencias rigen el resto de notificaciones). Lectura
    best-effort via `shared/config` (nunca lanza).
    """
    raw = config_params.get_parameter(
        session, SECURITY_EMAIL_BACKUP_KEY, DEFAULT_SECURITY_EMAIL_BACKUP
    )
    return _coerce_flag(raw, DEFAULT_SECURITY_EMAIL_BACKUP)


def _sanitize_label(value: object, default: str, max_len: int) -> str:
    """Etiqueta corta sin PII estructurada (acota y normaliza espacios)."""
    text = str(value or "").strip()
    if not text:
        return default
    collapsed = " ".join(text.split())
    return collapsed[:max_len] if len(collapsed) > max_len else collapsed


def _as_iso_at(value: object, now: datetime | None) -> str:
    """Instante ISO de la alerta (el emisor lo aporta; si no, `now`)."""
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value or "").strip()
    if text:
        return text[:64]
    moment = now or datetime.now(UTC)
    return moment.isoformat()


def _deliver_best_effort(
    *,
    sender: NotificationSender,
    channel: str,
    recipient: str,
    subject: str | None,
    body: str,
    max_attempts: int,
    backoff_base_seconds: int,
    sleep_fn,
) -> tuple[bool, str | None, str | None]:
    """Entrega de UN canal con reintentos; jamas lanza ni toca la sesion.

    Retorna `(ok, provider_ref, error)`. A diferencia de
    `_attempt_delivery` (fail-fast ante `ValueError`, usado por `send`),
    aqui TODO fallo del canal —incluida validacion del proveedor— se
    convierte en `(False, None, error)`: el aislamiento por canal es
    total y un canal caido/invalido nunca afecta al otro. Hilo-seguro
    (no toca la sesion; la fachada aplica las marcas despues).
    """
    last_error = "sin intentos"
    for attempt in range(1, max_attempts + 1):
        try:
            result = sender.send(channel=channel, recipient=recipient, subject=subject, body=body)
        except Exception as exc:  # noqa: BLE001 - aislamiento total por canal
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if result.ok:
                return True, result.provider_ref or f"sin-ref-{channel}", None
            last_error = result.error or "provider rechazo el envio"
        if attempt < max_attempts:
            delay = compute_backoff_seconds(attempt, backoff_base_seconds)
            if delay > 0:
                sleep_fn(delay)
    return False, None, str(last_error)[:500]


def notify_security_event(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    event_type: str,
    reference: str,
    device: object = None,
    at: object = None,
    location: object = None,
    recipient_email: str | None = None,
    sender_push: NotificationSender | None = None,
    sender_email: NotificationSender | None = None,
    include_push: bool = True,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_seconds: int = DEFAULT_BACKOFF_BASE_SECONDS,
    sleep_fn=time.sleep,
    now: datetime | None = None,
) -> dict:
    """Alerta de seguridad multicanal: push + correo en paralelo (E6-T12).

    Punto de entrada que los emisores llaman en el momento sensible (cada
    transaccion/login sensible dispara push inmediato y correo de respaldo
    en paralelo, ventana `< 5 s` en el camino feliz). Registra UNA fila
    `notifications` por canal en la sesion del llamante (`flush`, sin
    `commit`) y devuelve ids/estados por canal.

    - Respeta `user_channel_preferences` (`push`); `email` es respaldo
      garantizado (decision del dueno) salvo que la bandera
      `notifications.security_alerts_email_backup` se desactive; `sms`
      nunca participa en la ruta de seguridad.
    - `include_push=False` omite el push aunque la preferencia lo pida
      (lo usa el consumidor de `auth.failed_attempt`: el push de login lo
      sigue emitiendo `risk` (E2-T20) y este consumidor solo aporta el
      correo de respaldo, de modo que queda exactamente UN push por
      evento de login no duplicado).
    - Aislamiento total: el fallo de un proveedor solo marca SU fila
      (`FAILED` con `error`, reimpulsable con `retry_notification`);
      nunca retrasa ni impide al otro canal (la entrega corre en
      paralelo y las marcas se aplican despues, en un solo hilo).
    - Contenido sin PII innecesaria y legible: operacion como etiqueta
      humana ("Transferencia realizada", no el codigo crudo) e instante
      como fecha local legible (`DD/MM/YYYY HH:MM`, no ISO crudo),
      etiqueta de dispositivo acotada y ubicacion aproximada; el
      `reference` (no PII) solo viaja en el correo para
      que `E6-T14` correlacione el reporte "no reconozco". El codigo
      tecnico crudo se conserva en el `payload` (`event_type`) para
      correlacion, nunca en el texto visible.
    - Sin PII en logs: solo ids, canal y codigo de plantilla.
    """
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"user_id debe ser UUID, recibido: {user_id!r}") from exc
    operation = str(event_type or "").strip()
    if not operation:
        raise ValueError("event_type es obligatorio")
    ref = str(reference or "").strip()
    if not ref:
        raise ValueError("reference es obligatoria")
    if max_attempts < 1:
        raise ValueError("max_attempts debe ser >= 1")
    moment = now or datetime.now(UTC)
    at_iso = _as_iso_at(at, moment)
    device_label = _sanitize_label(device, UNKNOWN_DEVICE, 64)
    location_label = _sanitize_label(location, UNKNOWN_LOCATION, 120)
    # Contenido humano: el codigo tecnico crudo (`transfer.settled`, ...)
    # y el ISO con microsegundos jamas llegan al texto visible; se
    # muestran la etiqueta (`Transferencia realizada`, ...) y la fecha
    # local legible (`DD/MM/YYYY HH:MM`). El codigo crudo se conserva en
    # el `payload` (`event_type`) y en el `outbox` para correlacion.
    display_data = {
        "operation": humanize_operation(operation),
        "at": format_at_legible(at_iso, moment),
        "device": device_label,
        "location": location_label,
    }

    stored = repo.get_preferences(session, uid)
    want_push = (stored.push if stored is not None else DEFAULT_PUSH_ENABLED) and bool(include_push)
    prefs_email = stored.email if stored is not None else DEFAULT_EMAIL_ENABLED
    want_email = bool(prefs_email) or is_security_email_backup_enabled(session)
    window = get_security_delivery_window_seconds(session)

    push_template = _resolve_template(session, PUSH_SECURITY_TEMPLATE)
    email_template = _resolve_template(session, EMAIL_SECURITY_TEMPLATE)
    push_data = dict(display_data)
    email_data = dict(display_data, reference=ref)
    push_body = render_template(push_template["body_template"], push_data)
    email_body = render_template(email_template["body_template"], email_data)
    push_subject = render_subject(push_template["subject"], push_data)
    email_subject = render_subject(email_template["subject"], email_data)
    logger.info(
        "notifications.security.render event=%s template_push=%s template_email=%s",
        operation,
        PUSH_SECURITY_TEMPLATE,
        EMAIL_SECURITY_TEMPLATE,
    )

    jobs: list[tuple[str, Notification, NotificationSender, str | None, str, str]] = []
    if want_push:
        push_sender = sender_push if sender_push is not None else sender_for_channel("push")
        push_row = repo.save_notification(
            session,
            channel="push",
            template_code=PUSH_SECURITY_TEMPLATE,
            payload={
                "template": PUSH_SECURITY_TEMPLATE,
                "recipient": str(uid),
                "data": push_data,
                "event_type": operation,
                "reference": ref,
            },
            user_id=uid,
        )
        jobs.append(("push", push_row, push_sender, push_subject, str(uid), push_body))
    if want_email:
        email_sender = sender_email if sender_email is not None else sender_for_channel("email")
        # Destinatario del respaldo (A3): el que aporta el emisor
        # (`recipient_email`), si no el resolutor cableado por
        # `app.composition` (fachada `holder_contact`, D-03, mismo patron
        # que la copia certificada de HU11) y en ultimo caso `str(user_id)`
        # (los senders mock resuelven; el sender Gmail valida el formato y
        # deja la fila `FAILED`/reimpulsable ante un no-email).
        to = _resolve_security_email(session, uid, recipient_email)
        email_row = repo.save_notification(
            session,
            channel="email",
            template_code=EMAIL_SECURITY_TEMPLATE,
            payload={
                "template": EMAIL_SECURITY_TEMPLATE,
                "recipient": to,
                "data": email_data,
                "event_type": operation,
                "reference": ref,
            },
            user_id=uid,
        )
        jobs.append(("email", email_row, email_sender, email_subject, to, email_body))

    outcomes: dict[str, tuple[bool, str | None, str | None]] = {}
    channels: dict[str, dict] = {}
    started = time.monotonic()
    if jobs:
        # B2: cada canal se marca al completarse SU futuro (`as_completed`),
        # sin esperar al mas lento (la entrega ya corre en paralelo; las
        # marcas se aplican en este hilo, que es el unico que toca la
        # sesion). Un futuro que lanzara (solo cancelacion: el worker
        # `_deliver_best_effort` jamas lanza) marca `FAILED` sin tumbar
        # al otro canal.
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            pending = {
                pool.submit(
                    _deliver_best_effort,
                    sender=sender,
                    channel=channel,
                    recipient=recipient,
                    subject=subject,
                    body=body,
                    max_attempts=max_attempts,
                    backoff_base_seconds=backoff_base_seconds,
                    sleep_fn=sleep_fn,
                ): (channel, row)
                for channel, row, sender, subject, recipient, body in jobs
            }
            for future in as_completed(pending):
                channel, row = pending[future]
                try:
                    ok, provider_ref, error = future.result()
                except Exception as exc:  # noqa: BLE001 - defensivo (ver docstring)
                    ok, provider_ref, error = False, None, f"{type(exc).__name__}: {exc}"
                outcomes[channel] = (ok, provider_ref, error)
                if ok:
                    repo.mark_sent(
                        session, row.id, provider_ref or f"sin-ref-{channel}", now=moment
                    )
                else:
                    repo.mark_failed(session, row.id, error or "provider caido", now=moment)
                channels[channel] = {
                    "notification_id": str(row.id),
                    "status": row.status,
                    "provider_ref": row.provider_ref,
                    "error": row.error,
                }
    elapsed = time.monotonic() - started
    logger.info(
        "notifications.security.done event=%s channels=%s elapsed=%.3f window=%s",
        operation,
        sorted(channels),
        elapsed,
        window,
    )
    return {
        "user_id": str(uid),
        "event_type": operation,
        "reference": ref,
        "email_backup": want_email,
        "window_seconds": window,
        "elapsed_seconds": elapsed,
        "channels": channels,
    }


# --------------------------------------- preferencias y centro (E6-T12)


def get_channel_preferences(session: Session, user_id: uuid.UUID | str) -> dict:
    """Preferencias del titular (`{push, email, sms}`; defaults si no hay fila)."""
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"user_id debe ser UUID, recibido: {user_id!r}") from exc
    row = repo.get_preferences(session, uid)
    if row is None:
        return {
            "push": DEFAULT_PUSH_ENABLED,
            "email": DEFAULT_EMAIL_ENABLED,
            "sms": DEFAULT_SMS_ENABLED,
        }
    return {"push": bool(row.push), "email": bool(row.email), "sms": bool(row.sms)}


def set_channel_preferences(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    push: bool,
    email: bool,
    sms: bool,
) -> dict:
    """Guarda las preferencias del titular (idempotente por `user_id`).

    Nota de seguridad: desactivar `email` NO desactiva el respaldo de
    alertas de seguridad (decision del dueno: el correo de respaldo se
    garantiza; las preferencias rigen el resto).
    """
    row = repo.upsert_preferences(session, user_id=user_id, push=push, email=email, sms=sms)
    return {"push": bool(row.push), "email": bool(row.email), "sms": bool(row.sms)}


def _iso_moment(value: datetime | None) -> str | None:
    """ISO-8601 con zona (SQLite devuelve naive: se asume UTC, como Postgres)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.isoformat()


def _notification_view(row: Notification) -> dict:
    """Vista del centro: sin `payload_json` crudo ni PII (solo metadatos).

    A2: expone los campos de render acotados `device`/`location`/
    `reference` (etiquetas ya saneadas por `notify_security_event`, sin
    PII) para que el centro muestre dispositivo/hora/ubicacion sin abrir
    el `payload`. `None` cuando la fila no los trae (plantillas viejas).
    """
    stored = row.payload_json if isinstance(row.payload_json, dict) else {}
    data = stored.get("data") if isinstance(stored.get("data"), dict) else {}
    device = data.get("device") if isinstance(data, dict) else None
    location = data.get("location") if isinstance(data, dict) else None
    reference = stored.get("reference") if isinstance(stored, dict) else None
    return {
        "id": str(row.id),
        "channel": row.channel,
        "template_code": row.template_code,
        "status": row.status,
        "created_at": _iso_moment(row.created_at),
        "read_at": _iso_moment(row.read_at),
        "device": str(device)[:64] if isinstance(device, str) and device.strip() else None,
        "location": str(location)[:120] if isinstance(location, str) and location.strip() else None,
        "reference": (
            str(reference)[:64] if isinstance(reference, str) and reference.strip() else None
        ),
    }


def list_notifications_for_user(
    session: Session,
    *,
    user_id: uuid.UUID | str,
    channel: str | None = None,
    unread_only: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Centro del titular: listado paginado solo propio (sin PII)."""
    if page < 1:
        raise ValueError("page debe ser >= 1")
    if page_size < 1 or page_size > 100:
        raise ValueError("page_size debe estar entre 1 y 100")
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"user_id debe ser UUID, recibido: {user_id!r}") from exc
    rows = repo.list_user_notifications(
        session,
        user_id=uid,
        channel=channel,
        unread_only=bool(unread_only),
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    total = repo.count_user_notifications(
        session, user_id=uid, channel=channel, unread_only=bool(unread_only)
    )
    return {
        "items": [_notification_view(row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def get_notification_detail(
    session: Session,
    *,
    notification_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
) -> dict | None:
    """Detalle read-only de UNA notificacion propia (A2).

    Solo el titular (`None` si ajena/inexistente: el llamante responde
    `404` neutro). Misma vista acotada del listado (sin `payload_json`
    crudo ni PII). Solo lectura (sin `flush`/`commit`).
    """
    try:
        nid = (
            notification_id
            if isinstance(notification_id, uuid.UUID)
            else uuid.UUID(str(notification_id))
        )
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"notification_id debe ser UUID, recibido: {notification_id!r}") from exc
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"user_id debe ser UUID, recibido: {user_id!r}") from exc
    row = repo.get_notification(session, nid)
    if row is None or row.user_id != uid:
        return None
    return _notification_view(row)


def mark_notification_as_read(
    session: Session,
    *,
    notification_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    now: datetime | None = None,
) -> dict | None:
    """Marca leida una notificacion propia (idempotente; `None` si ajena)."""
    try:
        nid = (
            notification_id
            if isinstance(notification_id, uuid.UUID)
            else uuid.UUID(str(notification_id))
        )
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"notification_id debe ser UUID, recibido: {notification_id!r}") from exc
    row = repo.mark_notification_read(session, notification_id=nid, user_id=user_id, now=now)
    if row is None:
        return None
    return _notification_view(row)


# --------------------------------------- reporte "no reconozco" (E6-T14, HU22)


#: Plantilla del acuse del reporte (push, ya sembrada en E6-T12).
REPORT_CONFIRM_TEMPLATE = "security_report_received"
#: Accion de auditoria del reporte (best-effort, sin PII).
REPORT_AUDIT_ACTION = "notifications.unrecognized_report"
#: Tipo de agregado del evento de reporte en el `outbox`.
REPORT_AGGREGATE_TYPE = "notification"

#: Largo maximo del motivo libre del titular (se acepta pero no se
#: persiste en claro: solo `reason_present`; asi un texto con PII
#: accidental nunca llega al `payload` del evento ni a la auditoria).
REPORT_REASON_MAX_LEN = 280


def _sanitize_reason(value: object) -> str | None:
    """Motivo libre del titular, normalizado (o `None` si ausente)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("reason debe ser texto o ausente")
    text = " ".join(value.split()).strip()
    if not text:
        return None
    return text[:REPORT_REASON_MAX_LEN]


def _report_reference(row: Notification, nid: uuid.UUID) -> str:
    """Referencia no PII del reporte (la del correo o el prefijo del id)."""
    stored = row.payload_json if isinstance(row.payload_json, dict) else {}
    for key in ("reference", "event_type"):
        candidate = stored.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return " ".join(candidate.split())[:64]
    return str(nid)[:8]


#: Consumidor de la marca transaccional del reporte en
#: `shared.processed_events` (B3, <= 60 caracteres).
REPORT_CLAIM_CONSUMER = "notifications-report"


def _find_report_confirmation(
    session: Session, uid: uuid.UUID, nid: uuid.UUID
) -> Notification | None:
    """Acuse previo del mismo reporte (`None` si es el primero).

    Solo lectura: recorre las confirmaciones `security_report_received`
    del titular cuyo `payload` enlaza `report_for == notification_id`
    (B3: sin limite arbitrario —antes solo las ultimas 50—; la robustez
    ante carreras la da la marca transaccional en `processed_events`,
    ver `report_unrecognized_operation`). Asi repetir el reporte no
    duplica acuse, evento ni bloqueo (idempotencia por estado, regla
    de oro 5).
    """
    import sqlalchemy as sa

    stmt = (
        sa.select(Notification)
        .where(Notification.user_id == uid)
        .where(Notification.template_code == REPORT_CONFIRM_TEMPLATE)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
    )
    for candidate in session.scalars(stmt).all():
        stored = candidate.payload_json if isinstance(candidate.payload_json, dict) else {}
        if str(stored.get("report_for") or "") == str(nid):
            return candidate
    return None


def report_unrecognized_operation(
    session: Session,
    *,
    notification_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    reason: str | None = None,
    sender: NotificationSender | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Registra el reporte "no reconozco esta operacion" (E6-T14, HU22).

    Solo el titular de la notificacion (`None` si ajena/inexistente: el
    llamante responde `404` neutro). Solo notificaciones de seguridad
    (B4: plantillas `security_*`; otra plantilla -> `ValueError`/422).
    Idempotente por estado + marca transaccional (B3:
    `processed_events` con el id de la alerta): si ya hay acuse para la
    misma notificacion retorna `replayed=True` sin nuevos efectos (sin
    duplicar evento ni bloqueo).

    Efectos del primer reporte (`flush`, sin `commit`): fija `read_at`
    de la alerta, crea el acuse push (`security_report_received`), emite
    `security.operation_reported` por `outbox` (lo consume `risk`, que
    escala a `fraud.alert.raised` y de ahi `identity` revoca sesiones) y
    audita best-effort (`notifications.unrecognized_report`). Sin PII
    en logs/evento/auditoria (solo ids y `reference` no PII; el motivo
    libre nunca se persiste en claro).
    """
    from app.core import events as domain_events
    from app.modules.shared.repository import outbox as outbox_repo

    try:
        nid = (
            notification_id
            if isinstance(notification_id, uuid.UUID)
            else uuid.UUID(str(notification_id))
        )
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"notification_id debe ser UUID, recibido: {notification_id!r}") from exc
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"user_id debe ser UUID, recibido: {user_id!r}") from exc
    clean_reason = _sanitize_reason(reason)
    row = session.get(Notification, nid)
    if row is None or row.user_id != uid:
        return None
    if not isinstance(row.template_code, str) or not row.template_code.startswith("security_"):
        # B4: el reporte solo aplica a notificaciones de seguridad del
        # titular (plantillas `security_*`); otra plantilla -> 422.
        raise ValueError("solo las notificaciones de seguridad son reportables")
    previous = _find_report_confirmation(session, uid, nid)
    if previous is not None:
        logger.info("notifications.report.replay id=%s", nid)
        return {
            "reported": True,
            "replayed": True,
            "notification_id": str(nid),
            "confirmation_id": str(previous.id),
        }
    moment = now or datetime.now(UTC)
    # B3: marca transaccional del reporte (`processed_events`, PK
    # (`event_id`, `consumer`) con el id de la alerta): el primer reporte
    # la reclama y crea los efectos; un repetido o un concurrente
    # perdedor va a replay sin nuevos efectos (sin duplicar acuse,
    # evento ni bloqueo). La marca vive en la misma transaccion del
    # reporte: si el reporte revierte, la marca revierte con el.
    if not outbox_repo.try_mark_processed(session, event_id=nid, consumer=REPORT_CLAIM_CONSUMER):
        raced = _find_report_confirmation(session, uid, nid)
        if raced is None:  # pragma: no cover - defensivo
            raise RuntimeError("reporte en curso para esta notificacion")
        logger.info("notifications.report.replay_carrera id=%s", nid)
        return {
            "reported": True,
            "replayed": True,
            "notification_id": str(nid),
            "confirmation_id": str(raced.id),
        }
    repo.mark_notification_read(session, notification_id=nid, user_id=uid, now=moment)
    ref = _report_reference(row, nid)
    at_iso = moment.isoformat()
    confirmation = send(
        session,
        channel="push",
        recipient=str(uid),
        template_code=REPORT_CONFIRM_TEMPLATE,
        data={"reference": ref, "at": at_iso},
        user_id=uid,
        sender=sender,
    )
    stored = dict(confirmation.payload_json or {})
    stored["report_for"] = str(nid)
    stored["reference"] = ref
    confirmation.payload_json = stored
    session.flush()
    outbox_repo.insert_event(
        session,
        aggregate_type=REPORT_AGGREGATE_TYPE,
        aggregate_id=nid,
        event_type=domain_events.SECURITY_OPERATION_REPORTED,
        payload={
            "user_id": str(uid),
            "notification_id": str(nid),
            "reference": ref,
            "at": at_iso,
        },
    )
    try:
        from app.modules.audit.service import record as audit_record

        audit_record(
            session,
            actor=uid,
            action=REPORT_AUDIT_ACTION,
            entity="notification",
            entity_id=nid,
            metadata={
                "notification_id": str(nid),
                "reference": ref,
                "confirmation_id": str(confirmation.id),
                "reason_present": clean_reason is not None,
            },
        )
    except Exception as exc:  # noqa: BLE001 - best-effort documentado
        logger.warning("notifications.report auditoria no registrada error=%s", type(exc).__name__)
    logger.info("notifications.report id=%s confirmation=%s", nid, confirmation.id)
    return {
        "reported": True,
        "replayed": False,
        "notification_id": str(nid),
        "confirmation_id": str(confirmation.id),
        "reference": ref,
    }
