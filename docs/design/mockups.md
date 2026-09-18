# mockups - registro de pantallas

Registro unico de los mockups. Cada imagen nueva se agrega aqui con una fila. Asi el agente que
construye una pantalla sabe que imagen seguir, sin que se lo expliquen.

## Registro

| Archivo | Pantalla | HU | Brief | Estado |
|---|---|---|---|---|
| `figma-sprint1-creacionCuenta.txt` (link Figma) | Onboarding / creacion de cuenta | HU01, HU02 | `E1-T05`, `E1-T11` | pendiente |
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
