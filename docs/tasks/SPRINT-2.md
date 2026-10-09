# Sprint 2 - Indice de briefs (transferencias, beneficiarios, pagos sensibles y ciclo de credito)

> Nota de archivo (2026-09-26; ampliada 2026-09-27): los briefs de Sprint 1 con `Estado: Hecho`
> viven en `docs/tasks/archive/` y se citan aqui por ID, sin cambios. En `docs/tasks/` solo
> estan los briefs no-`Hecho`, incluidos los 11 de este indice aun pendientes; `E2-T07`,
> `E2-T08`, `E2-T09`, `E2-T10`, `E2-T12`, `E2-T13`, `E2-T14`, `E2-T16`, `E2-T17`, `E2-T18`,
> `E2-T19`, `E2-T20`, `E2-T22`, `E2-T24`, `E2-T25` y `E2-T26` ya estan `Hecho` (archivados en
> `docs/tasks/archive/<ID>.md`).
>
> Cierre 2026-09-27: con `E2-T14`/`E2-T19`/`E2-T20`/`E2-T24`/`E2-T25`/`E2-T26` el **bloque
> backend de HU06-HU08 y de operaciones/administracion queda completo**; solo restan el
> frontend (`E2-T11`/`E2-T15`/`E2-T21`) y la QA de cierre de cada HU (`E2-T12`/`E2-T16`/`E2-T22`).
>
> Cierre 2026-10-02: con `E2-T12`/`E2-T16`/`E2-T22` la **QA de HU06-HU08 queda cerrada** y el
> **bloque HU06-HU08 completo (backend + frontend + QA)**; los features frontend ya fueron
> entregados (`E2-T11` commit `a79016f`, `E2-T15` commit `631b90a`, `E2-T21` commit `89610f2`).
> Pendiente en `docs/tasks/`: el bloque de creditos `E3-T01`..`E3-T11` y la deuda de Sprint 1
> `E2-T23` (los briefs frontend de HU06-HU08 aun no se archivan).
>
> Cierre 2026-10-04: las **mejoras de destino** `E2-T27`/`E2-T28`/`E2-T29` quedan `Hecho` y
> archivadas (`docs/tasks/archive/`); commits `03feb2d` (backend: lookup previo + cuenta 14+DV +
> backfill `0024`) y `46f7304` (UI Flutter). Cierran los gaps `G40`/`G41`/`G42` de `docs/18#9`.
> Con ellas, el unico bloque funcional pendiente del sprint es creditos (HU09/HU10) mas la deuda
> `E2-T23`.
>
> Cierre 2026-10-07: el fix `E2-T30` (transferir a un beneficiario de la agenda resuelve su cuenta
> real, no su `beneficiary.id`) queda `Hecho` y archivado (`docs/tasks/archive/E2-T30.md`); commit
> `df85e9f` (backend: resolucion server-side del destino + fachada
> `accounts.service.get_account_by_number`; UI Flutter de agenda). Cierra el gap `G43` de
> `docs/18#9`.
>
> Cierre 2026-10-08: quedan **aplicados** los fixes recientes sobre lo ya `Hecho` de `E2-T30`:
> la **coherencia de config OBS-2** (migracion `0025_biometric_coherence`: umbral `100000` <
> limite diario `500000`, reto alcanzable en `(100000, 500000]`; commit `53fb8bc`), la
> **observabilidad del detalle de validacion `422`** (campos+motivo, sin PII; handler en
> `app/main.py`; commit `846e159`) y la **UX de comprobante/seguimiento** (estados terminales sin
> back; CTA "Volver al inicio" a `/home`; commit `06da4f0`). **OBS-2 queda cerrada** (ver
> `docs/18#9`); siguen **abiertas OBS-3** (CAS del contador de fallos) y **OBS-4** (`nonce` no
> estrictamente de un solo uso).
>
> Cierre 2026-10-08 (E2-T31/E2-T32): la autorizacion por **PIN** del titular (alternativa real a
> la firma en `POST /transactions/{id}/authorize`; commit `d1b7091`, fachada
> `identity.service.pin_authorization.verify_holder_pin`, contrato en `docs/05#6.3`) y el **tope
> pre-aprobado como techo absoluto** de transferencia rapida (commit `d1b7091`; invierte la
> semantica de `E2-T14`) quedan `Hecho` y **archivados**
> (`docs/tasks/archive/E2-T31.md`, `docs/tasks/archive/E2-T32.md`); cierran los gaps `G44`/`G45`
> de `docs/18#9`.
>
> Cierre 2026-10-08 (reconciliacion de papeleo `E2-T11`/`E2-T15`/`E2-T21`/`E2-T23`): los cuatro
> briefs, que seguian `Pendiente` por drift documental pese a estar implementados y commiteados,
> se pasan a `Hecho` y se archivan en `docs/tasks/archive/` (`E2-T11` commit `a79016f`,
> `E2-T15` commit `631b90a`, `E2-T21` commit `89610f2`, `E2-T23` commit `b3c5805`). Con esto, en
> `docs/tasks/` solo restan los 11 briefs de creditos `E3-T01`..`E3-T11`.

Objetivo del sprint: mover dinero a terceros con controles (transferencias propias, a tercero e
interbancarias), gestionar beneficiarios frecuentes y exigir autorizacion biometrica del
dispositivo en pagos sensibles. Ver `docs/14-plan-sprints-y-ejecucion.md#3-sprint-2---transferencias-y-ciclo-de-credito-37-sp`.

Base ya construida (Sprint 1, `Hecho`): motor `E5-T03`/`E5-T04`/`E5-T06`, outbox `E5-T05`,
ledger `E5-T09`..`E5-T14`, cuentas/proyecciones `E2-T01`..`E2-T06`, identidad/KYC/login y
biometria de dispositivo (`F-T03`, `E1-T42`).

Total: **37 briefs** (26 del bloque transferencias/beneficiarios/pagos sensibles + 11 del bloque
creditos `E3-T01`..`E3-T11`): **11 pendientes + 26 `Hecho`** (`E2-T07`/`E2-T08`/`E2-T09`/
`E2-T10`/`E2-T11`/`E2-T12`/`E2-T13`/`E2-T14`/`E2-T15`/`E2-T16`/`E2-T17`/`E2-T18`/`E2-T19`/
`E2-T20`/`E2-T21`/`E2-T22`/`E2-T23`/`E2-T24`/`E2-T25`/`E2-T26`/`E2-T27`/`E2-T28`/`E2-T29`/
`E2-T30`/`E2-T31`/`E2-T32`, archivados).

> Nota de alcance (decision del dueno 2026-09-26): `E2-T07` **implementa la ruta atomica SIN hold
> de transferencias entre cuentas propias** (asiento unico `2000-S -> 2000-D`, sin comision) y
> **corrige la cuenta de comision del motor a `4100-Ingresos por comisiones`** (cierra G05). El
> `hold + settle` se conserva para terceros e interbancaria.

## HU06 - Transferencias propias e interbancarias (Must, 8 SP) - `transactions`
`E2-T07` Casos de uso de transferencia propia y a tercero (**`Hecho`**, archivado
`docs/tasks/archive/E2-T07.md`) - `E2-T08` Validacion de titular/CCI (adaptador mock;
**`Hecho`**, archivado `docs/tasks/archive/E2-T08.md`) - `E2-T09` Saga interbancaria
(estados, timeout, polling, compensacion; **`Hecho`**, archivado
`docs/tasks/archive/E2-T09.md`) - `E2-T10` Comprobante PDF con numero de operacion unico
(**`Hecho`**, archivado `docs/tasks/archive/E2-T10.md`) - `E2-T11` Pantallas Flutter de
transferencia (**`Hecho`**, archivado `docs/tasks/archive/E2-T11.md`) - `E2-T12` QA (feliz,
insuficiente, duplicada, timeout, reverso; **`Hecho`**,
archivado `docs/tasks/archive/E2-T12.md`).

## HU07 - Gestion de beneficiarios frecuentes (Should, 3 SP) - `accounts`
`E2-T13` CRUD de beneficiarios con cifrado y busqueda indexada (**`Hecho`**, archivado
`docs/tasks/archive/E2-T13.md`) - `E2-T14` Tope pre-aprobado integrado al motor (**`Hecho`**,
archivado `docs/tasks/archive/E2-T14.md`) - `E2-T15`
Pantalla de agenda Flutter (busqueda/agrupacion; **`Hecho`**, archivado
`docs/tasks/archive/E2-T15.md`) - `E2-T16` QA (busqueda, tope, edicion;
**`Hecho`**, archivado `docs/tasks/archive/E2-T16.md`).

## HU08 - Autorizacion biometrico-facial de pagos sensibles (Must, 8 SP) - `transactions`/`risk`
`E2-T17` Integracion motor <-> autorizacion biometrica del dispositivo (`nonce` firmado; dueno de
la regla de bloqueo preventivo `CA-02` y del limite diario por perfil `CA-03`; **`Hecho`**,
archivado `docs/tasks/archive/E2-T17.md`) - `E2-T18` Umbral y
perfiles de riesgo configurables sin despliegue (datos/config; **`Hecho`**, archivado
`docs/tasks/archive/E2-T18.md`) - `E2-T19`
`POST /transactions/{id}/authorize` (**`Hecho`**, archivado
`docs/tasks/archive/E2-T19.md`) - `E2-T20` Evento a `risk` + notificacion ante fallo (consumo
de la alerta de bloqueo; **`Hecho`**, archivado `docs/tasks/archive/E2-T20.md`) - `E2-T21` UI
reto biometrico en el flujo de pago (**`Hecho`**, archivado
`docs/tasks/archive/E2-T21.md`) - `E2-T22` QA (umbral,
limite diario, bloqueo; **`Hecho`**, archivado `docs/tasks/archive/E2-T22.md`).

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

## Mejoras de destino (decisiones finales del dueno 2026-10-03) - `transactions`/`identity`/`accounts`/`frontend`

Transferencia a tercero por dos vias, con decisiones finales del dueno:

- **Vía DNI/celular = lookup previo.** No se extiende el body de `POST /transfers/third-party`:
  se agrega un endpoint de **previsualizacion** (`POST /transfers/third-party/resolve-target`, solo
  lectura, sin `Idempotency-Key`) que valida existencia y devuelve **titular enmascarado + referencia
  `target_account_id`**; la transferencia consume esa referencia. Anti-enumeracion: `404
  TARGET_NOT_FOUND` **neutro** (mismo cuerpo si no existe o no hay cuenta `ACTIVE`). Un usuario con
  **varias cuentas `ACTIVE`** no se resuelve solo: **error `409 TARGET_AMBIGUOUS`** (usar el numero
  de cuenta).
- **Numero de cuenta = 14 digitos con digito verificador.** Reemplaza `_new_account_number()`
  (`001-<12 hex>`); algoritmo **modulo 11** (pesos `2..7` de derecha a izquierda; `dv = 11 - (suma
  mod 11)`, `10`/`11` -> `0`). `account_number` sigue `String(20)`.
- **Backfill** de las cuentas existentes (incluida la del dueno `001-01a7124e8ebe`) via **migracion
  Alembic** con numeros unicos de 14 digitos. Verificado en codigo: `/accounts/{id}` usa UUID y
  `movements_view`/`account_ref`/beneficiarios no guardan el numero en claro -> no rompe URLs ni
  proyecciones.
- **`normalize_account_or_cci`** acepta 14 digitos con DV valido (CuyCash) o 20 digitos (CCI);
  se retira el rango laxo `8..20`.

Insumos verificados: `_new_account_number()` genera hex `001-<12 hex>`
(`identity/service/__init__.py:58-59`) y el formulario de beneficiarios usa `_digits()` y valida
`8..20` con `TextInputType.number` (`beneficiary_form_page.dart:148,181-183,218,497`), por lo que el
hex rompe la UI.

`E2-T27` Lookup previo de destino por DNI/celular (anti-enumeracion; **`Hecho`**, archivado
`docs/tasks/archive/E2-T27.md`) - `E2-T28` Numero de cuenta de 14 digitos con DV +
migracion/backfill (`0024`; **`Hecho`**, archivado `docs/tasks/archive/E2-T28.md`) - `E2-T29` UI:
cuenta de 14 digitos (DV) y destino por DNI/celular con lookup previo (**`Hecho`**, archivado
`docs/tasks/archive/E2-T29.md`).

`E2-T30` Fix: transferir a un beneficiario de la agenda resuelve su cuenta real (no su
`beneficiary.id`); `POST /transfers/third-party` resuelve el destino server-side (`target_account_id`
opcional + `beneficiary_id`; **`Hecho`**, archivado `docs/tasks/archive/E2-T30.md`).

> Dependencias: `E2-T27` y `E2-T28` son independientes entre si; `E2-T29` (UI) depende de ambos.
> `E2-T27` depende de `E2-T07`/`E2-T13` (ambos `Hecho`); `E2-T28` depende de `E2-T01`/`E2-T02`
> (ambos `Hecho`); `E2-T30` depende de `E2-T07`/`E2-T13`/`E2-T14`/`E2-T27`/`E2-T29` (todos `Hecho`).
> Gaps registrados/actualizados en `docs/18#9` (G40/G41/G42 y G43).

## Autorizacion por PIN y tope como techo (decisiones 2026-10-08) - `transactions`/`identity`/`frontend`

`E2-T31` Autorizar un pago sensible por PIN (alternativa real a la firma biometrica: campo `pin`
mutuamente excluyente en `POST /transactions/{id}/authorize` + fachada
`identity.service.pin_authorization.verify_holder_pin`; **`Hecho`**, archivado
`docs/tasks/archive/E2-T31.md`) - `E2-T32` Tope pre-aprobado por beneficiario como techo absoluto
(exime por encima del umbral global; **`Hecho`**, archivado `docs/tasks/archive/E2-T32.md`).

> Dependencias: `E2-T31` depende de `E2-T19`/`E2-T17`/`E2-T21`/`E1-T14`; `E2-T32` depende de
> `E2-T14`/`E2-T17`/`E2-T18`/`E2-T30`/`E2-T15` (todas `Hecho`). Gaps `G44`/`G45` registrados en
> `docs/18#9`. `E2-T32` **cambia la semantica** de `E2-T14`: el tope ya no queda supeditado al
> umbral global (es un techo por operacion; el limite diario y los bloqueos nunca se saltan).

## Retiro real del tope pre-aprobado (decision del dueno 2026-10-08) - `accounts`/`transactions`/`frontend`

`E2-T33` Retiro real del tope pre-aprobado de beneficiarios: la columna/`CHECK` se eliminan por
la migracion `0026`, la rama de exencion sale del motor (el unico gate es umbral + limite
diario), el contrato ignora el campo historico (sin `422`) y la agenda deja de distinguir por
tope (**`Hecho`**, brief `docs/tasks/E2-T33.md`).

> Dependencias: `E2-T13`, `E2-T14`, `E2-T17`, `E2-T18`, `E2-T30`, `E2-T32` (todas `Hecho`).
> Gap `G46` registrado en `docs/18#9` (`G37`/`G45` superados; `CA-05` de HU07 de `E2-T32`
> **ANULADO** y `CA-03` de HU07 (tope) **RETIRADO**).

## Umbral biometrico por operacion (bug del dueno 2026-10-09) - `transactions`

`E2-T34` Umbral biometrico por operacion (no por acumulado del dia): el gate decide con
`total = amount_minor + fee_minor` (`requires_biometry = (not limit_exceeded) and
(total > threshold)`); el limite diario SIGUE acumulado y el acumulador excluye ademas
`PENDING_AUTHORIZATION` y `REVERSED` (brief `docs/tasks/E2-T34.md`).

> Dependencias: `E2-T17`, `E2-T18`, `E2-T19`, `E2-T31`, `E2-T33` (todas `Hecho`).
> Sin migracion; valores `100000`/`500000` intactos. La semantica acumulada del umbral de
> `E2-T17` queda superada (nota al inicio de `docs/tasks/archive/E2-T17.md`).

## Cierre del bypass del limite diario (correccion del dueno 2026-10-09) - `transactions`

`E2-T36` Cerrar el bypass del limite diario: contar `PENDING_AUTHORIZATION` en el acumulado
(`EXCLUDED_ACCUMULATED_STATUSES = (REJECTED, FAILED, REVERSED)`) + re-chequear `acumulado + total >
daily_limit` en `PENDING_AUTHORIZATION -> AUTHORIZED` (firma/PIN/interbancaria), rechazando
`REJECTED`/`DAILY_LIMIT_EXCEEDED` sin mover dinero y liberando el hold (`HOLD_RELEASE` existente)
(brief `docs/tasks/E2-T36.md`).

> Dependencias: `E2-T34`, `E2-T17`, `E2-T18`, `E2-T19`, `E2-T31`, `E2-T33` (todas `Hecho`).
> Sin migracion; valores `100000`/`500000` intactos. Gap `G48` registrado en `docs/18#9`.

## Movimientos reales en home e historial (bug del dueno 2026-10-09) - `accounts`/`ledger`/`frontend`

`E2-T35` Concepto contable `entry_type` en `ledger.entry.posted` (solo payload),
columna `accounts.movements_view.entry_type` (migracion `0027_movements_entry_type`),
regla de atribucion por `ledger_account_id` (las patas de sistema/transito
`2200`/`1000`/`4100` no se proyectan al cliente), `entry_type` en
`GET /accounts/{id}/movements` y presentacion Flutter (mapper central,
preview con filtro/paginacion/refresh + historial sin UUIDs; brief
`docs/tasks/E2-T35.md`).

> Dependencias: `E2-T03`, `E2-T04`, `E2-T23`, `E2-T24`, `E2-T11`, `E2-T25` (todas `Hecho`).
> Sin backfill: las filas ya proyectadas quedan `entry_type = NULL` (fallback en
> cliente); el motor de dinero no cambia (solo la clave `entry_type` del payload).

## Operaciones y administracion (Must, soporte HU06/HU08/HU17) - `transactions`/`admin`
`E2-T24` `GET /transactions` (listado paginado de movimientos del titular, filtros por
fecha/tipo/estado; enmascaramiento; sin PII; **`Hecho`**, archivado
`docs/tasks/archive/E2-T24.md`) - `E2-T25` `POST /transactions/{id}/reverse`
(reverso/compensacion con asiento inverso, idempotente, solo estados permitidos; **`Hecho`**,
archivado `docs/tasks/archive/E2-T25.md`) - `E2-T26`
parametros administrables `GET/PUT /admin/parameters` (fachada sobre `config.parameters`; rol
admin; **`Hecho`**, archivado `docs/tasks/archive/E2-T26.md`).

## Deuda de Sprint 1 - `accounts`/`ledger`/`shared`
`E2-T23` Cableado en produccion del consumidor de `movements_view` (`ledger.entry.posted`): el
consumidor `handle_movement_event` ya existe (`E2-T04`, `Hecho`) pero no estaba suscrito al
bus/outbox en produccion (`docs/17#6`). Depende de `E2-T04` y `E5-T05`. **`Hecho`**, archivado
`docs/tasks/archive/E2-T23.md` (commit `b3c5805`).

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
   evidencia contable para todo el sprint; **`Hecho`**).
2. `E2-T07` (casos de uso base sobre el motor; **`Hecho`**) y `E2-T08` (adaptador
   titular/CCI; **`Hecho`**).
3. `E2-T09` (saga interbancaria; **`Hecho`**) y `E2-T10` (comprobante; **`Hecho`**).
4. `E2-T13` (beneficiarios; **`Hecho`**) y `E2-T18` (parametros/perfiles; **`Hecho`**) en
   paralelo.
5. `E2-T17` (integracion biometrica; incluye bloqueo `CA-02` y limite diario `CA-03`;
   **`Hecho`**), `E2-T19` (endpoint authorize; **`Hecho`**), `E2-T14` (tope pre-aprobado;
   **`Hecho`**), `E2-T20` (evento a `risk` + notificacion; **`Hecho`**), y luego `E2-T31`
   (authorize por PIN; **`Hecho`**) y `E2-T32` (tope como techo absoluto; **`Hecho`**), que
   cierran `G44`/`G45`.
6. `E2-T24` (listado; **`Hecho`**), `E2-T25` (reverso; **`Hecho`**) y `E2-T26` (parametros admin;
   **`Hecho`**) sobre `E2-T07`/`E2-T18`.
7. `E2-T11`, `E2-T15`, `E2-T21` (frontend; **`Hecho`**, archivados).
8. `E2-T12`, `E2-T16`, `E2-T22` (QA al cierre de cada HU; **`Hecho`**, archivados).
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
- `E2-T07` antes de `E2-T09` (saga) y de `E2-T10` (comprobante). `E2-T09` y `E2-T10`
  **`Hecho`**.
- `E2-T08` antes de `E2-T09` (validar CCI/titular antes de enviar). `E2-T08` **`Hecho`**.
- `E2-T13` antes de `E2-T14` (tope) y de `E2-T15` (agenda). `E2-T13` **`Hecho`**; `E2-T14`
  **`Hecho`**; `E2-T15` **`Hecho`**.
- `E2-T18` antes de `E2-T17`/`E2-T19`/`E2-T14` (umbral/límites configurables). `E2-T18`
  **`Hecho`**.
- `E2-T17` antes de `E2-T19`; `E2-T19` antes de `E2-T20` y `E2-T21`. `E2-T17` **`Hecho`**;
  `E2-T19`, `E2-T20` y `E2-T21` **`Hecho`**.
- `E2-T07` antes de `E2-T24` (listado) y de `E2-T25` (reverso). `E2-T24` y `E2-T25` **`Hecho`**.
- `E2-T18` (archivado) antes de `E2-T26` (parametros admin); `E2-T26` es la fachada de exposicion,
  no el dueno del esquema (`shared/config`). `E2-T26` **`Hecho`**.
- `E2-T04` + `E5-T05` antes de `E2-T23` (proyeccion en produccion). `E2-T23` **`Hecho`**.
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
