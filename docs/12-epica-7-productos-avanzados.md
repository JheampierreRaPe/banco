# 12 - Epica 7: Productos Avanzados (HU24-HU26)

**Modulo responsable:** `fx`, apoyado en `accounts`, `transactions`, `ledger`, `notifications`.
**Resultado de la epica:** el cliente organiza su dinero en bolsillos/multidivisa, cambia divisas
y (post-MVP) distribuye su sueldo automaticamente.

## HU24 - Cuentas Multidivisa y Meta de Ahorro Automatico (Should, 5 SP, Sprint 4)

**Logica**
1. El cliente crea **bolsillos** de ahorro y cuentas en USD/EUR desde la app.
2. Configura reglas de **redondeo automatico**: al pagar, se redondea al sol superior y el
   excedente va al bolsillo de ahorro.
3. Los bolsillos remunerados calculan y acreditan rendimientos segun la tasa pactada.
4. Se pueden hacer traspasos entre bolsillos y cuentas del mismo cliente.

**Reglas**: traspaso < 5 s; rendimientos automaticos y periodicos; soportar varias monedas sin
errores de redondeo; cada traspaso/rendimiento pasa por el motor y el ledger.
**CA**: CA-01 (apertura), CA-02 (redondeo), CA-03 (rendimientos), CA-04 (traspaso libre).

**Tareas**
- `E7-T01` Modelo de `savings_pockets` y `pocket_movements`. *Datos.*
- `E7-T02` Servicio de apertura de bolsillos y cuentas multidivisa. *Backend.*
- `E7-T03` Regla de redondeo integrada al flujo de pago. *Backend.*
- `E7-T04` Job de calculo y abono de rendimientos (asiento contable). *Backend.*
- `E7-T05` Traspasos via motor (asiento tipo 7.8). *Backend.*
- `E7-T06` UI Flutter de bolsillos y metas. *Frontend.*
- `E7-T07` Pruebas de redondeo, rendimiento y traspaso. *QA.*

## HU25 - Modulo de Cambio de Divisas Preferencial (Should, 8 SP, Sprint 4)

**Logica**
1. El cliente ve la cotizacion en tiempo real (adaptador de tipo de cambio o tasa semilla).
2. Al solicitar, el sistema **congela** la tasa por 30 s (`fx_quotes`).
3. El **spread** es dinamico segun el volumen.
4. Al confirmar dentro del plazo, se ejecutan los dos asientos ligados (7.7); si la cotizacion
   expiro, se cancela y se recotiza.

**Reglas**: cotizacion actualizada desde el proveedor; rechazo automatico al expirar; spread
configurable por tesoreria sin cambios de codigo.
**CA**: CA-01 (congelar 30 s), CA-02 (spread por volumen), CA-03 (ejecucion inmediata), CA-04
(rechazo por expiracion).

**Tareas**
- `E7-T08` Adaptador `FxRateProvider` (mock + API real) con cache y TTL. *Backend.*
- `E7-T09` `fx_quotes` con temporizador y validacion de vigencia. *Backend/Datos.*
- `E7-T10` Calculo de spread dinamico configurable. *Backend.*
- `E7-T11` Ejecucion del cambio con dos asientos ligados. *Backend/Ledger.*
- `E7-T12` UI Flutter de cotizacion y confirmacion con temporizador. *Frontend.*
- `E7-T13` Pruebas de expiracion, spread y cuadre por moneda. *QA.*

## HU26 - Distribucion Automatica de Ingresos y Nomina (Could, 8 SP, Sprint 4 - FUERA DEL MVP)

**Logica (post-MVP)**
1. El cliente define reglas de distribucion **por porcentaje** (facturas, ahorro, inversion).
2. Al recibir un abono masivo/sueldo, el sistema dispersa automaticamente.
3. La suma de porcentajes debe ser **exactamente 100%** (ni mas ni menos).
4. Se emite un reporte detallado de cada distribucion.

**CA**: CA-01 (reglas), CA-02 (dispersion), CA-03 (validacion 100%), CA-04 (reporte).

**Tareas (post-MVP)**
- `E7-T14` Modelo y CRUD de reglas de distribucion. *Backend/Datos.*
- `E7-T15` Deteccion de abono masivo y ejecucion de dispersion via motor. *Backend.*
- `E7-T16` Validacion estricta de suma 100%. *Backend.*
- `E7-T17` Reporte de distribucion. *Backend/Frontend.*
- `E7-T18` Pruebas de suma, dispersion y reversibilidad. *QA.*

## Criterios de salida de la epica

- Bolsillos y cambio de divisas operan con asientos contables correctos y sin errores de redondeo.
- La cotizacion congelada se respeta o se rechaza por vencimiento.
- HU26 queda especificada y lista para una fase posterior.
