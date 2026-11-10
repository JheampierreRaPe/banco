# Guion de demo — Sprint 2 (Banca Online Integral)

> Alcance: transferencias y beneficiarios (HU06/HU07), reto biometrico/PIN y
> limites (HU08), simulador (HU09), solicitud + dictamen + analista (HU10),
> contrato + firma + copia por correo (HU11) y **"Mis creditos" (E3-T23)**.
> Todo con datos **sinteticos** (correos `@example.com`, documentos `000...`,
> montos en **centimos**). Sin PII real, sin secretos en el repo ni en logs.

Fuentes verificadas de este guion (no inventar otras rutas):
`docker-compose.yml`, `backend/app/core/health.py` (`GET /health/ready`),
`backend/scripts/seed_demo.py`, `backend/scripts/mint_demo_token.py`,
`admin-web/README.md`, `docs/guia-acceso-panel-web.md`, `frontend/README.md`,
`backend/app/adapters/credit_bureau.py` (`MOCK_PROFILES`, `mock_profile_for`).

---

## 1. Requisitos previos

### 1.1 Stack Docker (5 servicios healthy)

Desde la **raiz** del repo:

```powershell
docker compose up -d
docker compose ps
```

Los 5 servicios deben figurar `healthy`: `postgres`, `redis`, `backend`,
`worker`, `admin-web` (ver `docker-compose.yml` y
`docs/guia-acceso-panel-web.md#2`).

- Backend: `http://localhost:8000`.
- Panel del analista: `http://localhost:8080`.

Si hay **version skew** (imagenes viejas tras actualizar la rama):

```powershell
docker compose build backend worker
docker compose up -d
```

Si solo falla el panel (`:8080` no responde), reconstruir y recrear ese
servicio (ver `docs/guia-acceso-panel-web.md#8`):

```powershell
docker compose build admin-web
docker compose up -d --force-recreate admin-web
```

### 1.2 Healthcheck

```powershell
Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:8000/health/ready' -TimeoutSec 10 |
  Select-Object -ExpandProperty Content
```

Ruta real a **raiz** (sin `/api/v1`): ver `backend/app/core/health.py`
(`@router.get("/health/ready")`) y el healthcheck del servicio `backend` en
`docker-compose.yml`. El API versionado vive bajo `/api/v1`
(ver `frontend/README.md`: `AppConfig.apiUrl` agrega `/api/v1` solo).

### 1.3 App movil (APK) y `API_BASE_URL`

Instalar el APK de la demo y apuntarlo al backend correcto
(ver `frontend/README.md#configurar-api_base_url`):

```bash
# Emulador Android contra backend local (default, puedes omitirlo):
flutter run --dart-define=API_BASE_URL=http://10.0.2.2:8000

# Dispositivo fisico: IP LAN de tu PC (mismo Wi-Fi), puerto 8000:
flutter run --dart-define=API_BASE_URL=http://192.168.1.10:8000

# iOS simulator contra backend local:
flutter run --dart-define=API_BASE_URL=http://127.0.0.1:8000
```

> El backend debe escuchar en `0.0.0.0:8000` (no solo `127.0.0.1`) para que el
> dispositivo fisico lo alcance.

### 1.4 KYC arriba

Por defecto `KYC_PROVIDER=mock` (ver `docker-compose.yml`): **no** hace falta
levantar nada extra para la demo. El liveness del microservicio KYC se usa
**solo** en creacion de cuenta (HU01). Si la demo exige el servicio real,
activarlo con el perfil opcional (ver comentario en `docker-compose.yml`):

```powershell
docker compose --profile kyc up -d
```

y apuntar `KYC_BASE_URL=http://kyc:8000` en el `.env`.

### 1.5 Token de analista + abrir el panel

Los logins normales emiten `roles` vacio; la bandeja de credito exige rol
`analyst`/`credit_analyst`. Emitir el JWT con el script local
(desde `backend/`, ver `admin-web/README.md#token-de-analista`):

```powershell
cd backend
.venv\Scripts\python.exe scripts\mint_demo_token.py > $env:TEMP\analyst.jwt
# Variantes reales: --role credit_analyst | --subject <uuid> | --minutes 60
```

- Imprime **solo** el JWT por stdout; el diagnostico (`sub`/`roles`/vigencia/
  fuente del secreto) va a stderr. El secreto **jamas** se imprime.
- Vigencia por defecto: **15 minutos** (`--minutes <n>` para mas tiempo).
- Firma con el mismo `JWT_SECRET` del `.env` de la raiz (el que `docker compose`
  inyecta al backend). Para apuntar contra Docker, **no** exportes `JWT_SECRET`.
- Cada ejecucion sin `--subject` genera un `sub` aleatorio: usar uno **distinto**
  al del solicitante (si el analista dictamina su propio expediente, el servidor
  responde `409`).

Abrir el panel y pegar el token:

1. Abrir `http://localhost:8080` (misma maquina) o
   `http://<IP-LAN-del-host>:8080` (otra maquina de la red).
2. En la pantalla de sesion, pegar el contenido de `$env:TEMP\analyst.jwt`
   (se envia como `Authorization: Bearer`). Solo vive en memoria/
   `sessionStorage`: nunca va al repo, a archivos ni a logs.
3. Sin rol `analyst`/`credit_analyst` se ve la pantalla **denegada**
   (equivale al `403 NOT_AUTHORIZED`).

Detalle completo en `docs/guia-acceso-panel-web.md#3-#5`.

---

## 2. Datos de demo

### 2.1 Semilla reproducible (`backend/scripts/seed_demo.py`)

Dataset determinista (`SEED=42`): misma semilla => mismo dataset en cualquier
entorno (fuente: `backend/tests/factories.py`, huella `sha256`). Contenido
simulado, cero PII real:

- 4 usuarios demo: `ana`, `bruno`, `carmen` (`@example.com`, docs `000...`,
  KYC aprobado) + `diego` (KYC rechazado, para el camino de error).
- 4 cuentas en centimos: `ana-pen`, `bruno-pen`, `bruno-usd`, `carmen-pen`.
- Catalogo contable minimo (8 cuentas), 2 comercios QR (`cafe-central`,
  `libreria-norte`), 3 billers, 2 asientos cuadrados, llaves de idempotencia.
- **Cuenta de liquidacion demo de servicios/recargas (HU16):** UUID fijo
  `d0000000-0000-4000-8000-000000000001` (PEN, `ACTIVE`), sembrada por la
  migracion `0046_services_settlement` e inyectada por
  `WALLET_SERVICES_SETTLEMENT_ACCOUNT_ID` a `backend`/`worker`
  (`docker-compose.yml`). El motor acredita aqui al pagar/recargar
  (`docs/04#7.10`); sin ella el pago/recarga se rechaza fail-closed. Con
  esto, las recargas y pagos funcionan en la demo **sin configuracion manual**.

Comandos reales (desde `backend/`, ver docstring del script):

```powershell
cd backend
.venv\Scripts\python.exe scripts\seed_demo.py check
.venv\Scripts\python.exe scripts\seed_demo.py dump
.venv\Scripts\python.exe scripts\seed_demo.py load --database-url <TEST_DB_URL> [--seed 42]
```

> `load` **solo** opera sobre BDs de prueba (nombre con `test`), salvo
> `--allow-non-test-db`. La demo contra Docker **no** carga la semilla en la
> BD demo: los usuarios de la demo se crean por el **flujo real de la app**
> (registro + KYC mock). La semilla es la referencia de datos sinteticos y de
> las suites QA, no un login con password (el dataset no incluye passwords).

### 2.2 Cuentas y beneficiarios para el guion

- **Cuentas:** usar las creadas en el flujo real (al menos 2 del titular para
  la transferencia propia + 1 cuenta de tercero conocida para HU06/HU07).
  Montos siempre en **centimos** y cada envio con `Idempotency-Key`.
- **Beneficiarios:** dar de alta al tercero por la pantalla de beneficiarios
  antes de la transferencia a terceros (HU07 -> HU06).

### 2.3 Forzar un buro que **apruebe** (perfil `prime`)

El adaptador usa `CREDIT_BUREAU_PROVIDER=mock` por defecto (sin red). Perfiles
fijos en `backend/app/adapters/credit_bureau.py`:

| Perfil | Score | Deudas (centimos) | Morosidad |
|---|---|---|---|
| `prime` | 780 | 0 | no |
| `standard` | 650 | 50000 | no |
| `subprime` | 520 | 150000 | si |

El mock asigna perfil por hash del sujeto (`mock_profile_for(clave)`). Para
elegir un solicitante que caiga en `prime`, calcularlo (sin PII, el `sub` es
un UUID opaco):

```powershell
cd backend
.venv\Scripts\python.exe -c "from app.adapters.credit_bureau import mock_profile_for; subs=['<SUB-1>','<SUB-2>','<SUB-3>']; print([(s, mock_profile_for(s)) for s in subs])"
```

Usar como solicitante un `sub` cuyo perfil sea **`prime`** (score 780: aprueba
con los parametros por defecto). Generar su token con ese `sub` fijo:

```powershell
.venv\Scripts\python.exe scripts\mint_demo_token.py --subject <SUB-PRIME> 2>$null
```

### 2.4 Crear un expediente `EN_REVISION` para el panel

Si la bandeja sale vacia ("Sin expedientes por revisar"), crear uno con la
receta verificada de `admin-web/README.md#crear-un-expediente-en-revision`
(montos en centimos; con `loan.min_score=650` y `loan.review_margin=50`, el
score `650` cae en la banda `[650, 700)` => `202 IN_REVIEW`):

```powershell
# A) Elegir producto (token de analista). De aqui sale <PRODUCT_ID>.
$t = (Get-Content $env:TEMP\analyst.jwt -Raw).Trim()
$h = @{Authorization = "Bearer $t"}
(Invoke-WebRequest -UseBasicParsing `
  -Uri 'http://localhost:8000/api/v1/credits/products' `
  -Headers $h -TimeoutSec 10 | Select-Object -ExpandProperty Content | ConvertFrom-Json).data |
  Select-Object code, id, min_amount_minor, max_amount_minor

# B) Token del solicitante de prueba (perfil "standard" del buro mock, score 650).
$a = (.venv\Scripts\python.exe scripts\mint_demo_token.py `
  --subject 6513270e-269e-0d37-f2a7-4de452e6b438 2>$null).Trim()

# C) Solicitud con DTI holgado (deuda 100000 + 50000 del buro = 150000 sobre
#    ingreso 2000000 => DTI 0.075 <= 0.4) y monto dentro del producto.
$ha = @{Authorization = "Bearer $a"; 'Idempotency-Key' = "demo-inreview-001"}
$body = '{"product_id":"<PRODUCT_ID>","requested_amount_minor":500000,"term_months":12,"total_income_minor":2000000,"total_debt_minor":100000,"bureau_authorized":true}'
Invoke-WebRequest -UseBasicParsing -Method Post `
  -Uri 'http://localhost:8000/api/v1/credits/applications' `
  -Headers $ha -Body $body -ContentType 'application/json' -TimeoutSec 15
# Esperado: 202 con "status":"IN_REVIEW" (repetir con la misma Idempotency-Key
# no duplica: devuelve el mismo expediente con meta.replayed=true).

# D) Ver la bandeja con el token de analista => 200 con total >= 1.
Invoke-WebRequest -UseBasicParsing `
  -Uri 'http://localhost:8000/api/v1/credits/applications?status=IN_REVIEW' `
  -Headers $h -TimeoutSec 10 | Select-Object -ExpandProperty Content
```

Recargar el panel: el expediente aparece en `EN_REVISION` y ya se puede
dictaminar (`APPROVED`/`REJECTED` con motivo obligatorio).

---

## 3. Guion paso a paso por HU

> Prefijo API: `/api/v1`. Dinero solo en centimos + `Idempotency-Key`.
> Cada paso indica **lo que se debe ver**.

### HU06 — Transferencias (propia / tercero / interbancaria + comprobante)

Rutas reales: `POST /transfers/own`, `POST /transfers/third-party`
(+ `POST /transfers/third-party/resolve-target`), `POST /transfers/interbank`.

1. **Propia:** en la app, transferir entre dos cuentas del titular
   (`POST /transfers/own`, ruta atomica sin hold). **Ver:** debito y credito
   por el mismo monto, saldos actualizados, comprobante con referencia.
2. **Tercero:** resolver el destino (`resolve-target`, titular enmascarado),
   transferir a la cuenta del beneficiario dado de alta en HU07
   (`POST /transfers/third-party`, hold + liquidacion, con comision).
   **Ver:** hold creado y liquidado, comision aplicada, comprobante.
3. **Interbancaria:** enviar a otro banco (`POST /transfers/interbank`).
   **Ver:** estado de envio/confirmacion y comprobante con referencia.
4. **Idempotencia (mostrar 30 s):** reenviar una operacion con la misma
   `Idempotency-Key`. **Ver:** misma respuesta con `meta.replayed=true`, sin
   doble cargo.

### HU07 — Beneficiarios

Rutas reales: `GET/POST/PUT/DELETE /beneficiaries`.

1. **Alta** (`POST /beneficiaries`, `Idempotency-Key` respetada aunque opcional).
   **Ver:** beneficiario en la lista (< 1 s, busqueda `q` disponible).
2. **Busqueda** (`GET /beneficiaries?q=...`). **Ver:** coincide por nombre/
   cuenta enmascarada (titular/datos sensibles cifrados + indice ciego).
3. **Edicion** (`PUT /beneficiaries/{id}`). **Ver:** cambios reflejados.
4. **Baja** (`DELETE /beneficiaries/{id}?confirm=true`, soft-delete).
   **Ver:** desaparece de la lista activa.

### HU08 — Reto biometrico / PIN y limites

1. **Login biometrico del dispositivo** (la biometria solo desbloquea/firma en
   el dispositivo; el servidor verifica sesion). **Ver:** sesion iniciada sin
   password.
2. **Pago sensible con biometria o fallback PIN.** **Ver:** operacion autorizada
   con un solo factor (`signature` **o** `pin`, nunca ambos).
3. **Limite diario:** intentar superar el tope. **Ver:** rechazo de negocio
   claro (monto/limite en centimos), sin cargo parcial.

### HU09 — Simulador

Rutas reales: `POST /credits/simulate` (+ `POST /credits/simulate/export`).

1. Elegir producto (`GET /credits/products`) y simular monto/plazo
   (`POST /credits/simulate`). **Ver:** cuota + cronograma provisionales.
2. Exportar (`POST /credits/simulate/export`). **Ver:** PDF de simulacion
   descargado (provisional, no es el contrato).

### HU10 — Solicitud + dictamen + analista (panel)

Rutas reales: `POST /credits/applications`, `GET /credits/applications`
(+ `/{id}` solo titular), `POST .../review` (rol `analyst`/`credit_analyst`).

1. **Solicitud-prime (aprueba):** con el `sub` perfil `prime` (§2.3), pedir el
   credito (`bureau_authorized=true`, `Idempotency-Key` obligatoria).
   **Ver:** `201/200` de aprobacion directa (score 780).
2. **Solicitud-standard (revisión):** receta del §2.4 (`sub` `6513270e-...`,
   score 650). **Ver:** `202` con `"status":"IN_REVIEW"`.
3. **Panel (http://localhost:8080):** pegar el JWT de analista (§1.5).
   **Ver:** bandeja `EN_REVISION` con el expediente (datos enmascarados
   `••••`, riesgo score/DTI del servidor, banner de consentimiento auditado).
4. **Dictamen:** `APPROVED` o `REJECTED` con **motivo obligatorio** (sin motivo
   no se envia; el servidor valida `422`). **Ver:** sale de la bandeja, queda
   auditado. `409` (ya decidido) => avisar y refrescar; `403` (sin rol) =>
   pantalla denegada.

### HU11 — Contrato + firma + copia por correo

Rutas reales: `POST/GET .../contract`, `POST .../sign/challenge`,
`POST .../sign` (un factor + `Idempotency-Key` obligatoria).

1. **Contrato:** generar/obtener el juego contractual del expediente aprobado
   (`POST/GET .../contract`). **Ver:** metadatos + PDF del contrato.
2. **Reto:** emitir el reto de firma (`POST .../sign/challenge`, opcional
   `device_id`). **Ver:** `nonce` vigente ligado al dispositivo.
3. **Firma biometrica:** firmar con `{device_id, signature[, nonce]}`.
   **Ver:** estado **Firmado**, sin re-firma posible.
4. **Fallback PIN:** en otro expediente aprobado, firmar con `{pin}`.
   **Ver:** mismo estado Firmado (el `pin` viaja como texto libre solo en el
   request; jamas se persiste: solo digests HMAC).
5. **Copia por correo:** tras la firma exitosa el servidor envia la copia
   certificada **best-effort** (plantilla de `notifications`, canal `email`;
   `EMAIL_PROVIDER=mock` por defecto, `gmail` real opcional).
   **Ver:** firma OK aunque el correo falle (el fallo no bloquea la firma;
   queda en log como `copy_failed`, nunca con PII).

### E3-T23 — "Mis creditos"

1. Abrir la pantalla **Creditos** (pestanas). **Ver:** listado de **mis**
   solicitudes (`GET /credits/applications` sin `status`: solo las del titular).
2. Abrir una solicitud **firmada**. **Ver:** estado **Firmado**, accion de firma
   **deshabilitada** (no re-firmable) y opcion **volver al inicio**.
3. Volver al inicio y reintentar firmar (si la UI lo permitiera).
   **Ver:** el servidor lo rechaza (ya firmado), sin duplicar firma ni correo.

---

## 4. Checklist de verificacion (pasar antes de presentar)

- [ ] `docker compose ps`: 5/5 `healthy`.
- [ ] `GET /health/ready` responde 200.
- [ ] APK con `API_BASE_URL` correcta (emulador `10.0.2.2`, fisico IP LAN,
      iOS `127.0.0.1`).
- [ ] KYC: mock por defecto (o perfil `kyc` arriba si se usa el servicio real).
- [ ] `seed_demo.py check` OK (semilla reproducible, dataset valido).
- [ ] Token de analista vigente (< 15 min o reemitido) y panel `:8080` con sesion.
- [ ] Buro mock en `success` (default) + `sub` prime localizado (§2.3).
- [ ] HU06: propia + tercero + interbancaria con comprobante; replay con
      `meta.replayed=true` sin doble cargo.
- [ ] HU07: alta, busqueda, edicion y baja de beneficiario.
- [ ] HU08: biometria/PIN OK + limite diario rechaza.
- [ ] HU09: simulacion + export PDF.
- [ ] HU10: solicitud prime aprueba, standard queda `IN_REVIEW`, analista
      dictamina en el panel y sale de la bandeja.
- [ ] HU11: contrato + firma (biometria y PIN) + copia por correo best-effort.
- [ ] E3-T23: listado propio, firmado no re-firmable, volver al inicio.

---

## 5. Plan B (si algo falla en vivo)

| Falla | Sintoma | Plan B |
|---|---|---|
| Buro caido (`down`) | `503` / `BUREAU_UNAVAILABLE` | Verificar `CREDIT_BUREAU_PROVIDER=mock` y `CREDIT_BUREAU_MOCK_MODE=success` (defaults, sin red); reintentar. Si persiste, mostrar el expediente `IN_REVIEW` ya sembrado (§2.4) y seguir con el panel. |
| Buro timeout | `502` / `BUREAU_TIMEOUT` | Reintento con backoff (el adaptador ya reintenta `CREDIT_BUREAU_MAX_RETRIES=2`); si persiste, mismo plan B que caida. |
| Correo real falla | Firma OK pero sin correo | Esperado: la copia es **best-effort** y no bloquea la firma. Mostrar el log `copy_queued`/`copy_failed` (sin PII) y seguir; con `EMAIL_PROVIDER=mock` la demo no depende de Gmail. |
| Token expirado | `401` en el panel/API | Reemitir (`mint_demo_token.py`, `--minutes 60` para la sesion de demo) y volver a pegar. |
| `403` en bandeja | Pantalla denegada | El JWT no tiene rol: reemitir con `--role analyst` o `--role credit_analyst`. |
| `409` al dictaminar/firmar | Ya decidido / ya firmado | Avisar y refrescar: es el comportamiento correcto (no duplicar). Tener un segundo expediente sembrado (§2.4 con otra `Idempotency-Key`). |
| Bandeja vacia | "Sin expedientes por revisar" | Crear uno con la receta del §2.4 (3 min). |
| Panel `:8080` caido | No abre | `docker compose build admin-web` + `up -d --force-recreate admin-web`; fallback: mostrar la bandeja por API (`GET /credits/applications?status=IN_REVIEW` con el JWT). |
| Backend version skew | Errores raros tras pull | `docker compose build backend worker` + `up -d`, revalidar `/health/ready`. |
| Sin red para el movil | APK no alcanza al backend | Cambiar `API_BASE_URL` a la IP LAN del host (mismo Wi-Fi) y confirmar `0.0.0.0:8000`. |

---

## 6. Cierre

Al terminar la demo: cerrar sesion del panel (el JWT vive solo en
`sessionStorage`), descartar tokens temporales (`$env:TEMP\analyst.jwt`) y no
guardar ningun dato personal real. La semilla y los `sub` de este guion son
sinteticos y reutilizables para la proxima demo o el video de respaldo
(`docs/15#8`: guion + video).
