# Modulo notifications

Contrato y fronteras: ver `docs/modules/README.md#notifications`.

Estado: implementado (modelos `notifications`/`notification_templates` con `push|email|sms` y `QUEUED|SENT|FAILED` + `read_at`, repositorio `queue/mark_sent/mark_failed`, `GmailNotificationSender` con `EMAIL_PROVIDER=gmail|mock`; E1-T24..T28, P-S2-01; adaptador push desacoplado `PushNotificationSender` con `PUSH_PROVIDER=mock` + routing `sender_for_channel`/`push_sender`, E6-T13; alertas de seguridad multicanal + preferencias + centro, E6-T12). Detalle de frontera en `docs/modules/README.md#notifications`.

## Alertas de seguridad (E6-T12, HU22)

- Fachada `service.notify_security_event(session, *, user_id, event_type, reference, device=None,
  at=None, location=None, recipient_email=None, sender_push=None, sender_email=None, ...)`:
  registra UNA fila `notifications` por canal (`flush`, sin `commit`; misma transaccion del
  llamante), entrega push + correo **en paralelo** (`ThreadPoolExecutor`; los hilos solo llaman
  al sender, nunca tocan la sesion) y aplica `SENT`/`FAILED` despues. Retorna
  `{user_id, event_type, reference, email_backup, window_seconds, elapsed_seconds, channels}`.
- **Email garantizado como respaldo** (decision del dueno): aunque `email=false` en
  `user_channel_preferences`, la alerta de seguridad envia el correo (bandera
  `notifications.security_alerts_email_backup`, default `true`; las preferencias rigen el resto).
  `sms` nunca participa en la ruta de seguridad.
- Plantillas (`domain/templates.py`): `security_operation_alert` (push),
  `security_operation_email` (email, con `reference` no PII) y `security_report_received`
  (acuse para `E6-T14`). Sin PII ni cifras: operacion + `{at}` + `{device}` + `{location}`.
- Preferencias: `repository.get_preferences/upsert_preferences` (idempotente por `user_id` PK) +
  `service.get/set_channel_preferences`; migracion `0044_notifications_preferences`.
- Centro (solo titular, `api/`): `GET /notifications` (filtros `channel`/`unread`, paginado),
  `GET /notifications/{id}` (detalle read-only de una notificacion propia, `404` neutro; fix A2),
  `POST /notifications/{id}/read` (idempotente, `404` neutro), `GET/PUT /notifications/preferences`.
  Respuestas solo con metadatos + `device`/`location`/`reference` acotados (sin `payload_json` ni PII); contrato en `docs/05#6.9`.
- Consumidor D-04 `events.on_auth_failed_attempt` (consumidor `notifications-security`,
  idempotente por `processed_events`): mapea `auth.failed_attempt` a la fachada con
  `include_push=False` —solo el correo de respaldo, porque el push de login lo emite
  `risk` (E2-T20): exactamente UN push por evento (fix A1, cierra `docs/18` G80).
  Consumidor D-04 `events.on_sensitive_business_event` (mismo consumidor idempotente):
  `transfer.settled`/`credit.disbursed`/`loan.installment.paid`/`qr.payment.confirmed`/
  `service.paid`/`topup.completed` -> `notify_security_event` (push + correo con
  dispositivo/hora/ubicacion; fallback aproximado si el evento no los trae; fix A1).
  Ambos suscritos en el arranque (`composition.subscribe_security_alerts`, verificado en
  `backend/tests/test_fix_security_alerts_a1.py::test_a1_security_alerts_wired_without_duplicates`).
  Destinatario del respaldo (fix A3): `recipient_email` del emisor > resolutor cableado
  (`composition.wire_security_email_resolver` -> `holder_contact.get_holder_email`, D-03,
  patron HU11) > fallback `str(user_id)` (mock resuelve; Gmail valida y deja `FAILED`
  ante no-email: el proveedor NO resuelve). `service/` nunca importa otro modulo (guardian
  `test_e6_t14_unrecognized_operation.py::test_notifications_never_touches_risk_or_identity_tables`).
- Claves configurables (`config.parameters`, lectura best-effort con fallback, nunca lanzar):
  `notifications.security_delivery_window_seconds` (`5`) y
  `notifications.security_alerts_email_backup` (`true`); sembradas por `0044`.
- Pruebas: `backend/tests/test_e6_t12_notifications_security.py` (26 pruebas: feliz < 5 s,
  paralelismo por rendezvous, preferencias con respaldo garantizado, aislamiento por canal,
  centro aislado por titular, consumidor idempotente, sin PII en logs, cadena `0044` +
  roundtrip en Postgres).

## Reporte "no reconozco" (E6-T14, HU22)

- Endpoint `POST /notifications/{id}/report` (`api/`, Bearer, solo el titular; body
  opcional `{reason?}` con `schemas.ReportUnrecognizedBody`): `200
  {reported, replayed, notification_id, confirmation_id, reference}` idempotente,
  `404 NOT_FOUND` neutro (ajena/inexistente), `422` (id/body malformados);
  contrato en `docs/05#6.9`.
- `service.report_unrecognized_operation(...)` (`flush`, sin `commit`): fija `read_at` de
  la alerta, crea el acuse push `security_report_received` (con `report_for` para la
  idempotencia por estado + marca transaccional en `processed_events`, fix B3; solo
  plantillas `security_*`, fix B4), emite `security.operation_reported` por `outbox`
  (constante en `app/core/events.py`) y audita best-effort
  (`notifications.unrecognized_report`). El motivo libre nunca se persiste en claro
  (solo `reason_present`); sin PII en logs/evento/auditoria. No importa ni escribe
  modelos de `risk`/`identity` (solo `shared`/`audit`; guardian en
  `backend/tests/test_e6_t14_unrecognized_operation.py`).
- La cadena sigue por eventos D-04 (suscritos en el arranque via `app.composition`):
  `risk.events.on_unrecognized_operation_reported` (severidad + `fraud.alert.raised`,
  sin `risk.alerts`, D-16) -> `identity.events.on_fraud_alert_raised`
  (`sessions.revoked_at`, nada se borra).
- Pruebas: `backend/tests/test_e6_t14_unrecognized_operation.py` (12 pruebas: feliz con
  bloqueo efectivo + alerta unica + auditoria, motivo opcional sin persistirlo en claro,
  idempotencia de endpoint y de eventos, `404` neutro sin efectos, `401`, `422`,
  frontera, sin PII, OpenAPI y cableado).
