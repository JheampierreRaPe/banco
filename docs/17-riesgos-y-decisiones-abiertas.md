# 17 - Riesgos y decisiones abiertas

## 1. Decisiones abiertas (requieren confirmacion)

| # | Decision | Opciones | Impacto | Recomendacion |
|---|---|---|---|---|
| D01 | Contrato interbancario con el equipo par | **Resuelto** | Alto | Se usa un **simulador** con contrato propio; se adapta cuando el equipo par publique el suyo. |
| D02 | Stack del panel web | React+TS / Flutter Web | Medio | React+TS por grillas y tableros. |
| D03 | Proveedor de notificaciones | Real (free tier) / mock | Bajo | Mock desacoplado + un canal real si hay tiempo. |
| D04 | API de tipo de cambio | Proveedor real / tasa semilla | Medio | Adaptador con fallback a semilla. |
| D05 | Responsable DevOps (rol 6) | **Riesgo aceptado para el MVP/demo (2026-09-26)**: sin titular formal; los artefactos CI/Docker existen y estan healthy; el titular formal queda para produccion | Medio | Aceptar para el MVP/demo; designar titular formal antes de produccion. |
| D06 | Estandar de QR interoperable | EMVCo-like / formato propio firmado | Medio | Formato propio firmado + adaptador de estandar. |
| D07 | Biometria | **Resuelto**: liveness solo en HU01; login/recuperacion/pagos/firma con biometria del dispositivo | Medio | Nonce firmado por dispositivo + `device_bindings`. |
| D08 | Naming y estrategia de repos | Monorepo + publicaciones / un repo por modulo | Bajo | Monorepo y extraccion a repo propio por modulo cuando madure. |
| D09 | Score de central de riesgo simulada | Reglas fijas / aleatorio controlado | Bajo | Reglas fijas por perfil para pruebas reproducibles. |
| D10 | Recorte de `Should` si hay retraso | Recortar HU04/HU07/HU09/HU16/HU22/HU24/HU25 | Alto | Acordar de antemano el orden de recorte. |
| D11 | Uso de librerias de IA (DeepFace) | Ya esta en el microservicio | Bajo | Mantener fuera del nucleo; es un servicio externo. |

## 2. Riesgos heredados de los documentos fuente (consolidados)

| ID | Riesgo | P | I | Nivel | Mitigacion | Contingencia | Responsable |
|---|---|---|---|---|---|---|---|
| R01 | Falla al integrar KYC en semanas tempranas | 3 | 3 | Critico | Probar con el microservicio desde Semana 1. | Registro temporal con OTP y biometria despues. | Backend/Seguridad |
| R02 | Rol DevOps sin titular | 3 | 2 | Alto | Aceptado para el MVP/demo (2026-09-26): artefactos CI/Docker existentes y healthy; tareas repartidas; titular formal pendiente para produccion. | Despliegues manuales asistidos. | Scrum Master |
| R03 | Inconsistencia de saldos/partida doble | 2 | 3 | Alto | Suite del motor y revision cruzada. | Pausar y depurar la traza contable. | Datos |
| R04 | Fallo en la matriz de accesos (ver datos de otro) | 2 | 3 | Alto | Pruebas de roles y enmascaramiento por sprint. | Bloquear modulo y control manual. | Seguridad |
| R05 | Sobrecarga en Sprints 1-2 | 2 | 2 | Medio | Dailies y regla de recorte de `Should`. | Enfocar solo `Must` del MVP. | Scrum Master |
| R06 | Complejidad del motor de cuotas/credito | 2 | 2 | Medio | Diseno de formulas antes de programar; casos de referencia. | Modelo base fijo y refinar despues. | Frontend/Datos |
| R07 | Concurrencia/duplicidad en transferencias | 2 | 3 | Alto | Idempotencia obligatoria + bloqueo de filas. | Reversar y reintentar. | Backend |
| R08 | Incompatibilidad de QR interoperable | 2 | 2 | Medio | Formato firmado y pruebas cruzadas. | Entrada manual de referencia. | Frontend |
| R09 | Desfase en conciliacion | 2 | 3 | Alto | Cruce diario automatizado. | Ajuste manual autorizado. | Datos |
| R10 | Falsos positivos del antifraude | 3 | 2 | Alto | Calibracion con escenarios controlados. | Relajar umbrales y revision rapida. | Seguridad |
| R11 | Bugs criticos de ultima hora | 3 | 2 | Alto | Regresion continua por sprint. | Congelar features y corregir. | Todo el equipo |
| R12 | Fallo tecnico en la exposicion final | 1 | 3 | Medio | Video de respaldo y entorno estable. | Presentar con la grabacion. | Scrum Master |

## 3. Riesgos nuevos (arquitectura, seguridad, datos)

| ID | Riesgo | P | I | Nivel | Mitigacion |
|---|---|---|---|---|---|
| R13 | Fuga de datos biometricos o PII | 2 | 3 | Alto | No persistir frames; cifrado; enmascaramiento; no loggear PII. |
| R14 | Acoplamiento entre modulos que impide extraerlos | 3 | 2 | Alto | Respetar fachadas/eventos y revision de imports. |
| R15 | Latencia del microservicio KYC en la demo | 2 | 2 | Medio | Precalentar modelos; timeouts; flujo simulado de respaldo. |
| R16 | Dependencia de internet para modelo ArcFace | 2 | 2 | Medio | Cache de pesos y warmup en el arranque. |
| R17 | Idempotencia mal implementada (doble cobro) | 2 | 3 | Alto | Pruebas dedicadas + almacenamiento de respuesta. |
| R18 | Cierre contable que no cuadra por datos sucios | 2 | 3 | Alto | Validador de cuadre y control de consistencia. |
| R19 | Migraciones divergentes entre desarrolladores | 2 | 2 | Medio | Una sola linea de migraciones y CI que valida up/down. |
| R20 | Reloj/zona horaria en fechas de negocio | 2 | 2 | Medio | UTC en base, conversion en frontera; corte diario definido. |
| R21 | Listado/export de movimientos filtran en memoria (hasta `FETCH_LIMIT` 10000 filas) | 1 | 2 | Bajo | Aceptado por alcance académico (fase 4, decisión del dueño): si el volumen crece, mover filtros/paginación a SQL en `accounts/repository/movements.py`. |
| R22 | OTP en claro en `notifications.payload_json` | 2 | 1 | Bajo | Aceptado por alcance académico (fase E1-T24..T28): con proveedor `mock` el codigo queda en el payload persistido para pruebas. En produccion con proveedor real, cifrar/redactar el payload o no persistir el codigo; el acceso a la tabla ya esta restringido al backend. |
| R23 | Notificacion best-effort emitida antes del `commit` | 1 | 2 | Bajo | Aceptado: si la transaccion falla despues del envio, el usuario podria recibir un codigo sin alta persistida; el codigo es de un solo uso y el reenvio lo reemplaza. Mitigacion futura: publicar via outbox y notificar post-commit (worker). |
| R24 | Codigo muerto: constantes de canal/plantilla en `identity` sin uso | 1 | 1 | Bajo | Aceptado: `ACTIVATION_OTP_EMAIL_CHANNEL/TEMPLATE` y `ACTIVATION_OTP_SMS_CHANNEL/TEMPLATE` (`service/__init__.py`) quedaron definidas pero el routing real vive en `activation.resolve_activation_delivery`. Limpieza en una tarea de refactor; sin impacto funcional. |
| R25 | `DOC_HASH_PEPPER` con fallback constante si no se configura en prod | 2 | 2 | Bajo | Aceptado con mitigacion: el hash de documento usa HMAC-SHA256 con pepper de entorno; si falta, cae a una constante de desarrollo documentada (nunca logueada) para no romper local/pruebas. En produccion es obligatorio definir `DOC_HASH_PEPPER` largo/aleatorio (`.env.example`); cambiarlo invalida los hashes previos (UQ). |
| R26 | `device_public_key` = `"hmac:"+hex(secreto de firma)`: una fuga de `device_bindings.public_key` expone el mismo secreto usado para firmar (no es una clave publica asimetrica real) | 2 | 3 | Alto | Aceptado por ahora: el formato `hmac:<hex>` lo exige el backend (E1-T27) y no se cambia desde el cliente. Mitigacion: el backend debe tratar `public_key` como material secreto (no exponerlo en respuestas/logs, control de acceso a `device_bindings`) y rotar el binding ante sospecha. **Recomendacion a futuro:** migrar a un par asimetrico Ed25519/EC (que el backend ya soporta) donde solo la clave publica viaje y el secreto nunca se derive ni se comparta. |
| R27 | `biometric_type` se omite en produccion: `BiometricReader` no expone si fue FACE o FINGERPRINT | 1 | 1 | Bajo | Aceptado: `local_auth` no esta en `pubspec.yaml` y, aun instalado, `getAvailableBiometrics()` devuelve los tipos *enrolados* (face/fingerprint), no cual se uso en cada autenticacion; con ambos enrolados es ambiguo. El backend acepta la solicitud sin `biometric_type`, por lo que se deja omitido (no se inventa). Mitigacion futura: exponer el tipo desde el reader cuando la plataforma lo garantice y cablearlo a `LoginRouteDeps.biometricType`/binding. |
| R28 | Rate-limit de recuperacion (`E1-T31`) en memoria por replica (no compartido) | 2 | 2 | Medio | Aceptado por alcance (mismo patron que activacion/KYC): buckets `RECOVERY_REQUEST_*` / `RECOVERY_VERIFY_*` por `email+IP`, candidatos a `config.parameters` (`auth.recovery_request_*` / `auth.recovery_verify_*`). En prod multirreplica mover a Redis o a un middleware de rate limiting (misma clave). Nota: el drift de esquema de `identity.access_recovery` quedo resuelto con la migracion `0017_identity_access_recovery.py` (`alembic check` limpio). |
| R29 | Soporte RUC validado solo contra el mock: si `kyc-service/verify-full` rechazara `document_type=RUC`, el submit fallaria en vivo | 2 | 2 | Medio | Aceptado: validacion local + mock en verde (`docs/tasks/archive/E1-T36.md`); `kyc-service/` no se toca (prohibido) por lo que no es verificable aqui. Verificar contra el servicio real cuando este disponible; si se confirma el rechazo, adaptar el contrato o documentar la limitacion. |

> **Nota operativa — swap solo-texto `arriba`/`abajo` (F-T48):** el intercambio es
> solo presentacion Flutter (`kyc_task_page.dart::instructionFor`); el microservicio
> (`kyc-service/` intacto, `liveness_service.py` sin cambios) conserva el mapeo invertido.
> `failed_step`/logs/BD guardan los nombres del servicio, es decir lo inverso a lo que
> ve el usuario: `arriba` en logs = lo que la UI mostro como "mira hacia abajo" y
> `abajo` en logs = lo que la UI mostro como "mira hacia arriba". No reinterpretar
> al leer logs/soporte.

## 4. Dependencias externas

1. **Microservicio KYC**: disponible, con contrato estable (ya documentado).
2. **Equipo interbancario par**: API, autenticacion y ventanas de prueba.
3. **API de tipo de cambio**: disponibilidad y limites del plan gratuito.
4. **Servicios de notificacion**: plan gratuito o mock.
5. **Infraestructura de despliegue** gratuita para la demo.

## 5. Preguntas que conviene cerrar antes del Sprint 1
1. ?Cual es el contrato interbancario y quien lo define?
2. ~~?Quien asume DevOps y con que herramientas de despliegue?~~ **Resuelta/aceptada para el MVP/demo (2026-09-26):** sin titular formal (riesgo aceptado, ver `D05`/`R02`); titular formal pendiente para produccion.
3. ?Se aprueba la estrategia de QR propio firmado como base?
4. ?Se acepta el login con nonce firmado por dispositivo?
5. ?Cual es el orden de recorte de `Should` si el equipo se atrasa?
6. ?Que proveedor de tipo de cambio y de notificaciones se usara?

## 6. Pendientes transferidos al Sprint 2 (entrada para el creador de briefs)

> Guía de ubicación (según `GUIA-GENERAR-BRIEFS.md` §3): cada pendiente trae su
> futuro ID sugerido, documento fuente y referencias, para que el planificador
> genere el brief sin adivinar. Estado: `Pendiente` (Sprint 2).

### P-S2-01 - OTP real por correo Gmail (canal email)
- **Origen:** decisión del dueño 2026-09-18: Twilio/SMS queda como secundario
  (cuenta trial con restricción 572006); el OTP real del Sprint 1 viaja por el
  mock (veredicto en `notifications.payload_json`). Probaron PIN, cámara y KYC
  real; el envío del código queda pendiente.
- **Alcance Sprint 2:** `GmailNotificationSender` con `smtplib` (stdlib, sin
  dependencias): SMTP `smtp.gmail.com:587` + STARTTLS; selección
  `EMAIL_PROVIDER=gmail|mock` (default `mock`); credenciales `GMAIL_USER` /
  `GMAIL_APP_PASSWORD` (solo `.env`, placeholders en `.env.example`); canal
  `sms` sigue a Twilio/mock como fallback. El correo destino se pide al
  registrarse (campo email en el flujo KYC de la app → `users.email`).
- **Documentos fuente para el brief:** `docs/03b` (§13 notifications),
  `docs/05` (§6.1), `docs/11` si aplica a HU22, `docs/16` (sin PII en logs).
- **Frontera sugerida:** `backend/app/adapters/` (sender), `notifications/service`
  (selección), `identity/service` (pasar email), `frontend/.../kyc` (campo
  email). **Prohibido:** secretos en código; cambiar el contrato SMS.
- **Requisito del dueño:** crear un Gmail emisor con 2FA + contraseña de
  aplicación (16 letras) y entregarla al implementador; el correo de prueba es
  el que el usuario escriba al registrarse.
- **Pruebas sugeridas:** envío con SMTP mockeado (cero correos reales);
  selección por env; E2E alta → correo → activate con el código recibido.
- **IDs sugeridos para el brief:** `E1-T24` (backend sender + cableado) y
  `F-T19` (campo email + copy "revisa tu correo"), o uno solo si el
  planificador lo prefiere. **No duplicar** `E1-T09` (Hecho, mock).
- **Estado:** `Hecho` (2026-09-25: ya implementado — `GmailNotificationSender` en `backend/app/adapters/notification_sender.py` con `EMAIL_PROVIDER=gmail|mock`; briefs `E1-T25`/`E1-T26` archivados en `docs/tasks/archive/`).
- **Limitacion conocida (2026-09-25):** el consumidor de `ledger.entry.posted` hacia `movements_view` no esta cableado en produccion (stub idempotente en `accounts/repository/__init__.py:379`; proyeccion via `movements.py:266`). El cableado (worker/bus) es del Sprint 2.
