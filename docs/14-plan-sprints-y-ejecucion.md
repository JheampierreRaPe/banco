# 14 - Plan de sprints y ejecucion

## 1. Cronograma unico (18 semanas)

| Fase | Semanas | Contenido |
|---|---|---|
| Preparacion | 1-2 | Equipo, repo, arquitectura, modelo de datos, CI, semilla, contratos API. |
| **Sprint 1** | 3-5 | Identidad/KYC + cuentas + motor transaccional + ledger. |
| **Sprint 2** | 6-8 | Transferencias, beneficiarios, pagos sensibles y ciclo de credito. |
| **Sprint 3** | 9-11 | Creditos (firma/desembolso), billetera, QR, servicios, notificaciones. |
| **Sprint 4** | 12-14 | Conciliacion, fraude, cumplimiento, multidivisa y cambio de divisas. |
| Estabilizacion | 15-16 | Integracion, pruebas de regresion, correccion de bugs, despliegue demo. |
| Cierre | 17-18 | Documentacion final, memoria, guion y exposicion. |

> Se adoptan **4 sprints de 3 semanas** (12 semanas de desarrollo) para que el total cuadre con
> las 18 semanas y la capacidad real del equipo (~40 SP por sprint).

## 2. Sprint 1 - Fundacion: identidad, cuentas, motor y ledger (47 SP)

**Objetivo:** un cliente se registra y activa; el sistema ya sabe mover y contabilizar dinero.

| HU | Descripcion | SP | Modulo |
|---|---|---|---|
| HU01 | Registro y KYC biometrico | 8 | identity |
| HU02 | Verificacion y activacion (OTP) | 3 | identity |
| HU03 | Autenticacion biofacial | 5 | identity |
| HU05 | Consolidado de cuentas y saldos | 5 | accounts |
| HU17 | Motor transaccional | 13 | transactions |
| HU18 | Ledger de partida doble | 13 | ledger |

**Entregable:** se puede abrir cuenta, ver el dashboard y ejecutar internamente un movimiento
contable (aunque aun sin interfaz de transferencia completa).
**Criterios de salida:** KYC integrado al microservicio, un asiento cuadra (`debitos=creditos`),
saldo coherente con el ledger, auditoria basica activa.
**Riesgo principal:** acoplar KYC (Semana 3) demasiado tarde -> empezar pruebas con el
microservicio en la Semana 1.

## 3. Sprint 2 - Transferencias y ciclo de credito (37 SP)

**Objetivo:** mover dinero a terceros con controles y simular/solicitar creditos.

| HU | Descripcion | SP | Modulo |
|---|---|---|---|
| HU04 | Recuperacion de cuenta | 5 | identity |
| HU06 | Transferencias propias/interbancarias | 8 | transactions |
| HU07 | Beneficiarios frecuentes | 3 | accounts |
| HU08 | Autorizacion biometrica de pagos | 8 | transactions/risk |
| HU09 | Simulador de creditos | 5 | credits |
| HU10 | Solicitud y evaluacion crediticia | 8 | credits |

**Entregable:** transferencias propias y a terceros trazables; interbancaria con simulador;
credito simulado y solicitado con dictamen.
**Criterios de salida:** idempotencia probada, saldo insuficiente rechazado sin cambios, hold
correcto, dictamen con justificacion guardada.

## 4. Sprint 3 - Creditos avanzados, billetera y QR (39 SP)

**Objetivo:** cerrar el ciclo crediticio y habilitar pagos digitales.

| HU | Descripcion | SP | Modulo |
|---|---|---|---|
| HU11 | Firma digital de contrato | 8 | credits |
| HU12 | Desembolso y cuotas | 5 | credits |
| HU13 | Billetera digital | 8 | wallet |
| HU14 | Generacion y cobro QR | 5 | wallet |
| HU15 | Lectura y pago QR | 5 | wallet |
| HU16 | Servicios y recargas | 5 | wallet |
| HU22 | Notificaciones de seguridad | 3 | notifications |

**Entregable:** desembolso real en la cuenta con cronograma; pago y cobro por QR; servicios.
**Criterios de salida:** desembolso atomico, QR firmado y con caducidad, notificaciones < 5 s.

## 5. Sprint 4 - Control, cumplimiento y productos avanzados (37 SP)

**Objetivo:** blindar el banco y ampliar la propuesta de valor.

| HU | Descripcion | SP | Modulo |
|---|---|---|---|
| HU19 | Conciliacion bancaria | 8 | reconciliation |
| HU20 | Motor antifraude | 8 | risk |
| HU21 | Cumplimiento PLDFT/AML | 8 | risk |
| HU24 | Multidivisa y meta de ahorro | 5 | fx |
| HU25 | Cambio de divisas | 8 | fx |

**Entregable:** conciliacion con excepciones, alertas de fraude, cotejo de listas, bolsillos y
cambio de divisas.
**Criterios de salida:** diferencias documentadas, bloqueo preventivo funcional, asientos por
moneda cuadrados.

## 6. Historias fuera del MVP

| HU | Descripcion | SP | Motivo |
|---|---|---|---|
| HU23 | Bitacora de auditoria inalterable | 5 | Could; el log basico se cubre durante el MVP. |
| HU26 | Distribucion automatica de ingresos | 8 | Could; se especifica para fase posterior. |

## 7. Orden de construccion (camino critico)

1. Infra + esqueleto + CI + semilla.
2. `ledger` (motor contable puro y testeable) y `transactions` (maquina de estados).
3. `identity` (KYC) + `accounts`.
4. `transactions` sobre casos reales (transferencias) + `risk` basico.
5. `credits`.
6. `wallet` + `notifications`.
7. `reconciliation` + `risk`/`compliance` + `fx`.
8. Estabilizacion.

**Regla de oro:** no se empieza una funcionalidad de dinero sin que el motor y el ledger esten
estables.

## 8. Reglas de gestion (Scrum)

- **Planning** al inicio de cada sprint; **daily** de 15 min; **review** y **retro** al cierre.
- **Velocity** objetivo: 90% de lo planificado. Si un sprint se atrasa, se recortan primero los
  `Should` (HU04, HU07, HU09, HU16, HU22, HU24, HU25) antes de tocar `Must`.
- **Impedimentos**: 0 abiertos al cierre del sprint.
- **Defectos**: menos de 1 critico por historia entregada.
- **Definition of Done**: ver `16-guia-para-agentes.md`.

## 9. Hitos de verificacion (semanas clave)

| Semana | Hito |
|---|---|
| 2 | Arquitectura, modelo de datos y CI listos. |
| 5 | Demo: registro, login, dashboard y un movimiento contable. |
| 8 | Demo: transferencias y solicitud de credito. |
| 11 | Demo: desembolso, QR y servicios. |
| 14 | Demo: conciliacion, fraude y divisas. |
| 16 | Version candidata a exposicion. |
| 18 | Exposicion final. |
