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
