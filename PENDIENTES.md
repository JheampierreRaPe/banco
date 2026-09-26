# PENDIENTES - Handoff para el siguiente orquestador (2026-09-19)

> Estado tras la sesion de alta integrada + OTP Gmail + login con binding + fixes de camara/KYC.
> Leer junto con `HANDOFF.md` y `docs/17-riesgos-y-decisiones-abiertas.md`.
> Rama: `pruebas-flutter-nuevoagente`. Idioma de trabajo: espanol.

## 1. Estado actual (verificado)

- Backend: `pytest` **442 passed + 1 skipped**; `alembic check` limpio; migracion `0016` en head.
- Frontend: `flutter test` **222 passed**; `flutter analyze` sin issues.
- Docker: 6 contenedores Up (postgres, redis, backend, worker, admin-web, `kyc-facial-service-8001`).
- KYC real: `POST /api/v1/auth/kyc/challenge` → token de 32 + 5 pasos en espanol.
- Backend Docker con `EMAIL_PROVIDER=gmail` (OTP real por correo), `GMAIL_USER` y `DOC_HASH_PEPPER` configurados (solo en `.env`, ignorado).

## 2. Hecho en esta sesion (resumen)

- **Alta integrada** (`E1-T24`): `/auth/kyc/submit` crea usuario/credencial/cuenta + `KycVerification` + OTP y devuelve `user_id`; hash de documento con HMAC+pepper; 409 tipados.
- **OTP real por Gmail** (`E1-T25`, `E1-T26`): `GmailNotificationSender` (smtplib), routing por canal, plantilla email; canal/destino email con fallback SMS; un solo OTP que activa + fija PIN (`E1-T28`).
- **Login**: binding de dispositivo en el primer login con PIN (`E1-T27`); el biometrico valida solo con el dispositivo (HMAC del nonce, sin KYC). Persistencia de `user_ref`/`device_id` (`F-T19`..`F-T22`).
- **KYC frames** (`E1-T29`, `F-T23`): rafaga de ~12 frames/180 ms (~2.5 s) por tarea, `/auth/kyc/evaluate`, propagacion de `failed_step`/`step_results`/`overall_reason`, captura real del documento.
- **Documento**: proxy `/auth/kyc/document/validate` (`E1-T30`, path `/api/v1/document/validate`), validacion antes del challenge (`F-T26`), captura en `ResolutionPreset.high` y re-captura con preview (`F-T27`).
- **Camara**: fix del crash `setState() during build` en `KycTaskPage`/`KycCameraPreview`; ciclo de vida del `CameraController` sin "used after disposed" (`F-T24`); preview en vivo durante captura/evaluacion (`F-T25`).
- QA E2E del alta+login (`Q-T10`) y validadores de fase (backend y frontend) con dictamenes APROBADOS.

## 3. Pendientes priorizados

### P1 (ALTA) - Camara en negro durante/tras el challenge de liveness
- Sintoma en fisico: al hacer las tareas del challenge, el viewfinder queda **en negro** despues de la primera captura/evaluacion.
- Hipotesis: al cambiar de generacion/controller en `CameraFrameSource` (retiro diferido, rafaga o cambio de lente documento→frontal) el `KycCameraPreview` queda ligado a un controller retirado y no se re-suscribe al vigente (ver guarda de "controller no vigente" en `kyc_camera_preview.dart`). `F-T25` mantiene el preview montado, falta que reciba el controller nuevo.
- Brief sugerido: **`F-T28`** (frontend kyc). Diagnostico con `adb logcat` + `flutter run`.

### P2 (ALTA) - Recuperacion de acceso tras reinstalar/borrar datos
- Requisito del dueño: si se borran los datos o se reinstala, para iniciar sesion pedir el **email registrado** → enviar **OTP por correo** → pantalla de OTP → iniciar sesion (sin `user_ref` local).
- Encaja en HU04 (recuperacion): briefs `E1-T19`..`E1-T23` (aun sin crear) + un `F-T##`. Reusar OTP/notifications (email) y `device_bindings`.
- IDs sugeridos: `E1-T31` (backend solicitar/validar OTP de recuperacion por email + abrir sesion) y `F-T29` (pantalla email → OTP → login).

### P3 (ALTA) - Fase 7 QA del Sprint 1
- `E1-T07`, `E1-T12`, `E1-T18`, `E2-T06`, `E5-T07`, `E5-T08`, `E5-T15`, `Q-T02`, `Q-T04` (suite del motor, prueba de carga, contrato OpenAPI), segun `HANDOFF.md` §5 y `docs/tasks/SPRINT-1.md`.

### P4 (MEDIA) - Sprint 2: transferencias (HU06)
- Crear briefs `E2-T07`.. segun `GUIA-GENERAR-BRIEFS.md`; el OTP real por Gmail (`P-S2-01`, `docs/17#6`) ya quedo implementado.

### P5 (MEDIA) - Mejora del microservicio KYC (opcional)
- `best_frame` premia tamano+nitidez pero no frontalidad; penalizar `|yaw|/|pitch|` o promediar embeddings reduce falsos negativos en `face_match`.
- Revisar `BLUR_LAPLACIAN_THRESHOLD=80.0` en `kyc-service/.env` (estricto) al diagnosticar rechazos de documento.

### P6 (BAJA) - Riesgos y deudas aceptadas (`docs/17`)
- `R22` OTP en claro en `notifications.payload_json`; `R23` notificacion best-effort antes del commit; `R24` constantes de canal sin uso; `R25` fallback dev de `DOC_HASH_PEPPER`; `R26` `device_public_key` equivale al secreto de firma (migrar a Ed25519/EC); `R27` `biometric_type` omitido.
- Twilio en trial (SMS bloqueado, error 572006); `JWT_SECRET=change-me` (rotar en prod); pendientes `redis/argon2/cryptography/local_auth/openpyxl/file_saver`; `permission_handler` para pre-chequeo de camara; `kyc-service/` no versionado (secretos); diseno visual (`docs/20`, `docs/design`) pendiente tras validar logica.

### P7 (ABIERTO - a validar por el dueno) - Validacion del challenge reporta "el rostro no se mantuvo consistente" (face_match)
- Estado: **abierto - a validar por el dueno** (sin causa raiz aun; no inventar).
- Sintoma: al validar el challenge de liveness, el reporte indica "el rostro no se mantuvo consistente" en el paso `face_match`.
- Accion: el dueno lo validara mas adelante. Registrar evidencia (mensaje/`overall_reason`/`step_results`) antes de fijar causa. Linea a explorar (no confirmada): frontalidad de `best_frame` y umbrales del microservicio (ver P5 y `docs/17#6`).
- Diario: ver `DIARIO.md` (entrada 2026-09-20).

## 4. Comandos de verificacion rapida

```powershell
cd backend; .venv\Scripts\python.exe -m pytest tests/ -q        # 442+1
cd frontend; flutter test; flutter analyze                       # 222 / 0
docker compose ps --format "{{.Names}} {{.Status}}"              # 6 Up
curl.exe -s -X POST http://localhost:8000/api/v1/auth/kyc/challenge -H "Content-Type: application/json" -d "{}"
```
APK debug: `frontend/build/app/outputs/flutter-apk/app-debug.apk`
(IP PC `192.168.1.42`; celular por depuracion inalambrica `192.168.1.45:46003`.)

## 5. Subagentes (opencode) - obligatorio para delegar

El repo define 4 subagentes en `.opencode/agent/` (versionados con el proyecto), todos con
`model: opencode-go/deepseek-v4.1-flash` y `mode: subagent`:

| Agente | Uso | Permisos |
|---|---|---|
| `planificador` | Crea briefs en `docs/tasks/<ID>.md` (no escribe codigo) | edit allow, bash ask |
| `worker` | Implementa UNA tarea (su brief) + pruebas + reporte §6 | edit allow, bash allow |
| `fixer` | Corrige solo los defectos indicados + regresion | edit allow, bash allow |
| `validador` | SOLO LECTURA: suites + dictamen con severidad | edit deny, bash allow |

- El orquestador los invoca con la herramienta `Task` (`subagent_type: "worker"`, etc.).
- **Reiniciar opencode** despues de editar `.opencode/` (la config se carga al arrancar).
- Si se prefiere el tier gratuito, cambiar a `model: opencode/muse-spark-1.3-contributor-free`.
- Modelo actual: `opencode-go/deepseek-v4.1-flash` (se cambia por agente editando su `model:`).
- `explore` es built-in (solo lectura) y sigue disponible para investigacion.
