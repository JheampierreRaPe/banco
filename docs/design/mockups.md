# mockups - registro de pantallas

Registro unico de los mockups. Cada imagen nueva se agrega aqui con una fila. Asi el agente que
construye una pantalla sabe que imagen seguir, sin que se lo expliquen.

## Registro

| Archivo | Pantalla | HU | Brief | Estado |
|---|---|---|---|---|
| `figma-sprint1-creacionCuenta.txt` (link Figma) | Onboarding / creacion de cuenta | HU01, HU02 | `E1-T05`, `E1-T11` | pendiente |
| `recovery-email.png` | Recovery: pantalla de email (pagina `recovery (email)`, nodo `0:996`) — **retirada en F-T51** (ver `pin-reset.png`) | HU03 | `F-T42` | retirado |
| `recovery-otp.png` | Recovery: pantalla de OTP (pagina `recovery (otp)`, nodo `0:1027`) — **retirada en F-T51** (ver `pin-reset.png`) | HU03 | `F-T42` | retirado |
| `pin-reset.png` | Restablecer PIN email+DNI+OTP (pagina `pin-reset`, nodo `0:1062`) | HU03 | `F-T42` | hecho |
| `activation.png` | Activacion de cuenta (pagina `activation`, nodo `0:1106`) | HU03 | `F-T42` | hecho |
| `home-dashboard.png` | Home / dashboard de cuentas (pagina `home (dashboard)`, nodo `0:1147`) | HU04 | `F-T42` | hecho |
| `login-biometrico.png` + `pantallas.fig` login (`0:860`) / PIN (`0:901`) + onboarding (`figma-sprint1-creacionCuenta.txt`) | Entrada "iniciar sesión en este dispositivo" (`/login/device`, seam/placeholder; el flujo de 3 pasos lo construye `F-T56`) | HU03, HU04 | `F-T57` | pendiente |
| (pendiente de mockup del dueño) | Iniciar sesión en este dispositivo: email+DNI -> OTP -> PIN (flujo de 3 pasos sobre `/login/device`, `F-T56`) | HU03, HU04 | `F-T56` | pendiente |
| (fig `pantallas.fig`, pagina `dash` `0:3057`, root `0:3058`: topbar `0:3059`, hero-saldo `0:3070`, quick-actions `0:3082`, sec-cuentas `0:3108`, sec-mov `0:3130`, banner-seg `0:3160`, bottom-nav `0:3168`) | Dashboard redisenado (topbar con saludo/avatar, SALDO TOTAL del servidor, quick-actions, cuentas, movimientos, banner, bottom-nav) | HU05 | `F-T54` | hecho |
| (fig `pantallas.fig`, pagina `opciones-usuario` `0:2903`, root `0:2904`) | Opciones de usuario / Mi perfil (topbar, hero, secciones, switch biometria, cerrar sesion, bottom-nav) | HU03 | `F-T52` | hecho |
| (design system, sin imagen) | Tokens "Eucalipto y Ocre" (`docs/20` §3-§6) | transversal | `F-T34` | hecho |
| `creditos-simulador.png` (+ `.svg`; fig `pantallas.fig`, pagina `Créditos/Simulador` `0:6486`, root `0:6490`) | Simulador de crédito (producto, monto, plazo, desgravamen, cuota, TEA/TCEA, cronograma, CTAs) | HU09 | `E3-T04` | hecho |
| `creditos-solicitud.png` (+ `.svg`; fig `pantallas.fig`, pagina `Créditos/Solicitud` `0:6487`, root `0:6574`) | Solicitud + consentimiento bureau obligatorio | HU10 | `E3-T09` | pendiente |
| `creditos-estado.png` (+ `.svg`; fig `pantallas.fig`, pagina `Créditos/Estado` `0:6488`, root `0:6627`) | Estado del expediente (evaluando/aprobado/rechazado/en revisión) | HU10 | `E3-T09` | pendiente |
| `creditos-analista.png` (+ `.svg`; fig `pantallas.fig`, pagina `Créditos/Analista` `0:6489`, root `0:6669`, desktop 1280) | Panel analista: bandeja IN_REVIEW, detalle enmascarado, dictamen | HU10 | `E3-T10` | pendiente |
| `creditos-contrato.png` (+ `.svg`; fig `pantallas.fig`, pagina `Créditos/Contrato` `0:6716`, root `0:6717`) | Contrato: expediente aprobado + 3 documentos + CTA firma | HU11 | `E3-T15` | hecho |
| `creditos-firma.png` (+ `.svg`; fig `pantallas.fig`, pagina `Créditos/Firma` `0:6760`, root `0:6761`) | Firma con biometría + fallback PIN + éxito | HU11 | `E3-T15` | hecho |
| `creditos-mis-creditos.png` (+ `.svg`; fig `pantallas.fig`, pagina `Créditos/Mis créditos`, root `Creditos-MisCreditos` `0:6810`) | "Mis créditos": segmentos Simulador/Mis créditos + filas por estado (Aprobado con CTA firma) + vacío/carga/error | HU10, HU11 | `E3-T23` | pendiente |
| `creditos-simulador.png` (+ `.svg`; fig `pantallas.fig`, pagina `Créditos/Simulador` `0:6486`) — actualizado E3-T23: TopBar `Créditos` + segmentos | Simulador de crédito (producto, monto, plazo, desgravamen, cuota, TEA/TCEA, cronograma, CTAs) | HU09 | `E3-T04` | hecho |
| Spec de contenido de las 6 pantallas | `docs/design/creditos-pantallas.md` | HU09, HU10, HU11 | `E3-T04`, `E3-T09`, `E3-T10`, `E3-T15` | hecho |
| `(pendiente) docs/design/creditos-cronograma.png` | Cronograma del préstamo + débito automático + pago de cuota (rutas `/credits/loans`, `/credits/loans/{id}`; spec §8 en `docs/design/creditos-pantallas.md`, UI desde tokens `docs/20`) | HU12 | `E3-T21` | pendiente |
| `(pendiente) docs/design/creditos-desembolso.png` | Disparador de desembolso en el éxito de la firma: card + selector de cuentas + CTA `Desembolsar a mi cuenta` + éxito con `Ver cronograma` (spec §8c en `docs/design/creditos-pantallas.md`, UI desde tokens `docs/20`; sin página en el `.fig` abierto para extender) | HU12 | `M-1` | pendiente |
| `(pendiente) docs/design/billetera-vinculacion.png` (+ `.svg`; sin página en `pantallas.fig` — verificado E4-T04 y re-verificado en desvincular via `query_nodes` sin nodos wallet/billetera) | Billetera (lista enmascarada) + vincular cuenta/tarjeta + desvincular por vínculo activo con confirmación (`DELETE /wallet/links/{id}` -> `INACTIVE`; UI desde tokens `docs/20`) | HU13 | `E4-T04` | pendiente |
| `(pendiente) docs/design/billetera-qr-comercio.png` (+ `.svg`; sin página en `pantallas.fig` — verificado E4-T09 via `list_documents`: solo páginas de onboarding/login/cuentas/transferencias/créditos, sin nodos wallet/merchant) | Comercio: generar cobro QR (monto + concepto + moneda + modo) + QR con monto/vigencia + estado pendiente/pagado/vencido con refresco (`POST /merchants/qr`, `GET /merchants/qr/{id}`; UI desde tokens `docs/20`, `E4-T09`) | HU14 | `E4-T09` | pendiente |
| `(pendiente) docs/design/billetera-qr-pago.png` (+ `.svg`; sin página en `pantallas.fig` — UI del pagador desde tokens `docs/20`, `E4-T13`/`E4-T14`, G76) | Pagador: escáner QR con inyección de `qr_content` (`QrScanPage`, `/wallet/pay-qr`) + resumen/confirmación en un toque + biometría/PIN ante `PENDING_AUTHORIZATION` + recibo/rechazo (`QrPayPage`, `/wallet/pay-qr/confirm`) + alerta visual `QR inválido` (`POST /payments/qr`, `POST /transactions/{id}/authorize`; el `qr_content` nunca se muestra) | HU15 | `E4-T13`, `E4-T14` | hecho (UI desde tokens; imagen pendiente G76) |
| `(pendiente) docs/design/servicios-recargas.png` (+ `.svg`; sin página en `pantallas.fig` — verificado E4-T19 vía `list_documents`: solo páginas de onboarding/login/cuentas/transferencias/créditos, sin nodos wallet/servicios; UI desde tokens `docs/20`, G76) | Servicios y recargas: catálogo/búsqueda + pago con comprobante + recarga con comprobante + registrados/recordatorios (rutas `/wallet/services*`, `/wallet/topups`) | HU16 | `E4-T19` | hecho (UI desde tokens; imagen pendiente G76) |
| `(pendiente) docs/design/notificaciones-centro.png` (+ `.svg`; sin página en `pantallas.fig` — verificado E6-T15 vía `list_documents`: solo páginas de onboarding/login/cuentas/transferencias/créditos, sin nodos de notificaciones; UI desde tokens `docs/20`) | Centro de notificaciones + detalle (operación/dispositivo/hora/ubicación aproximada) + "no reconozco esta operación" + preferencias por canal (rutas `/notifications`, `/notifications/:id`, `/notifications/preferences`) | HU22 | `E6-T15` | hecho (UI desde tokens; imagen pendiente del dueño) |

## Como agregar un mockup

1. Guarda la imagen en `docs/design/` (PNG/JPG) con nombre `seccion-pantalla.png`.
2. Agrega una fila en la tabla de arriba: archivo, pantalla, HU, brief y estado (`pendiente`).
3. El brief de esa pantalla debe referenciar el mockup (campo `Mockup:`).
4. Cuando la pantalla este implementada, cambia el estado a `hecho`.

## Convenciones

- Nombre: `seccion-pantalla.png` (minusculas). Ejemplos:
  - `onboarding-kyc.png`
  - `otp-activacion.png`
  - `login-biometrico.png`
  - `cuentas-dashboard.png`
  - `cuentas-detalle.png`
  - `transferencias-formulario.png`
  - `transferencias-comprobante.png`
  - `creditos-simulador.png`
  - `billetera-qr.png`
  - `fraude-alertas.png`
- Si el diseno se define en Figma, guarda el link en `docs/design/figma-<flujo>.txt`.

## Como lo usa el agente

1. Lee `docs/20-diseno-ui.md` (tokens y componentes).
2. Busca en este registro la fila de su pantalla.
3. Abre la imagen/link indicado y construye la pantalla siguiendo el mockup.

> El frontend se esta construyendo para **probar funcionalidad** primero; el diseno se refina
> despues. La paleta y los estilos base ya estan fijados en `docs/20`.
