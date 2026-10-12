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
| | | | | |

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
