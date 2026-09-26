# HANDOFF — Orquestador (2026-09-26) → Sprint 2

Lee esto antes de continuar. Estado real del proyecto al cerrar la sesión. Reemplaza al handoff anterior (2026-09-18).

## 1. Dónde estamos

- Rama de trabajo: **`kyc-ux-24-08-2026`** (creada desde `refactorizacion-ui`), HEAD **`13e2847`**.
- Commits de esta sesión (de más viejo a más nuevo):
  - `08f6f12` docs(tasks): briefs E1-T35/F-T44/F-T45
  - `9d11847` feat(kyc,identity): lookup de titular por documento vía apiinti + estados/bloqueo KYC
  - `e72f057` fix(kyc,identity): camelCase de apiinti + estilo deshabilitado de botones
  - `053cbb8` feat: RUC real + precheck duplicado + consentimiento biométrico + UX KYC (E1-T36..T38, F-T46..T48)
  - `737802a` feat: consentimiento biométrico + precheck email + RUC en pin-reset + retiro de recovery (E1-T39..T41, F-T49..F-T55)
  - `ea1cda5` chore(release): cierre Sprint 1 — QA, coherencia docs, seguridad y features KYC/login/dashboard
  - `13e2847` docs: cierre formal del Sprint 1 (estados y drift documental)
- Ramas locales previas: `refactorizacion-ui` (padre, 25 commits sobre `master`, **NO está en origin**), `pruebas-flutter-nuevoagente`, `master`, varias `feature/*`.
- `origin` = https://github.com/JheampierreRaPe/banco . `gh` **no está instalado** en esta máquina (PR por API/URL manual).
- Backend: **727 passed + 1 skipped**, `ruff`/`black` limpios, `alembic check` limpio, head **`0020_identity_ruc_business_name`**.
- Frontend: **576/576** tests, `flutter analyze` limpio.
- Stack Docker: `healthy` (postgres/redis ahora solo en `127.0.0.1`; backend `:8000`, admin-web `:8080`).
- Dispositivo: adb TLS `192.168.1.43` (pair), app `com.bancaonline.banca_online.refactorui` v0.1.0 instalada; APK apunta a `http://192.168.1.20:8000` (IP LAN estática del PC). App vieja `com.bancaonline.banca_online` intacta.

## 2. Qué se hizo en esta sesión (resumen)

- **RUC real (E1-T36):** `identity.users.business_name`, CHECK `ck_users_doc_type` con `RUC`, migración `0020`, propagación en alta/submit, UI de razón social.
- **Lookup por documento (E1-T35):** `POST /auth/kyc/document/lookup` vía apiinti real (Bearer + key en `.env`, `DOC_LOOKUP_PROVIDER=apiinti`); mapeo camelCase.
- **Precheck de duplicado (E1-T37):** 409 `DUPLICATE_DOCUMENT` (DNI+RUC) antes del proveedor.
- **Consentimiento biométrico (E1-T38/T39, F-T46/T49/T52):** `POST /auth/biometric/consent`, flag en login PIN, biometría real con `local_auth` (fallback PIN), pantalla "Mi perfil" (`/profile`) con switch y "Cerrar sesión".
- **Fix login biométrico (E1-T42, F-T53):** rebind de `public_key` en login PIN + no rotar `device.secret` ante refresh benigno. (Requiere un login con PIN tras desplegar para refrescar la clave.)
- **Dashboard (E1-T43/T44, F-T54/T55):** rediseño según `dash`, `GET /accounts/totals` (total real del servidor) y `GET /me` (nombre real); bottom-nav (`Perfil`→`/profile`).
- **Login en dispositivo nuevo (E1-T45/T46, F-T56/T57):** email+DNI → OTP → PIN → asocia dispositivo y abre sesión; separado de "Olvidé mi PIN" (`/pin-reset`).
- **QA del Sprint 1 (10 briefs):** concurrencia/idempotencia/saldo/latencia, KYC, cadena de hashes de `journal_entries`, OTP, auth/lockout/refresh, carga <200 ms, enmascaramiento/consistencia saldos-ledger, contrato OpenAPI + integración KYC, verificación infra demo.
- **Docs:** coherencia con el código (canon `docs/14`, enums, catálogo de errores, `/auth/recover` retirado, READMEs de módulo, dueño único de `config.parameters`) y **archivado de los briefs `Hecho`** en `docs/tasks/archive/` (Sprint 1 cerrado formal y funcionalmente).
- **Seguridad (quick wins):** `JWT_SECRET` fuerte + validación de arranque, Postgres/Redis solo loopback, `.gitignore` (notas/temporales/`key.properties`), 401 sin detalle interno, scaffold de firma release Android.

## 3. Qué funciona de verdad (verificado)

- Alta KYC end-to-end, PIN, login PIN, login biométrico (tras un PIN-login), login por PIN en dispositivo nuevo, consentimiento biométrico, dashboard con datos reales, "Mi perfil", "Olvidó mi PIN".
- apiinti real para DNI/RUC (key en `.env`, backend-side; el cliente nunca la ve).
- Motor/ledger con los 12 casos del QA en verde; proyección de movimientos probada (ver pendiente en §4).

## 4. Pendientes / Sprint 2

- **Sprint 2 (transferencias HU06/HU07/HU08):** crear los briefs `E2-T07..E2-T22` desde `docs/07` y `docs/04`; el motor base ya está Hecho; falta el router `/transfers/*` y los endpoints `/transactions/*/authorize`, `/payments/qr`.
- **`E2-T04` / `movements_view`:** el consumidor de `ledger.entry.posted` NO está cableado en producción (diferido a Sprint 2; ver `docs/17:113` y nota en `docs/tasks/SPRINT-1.md`).
- **QA de fases posteriores:** `Q-T03` (seguridad), `Q-T07` (observabilidad), `Q-T08` (escaneo de deps/secretos, parcial en CI), `Q-T09` (guion demo) — sin brief aún.
- **Frontend no implementado:** `F-T06..F-T18` (transferencias/créditos/wallet-QR/divisas/panel admin).
- **HU04 (`E1-T19..E1-T23`):** superado por el flujo vigente; no crear salvo rediseño explícito.
- **Mejoras UI:** "opciones-usuario" ya existe; quedan filas visuales sin función y quick-actions/bottom-nav "Próximamente".

## 5. Riesgos / decisiones abiertas (`docs/17`)

- R26: `device_public_key` = secreto HMAC (migrar a asimétrica Ed25519/EC; `cryptography` no declarado).
- OTP hasheado con SHA-256 simple (usar PBKDF2/HMAC-pepper).
- Rate-limit en memoria por réplica (migrar a Redis/middleware) — R28.
- Dependencias/imágenes sin pinear (usar `==`/lockfile; ojo: un `uv sync` accidental subió `sqlalchemy` a 2.1.1, aún dentro de `>=2.0`).
- R29: RUC solo probado contra el mock de `kyc-service/verify-full` (no verificado contra el microservicio real).
- D05/R02: rol DevOps sin titular → **riesgo aceptado para MVP/demo** (2026-09-26).
- `kyc-service/` es repo anidado ignorado con secretos propios (no versionado a propósito).

## 6. Notas de trabajo

- Notas del dueño (`.txt`) ya están en `.gitignore`; **no commitearlas**.
- `.opencode/agent/diseno-frontend.md` queda untracked a propósito (excluido de commits).
- Subagentes del proyecto: `planificador` (briefs), `worker` (una tarea), `fixer` (solo defectos indicados), `validador` (solo lectura, dictamen con severidad), `diseno-frontend` (figma `pantallas.fig` + Flutter), `explore`/`general` (investigación).
- Archivo de tareas resueltas: `docs/tasks/archive/` (113 briefs `Hecho`). `docs/tasks/` solo tiene `README.md`, `SPRINT-1.md`, `_PLANTILLA.md`.

## 7. Comandos de verificación rápida

```powershell
# Backend (desde backend/)
.venv\Scripts\python.exe -m pytest tests/ -q        # 727 passed + 1 skipped
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m black --check .
.venv\Scripts\alembic.exe check ; .venv\Scripts\alembic.exe current   # head 0020

# Frontend (desde frontend/)
flutter analyze ; flutter test                       # 576/576

# Stack
docker compose ps                                    # postgres/redis/backend/worker/admin-web healthy
curl http://localhost:8000/health                    # {"status":"ok"}
```

- APK: `frontend/build/app/outputs/flutter-apk/app-debug.apk` — reconstruir con
  `flutter build apk --debug --dart-define=API_BASE_URL=http://192.168.1.20:8000 --dart-define=APP_VERSION=0.1.0`.
  Si cambia la IP LAN del PC, reconstruir con la nueva.
- Device: `adb devices -l` (serial TLS `adb-...._adb-tls-connect._tcp`); instalar con `adb -s <serial> install -r <apk>`.
- Tras desplegar el fix biométrico: hacer **un login con PIN** para refrescar la clave antes de probar la biometría.

## 8. Reglas de trabajo (vigentes)

- El orquestador **delega** (no implementa ni verifica directamente): briefs al `planificador`, implementación al `worker`, defectos al `fixer`, verificación al `validador`/`explore`.
- **No commitear ni pushear sin autorización explícita** del dueño. Commits por bloque temático; excluir `.opencode/` y `.txt`.
- No tocar `kyc-service/`. Sin PII/secretos en logs. Cliente delgado. No borrar pruebas para pasar.
- Antes de avanzar un campo: revisar si lo construido estaba especificado (o es alternativa de reemplazo) y resolver.

## 9. Protocolo de sesión del orquestador (hola / adiós)

El dueño usa dos palabras clave. **Todo lo de esta sección lo ejecuta un subagente delegado (`worker`); el orquestador no lo hace directamente.**

- **Cuando el dueño escriba `hola`:** significa que cerró la sesión anterior y hay que arrancar. **Lo primero** (delegado a un `worker`):
  1. **Levantar los contenedores** (`docker compose up -d`) y esperar a `healthy` (`docker compose ps`; backend `:8000`, admin-web `:8080`, postgres/redis, worker).
  2. **Warmup del microservicio de KYC** (contenedor `kyc-facial-service-8001` en `:8001`): conectarlo a la red si hace falta (`docker network connect banca-demo_default kyc-facial-service-8001`) y golpear su health/endpoint de listo hasta que responda.
  3. **Verificar que el device esté conectado por adb** (`adb devices -l`; serial TLS tipo `adb-...._adb-tls-connect._tcp`). Si no aparece, reconectar (`adb connect <ip:puerto>` o parear) y reportar.
  - Antes de continuar con cualquier tarea, reportar al dueño el estado de (1) contenedores, (2) warmup KYC y (3) device.

- **Cuando el dueño escriba `adios`:** cerrar la sesión. Delegado a un `worker`, en este orden:
  1. **Dar de baja los contenedores** (`docker compose down`; conservar volúmenes salvo indicación del dueño).
  2. **Preguntar si se desea commitear** antes de irse y, solo si autoriza, hacerlo (por bloque temático; excluir `.opencode/` y los `.txt`).
  3. **Generar un documento con todo lo avanzado en la sesión actual** (tareas, commits, suites, pendientes y riesgos) — p. ej. `docs/sesiones/<fecha>.md` o el destino que indique el dueño.
  - Cerrar con el mensaje exacto: **"Descansa bello"**.
