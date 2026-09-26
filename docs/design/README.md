# design

Carpeta para los **mockups y recursos de diseno**.

## Como usarla

1. Guarda aqui las imagenes de las pantallas (PNG/JPG) o exportaciones de Figma.
2. Nombra por pantalla y en minusculas:
   - `onboarding-kyc.png`
   - `otp-activacion.png`
   - `login-biometrico.png`
   - `dashboard.png`
   - `detalle-cuenta.png`
   - `transferencia.png`
   - `comprobante.png`
   - `panel-alertas-fraude.png`
3. Registra cada imagen en `docs/design/mockups.md` (archivo, pantalla, HU, brief, estado).
4. Opcional: guarda el link de Figma en `docs/design/figma-<flujo>.txt` y registralo en `mockups.md`.

## Que NO va aqui

- Codigo de implementacion (va en `frontend/` o `admin-web/`).
- CSS (solo para el panel web; ver `docs/20`, seccion 1).
- Datos reales o capturas con informacion personal.

> Recuerda: para Flutter se trabaja con **tokens + mockups**, no con CSS.
