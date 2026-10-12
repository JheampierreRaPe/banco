# HANDOFF — Orquestador → continuación (cierre final del Sprint 2) · actualizado 2026-10-12

Lee esto antes de continuar. Estado real del proyecto. Reemplaza al handoff del 2026-10-10
(cierre previo de créditos + panel web). El **Sprint 2 quedó CERRADO** (papeleo incluido) y la
rama **`sprint-2` está pusheada con CI verde**; el índice y el cierre oficial están en
`docs/tasks/SPRINT-2.md#cierre-del-sprint-2-2026-10-11`.

## 1. Dónde estamos

- Rama de trabajo: **`sprint-2`**, HEAD actual **`835078c`**, **YA PUSHEADA**
  (`sprint-2` con upstream `origin/sprint-2`). `origin` =
  https://github.com/JheampierreRaPe/banco . **CI verde**: run #14, **8/8 jobs `success`**
  (`lint`, `test`, `migrations`, `build`, `scan`, `frontend`, `panel`, `ci-success`) →
  https://github.com/JheampierreRaPe/banco/actions/runs/38255962404 . (`gh` no está instalado →
  PR vía push + URL web.)
- **Stack Docker:** **5/5 `healthy`** (`postgres`, `redis`, `backend`, `worker`, `admin-web`).
  El microservicio KYC vive aparte del compose (contenedor `kyc-facial-service-8001`, `:8001`,
  `healthy`).
- **Commits recientes relevantes** (rama pusheada):
  - **Créditos HU09/HU10 (E3-T01..T11):** `64383ad` (motor financiero + catálogo),
    `b961c72` (API simulador + UI + QA), `fa3b1df` (adaptador de buró + reglas + solicitud),
    `2532480` (UI solicitud + panel analista + QA).
  - **HU11 firma de contrato (E3-T12..T16):** `48667cd` (3 documentos + migración `0033`),
    `1e0eae3` (firma biometría/PIN + copia por correo), `20fca8c` (UI contrato/firma + QA).
  - **Calidad:** `7918266` (Q-T03 seguridad), `6ab357b` (Q-T07 observabilidad),
    `fbd38ce` (Q-T08 escaneo + Q-T12 cobertura/CI).
  - **E3-T23 "Mis créditos":** `dd3d6fe` (listado + pestañas), `b64e763` (contrato get-or-create
    al abrir la firma), `13cdeca` (Firmado no re-firmable + volver al inicio).
  - **Papeleo/cierre:** `dfdbaf7` (sesión 2026-10-11), `276ad5d` (guion demo Q-T09),
    `28640d3` (cierre del Sprint 2), `7bb160d` (estado tras cierre).
  - **Cierre de deuda técnica:** `9076546` (`G58` HMAC del PIN en `transactions` + `OBS-3` CAS
    del contador de fallos + `OBS-4` nonce de un solo uso), `c809518` (`G26` decimales en
    `PUT /admin/parameters` + `H5` env vars del buró en `docs/02#9`).
  - **CI endurecido:** `835078c` (el test de carga mide el camino de **escritura** en
    `backend/tests/load/harness.py`; `.gitleaks.toml` con allowlist a falsos positivos de
    fixtures de test).
- **Dispositivo adb:** `adb devices -l` puede mostrar duplicados con sufijo ` (2)` (p.ej.
  `adb-KJSGAUYHSCB6HUSS-yIJZve (2)._adb-tls-connect._tcp`, modelo `2311DRK48G`) →
  `adb disconnect "<serial (2)>"` para dejar uno.

## 2. Estado de suites / migraciones (verificado 2026-10-12)

- **Backend (`pytest`, desde `backend/` con `.venv`):** **1553 passed + 1 skipped**,
  cobertura **86%** (≥ 80, comando del CI con `--cov-fail-under=80`;
  `.github/workflows/ci.yml:82`). `ruff check .` limpio; `black --check .` (391 archivos sin
  cambios); `alembic check` → "No new upgrade operations detected". **Head de migración:
  `0033_credits_contracts`** (`backend/migrations/versions/0033_credits_contracts.py`).
  - Nota: `tests/load/test_engine_load.py::test_e5_t08_pico_fin_de_mes_sin_degradacion` es un
    test de **carga** con SLA p95 < 200 ms y es **sensible a la contención de la máquina**:
    falló una vez corriendo las tres suites en paralelo y pasó solo en 0.54 s. No es un defecto
    del producto; no ejecutar las suites pesadas en paralelo.
- **Frontend (`flutter`, desde `frontend/`):** `flutter analyze` → "No issues found!";
  `flutter test` → **829/829**.
- **Panel `admin-web` (desde `admin-web/`):** `npm test` → **18/18** (vitest, 3 archivos);
  `npm run lint` (`tsc --noEmit`) y `npm run build` limpios.
- **CI (GitHub Actions, `.github/workflows/ci.yml`):** **8 jobs** (`lint`, `test`,
  `migrations`, `build`, `scan`, `frontend`, `panel`) + agregador `ci-success`; **run #14 →
  8/8 `success`** (`835078c`). Dispara en `push` a `main`, `feature/**` y **`sprint-*`**
  (`.github/workflows/ci.yml:9`) y en PR a `main`. **El CI NO corre en local**: solo se ejecuta
  en GitHub al pushear.

## 3. Qué se hizo (por bloques)

- **HU06/HU07/HU08 (Sprint 2, base):** transferencias propias/tercero/interbancarias
  (hold+settle, saga con compensación, comprobante PDF), beneficiarios (AES-256-GCM + índice
  ciego), reto biométrico del dispositivo con fallback PIN, umbral + límite diario, cuenta de
  14 dígitos con DV + lookup DNI/celular, `/movements` con reverso, `/admin/parameters`.
  Detalle en `docs/tasks/archive/` y `docs/tasks/SPRINT-2.md`.
- **Correcciones `E2-T33..T37`:** retiro real del tope pre-aprobado (`0026`), umbral biométrico
  **por operación** (`E2-T34`), `entry_type` en movimientos (`0027`) + backfill (`0028`),
  cierre del bypass del límite diario (`E2-T36`). Gaps `G46..G49` en `docs/18`.
- **Créditos HU09/HU10 (`E3-T01..T11`):** motor financiero francés (TEA/TCEA), catálogo
  `loan_products` (`0029`), `POST /credits/simulate` + PDF, adaptador `CreditBureau`
  (mock+HTTP), motor de reglas DTI/score (`0030`), expediente `loan_applications` (`0031`) +
  outbox, panel de analista (bandeja `IN_REVIEW` → dictamen con motivo obligatorio).
- **HU11 firma de contrato (`E3-T12..T16`, adelanto de Sprint 3):** **3 documentos separados**
  (contrato + pagaré + resumen) con `doc_ref`/`doc_hash` (`0033`); servicio de firma con sello
  determinista + hash del token; endpoints `POST .../sign/challenge` y
  `POST .../sign` (biometría del dispositivo **o** PIN, mutuamente excluyentes,
  `Idempotency-Key`); copia certificada por correo **best-effort** (sender Gmail/mock);
  UI Flutter `/credits/contract` + `/credits/sign`; QA de integridad/no reutilización.
- **`E3-T23` "Mis créditos":** `GET /credits/applications` (sin `status`) devuelve **solo las
  solicitudes del titular** (vista mínima, sin ingresos/deuda/score); pantalla `/credits` con
  pestañas `Simulador | Mis créditos`; **contrato get-or-create al abrir la firma**; estado
  **Firmado no re-firmable** (`409`) y "volver al inicio". Gap `G59` en `docs/18`.
- **Calidad:** `Q-T03` (QA de seguridad RBAC/idempotencia/rate-limit/inyección), `Q-T07`
  (logs estructurados + `request_id` correlacionado), `Q-T08` (escaneo de deps/secretos:
  `bandit` + `pip-audit` + `npm audit` + gitleaks/secrets-check), `Q-T12` (cobertura con umbral
  `--cov-fail-under=80` + jobs frontend/panel + `ci-success`), `Q-T09` (guion y datos de demo).
  Todos archivados en `docs/tasks/archive/`.
- **Cierre de deuda técnica (post-cierre):** `9076546` (`G58` HMAC del PIN en `transactions`,
  `OBS-3` CAS en el contador de fallos, `OBS-4` nonce de un solo uso), `c809518` (`G26`
  decimales en `PUT /admin/parameters`, `H5` env vars del buró en `docs/02#9`), `835078c` (CI:
  el test de carga mide el camino de escritura + allowlist gitleaks de fixtures). Con esto el
  Sprint 2 queda **cerrado y con CI verde**.

## 4. Qué funciona de verdad (verificado)

- **Transferencias:** propia (ruta atómica), a tercero (hold+settle, comisión, comprobante) e
  interbancaria (saga, timeout, reverso). Destino por cuenta de 14 dígitos con DV o por
  DNI/celular con lookup previo neutro. Idempotencia por titular (replay con
  `meta.replayed=true`).
- **Créditos (ciclo completo hasta firma):** simular → solicitar → dictamen → panel del analista
  (`admin-web`) → **firmar con biometría o PIN** → **copia por correo**. El buró mock es
  determinista por hash del `sub` (`prime` aprueba, `standard` → `IN_REVIEW`).
- **"Mis créditos" (`E3-T23`):** listado del titular con pestañas, contrato al abrir la firma,
  estado Firmado sin re-firma.
- **Panel web (`:8080`):** bandeja `IN_REVIEW` + detalle enmascarado + dictamen; conectado al
  backend por **same-origin** (nginx `/api` → `backend:8000`). Token de analista con
  `backend/scripts/mint_demo_token.py` (firma local, sin backdoor). Runbook:
  `docs/guia-acceso-panel-web.md`.
- **Guion de demo:** `docs/demo/guion-demo-sprint-2.md` (requisitos previos, datos demo, paso a
  paso por HU, checklist y plan B).

## 5. Pendientes (para el que continúe)

> **Estado:** el Sprint 2 está **cerrado**, la rama `sprint-2` está **pusheada** (`origin/sprint-2`,
> HEAD `835078c`) y el **CI está verde** (run #14, 8/8 `success`). No queda deuda técnica abierta.

- **Vídeo de respaldo del demo (`docs/15`):** **único pendiente real** de papeleo. Lo graba el
  dueño; el guion está en `docs/demo/guion-demo-sprint-2.md` (`Q-T09` lo pide explícitamente en
  `docs/15-calidad-seguridad-devops.md:106`).
- **HU12 (Sprint 3) — desembolso + cuotas** (`E3-T17..E3-T22`): **sin briefs ni código**. Es el
  **siguiente bloque natural**. Referencias conceptuales:
  `docs/08-epica-3-creditos.md:78` (HU12) y los asientos `docs/04-motor-transaccional-y-ledger.md:163`
  (§7.5 desembolso) y `:170` (§7.6 pago de cuota). Requiere crear los briefs antes de implementar.
- **Sprint 3+:** HU13–HU16 (billetera/QR/servicios), panel general `F-T12`, frontend
  `F-T06..F-T18`.
- **Deuda técnica — CERRADA (no figura como abierta):**
  - ~~**`G58`** — HMAC del PIN en `transactions`~~ **CERRADO** (`9076546`): el motor de dinero ya
    persiste HMAC del PIN (no `sha256` sin sal) en el `request_hash`; regresión en
    `backend/tests/test_e2_t31_authorize_pin.py`.
  - ~~**`OBS-3`** — CAS en `save_block` del contador de fallos~~ **CERRADO** (`9076546`).
  - ~~**`OBS-4`** — `nonce` de un solo uso~~ **CERRADO** (`9076546`): ya no es reintentable tras
    fallo.
  - ~~**`G26`** — `PUT /admin/parameters` no acepta decimales~~ **CERRADO** (`c809518`): acepta
    decimales (`loan.max_dti_ratio`, `loan.review_margin`) vía `set_parameter`; regresión en
    `backend/tests/test_g26_admin_parameters_decimal.py`.
  - ~~**`H5` (de E3-T06)** — registrar las env vars del buró~~ **CERRADO** (`c809518`):
    documentadas en `docs/02-arquitectura.md#9`.
- **Papeleo:** ~~pushear `sprint-2`~~ **HECHO** (`origin/sprint-2` = `835078c`); solo resta el
  **vídeo de respaldo** del demo.

## 6. Riesgos abiertos (`docs/17-riesgos-y-decisiones-abiertas.md`)

- `device_public_key` = secreto **HMAC**, no clave pública asimétrica (R26) → si se filtra
  `device_bindings.public_key` se compromete el secreto de firma.
- OTP hasheado SHA-256 simple (R22); rate-limit **en memoria por réplica**, no compartido (R28).
- Dependencias/imágenes **sin pinear** (riesgo transversal de `docs/17` §3/§4).
- RUC validado **solo contra el mock** (R29); D05/R02 (DevOps sin titular formal, aceptado para
  el MVP/demo).
- **Nuevo:** los `nonce` de firma viven **en memoria del proceso** (no persistidos entre réplicas).
- **El CI no se ejecuta en local** (solo en GitHub al pushear): verificar localmente con los
  comandos de §8.

## 7. Cómo arrancar / cerrar sesión (`hola` / `adios`)

Protocolo vigente (lo ejecuta un `worker` delegado):

- **`hola` (arranque):**
  1. **Levantar contenedores:** `docker compose ps -a` → si existen detenidos, `docker compose
     start`; si no, `docker compose up -d`. Esperar `healthy`.
     - **VERSION SKEW (importante):** `start` **no recompila**. Si cambió el **código o las
       migraciones del backend** desde la última build, la imagen queda vieja → el backend
       crashea (`Can't locate revision …`) o sirve un contrato desactualizado. En ese caso:
       `docker compose build backend worker` + `docker compose up -d --force-recreate backend
       worker`. **Nota:** la sesión anterior cambió el middleware `request_id` (Q-T07) y el
       backend actual de Docker ya está recreado (`Up ... healthy`), pero **ante cualquier duda
       reconstruir**.
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
.venv\Scripts\python.exe -m pytest --cov=app --cov-fail-under=80 -q   # 1553 passed + 1 skipped, cov 86%
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m black --check .
.venv\Scripts\alembic.exe check ; .venv\Scripts\alembic.exe heads      # head 0033_credits_contracts

# Frontend (desde frontend/)
flutter analyze ; flutter test                                         # analyze limpio; 829/829

# Panel admin-web (desde admin-web/)
npm test ; npm run lint ; npm run build                                # 18/18; tsc; build limpio

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
.venv\Scripts\python.exe scripts\seed_demo.py check                   # dataset sintetico reproducible
```

- Config clave sembrada: `transfer.biometric_threshold_minor` = **100000** (S/ 1,000);
  `transfer.daily_limit_*_minor` = **500000** (S/ 5,000).
- **Tras cualquier cambio de backend:** reconstruir la imagen (§7) antes de probar; APK y
  backend del mismo HEAD.

## 9. Reglas de trabajo (vigentes)

- El **orquestador delega** (no implementa ni verifica ni ejecuta pruebas directamente): briefs al
  `planificador`/`general`; implementación, builds e instalaciones al `worker`; defectos al
  `fixer`; verificación al `explore`/`validador`; pantallas al `diseno-frontend`.
- **No commitear/pushear sin autorización** del dueño. Commits por bloque temático; excluir
  `.opencode/`, `eliminar-movements.jpeg` y `node_modules/`.
- **No tocar `kyc-service/`.** Sin PII/secretos en logs ni respuestas. Cliente delgado (el
  frontend no calcula saldos/comisiones/DTI/score). No borrar pruebas para pasar.
- Fronteras por módulo: ningún módulo accede a tablas de otro (fachadas/eventos); cambios de ORM
  con migración Alembic y `alembic check` limpio.
- Antes de avanzar: contrastar con `docs/01` (decisiones, fuente de verdad) y la doc canónica; si
  algo contradice, manda `docs/01` y se corrige.

## 10. Doc clave a leer

- `docs/01-hallazgos-y-decisiones.md` (fuente de verdad), `docs/04-motor-transaccional-y-ledger.md`
  (§7 asientos; :163 desembolso, :170 cuota), `docs/05-contratos-api.md` (§6.3/§6.4/§6.8; §64
  créditos), `docs/08-epica-3-creditos.md` (HU09–HU12).
- `docs/14-plan-sprints-y-ejecucion.md` (cronograma; Sprint 3 = firma/desembolso/wallet/QR),
  `docs/15-calidad-seguridad-devops.md` (§8 tareas Q-T*, §9 criterios de salida),
  `docs/16-guia-para-agentes.md` (DoD y reglas de oro), `docs/18-gestion-de-cambios.md`
  (gaps `G*`/`OBS-*`).
- `docs/demo/guion-demo-sprint-2.md`, `docs/guia-acceso-panel-web.md`,
  `docs/design/creditos-pantallas.md` (spec de contenido de las pantallas de créditos), y este
  `HANDOFF.md` §7 (§9 previo → protocolo `hola`/`adios`).
