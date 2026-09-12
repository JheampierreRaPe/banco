# 10 - Epica 5: Motor Transaccional y Contabilidad (HU17-HU19)

**Modulos responsables:** `transactions`, `ledger`, `reconciliation`.
**Resultado de la epica:** el dinero se mueve de forma consistente, se contabiliza en partida
doble y se concilia con terceros.

> La **logica** detallada (estados, holds, asientos, idempotencia, concurrencia, cierre) esta en
> `04-motor-transaccional-y-ledger.md`. Este documento es la lista de **tareas**.

## HU17 - Motor Transaccional en Tiempo Real (Must, 13 SP, Sprint 1)

**Que debe lograr**: validar saldo, retener fondos, evitar doble gasto y procesar rapido.

**CA**: CA-01 (valida saldo), CA-02 (bloqueo de fondos), CA-03 (idempotencia), CA-04 (<200 ms).

**Tareas**
- `E5-T01` Definir la maquina de estados y sus transiciones (doc 04, seccion 3). *Backend.*
- `E5-T02` Implementar `transactions`, `transaction_status_history` y `holds`. *Datos.*
- `E5-T03` Servicio transaccional con validacion de saldo y bloqueo de filas en orden estable.
  *Backend.*
- `E5-T04` Soporte de `Idempotency-Key` (tabla + middleware + almacenamiento de respuesta).
  *Backend.*
- `E5-T05` Emision de eventos por `outbox` en la misma transaccion. *Backend.*
- `E5-T06` Job de liberacion de holds vencidos. *Backend.*
- `E5-T07` Pruebas de concurrencia, duplicidad, saldo insuficiente y latencia. *QA.*
- `E5-T08` Prueba de carga con picos simulados. *QA/DevOps.*

## HU18 - Contabilidad en Libro Mayor de Partida Doble (Must, 13 SP, Sprint 1)

**Que debe lograr**: registrar debitos y creditos atomicos con UUID y cierre diario cuadrado.

**CA**: CA-01 (partida doble), CA-02 (UUID), CA-03 (atomicidad), CA-04 (cierre cuadrado).

**Tareas**
- `E5-T09` Catalogo contable semilla (`ledger_accounts`) y subcuentas por cliente. *Datos.*
- `E5-T10` Implementar `journal_entries` y `postings` append-only con hash encadenado. *Backend/Datos.*
- `E5-T11` Validador de cuadre (`sum(debitos) = sum(creditos)`) antes de confirmar. *Backend.*
- `E5-T12` Actualizacion atomica de proyecciones `ledger_balances` y `account_balances`.
  *Backend.*
- `E5-T13` Job de cierre diario + control de consistencia con `postings`. *Backend.*
- `E5-T14` Comprobacion anti-manipulacion (verificar cadena de hashes). *Seguridad/QA.*
- `E5-T15` Pruebas de atomicidad (rollback parcial), UUID unico y cierre cuadrado. *QA.*

## HU19 - Conciliacion Bancaria y Redes de Pago (Must, 8 SP, Sprint 4)

**Que debe lograr**: procesar clearing, cruzar con el ledger y gestionar diferencias.

**CA**: CA-01 (clearing procesado), CA-02 (cruce y deteccion), CA-03 (reporte de diferencias),
CA-04 (flujo de reclamos).

**Tareas**
- `E5-T16` Adaptador `ClearingSource` (archivo simulado de la red/equipo par). *Backend.*
- `E5-T17` Ingesta de `clearing_files` e `clearing_items` con validacion de lote. *Backend/Datos.*
- `E5-T18` Motor de matching (referencia, monto, moneda, fecha) con tolerancias. *Backend.*
- `E5-T19` Registro de `reconciliation_exceptions` y reporte para operaciones. *Backend.*
- `E5-T20` Flujo de `claims` y asiento de ajuste autorizado (nunca edicion). *Backend.*
- `E5-T21` UI del panel web: corridas, excepciones y reclamos. *Frontend.*
- `E5-T22` Pruebas: match total, diferencias, transaccion huerfana, ajuste. *QA.*

## Criterios de salida de la epica

- No existe ninguna operacion de dinero que no deje asiento contable.
- El cierre diario cuadra o bloquea con alerta.
- Las diferencias de conciliacion quedan documentadas con evidencia y flujo de resolucion.
- El motor responde dentro del objetivo de latencia en pruebas de carga controlada.
