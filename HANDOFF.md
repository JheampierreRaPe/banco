# HANDOFF — Orquestador → Sprint 2 (créditos) · actualizado 2026-10-08

Lee esto antes de continuar. Estado real del proyecto. Reemplaza al handoff del 2026-09-26 (Sprint 1).

## 1. Dónde estamos

- Rama de trabajo: **`sprint-2`** (creada desde `kyc-ux-24-08-2026`), HEAD **`8a5e091`**.
- `origin` = https://github.com/JheampierreRaPe/banco . `gh` **no está instalado** (PR por push + URL web). La rama **no se ha pusheado** (`sprint-2` sin upstream).
- **Backend:** `1152 passed + 1 skipped` (desde `backend/` con `.venv`), `ruff`/`black` limpios, `alembic check` limpio, head de migración **`0025_biometric_coherence`**.
- **Frontend:** `712/712` (`flutter test`), `flutter analyze` limpio.
- **Stack Docker:** `healthy` (postgres/redis en loopback, backend `:8000`, worker, admin-web `:8080`). Microservicio KYC `kyc-facial-service-8001` (`:8001`, healthy).
- **Dispositivo:** adb TLS `adb-KJSGAUYHSCB6HUSS-yIJZve._adb-tls-connect._tcp` (modelo `2311DRK48G`). A veces aparece **duplicado** con sufijo ` (2)` → hacer `adb disconnect "<serial (2)>"` para dejar uno.
- **APK debug** instalado: paquete `com.bancaonline.banca_online.refactorui` (v0.1.0), apunta a `http://192.168.1.20:8000` (IP LAN estática del PC). App vieja `com.bancaonline.banca_online` intacta.
- **Config clave sembrada:** `transfer.biometric_threshold_minor` = **100000** (S/ 1,000); `transfer.daily_limit_*_minor` = **500000** (S/ 5,000). (Si cambia la IP del PC, reconstruir el APK con la nueva `API_BASE_URL`.)

## 2. Qué se hizo (Sprint 2 — HU06/HU07/HU08 + operaciones + mejoras de destino)

**HU06 transferencias / HU07 beneficiarios / HU08 pagos sensibles — COMPLETO (backend + frontend + QA):**
- `E2-T07` transferencias propias (asiento atómico **sin hold**) y a tercero (hold+settle; comisión a `4100`). `E2-T08` adaptador titular/CCI (mock determinista + HTTP). `E2-T09` saga interbancaria (polling, timeout, compensación, exclusión por tx). `E2-T10` comprobante PDF con nº de operación `OP-<12hex>`. `E2-T11` frontend de transferencias + formulario interbancario CCI (4 estados). `E2-T12` QA E2E.
- `E2-T13` beneficiarios (AES-256-GCM en reposo, índice ciego, unicidad parcial `0022`). `E2-T14` tope pre-aprobado. `E2-T15` frontend de agenda/alta. `E2-T16` QA.
- `E2-T17` gate biométrico (`PENDING_AUTHORIZATION` + hold, `nonce` TTL 120 s), bloqueo preventivo CA-02 y límite diario CA-03. `E2-T18` fachada de parámetros + `risk.risk_profiles` (`0023`). `E2-T19` `POST /transactions/{id}/authorize` + perfil de riesgo end-to-end. `E2-T20` evento a `risk` + notificación (sin mover dinero). `E2-T21` UI del reto + 6 estados de error. `E2-T22` QA.
- **Operaciones:** `E2-T23` cableado de `movements_view` por `ledger.entry.posted`; `E2-T24` `GET /transactions` (listado + filtros); `E2-T25` `POST /transactions/{id}/reverse` (asiento inverso, 409 no reversible); `E2-T26` `GET/PUT /admin/parameters` (guard rol admin).

**Mejoras de destino (decisiones del dueño):**
- `E2-T28` **número de cuenta numérico real**: 14 dígitos con **dígito verificador** (prefijo `001`, módulo 11) + **backfill** de cuentas existentes (migración `0024`). `E2-T27` **lookup previo** `POST /transfers/third-party/resolve-target` por DNI/celular (anti-enumeración: `TARGET_NOT_FOUND` 404 neutro, `TARGET_AMBIGUOUS` 409). `E2-T29` UI de cuenta 14+DV y modo DNI/celular.

**Fixes posteriores (bugs reportados por el dueño):**
- `E2-T30` transferir a un **beneficiario de la agenda** resuelve su **cuenta real** (antes enviaba `beneficiary.id` → 404).
- `E2-T31` **autorizar por PIN** real (el reto sobre el umbral ya no es un callejón: hay campo de PIN en el flujo; verificación por fachada de `identity`; sin filtrar el PIN en respuestas/logs).
- `E2-T32` **tope pre-aprobado = techo absoluto sin biometría** (monto ≤ tope ⇒ sin biometría aunque supere el umbral; > tope ⇒ gate normal; el límite diario manda).
- Observabilidad: el backend **registra el detalle del `422`** (campos+motivo, sin PII) y **sanea** el `detail` (nunca expone `input`).
- Infra: **version skew** (ver §9) — causa del `422 target_account_id: missing`.
- UX comprobante/seguimiento: **sin botón back**, CTA **"Volver al inicio"** → `/home`.
- `E2-T23` cableado de proyección; `OBS-2` (umbral < límite diario) cerrada con `0025`.

## 3. Qué funciona de verdad (verificado)

- Flujo completo en la app: hub `/transfers` → destino (propia/tercero/interbancaria) → monto/motivo → confirmación → **reto biométrico** si supera el umbral (con **fallback a PIN**) → comprobante (`OP-…` + PDF) → "Volver al inicio".
- Destino por **cuenta de 14 dígitos** o por **DNI/celular** (lookup previo con titular enmascarado).
- **Tope pre-aprobado** de un beneficiario exime biometría hasta el tope.
- `/movements` (historial + filtros + **reverso**), `/beneficiaries` (agenda + alta/edición/tope), `/admin/parameters` (web).
- Backend: motor/ledger cuadran; saga interbancaria sin pérdida de fondos; idempotencia por titular.

## 4. Pendientes

- **BLOQUE PRINCIPAL — Créditos (HU09/HU10):** briefs `E3-T01..T11` ya creados en `docs/tasks/` (**sin implementar**). Simulador (`E3-T01..T05`) + solicitud/evaluación (`E3-T06..T11`). Ver `docs/08-epica-3-creditos.md` y `docs/tasks/SPRINT-2.md`.
- **Drift de papeleo:** los briefs `E2-T11`, `E2-T15`, `E2-T21`, `E2-T23` **siguen `Pendiente`** en `docs/tasks/` aunque ya están implementados y commiteados (`a79016f`, `631b90a`, `89610f2`, `b3c5805`). Al cerrar, pasarlos a `Hecho`/`archive/`.
- **Errores nuevos del dueño (sin especificar):** reportó "unos errores" que corregirá más tarde; pendiente de reproducir/describir.
- **Observaciones abiertas (`docs/18` §9):**
  - **OBS-3:** el contador de fallos biométricos (`save_block`) no usa CAS por `version` (dos fallos concurrentes pueden subcontar) y depende de que el llamante haga commit.
  - **OBS-4:** `nonce` reintentable tras fallo; tests de concurrencia serializados (evidencia débil); `resolve_risk_profile` lee `identity.users` por SQL directo (fuera de la fachada D-03); etc.
- **QA de fases posteriores:** `Q-T03` (seguridad), `Q-T07` (observabilidad), `Q-T08` (escaneo de deps/secretos), `Q-T09` (guion demo) — sin brief.
- **Frontend no implementado:** `F-T06..F-T18` (créditos/wallet-QR/divisas/panel admin).
- **Papeleo de cierre:** documentar/cerrar `G40..G45` (ya cerrados) y cualquier gap nuevo en `docs/18`.

## 5. Riesgos / decisiones abiertas

De `docs/17-riesgos-y-decisiones-abiertas.md` (varios siguen abiertos) + notas nuevas:
- `device_public_key` = secreto HMAC (R26); OTP hasheado SHA-256 simple; rate-limit en memoria (R28); dependencias/imágenes sin pinear; RUC contra mock (R29); D05/R02 (DevOps sin titular) aceptado.
- **PIN authorize:** contrato nuevo (`pin` mutuamente excluyente con firma) — el PIN **no** debe persistirse ni reflejarse en respuestas (ya saneado el `422`).
- **Tope como techo:** cambia la semántica previa de `E2-T14` ("sin saltarse el umbral global"); documentado en `docs/modules/README.md` y `docs/18` G45.
- **Idempotencia:** UQ global `(key, user_id)` **sin `endpoint`** → el mismo `Idempotency-Key` en `transfer` y `authorize` daba `409`. Mitigado en frontend (clave `<borrador>-auth`); a futuro podría incluir `endpoint` en la UQ (requiere migración).

## 6. Notas de trabajo

- Notas del dueño (`*.txt` en la raíz): **no commitearlas** (`.gitignore`). `frontend/android/build/` y `frontend/build/` también ignorados.
- `.opencode/agent/diseno-frontend.md` queda untracked a propósito (excluido de commits).
- Subagentes: `planificador` (briefs), `worker` (una tarea), `fixer` (solo defectos indicados + regresión), `validador` (solo lectura, dictamen), `diseno-frontend` (figma `pantallas.fig` + Flutter), `explore`/`general` (investigación).
- Fix de agentes (2026-09-26): `planificador`/`validador` ya **no** fijan `model:` (heredan el default). `worker`/`fixer`/`diseno-frontend` usan `opencode-go/muse-spark-1.3-contributor`.
- Briefs `Hecho` archivados en `docs/tasks/archive/`. `docs/tasks/SPRINT-2.md` es el índice del Sprint 2.

## 7. Comandos de verificación rápida

```powershell
# Backend (desde backend/)
.venv\Scripts\python.exe -m pytest tests/ -q        # 1152 passed + 1 skipped
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m black --check .
.venv\Scripts\alembic.exe check ; .venv\Scripts\alembic.exe current   # head 0025

# Frontend (desde frontend/)
flutter analyze ; flutter test                       # 712/712

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
- **No commitear/pushear sin autorización** del dueño. Commits por bloque temático; excluir `.opencode/` y los `.txt`.
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
