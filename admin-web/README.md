# admin-web (panel interno)

Panel web para roles internos: operaciones, fraude, cumplimiento, credito, administracion de
seguridad y auditoria (solo lectura).

- Stack: Vite + React + TypeScript, TanStack Query, CSS con tokens "Eucalipto y Ocre"
  (`docs/20-diseno-ui.md`, solo modo claro). Sin MUI/Ant: la vista de crédito usa los
  tokens directos (bootstrap mínimo E3-T10, gap F-T12 ver `docs/18` G52).
- Autenticacion y RBAC por rol (matriz de accesos, `docs/13#2.2`): sesión separada de la
  app móvil; el analista pega su JWT (`Authorization: Bearer`) y el panel solo lee `sub`
  + `roles` para el gate de UI (la verificación es del servidor). Sin rol
  `analyst`/`credit_analyst` → pantalla denegada (equivale al `403 NOT_AUTHORIZED`).
- Datos personales y cuentas enmascarados por defecto (`••••` + cola; jamás UUID/documento
  en claro); toda accion queda auditada (tira visible usuario/hora/antes-después).
- No edita asientos contables; los ajustes son asientos nuevos autorizados.
- Sin PII ni tokens en logs ni en el repo (solo `VITE_*` no secreta en `.env.example`).

Se crea en la tarea `F-T12` (`docs/13-frontend-flutter-panel.md`); la vista de Analista de
crédito vive desde `E3-T10`.

## Vista de Analista de crédito (E3-T10, HU10)

Bandeja `EN_REVISION` + detalle enmascarado + dictamen manual, según
`docs/design/creditos-pantallas.md` (pantalla "Analista", desktop 1280px).

- `GET /credits/applications?status=IN_REVIEW` → bandeja (filtro fijo).
- Detalle construido con la fila de la bandeja: `GET /credits/applications/{id}` es solo
  del titular (ajeno → `404` neutro), así que no hay segundo fetch con token de analista
  (gap `docs/18` G52). Riesgo (score/DTI) y consentimiento buró (banner auditado) son
  datos del servidor; el panel no recalcula nada (cliente delgado).
- `POST /credits/applications/{id}/review` `{decision: APPROVED|REJECTED, reason 1..2000}`
  con motivo obligatorio (sin motivo no se envía; el servidor valida `422`). Tras
  dictaminar sale de la bandeja y queda auditado. `409` (ya decidido) → avisa y refresca;
  `403` (sin rol) → pantalla denegada.
- 4 estados de UI por pantalla (`docs/20#7`): contenido, carga (skeleton), vacío
  ("Sin expedientes por revisar"), error accionable + Reintentar.

## Levantar y probar

```bash
# Desarrollo (proxy /api → backend local :8000)
cd admin-web
npm install
npm run dev        # http://localhost:5173

# Producción demo (panel en http://localhost:8080)
docker compose up --build -d   # desde la raíz; servicio `admin-web`

# Calidad del panel
cd admin-web
npm test         # vitest: 14 pruebas (feliz, errores, enmascaramiento, 4 estados)
npm run build    # tsc + build limpio (tsc es el lint del panel)
```

Variables: `VITE_API_BASE_URL` (defecto `http://localhost:8000/api/v1`; ver `.env.example`).
El JWT se pega en la pantalla de sesión (nunca en archivos ni logs).

## Token de analista (solo desarrollo/demo)

El panel solo lee `sub` + `roles` del JWT que pegas en la pantalla de sesión;
el que decide es el backend (`GET /credits/applications?status=IN_REVIEW` exige
rol `analyst`/`credit_analyst`, si no `403`). Los logins normales emiten `roles`
vacío, así que para probar la vista de crédito se usa el script local
`backend/scripts/mint_demo_token.py` (firma con el `JWT_SECRET` vigente; por
defecto lee el `.env` de la raíz, el mismo que `docker compose` inyecta al
backend). Solo emite `analyst`/`credit_analyst`: no es un backdoor (no crea ni
toca endpoints) y el secreto jamás se imprime. Pruebas en
`backend/tests/test_mint_demo_token.py`.

```powershell
# 1) Stack arriba (desde la raíz): backend :8000 + panel :8080
docker compose up --build -d

# 2) Emitir el token (desde backend/): imprime SOLO el JWT por stdout
cd backend
.venv\Scripts\python.exe scripts\mint_demo_token.py > $env:TEMP\analyst.jwt
# Variantes: --role credit_analyst | --subject <uuid> | --minutes 60
# Diagnóstico (sub/roles/vigencia/fuente del secreto) sale por stderr.

# 3) Pegarlo en el panel
# Abre http://localhost:8080 → pantalla de sesión → pega el contenido de
# $env:TEMP\analyst.jwt en el campo JWT (se envía como Authorization: Bearer).
# Sin rol analyst/credit_analyst verás la pantalla denegada (equivale al 403).
```

El token dura 15 minutos por defecto (`--minutes` para más). Cada ejecución sin
`--subject` genera un `sub` UUID aleatorio: úsalo distinto al del solicitante
(si el analista dictamina su propio expediente, el servidor responde `409`).

## Crear un expediente en revisión (datos sintéticos)

Si la bandeja sale vacía ("Sin expedientes por revisar"), crea uno con el flujo
real (montos en céntimos, sin datos personales). Con los parámetros por defecto
(`loan.min_score=650`, `loan.review_margin=50`) el score `650` cae en la banda
de revisión `[650, 700)` → `202 IN_REVIEW`:

```powershell
# A) Elegir producto (usa el token de analista)
$t = (Get-Content $env:TEMP\analyst.jwt -Raw).Trim()
$h = @{Authorization = "Bearer $t"}
(Invoke-WebRequest -UseBasicParsing `
  -Uri 'http://localhost:8000/api/v1/credits/products' `
  -Headers $h -TimeoutSec 10 | Select-Object -ExpandProperty Content | ConvertFrom-Json).data |
  Select-Object code, id, min_amount_minor, max_amount_minor

# B) Token del solicitante de prueba (sub en el perfil "standard" del buró mock,
#    score 650; búscalo así si quieres otro: el hash es determinista por sub)
$a = (.venv\Scripts\python.exe scripts\mint_demo_token.py `
  --subject 6513270e-269e-0d37-f2a7-4de452e6b438 2>$null).Trim()

# C) Solicitud con DTI holgado (deuda efectiva 100000 + 50000 del buró = 150000
#    sobre ingreso 2000000 → DTI 0.075 ≤ 0.4) y monto dentro del producto
$ha = @{Authorization = "Bearer $a"; 'Idempotency-Key' = "demo-inreview-001"}
$body = '{"product_id":"<PRODUCT_ID>","requested_amount_minor":500000,"term_months":12,"total_income_minor":2000000,"total_debt_minor":100000,"bureau_authorized":true}'
Invoke-WebRequest -UseBasicParsing -Method Post `
  -Uri 'http://localhost:8000/api/v1/credits/applications' `
  -Headers $ha -Body $body -ContentType 'application/json' -TimeoutSec 15
# Esperado: 202 con "status":"IN_REVIEW" (repetir con la misma Idempotency-Key
# no duplica: devuelve el mismo expediente con meta.replayed=true).

# D) Ver la bandeja con el token de analista → 200 con total ≥ 1
Invoke-WebRequest -UseBasicParsing `
  -Uri 'http://localhost:8000/api/v1/credits/applications?status=IN_REVIEW' `
  -Headers $h -TimeoutSec 10 | Select-Object -ExpandProperty Content
```

Recarga el panel: el expediente aparece en `EN_REVISION` y ya puedes dictaminar
(`APPROVED`/`REJECTED` con motivo obligatorio; tras dictaminar sale de la
bandeja).
