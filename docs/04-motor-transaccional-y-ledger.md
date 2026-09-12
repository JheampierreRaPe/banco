# 04 - Motor transaccional y ledger (la logica del dinero)

Este es el documento mas importante. Si algo aqui se incumple, el sistema queda contablemente
invalido. Todo modulo que mueve dinero (transferencias, QR, creditos, FX, bolsillos) **debe**
pasar por este motor.

## 1. Tres conceptos de saldo

| Concepto | Que es | De donde sale |
|---|---|---|
| Saldo contable | Lo que el ledger registra como propiedad del cliente. | Suma de `postings` de la cuenta. |
| Saldo disponible | Lo que el cliente puede gastar ahora. | `account_balances.available_minor`. |
| Fondos retenidos (hold) | Lo reservado para una operacion en curso. | `account_balances.held_minor`. |

Invariante: `disponible + retenido = contable`.

## 2. Contabilidad de partida doble (obligatoria)

- Toda operacion genera un **asiento** (`journal_entries`) con dos o mas **movimientos**
  (`postings`): debitos y creditos.
- Regla de oro: `sum(debitos) = sum(creditos)` por moneda. Si no cuadra, el asiento no se crea.
- El ledger es **append-only**: no se edita ni se borra; se corrige con un asiento nuevo
  (reverso).
- Cada asiento tiene UUID unico y `hash` encadenado al anterior (evidencia de no manipulacion).

### 2.1 Subcuentas de un cliente

Cada cuenta de cliente se representa con dos cuentas contables:
- `2000-<cuenta>`: saldo **disponible**.
- `2100-<cuenta>`: saldo **retenido**.

Durante un hold se mueve valor de `2000` a `2100`; el total del cliente no cambia.

## 3. Maquina de estados de una transaccion

```
INITIATED ─► VALIDATED ─┬─► PENDING_AUTHORIZATION ─► AUTHORIZED ─┐
                        └─► AUTHORIZED ───────────────────────────┤
                                                                  ▼
   REJECTED ◄──────── (validaciones/limites/saldo)            FUNDS_HELD
                                                                  │
                                              ┌───────────────────┼───────────────┐
                                              ▼                   ▼               ▼
                                            POSTED ───────────► SETTLED ──────► CONCILIATED
                                              │                   │
                                              └────► FAILED       └────► REVERSED
```

| Estado | Significado | Fondos |
|---|---|---|
| `INITIATED` | Solicitud recibida, sin validar. | Intactos |
| `VALIDATED` | Reglas basicas OK. | Intactos |
| `PENDING_AUTHORIZATION` | Espera biometria/OTP/riesgo. | Intactos |
| `AUTHORIZED` | Autorizacion aprobada. | Intactos |
| `FUNDS_HELD` | Fondos reservados. | Retenidos |
| `POSTED` | Asiento contable registrado. | Contabilizados |
| `SETTLED` | Entregado al destino. | Entregados |
| `CONCILIATED` | Cruzado con el tercero (terminal). | Conciliados |
| `REJECTED` | No se pudo iniciar (terminal, sin movimiento). | Intactos |
| `FAILED` | Error tecnico tras retener; el hold se libera. | Liberados |
| `REVERSED` | Compensada con asiento inverso (terminal). | Devueltos |

Reglas de transicion:
- Solo el motor cambia de estado; cada cambio deja fila en `transaction_status_history`.
- `REJECTED` y `FAILED` se diferencian: `REJECTED` es negocio, `FAILED` es tecnico.
- Un `FAILED` **siempre** libera holds (`2100` -> `2000`).
- `CONCILIATED` puede pasar a `REVERSED` si la conciliacion detecta un descuadre.

## 4. Algoritmo general de una operacion con dinero

1. **Resolver idempotencia**: buscar `Idempotency-Key`; si ya existe, devolver la respuesta
   guardada (misma clave + mismo cuerpo = mismo resultado; clave repetida con otro cuerpo = 409).
2. **Validar entrada**: cuentas existen y estan activas, moneda coherente, monto > 0, formato.
3. **Evaluar riesgo y autorizacion**: reglas de `risk`; si el monto supera el umbral, exigir
   biometria/OTP -> `PENDING_AUTHORIZATION`.
4. **Abrir transaccion de base de datos (ACID)**.
5. **Bloquear** las filas de saldo implicadas (`SELECT ... FOR UPDATE`) en orden estable de
   `account_id` (evita deadlocks).
6. **Comprobar fondos y limites**: `available_minor >= amount + fee`; limites diarios/perfil.
7. **Crear `transactions`** en estado `AUTHORIZED`.
8. **Retener fondos** (si la operacion es diferida): `2000` -> `2100`; estado `FUNDS_HELD`.
9. **Registrar asiento** en `ledger` (uno o varios segun el tipo).
10. **Actualizar proyecciones** `account_balances` y `ledger_balances`.
11. **Escribir eventos en `outbox`** y la fila en `transaction_status_history`.
12. **Commit**. Si algo falla: rollback total; ningun asiento parcial.
13. **Post-commit (asincrono)**: notificar, auditar, alimentar antifraude, conciliar.

## 5. Idempotencia

- Cabecera `Idempotency-Key` obligatoria en todo endpoint que mueve dinero.
- Se guarda: clave, usuario, endpoint, hash del cuerpo, `transaction_id` y respuesta.
- Repeticion exacta -> se devuelve la misma respuesta (no se vuelve a debitar).
- Misma clave con cuerpo distinto -> `409 Conflict`.
- TTL configurable (p. ej. 24 h).

## 6. Concurrencia

- Bloqueo pesimista de saldos en orden de `account_id` ascendente.
- Para entidades de estado, columna `version` (optimistic locking).
- Los reintentos de red producen solicitudes duplicadas: los cubre la idempotencia.
- Para picos (fin de mes) se admite encolar la operacion y confirmar asincronamente, pero el
  saldo se reserva en el momento.

## 7. Asientos por tipo de operacion

> `A` y `B` son cuentas de cliente; el monto es `100`.

### 7.1 Transferencia propia (mismo cliente, inmediata, sin comision)

```
Debe  2000-A   100   (baja el disponible de origen)
Haber 2000-B   100   (sube el disponible de destino)
```
Sin hold (se resuelve en un solo asiento atomico). Pasa a `SETTLED` de inmediato.

### 7.2 Transferencia a tercero (mismo banco, otro cliente)

Igual que 7.1 (origen -> destino). Puede llevar comision:

```
Debe  2000-A          100
Debe  2000-A          2      (comision)
Haber 2000-B          100
Haber 4000-Comisiones 2
```

### 7.3 Transferencia interbancaria saliente (100 + 2 de comision)

```
1) Retencion:
   Debe  2000-A          100
   Haber 2100-A          100
2) Comision:
   Debe  2000-A          2
   Haber 4000-Comisiones 2
3) Al confirmar envio:
   Debe  2100-A          100
   Haber 2200-Transito   100
4) Al confirmar recepcion del otro banco:
   Debe  2200-Transito   100
   Haber 1000-Banco      100
```
Si el tercero no confirma en el plazo: liberar el hold (`2100-A` -> `2000-A`) y marcar `FAILED`
o `REVERSED`.

### 7.4 Pago QR (50)

```
Retencion del pagador:  Debe 2000-Pagador  50 / Haber 2100-Pagador 50
Liquidacion:            Debe 2100-Pagador  50 / Haber 2000-Comercio 50
```
Si el QR expiro o la firma no valida: no hay retencion; se rechaza antes.

### 7.5 Desembolso de prestamo (5000)

```
Debe  5000-Cartera de prestamos  5000   (activo: nace la deuda)
Haber 2000-Cuenta-cliente        5000   (sube el disponible)
```

### 7.6 Pago de cuota (300 = 200 capital + 80 interes + 20 seguro)

```
Debe  2000-Cuenta-cliente     300
Haber 5000-Cartera            200
Haber 4100-Ingresos interes    80
Haber 4200-Ingresos seguro     20
```

### 7.7 Cambio de divisas (compra de 100 USD pagando 380 PEN)

Dos asientos ligados (uno por moneda), nunca uno solo cruzando monedas:

```
Asiento PEN:  Debe 2000-Cuenta PEN 380 / Haber 9100-Posicion FX PEN 380
Asiento USD:  Debe 9100-Posicion FX USD 100 / Haber 2000-Cuenta USD 100
```
El spread se registra como ingreso (`4000`) dentro del asiento de la moneda que corresponda.
Ambos asientos comparten `fx_operation_id`.

### 7.8 Traspaso a bolsillo de ahorro

Igual que 7.1, usando las cuentas contables de la cuenta principal y del bolsillo.

### 7.9 Reverso (compensacion)

Un reverso es una transaccion nueva que invierte los movimientos del asiento original, con
referencia `reverses_journal_entry_id`. **Nunca** se marca el original como borrado.

```
Debe  <lo que estaba al haber>   /   Haber <lo que estaba al debe>
```

## 8. Liquidacion y conciliacion

1. Toda operacion con terceros pasa por `2200-Transito` hasta confirmarse.
2. La conciliacion compara `transactions`/`journal_entries` contra `clearing_items`.
3. Coincidencia por: referencia externa, monto, moneda y fecha.
4. Diferencias -> `reconciliation_exceptions` -> flujo de `claims`.
5. Una diferencia confirmada se corrige con asiento de ajuste autorizado (nunca editando).

## 9. Cierre contable diario

Job programado (fuera del horario de operacion):
1. Suma debitos y creditos del dia por moneda.
2. Si `debitos = creditos` -> marca `daily_closings.balanced = true`.
3. Si no cuadra -> no cierra, genera alerta y bloquea el cierre hasta resolver.
4. Verifica que `account_balances` coincida con los `postings` (control de integridad).
5. Emite evidencia para auditoria.

## 10. Reglas y casos borde

| Caso | Comportamiento |
|---|---|
| Saldo insuficiente | `REJECTED`; no se modifica nada. CA-03 de HU06. |
| Peticion duplicada | Devuelve el resultado original (idempotencia). |
| Falla entre retencion y asiento | Rollback total; el hold no queda huerfano. |
| Timeout del interbancario | Se mantiene `FUNDS_HELD`, se reintenta; si expira, libera y compensa. |
| Monedas distintas sin FX | Rechazado. |
| Monto <= 0 o no entero en centimos | Rechazado en validacion. |
| Hold vencido | Job lo libera y actualiza estado. |
| Carrera de dos debitos | El bloqueo de filas + validacion de saldo evita sobregiro. |
| Redondeo | Se trabaja en centimos enteros; ningun redondeo pierde/crea dinero sin asiento. |
| Asiento descuadrado | No se confirma; error de servidor y alerta. |

## 11. Anti-patrones prohibidos

- ❌ Actualizar `available_minor` sin crear asiento contable.
- ❌ Usar `float` para dinero.
- ❌ Borrar o editar `postings`, `journal_entries` o `transactions`.
- ❌ Registrar un asiento cruzando monedas sin dos asientos.
- ❌ Publicar eventos directamente dentro de la transaccion (usar `outbox`).
- ❌ Ignorar `Idempotency-Key`.
- ❌ Dejar que otro modulo escriba en el ledger: solo el modulo `ledger` escribe asientos.

## 12. Pruebas obligatorias del motor

1. Transferencia feliz propia y a tercero.
2. Saldo insuficiente rechazado sin cambios.
3. Doble peticion con misma `Idempotency-Key` -> un solo debito.
4. Dos debitos concurrentes sobre la misma cuenta -> no hay sobregiro.
5. Reverso genera asiento inverso y el original permanece.
6. Cuadre: `debitos = creditos` por asiento y en el cierre diario.
7. Interbancario: exito, timeout y compensacion.
8. Prueba de carga con latencia < 200 ms en el camino de escritura.
