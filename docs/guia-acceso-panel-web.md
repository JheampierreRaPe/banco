# Guía de acceso al panel web del analista de crédito (`admin-web`)

Runbook paso a paso para entrar al **panel interno** como **analista de crédito**:
bandeja de expedientes `IN_REVIEW`, detalle enmascarado y dictamen manual
(`Aprobar`/`Rechazar` con motivo obligatorio), de punta a punta en local o desde
otra máquina de la LAN.

## 1. Qué es

`admin-web` es el panel interno del proyecto (Vite + React + TypeScript + TanStack
Query; ver `admin-web/README.md`). La vista de **Analista de crédito** se
implementó en `docs/tasks/E3-T10.md` y permite:

- **Revisar solicitudes** en estado `IN_REVIEW` (bandeja con filtro fijo).
- **Ver el detalle enmascarado** de una solicitud (datos personales/cuentas
  `••••` + cola; jamás UUID/documento en claro).
- **Dictaminar** con motivo **obligatorio**: `Aprobar` (`APPROVED`) o `Rechazar`
  (`REJECTED`). Al decidir, el expediente sale de la bandeja y la acción queda
  auditada.

Referencias: `docs/tasks/E3-T10.md`, `admin-web/README.md` y
`docs/design/creditos-pantallas.md` (§4, pantalla "Analista").

## 2. Requisitos previos

Necesitas el stack Docker levantado y **`healthy`** con sus 5 servicios:
`postgres`, `redis`, `backend`, `worker` y `admin-web`.

Desde la **raíz** del repo:

```powershell
# Ver el estado (los 5 servicios deben figurar "healthy")
docker compose ps

# Si falta alguno o no está arriba, levantar todo (build incluido):
docker compose up -d
```

- Backend escuchando en `http://localhost:8000`.
- Panel escuchando en `http://localhost:8080`.

## 3. Cómo obtener el token de analista

Los roles viven en el claim `roles` del JWT; **no se persisten en la base de
datos**. Los logins normales emiten `roles` vacío, así que para probar la vista
de crédito se usa el script local `backend/scripts/mint_demo_token.py`.

Desde `backend/`:

```powershell
.venv\Scripts\python.exe scripts\mint_demo_token.py
```

- Imprime **solo** un JWT con `roles=["analyst"]` por stdout; el diagnóstico
  (`sub`/`roles`/vigencia/fuente del secreto) va por stderr.
- **Vigencia por defecto: 15 minutos.**
- Opciones: `--role credit_analyst`, `--subject <uuid>`, `--minutes <n>`.
- Se firma con el **mismo secreto** del backend (el `JWT_SECRET` del `.env` de la
  raíz, el mismo que inyecta `docker compose`). Para apuntar contra Docker, **no**
  exportes `JWT_SECRET` y deja que lea el `.env` de la raíz.
- **No hay backdoors:** es un script local/dev que firma localmente y no crea ni
  toca endpoints (pruebas en `backend/tests/test_mint_demo_token.py`).

> Si necesitas el token en un archivo temporal (nunca en el repo):
> `.venv\Scripts\python.exe scripts\mint_demo_token.py > $env:TEMP\analyst.jwt`

## 4. Cómo abrir el panel

- **En la misma máquina:** abre `http://localhost:8080`.
- **Desde otra máquina de la red:** abre `http://<IP-LAN-del-host>:8080`.

El panel llama al API por el **mismo origen**: el navegador pega a `/api/v1` en el
host del panel y **nginx** lo proxea a `backend:8000` (ver `admin-web/nginx.conf`).
Por eso funciona igual desde cualquier host/IP (fix de conectividad reciente): no
hay que hardcodear la URL del backend en el cliente.

En **desarrollo** también puedes correr el panel con Vite (proxy `/api` al backend
local):

```powershell
cd admin-web
npm run dev        # http://localhost:5173
```

## 5. Cómo entrar

1. Abre el panel (§4).
2. En la **pantalla de sesión**, pega el JWT del paso 3.
3. El token se guarda **solo en memoria/`sessionStorage`**; **nunca** va al repo,
   a archivos ni a logs.

El panel solo lee `sub` + `roles` del token para el gate de UI; **quien decide es
el servidor**. Sin rol `analyst`/`credit_analyst` verás la pantalla **denegada**
(equivale al `403 NOT_AUTHORIZED`).

## 6. Qué se ve

- **Bandeja `IN_REVIEW`**: una fila (enmascarada) por expediente a revisar.
- **Detalle**: riesgo (score/DTI) provisto por el **servidor** (el panel no
  recalcula nada) y un **banner de consentimiento** auditado.
- **Dictamen**: formulario con **motivo obligatorio**; sin motivo no se envía. Al
  confirmar `Aprobar`/`Rechazar`, el expediente sale de la bandeja y la acción
  queda auditada.

Estados de UI por pantalla: contenido, carga (skeleton), vacío ("Sin expedientes
por revisar") y error accionable con **Reintentar**.

La bandeja trae un botón **Refrescar** y se actualiza sola cada 30 s (mientras la
última carga haya sido buena), para ver solicitudes nuevas sin recargar la página.
El backend no expone un total real para la bandeja, así que el contador sale como
**"50+"** cuando la última página vino llena y **"Cargar más"** pagina por `offset`.
Puedes **Cerrar sesión** desde la barra lateral. Las secciones aún no
implementadas ("Aprobados hoy", "Rechazados", "Auditoría") salen
**deshabilitadas** con la etiqueta "próximamente".

## 7. Cómo generar un expediente en revisión

Si la bandeja está vacía, crea uno con datos sintéticos (sin PII). Los pasos
están en `admin-web/README.md` (§ "Crear un expediente en revisión"); con los
parámetros por defecto (`loan.min_score=650`, `loan.review_margin=50`) un score
de `650` cae en la banda de revisión `[650, 700)` → `202 IN_REVIEW`.

Pasos, desde `backend/`:

```powershell
# A) Elegir un producto (usa el token de analista). De aquí sale <PRODUCT_ID>.
$t = (Get-Content $env:TEMP\analyst.jwt -Raw).Trim()
$h = @{Authorization = "Bearer $t"}
(Invoke-WebRequest -UseBasicParsing `
  -Uri 'http://localhost:8000/api/v1/credits/products' `
  -Headers $h -TimeoutSec 10 | Select-Object -ExpandProperty Content | ConvertFrom-Json).data |
  Select-Object code, id, min_amount_minor, max_amount_minor

# B) Token del solicitante de prueba (perfil "standard" del buró mock, score 650).
$a = (.venv\Scripts\python.exe scripts\mint_demo_token.py `
  --subject 6513270e-269e-0d37-f2a7-4de452e6b438 2>$null).Trim()

# C) Crear la solicitud (monto en céntimos; DTI holgado ≤ 0.4).
#    Reemplaza <PRODUCT_ID> por el id del paso A. Idempotency-Key obligatoria.
$ha = @{Authorization = "Bearer $a"; 'Idempotency-Key' = "demo-inreview-001"}
$body = '{"product_id":"<PRODUCT_ID>","requested_amount_minor":500000,"term_months":12,"total_income_minor":2000000,"total_debt_minor":100000,"bureau_authorized":true}'
Invoke-WebRequest -UseBasicParsing -Method Post `
  -Uri 'http://localhost:8000/api/v1/credits/applications' `
  -Headers $ha -Body $body -ContentType 'application/json' -TimeoutSec 15
# Esperado: 202 con "status":"IN_REVIEW".

# D) Ver la bandeja con el token de analista → 200 con total ≥ 1.
Invoke-WebRequest -UseBasicParsing `
  -Uri 'http://localhost:8000/api/v1/credits/applications?status=IN_REVIEW' `
  -Headers $h -TimeoutSec 10 | Select-Object -ExpandProperty Content
```

Recarga el panel: el expediente aparece en la bandeja y ya puedes dictaminar.

## 8. Solución de problemas

- **"No se pudo conectar con el servidor"**: verifica que el `backend` esté
  `healthy` (`docker compose ps`). El panel debe abrirse por el host del panel
  (`:8080`) y el API va por el **mismo origen**; si abres el panel por IP-LAN,
  funciona igual tras el fix. Si personalizas `VITE_API_BASE_URL`, recuerda que
  debe ser **relativo** (`/api/v1`), no una URL absoluta.
- **Token expirado** (dura 15 min): si el panel ya cargó y el token vence, la
  bandeja muestra **"Tu sesión expiró. Vuelve a ingresar."** con el botón
  **"Volver a ingresar"** (cierra la sesión y regresa a la pantalla de login).
  Reemite el token con el script del paso 3 (o usa `--minutes <n>` para más
  tiempo) y vuelve a pegarlo.
- **Acceso denegado**: el token no tiene rol de analista; emítelo con
  `--role analyst` o `--role credit_analyst`.
- **El panel en `:8080` no responde**: reconstruye y recrea solo ese servicio.

  ```powershell
  docker compose build admin-web
  docker compose up -d --force-recreate admin-web
  ```

## 9. Referencias

- `admin-web/README.md` — panel, tokens de diseño y comando de creación de
  expedientes sintéticos.
- `docs/tasks/E3-T10.md` — brief y reporte de la vista de Analista de crédito.
- `docs/design/creditos-pantallas.md` — §4, especificación de la pantalla
  "Analista".
