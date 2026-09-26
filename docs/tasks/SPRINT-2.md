# Sprint 2 - Indice de briefs (transferencias, beneficiarios, pagos sensibles y ciclo de credito)

> Nota de archivo (2026-09-26): los briefs de Sprint 1 con `Estado: Hecho` viven en
> `docs/tasks/archive/` y se citan aqui por ID, sin cambios. En `docs/tasks/` solo
> estan los briefs no-`Hecho`, incluidos los 30 de este indice aun pendientes; `E2-T07`
> ya esta `Hecho` (archivado en `docs/tasks/archive/E2-T07.md`).

Objetivo del sprint: mover dinero a terceros con controles (transferencias propias, a tercero e
interbancarias), gestionar beneficiarios frecuentes y exigir autorizacion biometrica del
dispositivo en pagos sensibles. Ver `docs/14-plan-sprints-y-ejecucion.md#3-sprint-2---transferencias-y-ciclo-de-credito-37-sp`.

Base ya construida (Sprint 1, `Hecho`): motor `E5-T03`/`E5-T04`/`E5-T06`, outbox `E5-T05`,
ledger `E5-T09`..`E5-T14`, cuentas/proyecciones `E2-T01`..`E2-T06`, identidad/KYC/login y
biometria de dispositivo (`F-T03`, `E1-T42`).

Total: **31 briefs** (20 del bloque transferencias/beneficiarios/pagos sensibles + 11 del bloque
creditos `E3-T01`..`E3-T11`): **30 pendientes + `E2-T07` `Hecho`** (archivado).

> Nota de alcance (decision del dueno 2026-09-26): `E2-T07` **implementa la ruta atomica SIN hold
> de transferencias entre cuentas propias** (asiento unico `2000-S -> 2000-D`, sin comision) y
> **corrige la cuenta de comision del motor a `4100-Ingresos por comisiones`** (cierra G05). El
> `hold + settle` se conserva para terceros e interbancaria.

## HU06 - Transferencias propias e interbancarias (Must, 8 SP) - `transactions`
`E2-T07` Casos de uso de transferencia propia y a tercero (**`Hecho`**, archivado
`docs/tasks/archive/E2-T07.md`) - `E2-T08` Validacion de titular/CCI
(adaptador mock) - `E2-T09` Saga interbancaria (estados, timeout, polling, compensacion) -
`E2-T10` Comprobante PDF con numero de operacion unico - `E2-T11` Pantallas Flutter de
transferencia - `E2-T12` QA (feliz, insuficiente, duplicada, timeout, reverso).

## HU07 - Gestion de beneficiarios frecuentes (Should, 3 SP) - `accounts`
`E2-T13` CRUD de beneficiarios con cifrado y busqueda indexada - `E2-T14` Tope pre-aprobado
integrado al motor - `E2-T15` Pantalla de agenda Flutter (busqueda/agrupacion) - `E2-T16` QA
(busqueda, tope, edicion).

## HU08 - Autorizacion biometrico-facial de pagos sensibles (Must, 8 SP) - `transactions`/`risk`
`E2-T17` Integracion motor <-> autorizacion biometrica del dispositivo (`nonce` firmado; dueno de
la regla de bloqueo preventivo `CA-02` y del limite diario por perfil `CA-03`) - `E2-T18` Umbral y
perfiles de riesgo configurables sin despliegue (datos/config) - `E2-T19`
`POST /transactions/{id}/authorize` - `E2-T20` Evento a `risk` + notificacion ante fallo (consumo
de la alerta de bloqueo) - `E2-T21` UI reto biometrico en el flujo de pago - `E2-T22` QA (umbral,
limite diario, bloqueo).

## HU09 - Simulador inteligente de creditos (Should, 5 SP) - `credits`
`E3-T01` Motor de calculo financiero puro y testeable (cuota francesa, cronograma, TCEA) -
`E3-T02` Catalogo `loan_products` configurable - `E3-T03` `GET /credits/products` +
`POST /credits/simulate` + export PDF - `E3-T04` UI Flutter (sliders y tabla de amortizacion) -
`E3-T05` QA de exactitud contra casos de referencia.

## HU10 - Solicitud y evaluacion crediticia automatica (Must, 8 SP) - `credits`
`E3-T06` Adaptador `CreditBureau` (mock + interfaz real) con reintentos - `E3-T07` Motor de reglas
de credito configurable (DTI, topes, score) - `E3-T08` Caso de uso de solicitud + expediente +
notificacion (+ revision del analista) - `E3-T09` UI Flutter (formulario, consentimiento, estado) -
`E3-T10` Pantalla de revision del analista (panel web) - `E3-T11` QA (aprobado, rechazado, en
revision, bureau caido).

## Operaciones y administracion (Must, soporte HU06/HU08/HU17) - `transactions`/`admin`
`E2-T24` `GET /transactions` (listado paginado de movimientos del titular, filtros por
fecha/tipo/estado; enmascaramiento; sin PII) - `E2-T25` `POST /transactions/{id}/reverse`
(reverso/compensacion con asiento inverso, idempotente, solo estados permitidos) - `E2-T26`
parametros administrables `GET/PUT /admin/parameters` (fachada sobre `config.parameters`; rol
admin).

## Deuda de Sprint 1 - `accounts`/`ledger`/`shared`
`E2-T23` Cableado en produccion del consumidor de `movements_view` (`ledger.entry.posted`): el
consumidor `handle_movement_event` ya existe (`E2-T04`, `Hecho`) pero no estaba suscrito al
bus/outbox en produccion (`docs/17#6`). Depende de `E2-T04` y `E5-T05`.

## Fuera de este lote

- **HU09 - Simulador de creditos** (`E3-T01`..`E3-T05`, `credits`) y **HU10 - Solicitud y
  evaluacion crediticia** (`E3-T06`..`E3-T11`, `credits`): **briefs generados en el 2o lote
  (2026-09-26)**, en `docs/tasks/E3-T01.md`..`docs/tasks/E3-T11.md` (indice en las secciones
  HU09/HU10 de arriba). Dependen de este bloque de transferencias/beneficiarios/pagos sensibles en
  la practica (motor y ledger estables) y del catalogo de parametros `E2-T18`.
- **HU04 - Recuperacion de cuenta** (`identity`): queda **fuera de este lote salvo rediseno**; el
  flujo vigente la supera (recuperacion via `POST /auth/recovery/request` + `POST /auth/pin-reset`,
  login en dispositivo nuevo `E1-T45`/`E1-T46`; ver `docs/05#61` y `docs/18` SCR-005). No se crean
  briefs nuevos para HU04 sin una decision explicita de rediseno.

## Orden de construccion recomendado

1. `E2-T23` (cableado de la proyeccion: deja `movements_view` viva en produccion; prerequisito de
   evidencia contable para todo el sprint).
2. `E2-T07` (casos de uso base sobre el motor; **`Hecho`**) y `E2-T08` (adaptador titular/CCI).
3. `E2-T09` (saga interbancaria) y `E2-T10` (comprobante).
4. `E2-T13` (beneficiarios) y `E2-T18` (parametros/perfiles) en paralelo.
5. `E2-T17` (integracion biometrica; incluye bloqueo `CA-02` y limite diario `CA-03`),
   `E2-T19` (endpoint authorize), `E2-T14` (tope pre-aprobado), `E2-T20` (evento a `risk` +
   notificacion).
6. `E2-T24` (listado), `E2-T25` (reverso) y `E2-T26` (parametros admin) sobre `E2-T07`/`E2-T18`.
7. `E2-T11`, `E2-T15`, `E2-T21` (frontend, pueden ir con mocks de `docs/05` en paralelo a 2-6).
8. `E2-T12`, `E2-T16`, `E2-T22` (QA al cierre de cada HU).
9. **Creditos (despues del bloque de transferencias/beneficiarios/pagos sensibles):**
   1. `E3-T01` (motor financiero puro) y `E3-T02` (catalogo `loan_products`) primero.
   2. `E3-T03` (API del simulador) sobre `E3-T01`/`E3-T02`.
   3. `E3-T06` (adaptador bureau) y `E3-T07` (motor de reglas) en paralelo; `E3-T07` consume
      `E2-T18`.
   4. `E3-T08` (solicitud/expediente/revision) sobre `E3-T06`/`E3-T07`/`E3-T02`.
   5. `E3-T04` (UI simulador) y `E3-T09` (UI solicitud/estado) con mocks de `docs/05`, y
      `E3-T10` (panel analista) sobre `E3-T08`, en paralelo.
   6. `E3-T05` y `E3-T11` (QA) al cierre de cada HU.

## Dependencias criticas

- `E5-T03`/`E5-T04`/`E5-T05` (`Hecho`) antes de cualquier endpoint de dinero (`E2-T07`).
- `E2-T07` antes de `E2-T09` (saga) y de `E2-T10` (comprobante).
- `E2-T08` antes de `E2-T09` (validar CCI/titular antes de enviar).
- `E2-T13` antes de `E2-T14` (tope) y de `E2-T15` (agenda).
- `E2-T18` antes de `E2-T17`/`E2-T19`/`E2-T14` (umbral/límites configurables).
- `E2-T17` antes de `E2-T19`; `E2-T19` antes de `E2-T20` y `E2-T21`.
- `E2-T07` antes de `E2-T24` (listado) y de `E2-T25` (reverso).
- `E2-T18` antes de `E2-T26` (parametros admin); `E2-T26` es la fachada de exposicion, no el
  dueno del esquema (`shared/config`).
- `E2-T04` + `E5-T05` antes de `E2-T23` (proyeccion en produccion).
- `ledger` y `transactions` no se paralelizan entre si: van juntos y primero
  (`docs/16#9`).
- **Creditos:** `E3-T01`/`E3-T02` antes de `E3-T03`; `E3-T06`/`E3-T07` antes de `E3-T08`;
  `E2-T18` (`shared/config`) antes de `E3-T07`/`E3-T08` (parametros DTI/topes/score);
  `E3-T03` antes de `E3-T04`/`E3-T05`; `E3-T08` antes de `E3-T09`/`E3-T10`/`E3-T11`.
- Creditos no ejecuta dinero en este lote: el simulador no crea registro real y la solicitud no
  genera `loans`/desembolso (Sprint 3, HU11/HU12).

## Criterios de salida del sprint (HU06-HU10)

- Ninguna transferencia modifica saldos sin asiento contable (`debitos=creditos`).
- La interbancaria maneja exito, timeout y reverso sin perdida de fondos.
- `movements_view` se actualiza en produccion por evento (`E2-T23`).
- Los pagos sobre el umbral no se completan sin biometria valida del dispositivo.
- El titular lista (`E2-T24`) y revierte (`E2-T25`) sus operaciones; umbral/topes/limites son
  editables sin despliegue por un admin (`E2-T26`).
- El simulador calcula cuota/cronograma en < 1 s y **no** genera registro crediticio real.
- Ninguna solicitud se evalua sin consentimiento explicito de bureau; el dictamen y su
  justificacion quedan guardados y la aprobacion es separada de la solicitud; si el bureau falla
  tras reintentos, el expediente queda `EN_REVISION`.

## Reglas transversales embebidas en cada brief

Cliente delgado (el frontend nunca llama proveedores ni calcula saldos); todo dinero pasa por el
motor y se contabiliza en `ledger` (partida doble, append-only, proyeccion no es fuente de verdad);
idempotencia obligatoria (`Idempotency-Key`); saldo insuficiente -> rechazo sin tocar saldos;
holds/compensacion correctos; anti-enumeracion en lookup; sin PII/secretos en logs; beneficiarios
cifrados; prohibido tocar `kyc-service/`; fronteras por evento/fachada (sin importar modelos
ajenos); cambios de ORM con migracion Alembic y `alembic check` limpio; no borrar pruebas;
umbrales/topes/límites configurables sin despliegue (dueno `shared/config`); un modulo por brief.
**Creditos ademas:** calculos financieros en backend, puros y testeables; el simulador no genera
registro crediticio real; consentimiento explicito de bureau antes de evaluar; dictamen con
justificacion guardada para auditoria; aprobacion separada de la solicitud; bureau con reintentos y
estado `EN_REVISION` si falla; tasas/DTI/topes/score configurables sin despliegue (dueno
`shared/config`).
