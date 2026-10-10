# Especificación de contenido — Pantallas de Créditos (HU09 / HU10)

> Mockups en `pantallas.fig` (páginas `Créditos/Simulador`, `Créditos/Solicitud`,
> `Créditos/Estado`, `Créditos/Analista`). Imágenes: `docs/design/creditos-*.png`
> (más `.svg` vectoriales homónimos). Sistema de diseño: `docs/20-diseno-ui.md`
> (tokens "Eucalipto y Ocre", solo modo claro). Sin PII real en los mockups
> (datos de ejemplo enmascarados).
>
> Nota de layout: en el mockup del simulador la tarjeta de cronograma quedó al
> final por una limitación del reemplazo de nodo; al implementar, el orden
> recomendado es: producto → monto → plazo → desgravamen → cuota → cronograma →
> nota → CTAs.

## Convenciones comunes (las 4 pantallas)

- TopBar `primary #152A1F`, 64px; título 16/600 blanco; botón atrás `‹`.
- Fondo `surface #FBF9F4`; cards blancas radio 16, borde `outline-variant #C2C8C2`;
  CTA primario `primary`, 52px, radio 12, texto 16/600 blanco; CTA secundario
  borde 1.5px `primary`, texto `primary`.
- Montos formato `S/ 9,999.99` (separador de miles, 2 decimales), provistos por
  el backend; el cliente **no calcula ni redondea** (cliente delgado, `docs/19`).
- 4 estados obligatorios por pantalla (§7 `docs/20`): contenido, cargando
  (skeleton), vacío, error (mensaje en español accionable + Reintentar).
- Textos de ejemplo del canvas son placeholders cortos; los textos legales
  completos están abajo (el canvas corta líneas largas en el export).

---

## 1. Simulador de crédito (HU09 · E3-T04)

Mockup: `docs/design/creditos-simulador.png` · Fig página `Créditos/Simulador`
(frame raíz `0:6490`).

### Contenido exacto

1. **Header**: título `Simula tu crédito` (24/600 `primary`); subtítulo
   `El cálculo lo hace el servidor. Sin compromiso.` (14, `secondary-text`).
2. **Card Producto** (chips seleccionables, default: primero):
   - Uno por producto de `GET /credits/products`: nombre + TEA
     (ej. `Personal 18.5%`, `Vehicular 12.9%`, `Pyme 22.0%`).
   - Nota: `TEA referencial del producto elegido.`
3. **Card Monto**: label `Monto: S/ 10,000` (dinámico con el valor actual);
   slider con rango del producto (ej. `S/ 1,000` – `S/ 50,000`);
   default `S/ 10,000`. Valor fuera de rango → error `422` del backend.
4. **Card Plazo**: label `Plazo: 12 meses`; chips `6 · 12 · 24 · 36`
   (default `12`); el set real de plazos lo manda `GET /credits/products`.
5. **Card Seguro de desgravamen**: toggle (default OFF). Texto:
   título `Seguro de desgravamen`, subtítulo `Recalcula la cuota. Opcional.`
   Al cambiar → re-simula con `insurance: true|false`.
6. **Card Cuota** (fondo `secondary-container #FDAA47`):
   - Label `CUOTA MENSUAL (del servidor)` (12/600 `on-secondary-container`).
   - Valor grande `S/ 927.50` (36/bold `on-secondary-fixed #2C1700`).
   - Línea `TEA 18.50% • TCEA 19.80% • Francesa` (valores del servidor).
7. **Card Cronograma resumido** (tabla, columnas `N · Capital · Interés · Saldo`,
   primeras 3 filas + link `Ver tabla completa (12 cuotas)` en `accent-text`):
   | N | Capital | Interés | Saldo |
   |---|---|---|---|
   | 1 | 762.10 | 165.40 | 9,237.90 |
   | 2 | 774.71 | 152.79 | 8,463.19 |
   | 3 | 787.52 | 139.98 | 7,675.67 |
   (valores de ejemplo; los reales vienen del backend, amortización francesa).
8. **Nota**: `Simulación sin registro crediticio. Sin compromiso.`
9. **CTA primario**: `Solicitar este crédito` → va a Solicitud con
   producto/monto/plazo/insurance pre-cargados.
10. **CTA secundario**: `Descargar simulación (PDF)` → endpoint de export de
    `E3-T03`; si responde `501`, mensaje `Exportación no disponible por ahora.`

### Comportamiento / validaciones visibles

- Cada cambio de control re-simula contra `POST /credits/simulate` (con debounce);
  durante la petición, skeleton en la card de cuota.
- Sin `Idempotency-Key` (no mueve dinero). Cargando/vacío (sin productos)/error
  de red con `Reintentar`. Monto/plazo fuera de rango: mensaje del `422`.

### Datos del backend

`GET /credits/products` (productos, TEA, rangos, plazos) →
`POST /credits/simulate` `{product_id, amount, term_months, insurance}` →
`{monthly_payment, tea, tcea, method: "francesa", schedule[]}`.
Errores: `VALIDATION_ERROR (422)`, `NOT_FOUND`, `EXPORT_FORMAT_NOT_SUPPORTED (501)`.

---

## 2. Solicitud de crédito (HU10 · E3-T09)

Mockup: `docs/design/creditos-solicitud.png` · Fig página `Créditos/Solicitud`
(frame raíz `0:6574`).

### Contenido exacto

1. **Header**: `Tu solicitud` + `Revisamos tus datos y te damos un dictamen en minutos.`
2. **Card resumen** (fondo `surface-container-low`): label `VIENES DE SIMULAR`;
   valor `Personal • S/ 10,000 • 12 m • S/ 927.50/mes`;
   link `Ajustar simulación` (vuelve al simulador conservando valores).
3. **Card Datos de la solicitud** (labels persistentes 14/600 `primary`):
   - `Documento`: solo lectura, enmascarado, ej. `DNI •••• 4521 (verificado)`.
   - `Ingreso mensual neto (S/)`: input numérico, ej. `3,500.00`. Obligatorio, > 0.
   - `Cuenta de desembolso`: selector de cuentas propias enmascaradas,
     ej. `Ahorros •••• 4521`. Obligatorio.
   - `Monto solicitado (S/)`: input, default del simulador, ej. `10,000.00`.
   - `Plazo (meses)`: input/selector, ej. `12`.
   - Aviso inline (error `error #BA1A1A`):
     `Si falta un dato verás aquí el aviso antes de enviar.`
4. **Card Consentimiento** (borde `primary`, checkbox 24px, obligatorio `*`):
   - Título: `Autorizo la consulta en centrales de riesgo` + `*`.
   - Texto legal completo (en app, con wrap): *"Autorizo expresamente a la
     entidad a consultar mi historial en las centrales de riesgo para evaluar
     esta solicitud. Esta autorización queda registrada con fecha y hora."*
   - Subtítulo corto: `Obligatorio. Sin esta autorización no se puede enviar.`
5. **CTA** `Enviar solicitud` (bloqueado/atenudado hasta marcar el checkbox).
6. **Nota**: `Sin marcar el consentimiento el botón se muestra bloqueado.
   El envío usa clave de idempotencia (reintentos seguros).`
7. **Card error ejemplo** (fondo `error-container`): `Si el servidor responde
   422 se indica qué campo corregir. Si el buró falla, el caso pasa a revisión
   manual (no es un error).`

### Comportamiento / validaciones visibles

- Sin checkbox → CTA deshabilitado (además el backend valida y responde `422`
  si falta el consentimiento).
- Envío: `POST /credits/applications` con `ApiClient.postWithIdempotency`
  (misma `Idempotency-Key` ante reintentos).
- `422` → marca campos; `404/409` → mensajes accionables;
  `BUREAU_UNAVAILABLE` → informa `En revisión` (no error).

### Datos del backend

`POST /credits/applications`
`{product_id, amount, term_months, income, disburse_account_id, consent_bureau: true, insurance}`
→ `{application_id, status}`. Nada de DTI/score/ elegibilidad se calcula en el
cliente.

---

## 3. Estado del expediente (HU10 · E3-T09)

Mockup: `docs/design/creditos-estado.png` · Fig página `Créditos/Estado`
(frame raíz `0:6627`).

### Contenido exacto

1. **Fila expediente**: `EXPEDIENTE CR-2026-0841` + badge de estado (pill):
   - `Evaluando` (gris `surface-container-highest` / `on-surface-variant`).
   - `En revisión` (fondo `warning-container #FFDCBC`, texto
     `on-warning-container #683D00`).
   - `Aprobado` (fondo `success-container #D0E8D7`, texto `#0B1F15`).
   - `Rechazado` (fondo `error-container #FFDAD6`, texto `#93000A`).
2. **Hero dictamen** (color según estado; ejemplo aprobado en verde):
   label `DICTAMEN DEL SERVIDOR`; título por estado:
   - Aprobado: `¡Crédito aprobado!` + `S/ 10,000 • 12 cuotas de S/ 927.50 • TCEA 19.80%`.
   - En revisión: `Estamos revisando tu solicitud.` +
     `Un analista la verificará. Te avisaremos.` (sin prometer aprobación).
   - Rechazado: `No pudimos aprobarlo esta vez.` + motivo del servidor.
   - Evaluando: `Evaluando tu solicitud…` + `Pre-calificación en curso (máx. 5 min).`
3. **Card Motivo / justificación**: texto **exacto** del `decision_reason` del
   backend, sin editar (ej. `Score y capacidad de pago dentro de política.`).
4. **Card Progreso** (pasos): `Solicitud enviada → Evaluación automática →
   Decisión lista → Firma y desembolso (siguiente)`.
5. **CTA por estado**:
   - Aprobado: primario `Continuar a la firma` + secundario `Ver cronograma`.
   - En revisión / Evaluando: solo `Volver al simulador` + pull-to-refresh.
   - Rechazado: primario `Simular otro monto` + secundario `Volver al inicio`.
6. **Nota**: `Cargando (skeleton), vacío (sin expediente) y error con Reintentar.
   Desliza para actualizar.`

### Datos del backend

`GET /credits/applications/{id}` → `{id, status ∈ DRAFT|SUBMITTED|IN_EVALUATION|
APPROVED|REJECTED|IN_REVIEW|EXPIRED, decision, decision_reason, amounts}`.
La UI muestra el dictamen tal cual; no interpreta ni recalcula.

---

## 4. Panel del analista (HU10 · E3-T10, `admin-web`)

Mockup: `docs/design/creditos-analista.png` (desktop 1280) · Fig página
`Créditos/Analista` (frame raíz `0:6669`).

### Contenido exacto

1. **Sidebar** (`primary`, 220px): `Panel Créditos`; nav
   `Bandeja IN_REVIEW (4)` (activa, ocre) · `Aprobados hoy` · `Rechazados` ·
   `Auditoría`; pie `Rol: Analista de crédito` (solo ese rol accede; sin rol →
   `403` / pantalla denegada).
2. **Bandeja** (fondo `surface-container-low`): título `En revisión manual`;
   filtro fijo `status = IN_REVIEW`; aviso `Datos enmascarados.`
   Filas (cards blancas, min 64px, divisor `divider`): la seleccionada con borde
   `primary`. Cada fila: `CR-2026-0841 - Personal S/ 10,000`;
   `Solicitante J••• P••• - DNI ••••4521 - hace 12 min`;
   pill `IN_REVIEW - DTI 38%`. (Nunca documento/cuenta en claro.)
3. **Detalle** (ej. `Detalle CR-2026-0841 (enmascarado)`):
   - Card `SOLICITUD`: `Personal - S/ 10,000 - 12 m`, `Cuenta •••• 4521`.
   - Card `RIESGO (servidor)`: `Score 720 - DTI 38% - Buró: sin mora`.
   - Banner consentimiento (verde): `Consentimiento buró: SÍ - 2026-10-08 10:24 (auditado)`.
4. **Dictamen manual**: label `Dictamen manual (motivo obligatorio *)`;
   textarea (ej. `Verificado sustento de ingresos. DTI dentro de política…`);
   error inline `Sin motivo no se puede enviar (el servidor valida).`
   Botones: `Aprobar` (fondo `success #2F6B4A`) y `Rechazar`
   (borde `error`, texto `error`).
5. **Nota**: `Tras dictaminar sale de la bandeja y queda auditado
   (usuario, hora, antes/después). 409 si ya fue decidido.`
6. Estados: vacío (`Sin expedientes por revisar`), cargando (skeleton de filas),
   error con `Reintentar`.

### Datos del backend

`GET /credits/applications?status=IN_REVIEW` (bandeja) →
`GET /credits/applications/{id}` (detalle enmascarado) →
`POST /credits/applications/{id}/review` `{decision: APPROVED|REJECTED, reason}`
(motivo obligatorio). Errores: `VALIDATION_ERROR`, `NOT_FOUND`, `CONFLICT (409)`,
`NOT_AUTHORIZED (403)`. El panel no recalcula DTI/score; solo muestra y envía.

---

## Chequeo contra `docs/01` (contradicciones)

Ninguna: HU09 (Should, 5 SP, Sprint 2) y HU10 (Must, 8 SP, Sprint 2) figuran en
alcance (`docs/01-hallazgos-y-decisiones.md`); consentimiento explícito,
dictamen `APROBADO/RECHAZADO/EN_REVISION` y aprobación separada coinciden con
`docs/08`. No se introduce lógica nueva en el diseño.

## Archivos

| Archivo | Uso |
|---|---|
| `docs/design/creditos-simulador.png` (+ `.svg`) | Mockup HU09 |
| `docs/design/creditos-solicitud.png` (+ `.svg`) | Mockup HU10 app |
| `docs/design/creditos-estado.png` (+ `.svg`) | Mockup HU10 estado |
| `docs/design/creditos-analista.png` (+ `.svg`) | Mockup HU10 panel |
| `docs/design/creditos-pantallas.md` (este archivo) | Spec de contenido |
