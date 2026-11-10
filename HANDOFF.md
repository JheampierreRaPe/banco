# HANDOFF — Orquestador → continuación (Sprint 3 cerrado y mergeado; Sprint 4 planificado) · actualizado 2026-11-10

Lee esto antes de continuar. Estado real del proyecto. Reemplaza al handoff del 2026-10-14
(cierre del Sprint 3 + auditoría UX). El **Sprint 3 quedó CERRADO Y MERGEADO** a `main`
(38 briefs `Hecho` + 6 incrementos post-cierre `Hecho` = **44 briefs**, validados). El **PR #9**
(base `main`, head `sprint-3`) se **cerró y mergeó el 2026-11-10** con merge commit `d2d038d`;
`main` es la **rama por defecto** del repo. Lo siguiente es el **Sprint 4**
(conciliación/antifraude/cumplimiento/multidivisa), **ya planificado**:
`docs/tasks/SPRINT-4.md` + **38 briefs `Pendiente`**, y el pulido de **interfaz** que pidió el dueño.

## 1. Dónde estamos

- Rama de trabajo: **`main`**, HEAD actual **`d2d038d`** ("Release: Sprints 1-3"), sincronizada con
  `origin/main`. `main` es la **rama por defecto** del repo. `origin` =
  https://github.com/JheampierreRaPe/banco .
- **PR #9 cerrado y mergeado (2026-11-10):** base **`main`**, head **`sprint-3`**, merge commit
  **`d2d038d`**. No hay PR de release pendiente. `feature/qa-bootstrap` quedó **obsoleta/divergente**
  (solo remota, `d6c8e2d`); **no usarla como base**.
- **Ramas de referencia:** `sprint-3` local/remota en **`df2ff9c`**; `sprint-2` en **`6a62ecf`**.
  El push a `sprint-*` dispara CI; **el CI solo corre en GitHub al pushear, no en local**.
- **Working tree:** `HANDOFF.md` **modificado sin commitear** (contenido del dueño, **no descartar**),
  los 6 briefs post-cierre movidos a `docs/tasks/archive/` (aún sin commit) y untracked excluidos:
  `.opencode/`, `docs/sesiones/2026-10-16.md`, `eliminar-movements.jpeg`. Además están los **38 briefs
  nuevos del Sprint 4** y `docs/tasks/SPRINT-4.md` (planificación cerrada, todos `Pendiente`).
  `15-10-2026.txt` (feedback crudo) está ignorado por `*.txt`.
- **Stack Docker validado:** 5/5 `healthy` (`postgres`, `redis`, `backend`, `worker`, `admin-web`);
  `/health/ready` **OK**; panel `admin-web` responde **`200`**; KYC (`kyc-facial-service-8001`,
  `:8001`) `healthy`; Alembic en **`0047_wallet_personal_qr`** al día. **Nota nginx:** `admin-web`
  ya usa el **resolver de Docker** (`127.0.0.11`) para re-resolver `backend`; aun así, si el panel
  devuelve `502/Connection refused` tras recrear `backend`, `docker compose up -d --force-recreate
  admin-web` lo resuelve al instante.
- **Commits del Sprint 3 por bloque** (histórico; integrados en `main` por el merge `d2d038d`):
  - **HU12 desembolso + cuotas** (`E3-T17`..`E3-T22`) + prereq catálogo contable (`E5-T23`): `ae9a4d9`.
  - **HU13 billetera** (`E4-T01`..`E4-T05`): `4b6a316`.
  - **HU14 QR** (`E4-T06`..`E4-T10`): `4f16296`.
  - **HU15 cobro QR** (`E4-T11`..`E4-T15`): `e8a7ead`.
  - **HU16 servicios/recargas** (`E4-T16`..`E4-T20`): `65e1ad2`.
  - **HU22 notificaciones de seguridad** (`E6-T12`..`E6-T16`): `55f21e3`.
  - **Infra compartida / wiring / docs del sprint:** `390b7a5`.
- **Fixes post-Sprint 3:**
  - **Auditoría UX de pantallas** (app + panel): `8e6261b` (frontend) y `21d69a4` (panel).
  - **Bloque feedback 15-10-2026** (recargas/servicios, desembolso/cronograma, correos, QR
    personal `E4-T21`): `24b0c41`.
  - **Bloque UX** (desembolso automático `E3-T24`, cámara QR `E4-T22`, rol comercio `E4-T23`,
    nombre enmascarado `E4-T24`, retrocesos `E4-T25` + resolver nginx): `73adb37`.
- **Referencia previa:** `sprint-2` quedó en `6a62ecf`; `origin/sprint-2` = `6a62ecf`.
- **Dispositivo adb:** `adb devices -l` puede mostrar duplicados con sufijo ` (2)` →
  `adb disconnect "<serial (2)>"` para dejar uno. El teléfono del dueño suele ser `NOVAT_15`
  (serial `adb-20230300912-SYXLlv._adb-tls-connect._tcp`); su IP puede ser `192.168.1.67` y la del
  **PC/backend** `192.168.1.20` (la que lleva el APK).

## 2. Estado de suites / migraciones (suites históricas; **stack validado**)

> **Suites no re-ejecutadas en esta pasada.** El **stack Docker sí quedó validado**: 5/5 `healthy`,
> `/health/ready` OK, panel `admin-web` 200 y KYC `healthy`; Alembic está en el head único
> **`0047_wallet_personal_qr`** al día. Los números de suites de abajo son los **últimos registrados**
> en las sesiones previas, no una re-verificación actual; re-verificar con §8 antes de usarlos como
> evidencia.

- **Backend (`pytest`, desde `backend/` con `.venv`):** **~2107 passed + 1 skipped**; cobertura
  **~86%** (umbral del CI `--cov-fail-under=80`); `ruff`/`black` en verde.
- **Frontend (`flutter`, desde `frontend/`):** `flutter analyze` limpio; `flutter test` **1118**.
- **Panel `admin-web` (desde `admin-web/`):** **23 tests** (vitest); `npm run lint` (`tsc --noEmit`)
  y `npm run build` limpios.
- **Migraciones:** un solo head Alembic **`0047_wallet_personal_qr`**
  (`backend\.venv\Scripts\alembic.exe heads`); cadena `0044 → 0045 → 0046 → 0047`; `alembic check`
  limpio. (`0045` correos, `0046` cuenta de liquidación, `0047` QR personal.)
- **Nota:** el CI **no corre en local**; los números son de las corridas locales documentadas.
  **No correr suites pesadas en paralelo** (contención de máquina; ya causó un falso fallo).

## 3. Qué se hizo (por bloques)

- **Sprint 3 (HU11–HU16, HU22):** crédito completo (simular → solicitar → dictamen → firmar →
  desembolsar → cuotas), billetera tokenizada, QR firmado/caducidad/cobro E2E, servicios y recargas
  con comprobante, y notificaciones de seguridad multicanal + "no reconozco". Detalle por HU en
  `docs/tasks/SPRINT-3.md#cierre-del-sprint-3` y en el handoff previo (2026-10-14).
- **Incrementos post-cierre (esta etapa):**
  - **`E4-T21` QR personal estático** (`POST /wallet/qr` + `GET /wallet/qr/{id}`): cualquier titular,
    **sin monto** (lo digita el pagador), **sin caducidad** y **reutilizable**; migración `0047`.
  - **Fix crítico recargas/servicios:** cuenta de liquidación sembrada (`0046`,
    `WALLET_SERVICES_SETTLEMENT_ACCOUNT_ID` inyectada en compose) y `503 SETTLEMENT_UNAVAILABLE`
    honesto (antes `422` genérico).
  - **Correos humanizados:** `operation`→etiqueta, fecha legible, asuntos/tildes (`0045`).
  - **`E3-T24` desembolso AUTOMÁTICO al firmar:** al firmar se desembolsa **solo** a la cuenta
    principal (sin CTA manual), con confirmación en pantalla + **notificación dedicada**
    `credit_disbursed` (push+email, sin duplicar la genérica). CTA manual solo como **reintento**.
  - **`E4-T22` cámara real** (`mobile_scanner`) para escanear el QR de pago; fallback de texto si se
    niega el permiso.
  - **`E4-T23` rol comercio:** `GET /wallet` expone `is_merchant`/`merchant_status`; la app **oculta**
    "Cobrar como comercio" a cuentas personales (fail-closed).
  - **`E4-T24` nombre del dueño del QR enmascarado:** `payee_name_masked` (`DIEG••• GONZ•••`) en
    estado del QR personal y en el **preview de pago**.
  - **`E4-T25` botones de retroceso:** auditoría (~70 pantallas); back real (`pop`) donde falta —
    principal fix en **Movimientos**; sin botones redundantes a Home; pantallas terminales intactas.
  - **`nginx.conf`:** resolver de Docker para que el panel no quede apuntando a la IP vieja del
    backend tras cada rebuild.
- **Gaps registrados en `docs/18`:** `G54`..`G92` (ver §5). Cerrados recientes: `G88` (rol comercio),
  `G89` (cámara), `G90` (desembolso automático), `G91` (nombre enmascarado), `G92` (retrocesos).

## 4. Qué funciona de verdad (verificado)

- **Ciclo de crédito completo:** simular → solicitar → dictamen (panel analista) → **firmar** →
  **desembolso automático** a la cuenta principal (asiento `docs/04#7.5`, cronograma) + notificación →
  **pagar cuota** (asiento `docs/04#7.6`), con débito automático y recordatorios. El buró mock es
  determinista por hash del `sub`.
- **Billetera/QR/servicios:** vinculación tokenizada (nunca PAN), pago express con límites, QR de
  comercio (dinámico/estático) y **QR personal estático** (`/wallet/qr`), **escaneo con cámara real**,
  cobro E2E (`POST /payments/qr`) con abono < 5 s, **preview con nombre del titular enmascarado**,
  servicios/recargas con comprobante `OP-…` (cuenta de liquidación sembrada en la demo).
- **Notificaciones:** alertas push **+ correo** por evento sensible, preferencias por canal, centro en
  la app y "no reconozco" (revoca sesiones + escala a `risk`); correos con contenido legible.
- **Panel web (`:8080`):** bandeja `IN_REVIEW` + detalle enmascarado + dictamen; same-origin
  (nginx `/api` → `backend:8000`, con resolver durable); token de analista con
  `backend/scripts/mint_demo_token.py`; sesión expirada, refresco y logout cableados. Runbook:
  `docs/guia-acceso-panel-web.md`.
- **Navegación:** entradas reales a `/wallet*` y `/notifications*`; sin CTAs muertos en el recorrido
  auditado; botones de retroceso donde corresponde.

## 5. Pendientes (para el que continúe)

> **Estado:** Sprint 3 **cerrado y mergeado** a `main` (HEAD `d2d038d`; PR #9 mergeado el
> 2026-11-10). Los 6 incrementos post-cierre quedaron `Hecho` y archivados. **Sprint 4 planificado**:
> `docs/tasks/SPRINT-4.md` + **38 briefs `Pendiente`**.

- **(a) Pulido de interfaz (pedido explícito del dueño):** "solo hay cosas de interfaz que mejorar";
  el resto funciona. Es una pasada UX/cosmética (estilos, copy, espaciados, mockups), no funcional.
- **(b) Vídeo de respaldo del demo:** lo graba el **dueño**; guion en `docs/demo/guion-demo-sprint-2.md`.
- **(c) Sprint 4 — planificado (`docs/tasks/SPRINT-4.md` + 38 briefs `Pendiente`):**
  - Briefs: `E5-T16..E5-T22` (7), `E6-T01..E6-T11` (11), `E7-T01..E7-T13` (13), `F-T12..F-T18` (7).
  - Todos en `docs/tasks/` con `Estado: Pendiente`; la implementación aún no empezó.
  - **HU19 Conciliación bancaria** (`reconciliation`, 8 SP) — `E5-T16..E5-T22`.
  - **HU20 Motor antifraude** (`risk`, 8 SP) — `E6-T01..E6-T06`.
  - **HU21 Cumplimiento PLDFT/AML** (`risk`, 8 SP) — `E6-T07..E6-T11`.
  - **HU24 Multidivisa/meta de ahorro** (`fx`, 5 SP) — `E7-T01..E7-T07`.
  - **HU25 Cambio de divisas** (`fx`, 8 SP) — `E7-T08..E7-T13`.
  - Frontend/panel pendiente: `F-T12..F-T18` (panel general y vistas).
  - Fuera del MVP: `HU23` (auditoría inalterable) y `HU26` (distribución de ingresos).
- **(d) Deuda / observaciones abiertas (numeradas):**
  1. **`location` real de alertas (`G80`):** ubicación aprox. por ciudad/región a cargo del servidor;
     hoy fallback "no disponible" (jamás GPS/PII). Pendiente de decisión del dueño.
  2. **`WALLET_SERVICES_SETTLEMENT_ACCOUNT_ID` en producción:** la demo usa la cuenta determinista
     (`0046`, UUID `d0000000-0000-4000-8000-000000000001`); en prod apuntar al UUID real del proveedor
     (rotable por entorno; fail-closed → `503 SETTLEMENT_UNAVAILABLE`).
  3. **Sintonía de `risk.alerts`/severidad con HU20 (`G83`):** HU22 escala (severidad + evento
     `fraud.alert.raised`) sin escribir `risk.alerts`; coordinar con `E6-T03`/`E6-T04`.
  4. **`meta.total` real en la bandeja del panel:** el backend devuelve `meta.total = tamaño de
     página`; el panel pagina por `offset` y muestra "50+" cuando la última página vino llena.
  5. **Mockups `G71`/`G76`/`G63`:** UI construida con tokens `docs/20`; el **dueño aporta** las
     imágenes/spec (`billetera-*`, `billetera-qr-pago.png`, `creditos-cronograma.png`).
  6. **`G75`** (dispatch por `metadata` en `POST /transactions/{id}/authorize`): módulo `transactions`;
     hoy usa el lector por defecto.
  7. **`G65`** (pago parcial/prepago/abono a capital): fuera de alcance por decisión del dueño.
  8. **Hash de teléfono sin sal** (hallazgo BAJO, preexistente): `hash_supply_ref`/`phone` se registran
     como `sha256[:16]`; migrar a HMAC con secreto si se exige "no PII en logs" estricto.
  9. **QA HU16 (medio, fail-closed):** un `biller` nuevo se lista pero el proveedor `mock` rechaza el
     suministro (`422`) antes de cobrar; hacerlo pagable exige ampliar el proveedor.
  10. **Mockups/spec de pantallas** (G71/G76/G63) y **mejoras de interfaz** (§5a).

## 6. Riesgos abiertos (`docs/17-riesgos-y-decisiones-abiertas.md`)

- `device_public_key` = secreto **HMAC**, no clave pública asimétrica (R26).
- OTP hasheado SHA-256 simple (R22); rate-limit **en memoria por réplica**, no compartido (R28).
- Dependencias/imágenes **sin pinear** (riesgo transversal de `docs/17` §3/§4).
- RUC validado **solo contra el mock** (R29); D05/R02 (DevOps sin titular formal, aceptado para demo).
- Los `nonce` de firma viven **en memoria del proceso** (no persistidos entre réplicas).
- **Notificaciones:** el reporte "no reconozco" escala a `risk` sin escribir `risk.alerts` (HU20); la
  severidad fina y la ubicación servidora quedan abiertas (§5.1/§5.3).
- **Panel/nginx:** el resolver durable mitiga el cambio de IP del backend; si aun así falla el proxy,
  recrear `admin-web`.
- **El CI no se ejecuta en local** (solo en GitHub al pushear): verificar con §8.

## 7. Cómo arrancar / cerrar sesión (`hola` / `adios`)

Protocolo vigente (lo ejecuta un agente delegado):

- **`hola` (arranque):**
  1. **Levantar contenedores:** `docker compose ps -a` → si existen detenidos, `docker compose start`;
     si no, `docker compose up -d`. Esperar `healthy`.
     - **VERSION SKEW (importante):** `start` **no recompila**. Si cambió código/migraciones del
       backend desde la última build → `docker compose up -d --build backend worker`. Ante duda,
       reconstruir.
     - **Si reconstruís `backend`, revisá el panel:** si `admin-web` quedó apuntando a la IP vieja
       (`502/Connection refused`), `docker compose up -d --force-recreate admin-web` (ya hay resolver
       durable, pero por las dudas).
     - Si cambió el **frontend**, reconstruir e instalar el APK (§8). APK y backend del **mismo HEAD**.
  2. **Warmup KYC:** `docker start kyc-facial-service-8001` si hace falta; red `banca-demo_default`;
     `http://localhost:8001/health`.
  3. **ADB / dispositivo (obligatorio):** `adb start-server`; `adb devices -l` y **validar que haya
     exactamente un dispositivo en estado `device`**. Si aparece duplicado con sufijo ` (2)` →
     `adb disconnect "<serial (2)>"`. Si **no** aparece ninguno: reconectar por USB o
     `adb connect <ip>:<puerto>` (ADB TLS/Wi-Fi) y repetir `adb devices -l` hasta confirmarlo; usar
     ese serial para instalar/logcat (§8). No dar el `hola` por terminado sin un dispositivo válido.
  4. **Rama y base:** `git status -sb` y `git log --oneline <base>..HEAD`. Si HEAD acumula commits
     de más de un brief sin PR, avisar antes de seguir (política `docs/21`).
- **`adios` (cierre):**
  1. **Detener** con `docker compose stop` (**NO** `down`); verificar con `docker compose ps -a` que
     los 5 servicios quedan en `Exited` (detenidos, **no** eliminados) y que KYC queda intacto.
  2. **Medir el diff:** `git diff --shortstat <base>...HEAD` y las líneas semánticas
     (`docs/21-politica-prs.md`). Si supera 700 líneas semánticas o mezcla más de un brief,
     **no cerrar con un solo PR**: abrir PRs apilados por corte.
  3. **Abrir el PR** (o los PRs) antes de cerrar. Prohibido terminar con "PR pendiente"; verificar
     que el PR tiene checks de CI.
  4. **Preguntar** si se desea commitear (solo si autoriza).
  5. **Generar** `docs/sesiones/<fecha>.md` (tareas, commits, suites, pendientes, riesgos).
  - Cerrar con el mensaje exacto: **"Descansa bello"**.

## 8. Comandos rápidos

```powershell
# Backend (desde backend/)
.venv\Scripts\python.exe -m pytest --cov=app --cov-fail-under=80 -q   # ~2107 passed + 1 skipped, cov ~86%
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m black --check .
.venv\Scripts\alembic.exe check ; .venv\Scripts\alembic.exe heads      # head 0047_wallet_personal_qr

# Frontend (desde frontend/)
flutter analyze ; flutter test                                         # analyze limpio; 1118

# Panel admin-web (desde admin-web/)
npm test ; npm run lint ; npm run build                                # 23 tests; tsc; build limpio

# Stack
docker compose ps                                                      # 5 servicios healthy
Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:8000/health/ready' -TimeoutSec 10 |
  Select-Object -ExpandProperty Content                               # {"status":"ok","database":true}

# APK (reconstruir con la IP LAN vigente del PC; hoy PC 192.168.1.20, telefono 192.168.1.67)
flutter build apk --debug --dart-define=API_BASE_URL=http://192.168.1.20:8000 --dart-define=APP_VERSION=0.1.0
adb -s <serial> install -r build\app\outputs\flutter-apk\app-debug.apk

# Token de analista (desde backend/; escribe a %TEMP%, nunca al repo)
.venv\Scripts\python.exe scripts\mint_demo_token.py > $env:TEMP\analyst.jwt
# Variantes: --role credit_analyst | --subject <uuid> | --minutes 60

# Dataset demo
.venv\Scripts\python.exe scripts\seed_demo.py check
```

- Config sembrada: `transfer.biometric_threshold_minor` = **100000** (S/1,000);
  `transfer.daily_limit_*_minor` = **500000** (S/5,000); `wallet.express_limit_minor` = **20000**
  (S/200); `wallet.daily_limit_minor` = **500000** (S/5,000); `wallet.qr_ttl_seconds` = **300**
  (5 min); `wallet.service_reminder_days_before` = **3**.
- **Tras cualquier cambio de backend:** reconstruir la imagen (§7) antes de probar; APK y backend del
  mismo HEAD.

## 9. Reglas de trabajo (vigentes)

- El **orquestador delega** (no implementa ni verifica ni ejecuta pruebas directamente): briefs al
  `planificador`/`general`; implementación/builds al `worker`; defectos al `fixer`; verificación al
  `explore`/`validador`; pantallas al `diseno-frontend`.
- **Entrega por corte:** 1 brief o 1 corte = 1 rama = 1 PR (`docs/21-politica-prs.md`).
  Prohibido el "commit paraguas" que mezcla tareas no relacionadas.
- **No commitear/pushear sin autorización** del dueño. Commits por bloque temático; excluir
  `.opencode/`, `eliminar-movements.jpeg` y `node_modules/`.
- **No tocar `kyc-service/`.** Sin PII/secretos en logs ni respuestas. Cliente delgado (el frontend no
  calcula saldos/comisiones/cuotas/DTI/score). No borrar pruebas para pasar.
- Fronteras por módulo: ningún módulo accede a tablas de otro (fachadas/eventos); cambios de ORM con
  migración Alembic, `alembic check` limpio y **un solo head**.
- Antes de avanzar: contrastar con `docs/01` (fuente de verdad) y la doc canónica; si algo contradice,
  manda `docs/01` y se corrige.

## 10. Doc clave a leer

- `docs/01-hallazgos-y-decisiones.md` (fuente de verdad), `docs/04-motor-transaccional-y-ledger.md`
  (§7 asientos; :7.4 QR, :7.5 desembolso, :7.6 cuota, :7.10 servicios), `docs/05-contratos-api.md`
  (§6.3/§6.4/§6.5/§6.9), `docs/08-epica-3-creditos.md` (HU09–HU12), `docs/09-epica-4-billetera.md`
  (HU13–HU16), `docs/11-epica-6-fraude-cumplimiento.md` (HU20–HU22).
- `docs/14-plan-sprints-y-ejecucion.md` (cronograma; **Sprint 4 = HU19–HU21/HU24–HU25**),
  `docs/15-calidad-seguridad-devops.md`, `docs/16-guia-para-agentes.md` (DoD y reglas de oro),
  `docs/18-gestion-de-cambios.md` (gaps `G*`/`OBS-*`/`M-*`/`H*`).
- `docs/20-diseno-ui.md` (tokens), `docs/design/mockups.md` / `docs/design/creditos-pantallas.md`
  (spec de pantallas), `docs/demo/guion-demo-sprint-2.md`, `docs/guia-acceso-panel-web.md`, y este
  `HANDOFF.md` §7.
- `docs/tasks/SPRINT-3.md#cierre-del-sprint-3` (cierre e incrementos) y los briefs archivados
  `docs/tasks/archive/E3-T24.md` y `docs/tasks/archive/E4-T21.md`..`E4-T25.md` (estado `Hecho`).
