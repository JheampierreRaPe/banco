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
| (design system, sin imagen) | Tokens "Eucalipto y Ocre" (`docs/20` §3-§6) | transversal | `F-T34` | hecho |
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
