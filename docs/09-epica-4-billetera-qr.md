# 09 - Epica 4: Billetera y Pagos QR (HU13-HU16)

**Modulo responsable:** `wallet`, apoyado en `transactions`, `ledger`, `accounts`, `risk`,
`notifications`.
**Resultado de la epica:** el cliente paga con billetera y QR; el comercio cobra y recibe abono;
se pagan servicios y recargas.

## HU13 - Billetera Digital e Interoperabilidad (Must, 8 SP, Sprint 3)

**Logica**
1. El cliente vincula una cuenta o tarjeta como fuente de fondos (se guarda un **token**, nunca
   el numero completo).
2. Los pagos express se procesan sin digitar claves (bajo el limite base).
3. La billetera es interoperable con las redes de pago contempladas en el alcance (el otro
   sistema del curso y el estandar interno de QR).
4. Se aplican limites diarios configurables; al superarlos se exige seguridad reforzada.

**Reglas**: pago < 3 s; datos tokenizados; limites por usuario/dispositivo.
**CA**: CA-01 (vincular), CA-02 (express sin clave), CA-03 (interoperabilidad), CA-04 (limite
diario).

**Tareas**
- `E4-T01` Modelo y CRUD de `wallets` y `wallet_links` con tokenizacion. *Backend/Datos.*
- `E4-T02` Servicio de pago express (usa el motor) con regla de limite base. *Backend.*
- `E4-T03` Adaptador de interoperabilidad (mock/contrato con el equipo par). *Backend.*
- `E4-T04` UI Flutter de billetera y vinculacion. *Frontend.*
- `E4-T05` Pruebas de limites y tokenizacion. *QA.*

## HU14 - Generacion y Cobro con QR Dinamico/Estatico (Must, 5 SP, Sprint 3)

**Logica**
1. El comercio ingresa monto y concepto; se genera un **QR dinamico** con firma/hash y
   vencimiento (p. ej. 5 min).
2. Al vencer, el QR se invalida automaticamente.
3. El QR respeta un **formato interoperable** (empaquetado del estandar definido por el equipo).
4. Al confirmarse el pago, se notifica al comercio (push/visual) de inmediato.

**Reglas**: generacion < 1 s; notificacion de abono < 5 s; la firma del QR impide manipulacion.
**CA**: CA-01 (QR generado), CA-02 (caducidad), CA-03 (interoperable), CA-04 (notificacion).

**Tareas**
- `E4-T06` Servicio de generacion de QR firmado + `qr_charges`. *Backend.*
- `E4-T07` Job/validacion de caducidad. *Backend.*
- `E4-T08` Endpoint `/merchants/qr` y consulta de estado. *Backend.*
- `E4-T09` UI Flutter/web de comercio para generar y ver cobros. *Frontend.*
- `E4-T10` Pruebas: QR vencido, firma alterada, notificacion de abono. *QA.*

## HU15 - Lectura y Confirmacion de Pago QR (Must, 5 SP, Sprint 3)

**Logica**
1. El cliente escanea el QR; el sistema lo decodifica y muestra **comercio y monto** antes de
   confirmar.
2. Si el monto esta dentro del limite base, pago en **un toque**.
3. Si supera el limite, se exige biometria adicional.
4. Si el QR esta vencido, corrupto o manipulado, se rechaza sin descontar fondos y se alerta.

**Reglas**: lectura/validacion < 2 s; valida firma/hash del QR; el pago pasa por el motor (7.4).
**CA**: CA-01 (lectura), CA-02 (un toque), CA-03 (biometria sobre limite), CA-04 (QR protegido).

**Tareas**
- `E4-T11` Validacion de integridad del QR y creacion del pago en el motor. *Backend.*
- `E4-T12` Uso del endpoint `/payments/qr` y autorizacion biometrica condicional. *Backend.*
- `E4-T13` UI Flutter: escaner, resumen y confirmacion. *Frontend.*
- `E4-T14` Manejo de errores de QR. *Frontend.*
- `E4-T15` Pruebas de seguridad del QR y concurrencia de pago. *QA.*

## HU16 - Pago de Servicios y Recargas Express (Should, 5 SP, Sprint 4)

**Logica**
1. El cliente busca una empresa de servicios por nombre o numero de suministro.
2. Puede recargar celular eligiendo operador, numero y monto.
3. Se envia recordatorio de vencimiento de servicios registrados.
4. Cada pago/recarga genera comprobante estructurado (numero de transaccion oficial).

**Reglas**: consulta < 2 s; catalogo extensible sin cambios estructurales; recordatorios con 3
dias de anticipacion.
**CA**: CA-01 (busqueda), CA-02 (recarga), CA-03 (recordatorio), CA-04 (comprobante).

**Tareas**
- `E4-T16` Catalogo `billers` y adaptador de servicios (mock). *Backend/Datos.*
- `E4-T17` Pago de servicio y recarga via motor. *Backend.*
- `E4-T18` Job de recordatorios. *Backend.*
- `E4-T19` UI Flutter de servicios y recargas. *Frontend.*
- `E4-T20` Pruebas: pago, recarga, recordatorio, comprobante. *QA.*

## Criterios de salida de la epica

- Cobro y pago QR completos, con firma, caducidad y notificacion.
- Pagos express respetan limites y exigen biometria al superarlos.
- Servicios/recargas generan comprobante y se contabilizan.
