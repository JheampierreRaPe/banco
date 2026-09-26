# Modulo notifications

Contrato y fronteras: ver `docs/modules/README.md#notifications`.

Estado: implementado (modelos `notifications`/`notification_templates` con `push|email|sms` y `QUEUED|SENT|FAILED` + `read_at`, repositorio `queue/mark_sent/mark_failed`, `GmailNotificationSender` con `EMAIL_PROVIDER=gmail|mock`; E1-T24..T28, P-S2-01). Detalle de frontera en `docs/modules/README.md#notifications`.
