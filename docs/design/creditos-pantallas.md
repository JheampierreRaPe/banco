# Especificación de contenido — Pantallas de Créditos (HU09 / HU10 / HU11)

> Mockups en `pantallas.fig` (páginas `Créditos/Simulador`, `Créditos/Solicitud`,
> `Créditos/Estado`, `Créditos/Analista`, `Créditos/Contrato`, `Créditos/Firma`,
> `Créditos/Mis créditos`). Imágenes: `docs/design/creditos-*.png`
> (más `.svg` vectoriales homónimos). Sistema de diseño: `docs/20-diseno-ui.md`
> (tokens "Eucalipto y Ocre", solo modo claro). Sin PII real en los mockups
> (datos de ejemplo enmascarados).
>
> Nota de layout: en el mockup del simulador la tarjeta de cronograma quedó al
> final por una limitación del reemplazo de nodo; al implementar, el orden
> recomendado es: producto → monto → plazo → desgravamen → cuota → cronograma →
> nota → CTAs.
>
> Nota E3-T23: la pantalla `/credits` tiene pestañas/segmentos arriba
> (`Simulador` | `Mis créditos`, decisión del dueño). El mockup del simulador
> muestra la pestaña `Simulador` activa; el mockup `Créditos/Mis créditos`
> muestra la pestaña `Mis créditos` activa con el listado.

## Convenciones comunes (las 6 pantallas)

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

## 5. Contrato de crédito (HU11 · E3-T15)

Mockup: `docs/design/creditos-contrato.png` (+ `.svg`) · Fig página
`Créditos/Contrato` (página `0:6716`, frame raíz `0:6717`).

> Nota: página `0:6716`, frame raíz `Creditos-Contrato` `0:6717`. Los textos del
> canvas son placeholders cortos; los textos legales completos están abajo.

### Contenido exacto

1. **Header**: TopBar `Contrato de crédito` (mismo patrón que las otras créditos).
2. **Fila expediente**: `EXPEDIENTE CR-2026-0841` + badge `Aprobado`
   (fondo `success-container #D0E8D7`, texto `#0B1F15`).
3. **Hero condiciones** (fondo `surface-container-low`): label
   `CONDICIONES APROBADAS (servidor)`; valor `S/ 10,000 - 12 meses`;
   línea `12 cuotas de S/ 927.50 - TCEA 19.80%`;
   nota `Montos del servidor. El cliente no calcula.`
4. **Card Documentos (3)** (blanca, radio 16, borde `outline-variant`): una fila
   por documento (alto mín. 64px, divisores `divider`), cada una con nombre,
   meta y acción `Abrir` (texto `accent-text #9B5F12`):
   - `Contrato de crédito` — `12 cláusulas - PDF del servidor` — `Abrir`.
   - `Pagaré` — `Se firma junto al contrato - PDF` — `Abrir`.
   - `Hoja resumen` — `Condiciones y cronograma - PDF` — `Abrir`.
5. **Card Aviso legal**: título `Aviso legal`; texto en canvas
   `Lee los 3 documentos antes de firmar.` Texto legal completo (en app, con
   wrap): *"Lee los 3 documentos antes de firmar. Al firmar aceptas las
   condiciones aprobadas. La copia certificada se enviará a tu correo."*
6. **CTA primario**: `Firmar contrato` → va a la pantalla Firma.
7. **CTA secundario**: `Descargar documentos (PDF)` → descarga los 3 PDF del
   backend; si responde `501`, mensaje `Exportación no disponible por ahora.`
8. **Nota** (§7): `Cargando, vacío y error (§7). Sin documentos: EmptyView.`

### Comportamiento / validaciones visibles

- Cada documento se abre/descarga individualmente desde
  `GET /credits/applications/{id}/contract` (el backend expone los 3).
- 4 estados: cargando (skeleton de la card de documentos), vacío (sin
  documentos generados → `EmptyView` + `Volver al estado`), error de red con
  `Reintentar`.
- Sin `Idempotency-Key` en lectura/descarga (no crea recursos).

### Datos del backend

`GET /credits/applications/{id}/contract` → los 3 documentos (contrato, pagaré,
resumen en PDF o metadatos) + `{doc_hash, status: GENERATED}`.
Errores: `NOT_FOUND (404` neutro si es ajeno/inexistente), `CONFLICT (409` si el
expediente no está `APPROVED)`, `EXPORT_FORMAT_NOT_SUPPORTED (501)`.
La UI muestra condiciones tal cual; no recalcula ni reinterpreta.

---

## 6. Firma con biometría (HU11 · E3-T15)

Mockup: `docs/design/creditos-firma.png` (+ `.svg`) · Fig página
`Créditos/Firma` (página `0:6760`, frame raíz `Creditos-Firma` `0:6761`).

> Decisión del dueño: la firma usa **biometría del dispositivo con fallback a
> PIN** (coherente con `docs/01`, contradicción 8: biometría del dispositivo +
> PIN de contingencia; liveness solo en HU01).

### Contenido exacto

1. **Fila expediente**: `EXPEDIENTE CR-2026-0841` + badge `Por firmar`
   (fondo `warning-container #FFDCBC`, texto `on-warning-container #683D00`).
2. **Card resumen + consentimiento** (fondo `surface-container-low`):
   label `RESUMEN (del servidor)`; valor
   `Personal - S/ 10,000 - 12 m - S/ 927.50/mes`;
   consentimiento explícito (checkbox 24px, obligatorio `*`):
   `He leído los 3 documentos y acepto firmarlos *`;
   subtítulo `Obligatorio. Sin esto no se puede firmar.`
3. **CTA primario**: `Firmar con biometría` (bloqueado hasta marcar el
   consentimiento).
4. **Card Reto biométrico**: título `Reto biométrico`; texto en canvas
   `Toca el sensor para autorizar la firma.`; línea de error
   (`error #BA1A1A`): `Si se cancela verás Reintentar. No revela el motivo.`
   Estados visibles: `Esperando biometría…` (reto emitido), `Reintentar`
   (cancelada/fallida).
5. **Card Fallback PIN** (borde `primary`): título `Sin biometría? Usa tu PIN`;
   label `PIN (6 dígitos)` + input enmascarado (`••••••`); CTA secundario
   `Firmar con PIN`; nota `Contingencia. El PIN nunca se guarda.`
   Solo se usa si la biometría no está disponible o falla.
6. **Card éxito** (fondo `success-container`): `Documento firmado!` +
   texto en canvas `Contrato SIGNED. Copia enviada al correo.` Texto completo:
   *"Contrato firmado (SIGNED). Copia certificada enviada a j•••@•••.com
   (correo enmascarado)."* + `Sello doc_hash verificado (no repudio).`
7. **Card error ejemplo** (fondo `error-container`): `Si algo falla verás esto`
   + `Reto vencido: se rota y se reintenta solo.` (caso `EXPIRED_NONCE`).
8. **Nota** (§7): `Cargando, vacío y error (§7) con Reintentar.`

### Comportamiento / validaciones visibles

- Sin marcar el consentimiento → CTA bloqueado (el backend también valida).
- Sin éxito biométrico **no se envía** la firma y el contrato no se firma.
- Biometría cancelada/fallida → mensaje genérico + `Reintentar` (sin revelar
  el motivo exacto del backend).
- `POST .../sign` usa `ApiClient.postWithIdempotency` (misma
  `Idempotency-Key` ante reintentos del intento en curso); tras
  `EXPIRED_NONCE` se rota el reto (`.../sign/challenge`) y se reintenta con
  **clave nueva**.
- Tras `SIGNED` se confirma firma certificada + copia remitida; sin prometer
  desembolso (es HU12).

### Datos del backend

`POST /credits/applications/{id}/sign/challenge` → `{nonce}` (un solo uso) →
firma local del `nonce` con la clave del dispositivo (tras desbloqueo
biométrico o PIN) →
`POST /credits/applications/{id}/sign` (`Idempotency-Key` obligatoria;
`{nonce, signature, device_id}`) →
`{contract_id, status: SIGNED, signed_at}`.
Errores: `NOT_AUTHORIZED` (firma/binding inválido, genérico sin oráculo),
`EXPIRED_NONCE` (rotar reto y reintentar con clave nueva), `CONFLICT (409`
ya `SIGNED`), `NOT_FOUND (404` neutro), `VALIDATION_ERROR (422)`.

### Mensajes (español, accionables)

| Caso | Texto |
|---|---|
| Éxito | `Contrato firmado. Copia certificada enviada a tu correo.` |
| Biometría cancelada/fallida | `No pudimos verificar tu biometría. Reintenta o usa tu PIN.` |
| PIN inválido | `PIN incorrecto. Verifica e intenta de nuevo.` |
| Reto vencido | `Tu sesión de firma venció. Estamos generando un nuevo reto…` |
| Ya firmado | `Este contrato ya fue firmado. Revisa tu correo.` |
| Red | `Sin conexión. Revisa tu red y toca Reintentar.` |

---

## 7. E3-T23 — Mis créditos (HU10/HU11 · listado del titular)

Mockup: `docs/design/creditos-mis-creditos.png` (+ `.svg`) · Fig página
`Créditos/Mis créditos` (frame raíz `Creditos-MisCreditos`). La pantalla
`/credits` lleva pestañas/segmentos arriba (`Simulador` | `Mis créditos`,
decisión del dueño); el mockup del simulador (`creditos-simulador.png`) se
actualizó con el mismo segmento (pestaña `Simulador` activa, TopBar `Créditos`).

### Contenido exacto

1. **Header**: título `Mis créditos` (24/600 `primary`); subtítulo
   `Tus expedientes. El estado lo decide el servidor.` (14, `secondary-text`).
2. **SegmentedButton** (fondo `surface-container-high #EAE8E3`, radio full,
   segmentos 40px, texto 14/600): segmento activo fondo `primary`, texto blanco;
   segmento inactivo texto `primary`. Aquí activo = `Mis créditos`.
3. **Fila de expediente** (card blanca, radio 16, borde `outline-variant`,
   padding 16; la fila `Aprobado` lleva borde `primary` para destacarla):
   - Línea 1: `{Producto} • {Monto} • {plazo} m` (14/600 `primary`,
     ej. `Personal • S/ 10,000 • 12 m`) + **badge de estado** (pill, 12/600).
    - Línea 2: `EXPEDIENTE {id} • {fecha}` (12, `secondary-text`,
      ej. `EXPEDIENTE a1b2c3d4e5f6 • 2026-10-11`). IDs reales (UUID, se
      muestran los primeros 12 caracteres), fecha = día de `created_at`.
      Sin antigüedad relativa ni cuota: la cuota queda **fuera de alcance
      del listado** (el endpoint no la entrega; cliente delgado, `docs/19`).
      El mockup conserva un texto ilustrativo anterior; vale lo descrito aquí.
   - Acciones: fila `Aprobado` → CTA primario `Continuar a la firma`
     (52px, → `/credits/contract` con el id) + link `Ver estado ›`
     (`accent-text`, → `/credits/status` con el id). Filas no aprobadas →
     solo `Ver estado ›`, **sin CTA de firma**.
4. **Badges de estado** (mismo set que §3 Estado):
   | Etiqueta | Fondo | Texto |
   |---|---|---|
   | `Evaluando` | `surface-container-highest #E4E2DD` | `on-surface-variant #424844` |
   | `En revisión` | `warning-container #FFDCBC` | `on-warning-container #683D00` |
   | `Aprobado` | `success-container #D0E8D7` | `#0B1F15` |
   | `Rechazado` | `error-container #FFDAD6` | `#93000A` |
   | `Vencido` | `surface-container-highest #E4E2DD` | `on-surface-variant #424844` |
   Mapeo servidor → etiqueta: `DRAFT|SUBMITTED|IN_EVALUATION → Evaluando`,
   `IN_REVIEW → En revisión`, `APPROVED → Aprobado`, `REJECTED → Rechazado`,
   `EXPIRED → Vencido`. La UI muestra el `status` tal cual (cliente delgado).
5. **Estado vacío** (card blanca): título `Aún no tienes solicitudes`;
   subtítulo `Simula un crédito y envía tu primera solicitud.`; CTA secundario
   `Ir al simulador` (cambia a la pestaña Simulador).
6. **Carga**: skeleton de filas (barras `surface-container-highest`).
   **Error**: mensaje en español accionable + Reintentar
   (ej. `Sin conexión. Revisa tu red y toca Reintentar.`).
7. **Nota**: `Orden: más reciente primero. Pull para actualizar. Sin PII en
   la lista.`

### Comportamiento / validaciones visibles

- Orden: `created_at DESC` (más reciente primero); pull-to-refresh.
- `Continuar a la firma` solo existe si el servidor marcó `APPROVED`
  (nunca se infiere en el cliente).
- El `decision_reason` **no** se muestra en la fila (para no saturarla);
  vive en la pantalla de estado (§3).
- Sin `Idempotency-Key` (lectura, no mueve dinero).

### Datos del backend

`GET /credits/applications` (titular autenticado, `Authorization: Bearer <jwt>`;
`user_id` del JWT, nunca del cliente) →
`200 {"data": [{id, product_id, product_name?, requested_amount_minor,
term_months, status, decision_reason, created_at, updated_at, decided_at}],
"meta": {total, limit?, offset?}}`, orden `created_at DESC, id`.
Sin expedientes → `data: []` (no `404`). Errores: `401 NOT_AUTHENTICATED`,
`422 VALIDATION_ERROR`. La fila usa `product_name ?? product_id`, monto
(`*_minor`, céntimos), `term_months`, `status` y fechas; sin ingresos, deuda,
score ni documento (regla de oro 7).

---

## Chequeo contra `docs/01` (contradicciones)

Ninguna:
- HU09 (Should, 5 SP, Sprint 2) y HU10 (Must, 8 SP, Sprint 2) figuran en
  alcance (`docs/01-hallazgos-y-decisiones.md`); consentimiento explícito,
  dictamen `APROBADO/RECHAZADO/EN_REVISION` y aprobación separada coinciden con
  `docs/08`. No se introduce lógica nueva en el diseño.
- HU11 (Must, 8 SP, Sprint 3, `Firma Digital de Contrato`) figura en alcance
  (`docs/01`, tabla de HUs). Biometría del dispositivo + PIN de contingencia
  coincide con la contradicción 8 resuelta (`docs/01#2`); liveness queda solo
  en HU01. Los 3 documentos separados (contrato, pagaré, resumen) y el
  fallback a PIN son decisiones del dueño, sin choque con `docs/01`.

## Archivos

| Archivo | Uso |
|---|---|
| `docs/design/creditos-simulador.png` (+ `.svg`) | Mockup HU09 |
| `docs/design/creditos-solicitud.png` (+ `.svg`) | Mockup HU10 app |
| `docs/design/creditos-estado.png` (+ `.svg`) | Mockup HU10 estado |
| `docs/design/creditos-analista.png` (+ `.svg`) | Mockup HU10 panel |
| `docs/design/creditos-contrato.png` (+ `.svg`) | Mockup HU11 contrato |
| `docs/design/creditos-firma.png` (+ `.svg`) | Mockup HU11 firma |
| `docs/design/creditos-mis-creditos.png` (+ `.svg`) | Mockup E3-T23 "Mis créditos" (HU10/HU11) |
| `docs/design/creditos-simulador.png` (+ `.svg`) | Mockup HU09 (actualizado E3-T23: TopBar `Créditos` + segmentos) |
| `docs/design/creditos-pantallas.md` (este archivo) | Spec de contenido |
