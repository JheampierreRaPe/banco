# Sprint 1 - Indice de briefs (fundacion)

Objetivo del sprint: registro + KYC + activacion + login, cuentas y saldos, y el motor
transaccional con ledger. Ver `docs/14-plan-sprints-y-ejecucion.md`.

Total: **49 briefs**.

## Preparacion (infraestructura) - hacer primero
`INIT-T01` Esqueleto del monorepo y proyecto backend base - `INIT-T02` Migraciones multi-esquema y
esquemas base.

## HU01 - Registro y KYC biometrico (8 SP)
`E1-T01` Adaptador KycProvider - `E1-T02` Endpoints proxy KYC - `E1-T03` Alta de cliente
(transaccional) - `E1-T04` Persistir KYC + auditoria - `E1-T05` Flutter captura doc + liveness -
`E1-T06` Flutter errores KYC - `E1-T07` QA KYC.

## HU02 - Verificacion y activacion (3 SP)
`E1-T08` Servicio OTP - `E1-T09` Adaptador notificaciones - `E1-T10` Endpoints activacion/reenvio -
`E1-T11` Flutter activacion - `E1-T12` QA OTP.

## HU03 - Autenticacion (5 SP)
`E1-T13` Login nonce/tokens - `E1-T14` Login PIN y bloqueo - `E1-T15` Sesiones/revocacion -
`E1-T16` Flutter login biometrico - `E1-T17` Auditoria de login - `E1-T18` QA auth.

## HU05 - Consolidado de cuentas (5 SP)
`E2-T01` Repositorio cuentas/saldos - `E2-T02` Endpoints cuentas - `E2-T03` Movimientos/export -
`E2-T04` Proyeccion `movements_view` - `E2-T05` Flutter dashboard - `E2-T06` QA cuentas.

## HU17 - Motor transaccional (13 SP)
`E5-T01` Maquina de estados - `E5-T02` Persistencia motor - `E5-T03` Servicio transaccional -
`E5-T04` Idempotencia - `E5-T05` Outbox - `E5-T06` Job holds vencidos - `E5-T07` QA concurrencia -
`E5-T08` Prueba de carga.

## HU18 - Ledger de partida doble (13 SP)
`E5-T09` Catalogo contable - `E5-T10` Asientos append-only - `E5-T11` Validador de cuadre -
`E5-T12` Proyecciones de saldo - `E5-T13` Cierre diario - `E5-T14` Cadena de hashes -
`E5-T15` QA ledger.

## Frontend base
`F-T01` Base Flutter - `F-T02` Almacenamiento seguro/sesion - `F-T03` Biometria local y nonce.

> Las pantallas de negocio del sprint ya viven en las tareas de epica: KYC (`E1-T05`, `E1-T06`),
> activacion (`E1-T11`), login (`E1-T16`) y dashboard (`E2-T05`). Por eso **no** se generan briefs
> separados para `F-T04`/`F-T05` de `docs/13` (serian duplicados).

## QA / DevOps
`Q-T01` Esqueleto de pruebas y semilla - `Q-T02` Suite del motor - `Q-T04` Contrato KYC/OpenAPI -
`Q-T05` Pipeline CI - `Q-T06` Docker de demo.

## Orden de construccion recomendado

1. `INIT-T01`, `INIT-T02`, `Q-T01`, `Q-T05`, `Q-T06` (estructura, migraciones, pruebas e infra base).
2. `E5-T01`, `E5-T02`, `E5-T09`, `E5-T10`, `E5-T11` (motor y ledger puros).
3. `E5-T12`, `E5-T13`, `E5-T03`, `E5-T04`, `E5-T05`, `E5-T06` (motor sobre ledger).
4. `E2-T01`..`E2-T04` (cuentas y proyecciones).
5. `E1-T01`..`E1-T04`, `E1-T08`..`E1-T10`, `E1-T13`..`E1-T15`, `E1-T17` (backend identidad).
6. `F-T01`..`F-T03`, `E1-T05`, `E1-T06`, `E1-T11`, `E1-T16`, `E2-T05` (frontend).
7. `E1-T07`, `E1-T12`, `E1-T18`, `E2-T06`, `E5-T07`, `E5-T08`, `E5-T15`, `Q-T02`, `Q-T04` (QA).

## Dependencias criticas

- `ledger` (`E5-T09`/`E5-T10`) antes de `transactions` (`E5-T03`).
- `E5-T04` (idempotencia) antes de exponer cualquier endpoint de dinero.
- `E5-T05` (outbox) antes de notificaciones/auditoria/fraude.
- `E1-T03` depende de `E2-T01` y `E5-T09`.
- `E1-T13` es prerequisito de `E1-T14`/`E1-T15`.
