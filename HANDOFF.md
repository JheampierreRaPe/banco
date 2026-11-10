# HANDOFF — Orquestador → continuación (cierre del Sprint 3 + auditoría UX) · actualizado 2026-10-14

Lee esto antes de continuar. Estado real del proyecto. Reemplaza al handoff del 2026-10-12
(cierre final del Sprint 2). El **Sprint 3 quedó CERRADO** (38 briefs `Hecho` + validados) y la
rama **`sprint-3` está pusheada** con upstream `origin/sprint-3`; el índice y el cierre oficial están
en `docs/tasks/SPRINT-3.md#cierre-del-sprint-3-2026-10-14`. Lo siguiente es el **Sprint 4**
(conciliación/antifraude/cumplimiento/multidivisa) **sin briefs** todavía.

## 1. Dónde estamos

- Rama de trabajo: **`sprint-3`**, HEAD actual **`21d69a4`**, **YA PUSHEADA** con upstream
  `origin/sprint-3` al día (`git status -sb` → `## sprint-3...origin/sprint-3`, sin ahead/behind).
  `origin` = https://github.com/JheampierreRaPe/banco .
- **PR pendiente de crear** (no hay `gh` instalado → vía web push+URL):
  https://github.com/JheampierreRaPe/banco/compare/main...sprint-3?expand=1 . El **push a
  `sprint-*` dispara CI** (GitHub Actions), pero **el CI solo corre en GitHub al pushear, no en
  local**; no hay `gh` para leer el run desde aquí.
- **Working tree limpio** salvo dos untracked excluidos: `.opencode/` y `eliminar-movements.jpeg`.
- **Stack Docker esperado:** 5/5 `healthy` (`postgres`, `redis`, `backend`, `worker`, `admin-web`).
  El microservicio KYC vive aparte del compose (`kyc-facial-service-8001`, `:8001`, `healthy`).
- **Commits del Sprint 3 por bloque** (todos en `sprint-3`):
  - **HU12 desembolso + cuotas** (`E3-T17`..`E3-T22`) + prereq catálogo contable (`E5-T23`): `ae9a4d9`.
  - **HU13 billetera** (`E4-T01`..`E4-T05`): `4b6a316`.
  - **HU14 QR** (`E4-T06`..`E4-T10`): `4f16296`.
  - **HU15 cobro QR** (`E4-T11`..`E4-T15`): `e8a7ead`.
  - **HU16 servicios/recargas** (`E4-T16`..`E4-T20`): `65e1ad2`.
  - **HU22 notificaciones de seguridad** (`E6-T12`..`E6-T16`): `55f21e3`.
  - **Infra compartida / wiring / docs del sprint:** `390b7a5`.
- **Fixes UX (esta sesión, post-Sprint 3):**
  - **Frontend Flutter:** `8e6261b` (accesos reales, flujo y CTA muertos).
  - **Panel `admin-web`:** `21d69a4` (sesión expirada, refresco, logout, navegación).
- **Referencia previa:** `sprint-2` quedó en `6a62ecf` (un commit más que el `835078c` citado en el
  handoff anterior: la auditoría UX de pantallas `6a62ecf`); `origin/sprint-2` = `6a62ecf`.
- **Dispositivo adb:** `adb devices -l` puede mostrar duplicados con sufijo ` (2)` →
  `adb disconnect "<serial (2)>"` para dejar uno.

## 2. Estado de suites / migraciones (documentado en `docs/tasks/SPRINT-3.md#cierre-del-sprint-3`)

- **Backend (`pytest`, desde `backend/` con `.venv`):** **~2040 passed + 1 skipped**; cobertura
  **~86%** (umbral del CI `--cov-fail-under=80`); `ruff`/`black` en verde.
- **Frontend (`flutter`, desde `frontend/`):** `flutter analyze` limpio; `flutter test` **~1060**
  (1042 al cierre del Sprint 3; +18 por los fixes UX de esta sesión).
- **Panel `admin-web` (desde `admin-web/`):** **23 tests** (vitest, 3 archivos); `npm run lint`
  (`tsc --noEmit`) y `npm run build` limpios.
- **Migraciones:** un solo head Alembic **`0044_notifications_preferences`**
  (confirmado: `backend\.venv\Scripts\alembic.exe heads` → `0044_notifications_preferences (head)`,
  ejecutado desde `backend/`); `alembic check` limpio.
- **CI (GitHub Actions, `.github/workflows/ci.yml`):** dispara en `push` a `main`, `feature/**` y
  **`sprint-*`**, y en PR a `main`. **El CI NO corre en local**: solo se ejecuta en GitHub al
  pushear.
- **Nota:** no se re-ejecutaron las suites completas (pesadas) en esta sesión; los números son los
  **documentados al cierre del Sprint 3**. Si se corren las suites pesadas, **no hacerlo en
  paralelo** (contención de máquina).

## 3. Qué se hizo (por bloques)

- **HU12 — Desembolso inmediato y programación de cuotas** (`E3-T17`..`E3-T22` + prereq `E5-T23`,
  módulo `credits`/`ledger`): desembolso transaccional (asiento `docs/04#7.5`) con creación de
  `loans` + cronograma definitivo y `contracts.loan_id` poblado; `loans`/`loan_schedules`/
  `loan_payments` (`0034`); pago de cuota por el motor (asiento `docs/04#7.6`); deceptive automático
  + recordatorios con toggle `PUT .../auto-debit`. Prereq `E5-T23` corrige el catálogo contable
  `4200`/`5000` (`0035`, cierra `G60`). Gaps `G54`/`G60`..`G65` en `docs/18`.
- **HU13 — Billetera digital e interoperabilidad** (`E4-T01`..`E4-T05`, módulo `wallet`):
  `wallets`/`wallet_links` tokenizados (nunca PAN; `0037`), pago express con límite base + límite
  diario (params `wallet.*`, `0038`), adaptador de interoperabilidad (`BO-QR1`) mock/HTTP, UI de
  billetera y QA de límites/tokenización. Gaps `G66`..`G71` (billetera) en `docs/18`.
- **HU14 — QR dinámico/estático** (`E4-T06`..`E4-T10`): QR firmado (HMAC-SHA256, payload canónico
  `BO-QR1` + `expires_at`, `0039`) + caducidad on-read/job + `merchant_accounts`/`qr_charges`;
  `POST /merchants/qr` + `GET /merchants/qr/{id}` + aviso de abono; UI de comercio; QA. Gaps
  `G68`/`G69`/`G71`/`G72`.
- **HU15 — Lectura y confirmación de pago QR** (`E4-T11`..`E4-T15`): `qr_payments` (`0040`) +
  guarda anti-doble-cobro (`0041`, índice único parcial + lock de fila); servicio de cobro que
  decodifica/verifica `qr_content` y paga por el motor (asiento `docs/04#7.4`); `POST /payments/qr`
  (+ `/preview`) con autorización biométrica condicional (un toque bajo el base, biometría/PIN
  sobre el base); UI escáner/resumen/errores; QA E2E **cierra `G72`**. Gaps `G74`/`G75`/`G76`.
- **HU16 — Pago de servicios y recargas express** (`E4-T16`..`E4-T20`): catálogo `billers` +
  `service_payments`/`topups`/`service_subscriptions` (`0042`) + adaptador de servicios (mock);
  `POST /services/payments` y `POST /topups` por el motor (asiento `docs/04#7.10`, cierra `G73`) con
  comprobante `OP-…`; job de recordatorios (`0043`, `wallet.service_reminder_days_before`); UI; QA.
  Gaps `G73`/`G76`/`G77`/`G77b`.
- **HU22 — Notificaciones de seguridad multicanal** (`E6-T12`..`E6-T16`, módulo `notifications`):
  plantillas + **preferencias** `user_channel_preferences` (`0044`); adaptadores push/correo mock
  desacoplados por canal (aislamiento de fallos); acción **"no reconozco esta operación"** → revoca
  sesiones + escala a `risk` (sin escribir `risk.alerts`, D-16); **centro de notificaciones** Flutter
  (listar/leer/detalle/preferencias + "no reconozco"); QA < 5 s y multi-canal. Fixes `A1`/`A2`/`A3`/
  `B2`/`B3`/`B4` (cableado E2E de `notify_security_event`, detalle/`device`/`location`/`reference`,
  resolutor de correo, marcado por canal, idempotencia del reporte, solo `security_*`). Gaps
  `G78`..`G83`.
- **Infra/wiring transversal:** jobs de cuotas (`M-2`) y de wallet/QR/servicios registrados en
  `lifespan` y `worker_entrypoint.py`; reconciliación de QR diferido (`R-1`, `test_r1_qr_reconcile`);
  frontera `wallet`↔`transactions` por fachada (`M-1`); fixes de robustez de billetera (`M-2`/`M-3`).
- **Auditoría UX/flujo (esta sesión):** se auditó app Flutter + panel + contratos (FE↔BE: **0 rutas
  huérfanas**) y se corrigieron en **frontend** (`8e6261b`): accesos reales a `/wallet*` y
  `/notifications*`, onboarding retornante, `Ver cronograma`, `kind=interbank`, logout→`/login`,
  ayuda de movimientos, salidas de error, tildes, CTA "Analizar" deshabilitado; y en **panel**
  (`21d69a4`): sesión expirada→login, **Refrescar + auto-refresco**, **logout**, navegación muerta
  deshabilitada, paginación **"50+"**, copy corregido.

## 4. Qué funciona de verdad (verificado)

- **Ciclo de crédito completo:** simular → solicitar → dictamen (panel analista) → **firmar**
  (biometría o PIN) → **desembolsar** (asiento `docs/04#7.5`, cronograma) → **pagar cuota**
  (asiento `docs/04#7.6`), con débito automático y recordatorios; "Mis créditos" del titular y
  contrato get-or-create. El buró mock es determinista por hash del `sub`.
- **Billetera/QR/servicios:** vinculación tokenizada (nunca PAN), pago express con límites
  base/diario, QR firmado + caducidad, **cobro QR E2E** (`POST /payments/qr`, abono < 5 s al
  comercio), pago de servicios y recargas con comprobante `OP-…`. La **cuenta de liquidación**
  del proveedor se siembra de forma determinista (migración `0046_services_settlement`, UUID fijo
  `d0000000-0000-4000-8000-000000000001`) y se inyecta por entorno (`docker-compose.yml`
  backend+worker): recargas/pagos **no** dependen de configurar nada a mano en la demo.
- **Notificaciones:** alertas de seguridad push **+ correo de respaldo** por evento sensible
  (`transfer.settled`, `credit.disbursed`, `loan.installment.paid`, `qr.payment.confirmed`,
  `service.paid`, `topup.completed`; login solo correo), preferencias por canal, centro en la app y
  acción "no reconozco" (revoca sesiones + escala a `risk`).
- **Panel web (`:8080`):** bandeja `IN_REVIEW` + detalle enmascarado + dictamen; same-origin
  (nginx `/api` → `backend:8000`); token de analista con `backend/scripts/mint_demo_token.py`.
  Runbook: `docs/guia-acceso-panel-web.md`. Sesión expirada, refresco y logout ya cableados.
- **Accesos de navegación ya cableados:** entradas reales a `/wallet*` (billetera, QR, servicios) y
  `/notifications*` en la app; ya no hay botones/CTAs muertos en el recorrido auditado.

## 5. Pendientes (para el que continúe)

> **Estado:** el Sprint 3 está **cerrado**, `sprint-3` está **pusheada** (`origin/sprint-3`, HEAD
> `21d69a4`) y solo falta crear el **PR** (URL en §1). Los fix de UX están commiteados pero el CI
> del push se valida en GitHub.

- **(a) Vídeo de respaldo del demo:** único pendiente de papeleo del cierre. Lo graba el **dueño**;
  guion en `docs/demo/guion-demo-sprint-2.md`.
- **(b) Sprint 4 — sin briefs (crear antes de implementar):** **HU19** (conciliación), **HU20**
  (antifraude), **HU21** (cumplimiento PLDFT/AML), **HU24–HU25** (multidivisa/cambio) y
  frontend/panel pendientes (`F-T12..F-T18`). Los briefs deben crearse antes de tocar código.
- **(c) Deuda / observaciones abiertas (numeradas):**
  1. **`location` real de alertas (`G80-b`):** ubicación aprox. por ciudad/región a cargo del
     servidor; el campo ya viaja cuando el emisor lo aporta, pero la resolución servidora está
     **pendiente del dueño** (hoy fallback "no disponible", jamás GPS/PII).
  2. **`WALLET_SERVICES_SETTLEMENT_ACCOUNT_ID` por entorno (fix HU16, 2026-10-15):** la demo ya la
     inyecta (`docker-compose.yml` backend+worker) con default a la cuenta determinista sembrada por
     la migración `0046_services_settlement` (UUID `d0000000-0000-4000-8000-000000000001`); **en
     producción** apuntar al UUID real de la cuenta de liquidación del proveedor (rotable por
     entorno; fail-closed si falta o no está `ACTIVE`). El fallo de liquidación responde
     `503 SETTLEMENT_UNAVAILABLE` (mensaje honesto), ya **no** `422` del formulario.
  3. **Sintonía de `risk.alerts`/severidad con HU20 (`G83`):** HU22 escala (severidad en
     `risk_profiles`, evento `fraud.alert.raised`) **sin** escribir `risk.alerts`; falta **coordinar
     la severidad fina** con `E6-T03`/`E6-T04` (HU20).
  4. **`meta.total` real en la bandeja del panel:** el backend devuelve `meta.total = tamaño de
     página` (no un total real); el panel pagina por `offset` y muestra **"50+"** cuando la última
     página vino llena. Resolver el total real si se requiere contador exacto.
  5. **Mockups `G71`/`G76`** (y `G63` de HU12): UI construida con tokens de `docs/20`; el **dueño
     aporta** las imágenes/spec (`billetera-*`, `billetera-qr-pago.png`, `creditos-cronograma.png`).
  6. **`G75`** (dispatch por `metadata` en `POST /transactions/{id}/authorize`): requiere dueño,
     módulo `transactions`; hoy usa el lector por defecto y no hay divergencia observada.
  7. **`G65`** (pago parcial/prepago/abono a capital): fuera de alcance por decisión del dueño (MVP
     cobra por **cuota total**).
  8. **QA HU16 (hallazgo medio, fail-closed):** un `biller` nuevo se lista pero el proveedor `mock`
     rechaza el suministro (`422`) antes de cobrar; hacerlo pagable exige ampliar el proveedor (no
     solo insertar la fila).

## 6. Riesgos abiertos (`docs/17-riesgos-y-decisiones-abiertas.md`)

- `device_public_key` = secreto **HMAC**, no clave pública asimétrica (R26) → si se filtra
  `device_bindings.public_key` se compromete el secreto de firma.
- OTP hasheado SHA-256 simple (R22); rate-limit **en memoria por réplica**, no compartido (R28).
- Dependencias/imágenes **sin pinear** (riesgo transversal de `docs/17` §3/§4).
- RUC validado **solo contra el mock** (R29); D05/R02 (DevOps sin titular formal, aceptado para el
  MVP/demo).
- Los `nonce` de firma viven **en memoria del proceso** (no persistidos entre réplicas).
- **Nuevos de HU22:** el reporte "no reconozco" escala a `risk` sin escribir `risk.alerts` (HU20) y
  la severidad fina queda por coordinar (§5.3); la **ubicación servidora** de alertas está pendiente
  (§5.1).
- **El CI no se ejecuta en local** (solo en GitHub al pushear): verificar localmente con los comandos
  de §8.

## 7. Cómo arrancar / cerrar sesión (`hola` / `adios`)

Protocolo vigente (lo ejecuta un `worker` delegado):

- **`hola` (arranque):**
  1. **Levantar contenedores:** `docker compose ps -a` → si existen detenidos, `docker compose
     start`; si no, `docker compose up -d`. Esperar `healthy`.
     - **VERSION SKEW (importante):** `start` **no recompila**. Si cambió el **código o las
       migraciones del backend** desde la última build, la imagen queda vieja → el backend crashea
       (`Can't locate revision …`) o sirve un contrato desactualizado. En ese caso:
       `docker compose build backend worker` + `docker compose up -d --force-recreate backend
       worker`. **Ante cualquier duda, reconstruir.**
     - Si cambió el **frontend**, reconstruir e instalar el APK (§8). APK y backend del **mismo
       HEAD**.
  2. **Warmup KYC:** `docker start kyc-facial-service-8001` si hace falta; conectar a la red
     `banca-demo_default`; golpear `http://localhost:8001/health`.
  3. **Device adb:** `adb devices -l` (reconciliar duplicados con sufijo ` (2)`); reconectar si no
     aparece.
- **`adios` (cierre):**
  1. **Detener** con `docker compose stop` (**NO** `down`; "baja" = detener, no eliminar).
  2. **Preguntar** si se desea commitear (y hacerlo solo si autoriza).
  3. **Generar** `docs/sesiones/<fecha>.md` con lo avanzado (tareas, commits, suites, pendientes,
     riesgos).
  - Cerrar con el mensaje exacto: **"Descansa bello"**.

## 8. Comandos rápidos

```powershell
# Backend (desde backend/)
.venv\Scripts\python.exe -m pytest --cov=app --cov-fail-under=80 -q   # ~2040 passed + 1 skipped, cov ~86%
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m black --check .
.venv\Scripts\alembic.exe check ; .venv\Scripts\alembic.exe heads      # head 0044_notifications_preferences

# Frontend (desde frontend/)
flutter analyze ; flutter test                                         # analyze limpio; ~1060

# Panel admin-web (desde admin-web/)
npm test ; npm run lint ; npm run build                                # 23 tests; tsc; build limpio

# Stack
docker compose ps                                                      # 5 servicios healthy
Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:8000/health/ready' -TimeoutSec 10 |
  Select-Object -ExpandProperty Content                               # {"status":"ok","database":true}

# APK (reconstruir con la IP LAN vigente del PC)
flutter build apk --debug --dart-define=API_BASE_URL=http://192.168.1.20:8000 --dart-define=APP_VERSION=0.1.0

# Token de analista (desde backend/; escribe a %TEMP%, nunca al repo)
.venv\Scripts\python.exe scripts\mint_demo_token.py > $env:TEMP\analyst.jwt
# Variantes: --role credit_analyst | --subject <uuid> | --minutes 60

# Guion de demo (referencia completa en docs/demo/guion-demo-sprint-2.md)
.venv\Scripts\python.exe scripts\seed_demo.py check                    # dataset sintetico reproducible
```

- Config clave sembrada: `transfer.biometric_threshold_minor` = **100000** (S/ 1,000);
  `transfer.daily_limit_*_minor` = **500000** (S/ 5,000); `wallet.express_limit_minor` = **20000**
  (S/ 200); `wallet.daily_limit_minor` = **500000** (S/ 5,000); `wallet.qr_ttl_seconds` = **300**
  (5 min); `wallet.service_reminder_days_before` = **3**.
- **Tras cualquier cambio de backend:** reconstruir la imagen (§7) antes de probar; APK y backend
  del mismo HEAD.

## 9. Reglas de trabajo (vigentes)

- El **orquestador delega** (no implementa ni verifica ni ejecuta pruebas directamente): briefs al
  `planificador`/`general`; implementación, builds e instalaciones al `worker`; defectos al `fixer`;
  verificación al `explore`/`validador`; pantallas al `diseno-frontend`.
- **No commitear/pushear sin autorización** del dueño. Commits por bloque temático; excluir
  `.opencode/`, `eliminar-movements.jpeg` y `node_modules/`.
- **No tocar `kyc-service/`.** Sin PII/secretos en logs ni respuestas. Cliente delgado (el frontend
  no calcula saldos/comisiones/cuotas/DTI/score). No borrar pruebas para pasar.
- Fronteras por módulo: ningún módulo accede a tablas de otro (fachadas/eventos); cambios de ORM con
  migración Alembic, `alembic check` limpio y **un solo head**.
- Antes de avanzar: contrastar con `docs/01` (decisiones, fuente de verdad) y la doc canónica; si
  algo contradice, manda `docs/01` y se corrige.

## 10. Doc clave a leer

- `docs/01-hallazgos-y-decisiones.md` (fuente de verdad), `docs/04-motor-transaccional-y-ledger.md`
  (§7 asientos; :7.5 desembolso, :7.6 cuota, :7.4 QR, :7.10 servicios), `docs/05-contratos-api.md`
  (§6.3/§6.4/§6.5/§6.9), `docs/08-epica-3-creditos.md` (HU09–HU12), `docs/09-epica-4-billetera.md`
  (HU13–HU16), `docs/11-epica-6-notificaciones.md` (HU22).
- `docs/14-plan-sprints-y-ejecucion.md` (cronograma; Sprint 4 = HU19–HU21/HU24–HU25),
  `docs/15-calidad-seguridad-devops.md` (§8 tareas Q-T*, §9 criterios de salida),
  `docs/16-guia-para-agentes.md` (DoD y reglas de oro), `docs/18-gestion-de-cambios.md`
  (gaps `G*`/`OBS-*`/`M-*`/`H*`).
- `docs/20-diseno-ui.md` (tokens), `docs/design/mockups.md` / `docs/design/creditos-pantallas.md`
  (spec de pantallas), `docs/demo/guion-demo-sprint-2.md`, `docs/guia-acceso-panel-web.md`, y este
  `HANDOFF.md` §7.
