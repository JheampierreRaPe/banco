# 07 - Epica 2: Cuentas y Transacciones (HU05-HU08)

**Modulos responsables:** `accounts`, `transactions`, `ledger`, con apoyo de `risk`,
`notifications`, `fx`.
**Resultado de la epica:** el cliente ve su patrimonio, transfiere entre sus cuentas, a terceros
e interbancariamente, y autoriza pagos altos con biometria.

> Obligatorio: todo movimiento de dinero pasa por el motor (`04-motor-transaccional-y-ledger.md`).

## HU05 - Consolidado de Cuentas y Saldos (Must, 5 SP, Sprint 1)

**Logica**
1. El dashboard lista las cuentas del cliente con tipo, numero enmascarado, moneda y saldo
   **disponible**.
2. El detalle muestra `disponible`, `retenido` y `contable` (`disponible + retenido`).
3. Al seleccionar una cuenta, se listan sus movimientos (desde `movements_view`, en tiempo real).
4. Se puede exportar el detalle a PDF/Excel.

**Reglas**: consulta < 2 s; numero de cuenta siempre enmascarado; export filtrable por fecha.
**CA**: CA-01 (vista consolidada), CA-02 (diferenciar saldos), CA-03 (movimientos en tiempo
real), CA-04 (exportacion).

**Tareas**
- `E2-T01` Repositorio de cuentas y proyeccion `account_balances`. *Datos.*
- `E2-T02` Endpoint `/accounts` y `/accounts/{id}` con enmascaramiento. *Backend.*
- `E2-T03` Endpoint de movimientos paginado + export PDF/Excel. *Backend.*
- `E2-T04` Proyeccion/vista `movements_view` alimentada por eventos del ledger. *Datos.*
- `E2-T05` Pantallas Flutter: dashboard, detalle y filtros. *Frontend.*
- `E2-T06` Pruebas de enmascaramiento y consistencia de saldos. *QA.*

## HU06 - Transferencias Propias e Interbancarias (Must, 8 SP, Sprint 2)

**Logica**
1. **Entre cuentas propias**: debito y credito inmediatos, sin comision (asiento unico 7.1).
2. **A terceros (mismo banco)**: valida beneficiario y saldo; asiento 7.2.
3. **Interbancaria**: valida CCI/titular, retiene fondos, envia por la pasarela, queda en
   `FUNDS_HELD` -> `POSTED` -> `SETTLED` (asiento 7.3); maneja timeout y reverso.
4. Si no hay saldo suficiente: `REJECTED` sin tocar saldos.
5. Al finalizar: comprobante con fecha, montos, cuentas y numero de operacion.

**Reglas**: idempotencia obligatoria; interbancaria < 10 s (o `202` con seguimiento); toda
transferencia trazada con usuario, fecha y hora.
**CA**: CA-01 (propias), CA-02 (CCI validado), CA-03 (saldo insuficiente), CA-04 (comprobante).

**Tareas**
- `E2-T07` Casos de uso de transferencia propia y a tercero sobre el motor. *Backend.*
- `E2-T08` Validacion de titular/CCI por adaptador (mock hasta tener el real). *Backend.*
- `E2-T09` Saga interbancaria: estados, timeout, polling y compensacion. *Backend.*
- `E2-T10` Comprobante PDF con numero de operacion unico. *Backend.*
- `E2-T11` Pantallas Flutter de transferencia (seleccion de destino, confirmacion, comprobante).
  *Frontend.*
- `E2-T12` Pruebas: feliz, insuficiente, duplicada, timeout interbancario, reverso. *QA.*

## HU07 - Gestion de Beneficiarios Frecuentes (Should, 3 SP, Sprint 2)

**Logica**
1. Tras una transferencia exitosa se puede guardar el beneficiario (alias, foto, datos).
2. Busqueda en tiempo real por alias/nombre (hasta 100 registros).
3. Se puede fijar un **limite pre-aprobado** por beneficiario (agiliza sin factor extra).
4. Se puede editar o eliminar (con confirmacion de seguridad).

**Reglas**: datos cifrados; busqueda < 1 s; el limite solo lo edita el titular autenticado.
**CA**: CA-01 a CA-04.

**Tareas**
- `E2-T13` CRUD de beneficiarios con cifrado y busqueda indexada. *Backend/Datos.*
- `E2-T14` Regla de "tope pre-aprobado" integrada al motor (omite biometria hasta el tope).
  *Backend.*
- `E2-T15` Pantalla de agenda con busqueda y agrupacion. *Frontend.*
- `E2-T16` Pruebas de busqueda, tope y edicion. *QA.*

## HU08 - Autorizacion Biometrico-Facial de Pagos Sensibles (Must, 8 SP, Sprint 2)

**Logica**
1. El motor calcula el monto acumulado y el perfil de riesgo; si supera el umbral, exige
   **validacion biometrica del dispositivo** antes de debitar (`PENDING_AUTHORIZATION`).
2. La transaccion queda **retenida** (hold) hasta que la biometria sea exitosa.
3. Se aplican limites diarios por perfil de riesgo.
4. Todo intento fallido se registra como evento de seguridad y notifica al cliente.

**Reglas**: la validacion no debe anadir > 3 s; el umbral y los limites son configurables sin
despliegue; los fallos alimentan al modulo `risk`.
**CA**: CA-01 (exigir biometria), CA-02 (bloqueo preventivo), CA-03 (limites por perfil),
CA-04 (alerta por intento fallido).

**Tareas**
- `E2-T17` Integracion motor <-> autorizacion biometrica del dispositivo (`nonce` firmado) para
  autorizacion puntual. *Backend.*
- `E2-T18` Parametro de umbral y reglas de perfil de riesgo. *Backend/Datos.*
- `E2-T19` Endpoint `/transactions/{id}/authorize`. *Backend.*
- `E2-T20` Emitir evento a `risk` y notificar al cliente ante fallo. *Backend.*
- `E2-T21` UI Flutter: reto biometrico dentro del flujo de pago. *Frontend.*
- `E2-T22` Pruebas de umbral, limite diario y bloqueo. *QA.*

## Criterios de salida de la epica

- Ninguna transferencia modifica saldos sin asiento contable.
- La interbancaria maneja exito, timeout y reverso sin perdida de fondos.
- Los saldos mostrados siempre coinciden con el ledger.
- Los pagos sobre el umbral no se completan sin biometria valida.
