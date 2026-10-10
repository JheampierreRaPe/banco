# HANDOFF — Orquestador → Sprint 2 (créditos) · actualizado 2026-10-10

Lee esto antes de continuar. Estado real del proyecto. Reemplaza al handoff del 2026-10-08 (Sprint 2, HU06/HU07/HU08).

## 1. Dónde estamos

- Rama de trabajo: **`sprint-2`** (creada desde `kyc-ux-24-08-2026`).
- `origin` = https://github.com/JheampierreRaPe/banco . `gh` **no está instalado** (PR por push + URL web). La rama **sigue sin pushear** (`sprint-2` sin upstream).
- **Commits locales de la fase de créditos (HU09/HU10):** `64383ad` (motor financiero y catálogo de productos, E3-T01/T02), `b961c72` (API de simulación + UI del simulador + QA, E3-T03/T04/T05), `fa3b1df` (adaptador de buró, reglas y solicitud, E3-T06/T07/T08), `2532480` (UI de solicitud, panel de analista y QA, E3-T09/T10/T11).
- **Commits locales de esta fase (2026-10-10):** `4c61201` (acceso a Créditos desde el home y el perfil), `30243a9` (panel `admin-web` conectado por same-origin, nginx `/api`), `c190618` (token de analista + guía de acceso al panel web) y el commit de este handoff. **Sin push.**
- **Backend:** `1403 passed + 1 skipped` (desde `backend/` con `.venv`), `ruff`/`black` limpios (370 archivos sin cambios), `alembic check` limpio, head de migración **`0032_credits_dti_wider`**.
- **Frontend:** `772/772` (`flutter test`), `flutter analyze` limpio.
- **Panel `admin-web`:** `18/18` (`npm test`), `build`/`lint` limpios (`tsc --noEmit`); sirve en `:8080` con **proxy same-origin** `/api/` → `backend:8000` (nginx).
- **Stack Docker:** **5/5 `healthy`** (`backend`, `worker`, `admin-web`, `postgres`, `redis`). Microservicio KYC `kyc-facial-service-8001` (`:8001`) sigue existiendo aparte del compose.
- **Dispositivo:** adb TLS `adb-KJSGAUYHSCB6HUSS-yIJZve._adb-tls-connect._tcp` (modelo `2311DRK48G`). Aparece **duplicado** con sufijo ` (2)` → hacer `adb disconnect "<serial (2)>"` para dejar uno.
- **APK debug** instalado: paquete `com.bancaonline.banca_online.refactorui` (v0.1.0), apunta a `http://192.168.1.20:8000` (IP LAN estática del PC). App vieja `com.bancaonline.banca_online` intacta.
- **Config clave sembrada:** `transfer.biometric_threshold_minor` = **100000** (S/ 1,000); `transfer.daily_limit_*_minor` = **500000** (S/ 5,000). (Si cambia la IP del PC, reconstruir el APK con la nueva `API_BASE_URL`.)

## 2. Qué se hizo (Sprint 2 — HU09/HU10 créditos COMPLETO + accesos + panel web)

**HU09 (simulador) / HU10 (solicitud y evaluación) — COMPLETO (E3-T01..T11, backend + frontend + QA):**
- `E3-T01` **motor financiero francés** (TEA/TCEA; `shared/config` para decimales) y `E3-T02` **catálogo `loan_products`** (migración `0029_credits_loan_products`).
- `E3-T03` **`POST /credits/simulate`** + `E3-T04` **PDF** del plan de pagos; `E3-T05` QA del simulador.
- `E3-T06` **adaptador `CreditBureau`** (mock determinista + HTTP) y `E3-T07` **motor de reglas DTI/score** (parámetros en `loan.*`, `shared/config` decimal; migración `0030_credits_loan_rule_params`); `E3-T08` **solicitud** (`loan_applications`, eventos outbox; migración `0031_credits_loan_applications`).
- `E3-T09` **UI Flutter de solicitud/estado**, `E3-T10` **panel de analista** (`admin-web`) y `E3-T11` QA de punta a punta. `E3-T12`+ aún no.
- Migración `0032_credits_dti_wider` (head actual).
- **Acceso a Créditos** desde el home (quick-action "Crédito", `qa-creditos` → `/credits`) y desde el perfil (fila "Créditos preaprobados", `profile-credits` → `/credits`). La ruta exige sesión (guarda F-T37).

**Panel web (`admin-web`):**
- Vista de **analista de crédito**: bandeja `IN_REVIEW` + detalle enmascarado + dictamen (`Aprobar`/`Rechazar` con motivo obligatorio). Backend decide por rol (`analyst`/`credit_analyst`), no la UI.
- **Conectividad same-origin:** nginx proxea `/api/` → `backend:8000`; base URL relativa `/api/v1` (antes absoluta `http://localhost:8000` → rompía desde otra IP).
- **Token de analista:** `backend/scripts/mint_demo_token.py` (firma local con el `JWT_SECRET` vigente; no es backdoor; pruebas en `backend/tests/test_mint_demo_token.py`). **Guía:** `docs/guia-acceso-panel-web.md`.

**Recap previo (HU06/HU07/HU08 — ya cerrado):** transferencias propias/tercero/interbancarias (hold+settle, saga con compensación, comprobante `OP-…`+PDF), beneficiarios (AES-256-GCM, unicidad parcial), gate biométrico (umbral + PIN + límite diario), cuenta de 14 dígitos con DV + lookup DNI/celular, `/movements` con reverso, `/admin/parameters`. Detalle en el handoff del 2026-10-08 y `docs/tasks/archive/`.

## 3. Qué funciona de verdad (verificado)

- **Créditos (HU09/HU10):** simulador (motor francés TEA/TCEA con catálogo `loan_products`, `/credits/simulate` + PDF, UI Flutter); solicitud/evaluación (adaptador `CreditBureau` mock+HTTP, motor de reglas DTI/score, expediente `loan_applications`, eventos outbox, UI Flutter de solicitud/estado). Acceso a `/credits` desde el **home** (botón "Crédito") y el **perfil**.
- **Panel web del analista:** bandeja `IN_REVIEW` → detalle → dictamen con motivo obligatorio; sale de la bandeja y queda auditado; funciona desde cualquier host/IP por el proxy same-origin.
- Flujo de transferencias (propia/tercero/interbancaria) con reto biométrico (fallback PIN), comprobante y "Volver al inicio"; destino por cuenta de 14 dígitos o DNI/celular; tope pre-aprobado exime biometría hasta el tope.
- `/movements` (historial + filtros + reverso), `/beneficiaries`, `/admin/parameters` (web). Backend: motor/ledger cuadran; saga interbancaria sin pérdida de fondos; idempotencia por titular.

## 4. Pendientes

- **Créditos (resto de la épica 3):** **HU11 (firma de contrato)** `E3-T12..T16` y **HU12 (desembolso/cuotas)** `E3-T17..T22` **sin briefs ni implementación**. Ver `docs/08-epica-3-creditos.md` y `docs/tasks/SPRINT-2.md`.
- **Observaciones abiertas (`docs/18` §9):**
  - **OBS-3:** el contador de fallos biométricos (`save_block`) no usa CAS por `version` (dos fallos concurrentes pueden subcontar) y depende de que el llamante haga commit.
  - **OBS-4:** `nonce` reintentable tras fallo; tests de concurrencia serializados (evidencia débil); `resolve_risk_profile` lee `identity.users` por SQL directo (fuera de la fachada D-03); etc.
- **QA de fases posteriores:** `Q-T03` (seguridad), `Q-T07` (observabilidad), `Q-T08` (escaneo de deps/secretos), `Q-T09` (guion demo) — **sin brief**.
- **Frontend no implementado:** `F-T06..F-T18` (créditos/wallet-QR/divisas/panel admin) y **HU13–HU16** (wallet/QR). **Panel admin general:** `F-T12`.
- **Gaps / deuda a cerrar (`docs/18`):** **G26** (`PUT /admin/parameters` sin decimales), **G52** (detalle de expediente para el analista), **H5 de E3-T06** (registrar env vars en `docs/02` #9). Cerrar/documentar gaps nuevos en `docs/18`.

## 5. Riesgos / decisiones abiertas

De `docs/17-riesgos-y-decisiones-abiertas.md` (varios siguen abiertos) + notas nuevas:
- `device_public_key` = secreto HMAC (R26); OTP hasheado SHA-256 simple; rate-limit en memoria (R28); dependencias/imágenes sin pinear; RUC contra mock (R29); D05/R02 (DevOps sin titular) aceptado.
- **PIN authorize:** contrato nuevo (`pin` mutuamente excluyente con firma) — el PIN **no** debe persistirse ni reflejarse en respuestas (ya saneado el `422`).
- **Tope como techo:** cambia la semántica previa de `E2-T14` ("sin saltarse el umbral global"); documentado en `docs/modules/README.md` y `docs/18` G45.
- **Idempotencia:** UQ global `(key, user_id)` **sin `endpoint`** → el mismo `Idempotency-Key` en `transfer` y `authorize` daba `409`. Mitigado en frontend (clave `<borrador>-auth`); a futuro podría incluir `endpoint` en la UQ (requiere migración).
- **Token de analista (dev):** `mint_demo_token.py` firma local y solo emite `analyst`/`credit_analyst`; nunca imprime el secreto. Es herramienta dev/demo, no un backdoor.

## 6. Notas de trabajo

- Notas del dueño (`*.txt` en la raíz): **no commitearlas** (`.gitignore`). `frontend/android/build/` y `frontend/build/` también ignorados.
- `.opencode/agent/diseno-frontend.md` y `eliminar-movements.jpeg` quedan untracked a propósito (excluidos de commits).
- Subagentes: `planificador` (briefs), `worker` (una tarea), `fixer` (solo defectos indicados + regresión), `validador` (solo lectura, dictamen), `diseno-frontend` (figma `pantallas.fig` + Flutter), `explore`/`general` (investigación).
- Fix de agentes (2026-09-26): `planificador`/`validador` ya **no** fijan `model:` (heredan el default). `worker`/`fixer`/`diseno-frontend` usan `opencode-go/muse-spark-1.3-contributor`.
- Briefs `Hecho` archivados en `docs/tasks/archive/`. `docs/tasks/SPRINT-2.md` es el índice del Sprint 2.

## 7. Comandos de verificación rápida

```powershell
# Backend (desde backend/)
.venv\Scripts\python.exe -m pytest tests/ -q        # 1403 passed + 1 skipped
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m black --check .
.venv\Scripts\alembic.exe check ; .venv\Scripts\alembic.exe heads   # head 0032_credits_dti_wider

# Frontend (desde frontend/)
flutter analyze ; flutter test                       # 772/772

# Panel admin-web (desde admin-web/)
npm test ; npm run lint ; npm run build              # 18/18; tsc; build limpio

# Stack
docker compose ps                                    # 5 servicios healthy
curl http://localhost:8000/health/ready              # {"status":"ok","database":true}
```

- APK: reconstruir con
  `flutter build apk --debug --dart-define=API_BASE_URL=http://192.168.1.20:8000 --dart-define=APP_VERSION=0.1.0`
  e instalar con `adb -s <serial> install -r "frontend/build/app/outputs/flutter-apk/app-debug.apk"`.
- **Tras cualquier cambio de backend:** reconstruir la imagen (§9) antes de probar; el APK y el backend deben ser del **mismo HEAD**.

## 8. Reglas de trabajo (vigentes)

- El orquestador **delega** (no implementa ni verifica ni ejecuta pruebas directamente): briefs al `planificador`/`general`; implementación, builds e instalaciones al `worker`; defectos al `fixer`; verificación al `explore`/`validador`; pantallas al `diseno-frontend`.
- **No commitear/pushear sin autorización** del dueño. Commits por bloque temático; excluir `.opencode/`, `eliminar-movements.jpeg` y `node_modules/`.
- No tocar `kyc-service/`. Sin PII/secretos en logs ni respuestas. Cliente delgado (el frontend no calcula saldos/comisiones). No borrar pruebas para pasar.
- Antes de avanzar: contrastar con `docs/01` (decisiones) y la doc canónica; si algo contradice, manda `docs/01` y se corrige.

## 9. Protocolo de sesión (hola / adiós) — lo ejecuta un `worker` delegado

- **`hola`:** arranque de sesión. Lo primero:
  1. **Levantar contenedores:** `docker compose ps -a` → si existen detenidos, `docker compose start`; si no, `docker compose up -d`. Esperar `healthy`.
     - **VERSION SKEW (importante):** `start` **no recompila**. Si cambió **código o migraciones del backend** desde la última build, la imagen queda vieja → el backend crashea (`Can't locate revision …`) o sirve un contrato desactualizado (causó el `422 target_account_id: missing` de E2-T30). En ese caso: `docker compose build backend worker` + `docker compose up -d --force-recreate backend worker`.
     - Si cambió el **frontend**, reconstruir e instalar el APK (§7). APK y backend del mismo HEAD.
  2. **Warmup KYC:** `docker start kyc-facial-service-8001` si hace falta; conectar a la red `banca-demo_default`; golpear `http://localhost:8001/health`.
  3. **Device adb:** `adb devices -l` (reconciliar duplicados con sufijo ` (2)`); reconectar si no aparece.
- **`adios`:** cierre de sesión.
  1. **Detener** con `docker compose stop` (**NO** `down`; "baja" = detener, no eliminar).
  2. **Preguntar** si se desea commitear (y hacerlo solo si autoriza).
  3. **Generar** `docs/sesiones/<fecha>.md` con lo avanzado (tareas, commits, suites, pendientes, riesgos).
  - Cerrar con el mensaje exacto: **"Descansa bello"**.
