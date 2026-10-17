# Sprint 3 - Indice de briefs (creditos avanzados, billetera, QR y notificaciones)

> **Estado del indice (2026-10-14):** **Sprint 3 cerrado: los 38 briefs del sprint estan `Hecho` y
> validados** (32 en este indice + los 6 del adelanto HU11/`E3-T23`, ya archivados). El adelanto
> (`E3-T12`..`E3-T16` + `E3-T23`, firma de contrato y "Mis creditos") quedo `Hecho` y archivado en
> `docs/tasks/archive/`. **HU12-HU16 `Hecho` y validados** (`E3-T17`..`E3-T22` + prerequisito
> `E5-T23`; `E4-T01`..`E4-T20`, modulo `wallet`; QA sin defectos; ver `docs/18`): HU15 cierra el
> **cobro QR** E2E (`POST /payments/qr`, cierra el hueco `G72`) y HU16 habilita **servicios y
> recargas**. **HU22 `Hecho` y validado** (`E6-T12`..`E6-T16` + fixes A1/A2/A3/B2/B3/B4): backend de
> notificaciones de seguridad + **centro de notificaciones** Flutter (`E6-T15`) y QA de cierre
> (`E6-T16`). Base construida en Sprints 1-2 (`Hecho`): motor/ledger (`E5-*`), cuentas
> (`E2-T01`..`E2-T06`), transferencias/beneficiarios/pagos sensibles (`E2-T07`..`E2-T22`) y el ciclo
> de credito HU09/HU10 (`E3-T01`..`E3-T11`). Ver "Cierre del Sprint 3" abajo y
> `docs/14-plan-sprints-y-ejecucion.md#4-sprint-3---creditos-avanzados-billetera-y-qr-39-sp`.

Objetivo del sprint: cerrar el ciclo crediticio (firma -> desembolso -> cuotas) y habilitar pagos
digitales (billetera, QR, servicios) y notificaciones de seguridad.

Alcance (`docs/14#4`): **HU11** (8 SP, `credits`), **HU12** (5 SP, `credits`), **HU13** (8 SP,
`wallet`), **HU14** (5 SP, `wallet`), **HU15** (5 SP, `wallet`), **HU16** (5 SP, `wallet`),
**HU22** (3 SP, `notifications`) = **39 SP**.

Total briefs de este indice: **32**, **todos `Hecho`** (`E3-T17`..`E3-T22` + el prerequisito
`E5-T23`; `E4-T01`..`E4-T20` de HU13-HU16; y `E6-T12`..`E6-T16` de HU22). Sumando el adelanto de HU11
(`E3-T12`..`E3-T16`) y `E3-T23`, el **Sprint 3 completo son 38 briefs, todos `Hecho`** (los 6 del
adelanto ya en `docs/tasks/archive/`).

## Cierre del Sprint 3 (2026-10-14)

**Alcance (`docs/14#4`):** Sprint 3 = **HU11/HU12** (creditos: firma -> desembolso -> cuotas),
**HU13-HU16** (billetera, QR, servicios y recargas) y **HU22** (notificaciones de seguridad
multicanal) = **39 SP** (HU11 8 + HU12 5 + HU13 8 + HU14 5 + HU15 5 + HU16 5 + HU22 3).

### Resumen por HU

| HU | Descripcion | Tareas | Estado |
|---|---|---|---|
| HU11 | Firma digital de contrato (adelanto) | `E3-T12`..`E3-T16` | `Hecho` (archivados; cerrados en el Sprint 2) |
| HU10/HU11 | "Mis creditos" (adelanto) | `E3-T23` | `Hecho` (archivado) |
| HU12 | Desembolso inmediato y programacion de cuotas | `E3-T17`..`E3-T22` + `E5-T23` | `Hecho` |
| HU13 | Billetera digital e interoperabilidad | `E4-T01`..`E4-T05` | `Hecho` |
| HU14 | Generacion y cobro con QR dinamico/estatico | `E4-T06`..`E4-T10` | `Hecho` |
| HU15 | Lectura y confirmacion de pago QR | `E4-T11`..`E4-T15` | `Hecho` |
| HU16 | Pago de servicios y recargas express | `E4-T16`..`E4-T20` | `Hecho` |
| HU22 | Notificaciones de seguridad multicanal | `E6-T12`..`E6-T16` | `Hecho` |

### Estado de suites (al cierre)

- **Backend:** `~2040 passed + 1 skipped`; cobertura `~86%`; `ruff`/`black` en verde.
- **Frontend:** `~1042` pruebas (`flutter test`); `flutter analyze` limpio.
- **Migraciones:** un solo head Alembic **`0044_notifications_preferences`**; `alembic check` limpio.

### Commit del Sprint 3

Toda la implementacion del Sprint 3 vive en la rama **`sprint-3`** y se consolida en commits tematicos
(HU12/HU13/HU14/HU15/HU16/HU22 + infra transversal), con la autorizacion del dueno. Incluye los fixes
aplicados sobre briefs ya `Hecho` (HU12: guarda de cobro QR `0041`; HU13/HU14/HU15:
ALTO-1/ALTO-2/MEDIO-1/2/3 + N-1/R-1; HU16: H1/H2/H3; HU22: A1/A2/A3/B2/B3/B4/B5).

### Archivado de briefs

El protocolo (`docs/tasks/README.md#1`) deja en `docs/tasks/` **solo briefs no-`Hecho`** y mueve los
`Hecho` verificados a `docs/tasks/archive/` (precedente: `SPRINT-2.md#cierre-del-sprint-2-2026-10-11`).
Con los 32 briefs de este indice ya `Hecho`, se **archivaron** los 32
(`E3-T17`..`E3-T22`, `E5-T23`, `E4-T01`..`E4-T20`, `E6-T12`..`E6-T16`) a `docs/tasks/archive/<ID>.md`,
verificando `Estado: Hecho` en cada brief antes de mover y actualizando sus enlaces en este indice.

## Adelanto de Sprint 3 ya `Hecho` (HU11 + E3-T23)

| Tarea | HU | Modulo | Estado | Brief / evidencia |
|---|---|---|---|---|
| `E3-T12` Generacion de documentos del contrato | HU11 | `credits` | **Hecho** | `docs/tasks/archive/E3-T12.md`; commit `48667cd` |
| `E3-T13` Servicio de firma (hash + token + sello) | HU11 | `credits` | **Hecho** | `docs/tasks/archive/E3-T13.md`; commit `1e0eae3` |
| `E3-T14` Endpoint de firma + copia por correo | HU11 | `credits` | **Hecho** | `docs/tasks/archive/E3-T14.md`; commit `1e0eae3` |
| `E3-T15` UI lectura/firma del contrato | HU11 | `frontend` | **Hecho** | `docs/tasks/archive/E3-T15.md`; commit `20fca8c` |
| `E3-T16` QA integridad/no reutilizacion | HU11 | `qa` | **Hecho** | `docs/tasks/archive/E3-T16.md`; commit `20fca8c` |
| `E3-T23` "Mis créditos" + dos secciones en `/credits` | HU10/HU11 | `credits` + `frontend` | **Hecho** | `docs/tasks/archive/E3-T23.md`; commits `dd3d6fe`/`13cdeca`/`b64e763` |

> HU11 y `E3-T23` se implementaron como adelanto del Sprint 3 y se cerraron documentalmente en el
> Sprint 2 (2026-10-11/12; ver `docs/tasks/SPRINT-2.md#cierre-del-sprint-2-2026-10-11`). **No mover
> dinero:** `E3-T12`..`E3-T16` dejaron el juego contractual (`credits.contracts`) `SIGNED`; el
> desembolso es HU12. Gaps asociados: `docs/18` G53-G59.

## HU12 - Desembolso inmediato y programacion de cuotas (Must, 5 SP) - `credits`

**Logica** (`docs/08#hu12---desembolso-inmediato-y-programacion-de-cuotas-must-5-sp-sprint-3`):
con el contrato firmado se ordena el desembolso a la cuenta elegida (asiento `docs/04#7.5`), se
genera el **cronograma definitivo**, el cliente puede habilitar **debito automatico** y se notifica
cada cuota proxima a vencer (2-3 dias antes).

**Reglas:** desembolso < 30 s tras la firma; operacion **todo o nada**; el pago de cuota tambien
pasa por el motor (asiento `docs/04#7.6`).
**CA:** CA-01 (desembolso), CA-02 (cronograma), CA-03 (debito automatico), CA-04 (recordatorio).

| Tarea | Descripcion | Tipo | Brief | Estado |
|---|---|---|---|---|
| `E3-T17` | Caso de uso de desembolso transaccional (`credits` -> `transactions` -> `ledger`); crea `loans` + cronograma y pobla `contracts.loan_id` | `motor-ledger` | `docs/tasks/archive/E3-T17.md` | `Hecho` |
| `E3-T18` | Esquema/persistencia `loans`/`loan_schedules`/`loan_payments` + generacion del cronograma definitivo | `datos` | `docs/tasks/archive/E3-T18.md` | `Hecho` |
| `E3-T19` | Job de debito automatico y recordatorios (+ toggle `PUT .../auto-debit`) | `backend` (jobs) | `docs/tasks/archive/E3-T19.md` | `Hecho` |
| `E3-T20` | Endpoint de pago de cuota y aplicacion contable (`docs/04#7.6`) | `motor-ledger` | `docs/tasks/archive/E3-T20.md` | `Hecho` |
| `E3-T21` | UI Flutter: cronograma y activacion de debito automatico | `frontend` | `docs/tasks/archive/E3-T21.md` | `Hecho` |
| `E3-T22` | QA: desembolso atomico, cuota vencida, debito automatico | `qa` | `docs/tasks/archive/E3-T22.md` | `Hecho` |
| `E5-T23` | **Prerequisito HU12** (modulo `ledger`): corrige el catalogo contable `4200`/`5000` + migracion `0035_ledger_catalog_fix` (cierra `docs/18` G60) | `datos` | `docs/tasks/archive/E5-T23.md` | `Hecho` |

> **Fronteras (HU12):** `credits` crea `loans`/`loan_schedules`/`loan_payments` **pero no** escribe
> tablas de `transactions`/`ledger`; mueve dinero por una **fachada aditiva del motor**
> (`transactions/service/`), que a su vez usa la fachada de `ledger` (`post_entry`). Un modulo por
> brief; la fachada del motor es el punto de contacto.

> **Gaps de HU12 registrados en `docs/18#9`:** **G60** (catalogo contable: `5000` debe ser `asset`
> "Cartera de prestamos" y `4200` "Ingresos por seguro" — BLOQUEANTE para `E3-T17`/`E3-T20`;
> lo resuelve el brief **`E5-T23`** de `ledger`),
> **G61** (`loans.auto_debit_enabled`, columna aditiva), **G62** (`docs/05#6.4` sin el detalle de los
> endpoints de HU12), **G63** (sin mockup/spec de las pantallas de HU12), **G64** (plantillas de
> notificacion de recordatorio/fallo de debito automatico).

## HU13 - Billetera Digital e Interoperabilidad (Must, 8 SP) - `wallet`

**Logica** (`docs/09#hu13---billetera-digital-e-interoperabilidad-must-8-sp-sprint-3`): el cliente
vincula una cuenta/tarjeta como fuente de fondos (**token**, nunca el numero completo); los pagos
**express** se procesan sin clave bajo el **limite base**; la billetera es **interoperable** con las
redes del alcance; se aplican **limites diarios** configurables (al superarlos, seguridad reforzada).

**Reglas:** pago < 3 s; datos tokenizados; limites por usuario/dispositivo.
**CA:** CA-01 (vincular), CA-02 (express sin clave), CA-03 (interoperabilidad), CA-04 (limite diario).

| Tarea | Descripcion | Tipo | Brief | Estado |
|---|---|---|---|---|
| `E4-T01` | Modelo/CRUD `wallets` + `wallet_links` con tokenizacion (nunca PAN); migracion `0037_wallet`; `GET /wallet` y `POST /wallet/links` | `dominio` | `docs/tasks/archive/E4-T01.md` | `Hecho` |
| `E4-T02` | Servicio de pago express (usa el motor) con regla de limite base + limite diario (params `wallet.*`) | `motor-ledger` | `docs/tasks/archive/E4-T02.md` | `Hecho` |
| `E4-T03` | Adaptador de interoperabilidad (mock/contrato con el equipo par; `D06`/`R08`) | `adaptador` | `docs/tasks/archive/E4-T03.md` | `Hecho` |
| `E4-T04` | UI Flutter: billetera y vinculacion de fuente de fondos | `frontend` | `docs/tasks/archive/E4-T04.md` | `Hecho` |
| `E4-T05` | QA: limites (base/diario) y tokenizacion (no PAN) | `qa` | `docs/tasks/archive/E4-T05.md` | `Hecho` |

> **Fronteras (HU13):** `wallet` es dueno de `wallets`/`wallet_links` **pero no** escribe tablas de
> `transactions`/`ledger`/`accounts`/`identity`/`risk`/`notifications`; el dinero pasa por el **motor**
> (`wallet` puede llamar a `transactions`). La cuenta enlazada se valida por la **fachada** de
> `accounts`; nunca se guarda PAN (solo `token_ref`/`last4`).

> **Gaps de HU13 (`docs/18#9`):** **G66** (`docs/05#6.5` sin el detalle de `/wallet*`; sin endpoint de
> pago express), **G67** (sin proveedor de tokenizacion), **G70** (limites `wallet.*` no existen en
> `config.parameters`), **G71** (sin mockups/spec de las pantallas de billetera).

## HU14 - Generacion y Cobro con QR Dinamico/Estatico (Must, 5 SP) - `wallet`

**Logica** (`docs/09#hu14---generacion-y-cobro-con-qr-dinamicoestatico-must-5-sp-sprint-3`): el comercio
ingresa monto y concepto; se genera un **QR dinamico con firma/hash y vencimiento (p. ej. 5 min)**; al
vencer se **invalida**; el QR respeta un **formato interoperable**; al confirmarse el pago se
**notifica al comercio** de inmediato.

**Reglas:** generacion < 1 s; notificacion de abono < 5 s; la firma impide manipulacion.
**CA:** CA-01 (QR generado), CA-02 (caducidad), CA-03 (interoperable), CA-04 (notificacion).

| Tarea | Descripcion | Tipo | Brief | Estado |
|---|---|---|---|---|
| `E4-T06` | Servicio de generacion de QR firmado (payload canonico + hash/firma + `expires_at`) + `merchant_accounts`/`qr_charges` (migracion `0039_wallet_qr`) | `dominio` | `docs/tasks/archive/E4-T06.md` | `Hecho` |
| `E4-T07` | Job + validacion de caducidad (`run_qr_expiry_once`; on-read; wiring runtime) | `backend` (jobs) | `docs/tasks/archive/E4-T07.md` | `Hecho` |
| `E4-T08` | Endpoint `POST /merchants/qr` + `GET /merchants/qr/{id}` (estado) + notificacion de abono | `backend` | `docs/tasks/archive/E4-T08.md` | `Hecho` |
| `E4-T09` | UI Flutter de comercio: generar QR y ver cobros | `frontend` | `docs/tasks/archive/E4-T09.md` | `Hecho` |
| `E4-T10` | QA: QR vencido, firma alterada, notificacion de abono | `qa` | `docs/tasks/archive/E4-T10.md` | `Hecho` |

> **Fronteras (HU14):** `wallet` genera/firma el QR y persiste `merchant_accounts`/`qr_charges`; **no**
> mueve dinero (el pago es HU15 via el motor, asiento `docs/04#7.4`). El aviso al comercio se emite por
> la fachada `notifications.send(...)` (sin PII).

> **Gaps de HU14 (`docs/18#9`):** **G68** (formato canonico/algoritmo de firma del QR; `D06`/`R08`),
> **G69** (sin alta de `merchant_accounts` en el alcance), **G71** (sin mockups/spec de las pantallas de
> comercio), **G72** (la notificacion de abono depende del pago de HU15 / `qr.payment.confirmed`).

## HU14 (incremento post-cierre) - QR personal estatico (Must, `wallet` + `frontend`)

**Requisito del dueno (nuevo):** la generacion de QR no debe ser **unica para negocios** sino tambien
para **personas**, y debe ser **estatico**. Hoy `POST /merchants/qr` exige rol de comercio
(`require_merchant_for_user` -> `403`) y el modo `STATIC` aun exige monto; `pay_qr_charge` abona a la
cuenta del comercio. Falta un **QR personal** (cualquier titular) y **estatico** (sin monto,
reutilizable).

**Logica:** cualquier titular `ACTIVE` genera un QR personal que **recibe dinero en su propia cuenta**
(sin rol comercial); el payload es `qr_content` (`BO-QR1`) escaneable; el QR es **estatico** (sin monto
—lo digita el pagador— y sin caducidad, reutilizable); el pago (`POST /payments/qr`) abona a la cuenta
del titular personal via el motor.

| Tarea | Descripcion | Tipo | Brief | Estado |
|---|---|---|---|---|
| `E4-T21` | **QR personal estatico:** endpoint `POST /api/v1/wallet/qr` (+ `GET /wallet/qr/{id}`); `amount_minor` nullable + destino personal (`payee_type`/`owner_user_id`/`payee_account_id`) y `reusable` en `qr_charges`; guarda de doble cobro acotada a `reusable = false` en `qr_payments`; pago sin monto (pagador aporta `amount_minor`) y abono a la cuenta del titular personal; migracion `0047_wallet_personal_qr` (tras `0046_services_settlement`; correccion de numeracion: `0045`/`0046` ya tomadas); UI Flutter "Mi QR personal" + reencuadre del copy "Soy comercio" en `wallet_page.dart` | `dominio` + `datos` + `frontend` | `docs/tasks/E4-T21.md` | `Hecho` |

> **Fronteras (E4-T21):** `wallet` es dueno de `qr_charges`/`qr_payments` y genera/valida el QR, pero
> **no** mueve dinero por su cuenta: usa la fachada `pay_express` (`E4-T02` -> `transactions` ->
> `ledger`, asiento `docs/04#7.4`) y resuelve la cuenta de abono por la fachada `accounts`; el aviso
> va por `notifications.send(...)`. El frontend feature `wallet` solo captura/muestra (cliente delgado).

> **Cambio de esquema (E4-T21):** `qr_charges.merchant_id` y `amount_minor` pasan a **nullable**
> (`CHECK amount_minor IS NULL OR > 0`) + columnas `payee_type`/`owner_user_id`/`payee_account_id`/
> `reusable`; `qr_payments.merchant_id` nullable + `payee_user_id`/`reusable`; el indice unico parcial
> `uq_qr_payments_charge_active` se recrea con predicado `status <> 'REJECTED' AND reusable = false`.
> **Gaps de HU14/E4-T21 (`docs/18#9`, tras `G83`):** **G84** (semantica del QR estatico sin monto en
> `BO-QR1`/`qr_charges`), **G85** (destino personal + guarda de doble cobro vs QR reutilizable),
> **G86** (decision de esquema/migracion `0047` y ownership del endpoint `/wallet/qr`; `Hecho`).

> **Dependencias (`E4-T06`/`E4-T08`/`E4-T11`/`E4-T12` `Hecho`):** reutiliza la firma/`qr_charges`
> (`E4-T06`), el endpoint/estado de comercio (`E4-T08`), el cobro por el motor (`E4-T11`) y el endpoint
> `POST /payments/qr` + `preview` (`E4-T12`); concurrencia critica `wallet`/`ledger` (un modulo de dinero
> a la vez). Decisiones del dueno adoptadas (`Hecho`): personal sin rol comercial, estatico sin
> monto/caducidad y reutilizable, esquema de menor impacto, endpoint aditivo `POST /wallet/qr`,
> limite diario de billetera (sin topes nuevos) y plantilla `qr_charge_paid` reutilizada.

## Incremento post-cierre (2026-10-16) - correcciones de la prueba del dueno (`wallet`)

**Requisito del dueno (2 problemas reportados al probar):** (1) el **escaner QR de la app es un
placeholder** (no lee con camara real); (2) **"Cobrar QR como negocio" aparece en cuentas personales**
(la entrada de comercio se muestra siempre). Ambas son correcciones sobre HU13/HU14/HU15 ya `Hecho`;
**ninguna mueve dinero**.

| Tarea | Descripcion | Tipo | Brief | Estado |
|---|---|---|---|---|
| `E4-T22` | **Camara real de escaneo QR:** agrega `mobile_scanner` y sustituye el placeholder `qr-pay-camera-placeholder` por `MobileScanner`, mapeando `barcode.rawValue` -> `_continue(qr_content)` -> `/wallet/pay-qr/confirm`; conserva el `TextField` `qr-pay-scan-input` como fallback; permiso iOS `NSCameraUsageDescription` (Android ya declara `CAMERA`); degradacion si se niega el permiso | `frontend` | `docs/tasks/E4-T22.md` | `Hecho` |
| `E4-T23` | **Ocultar comercio segun rol:** `GET /wallet` expone `is_merchant` (y opcional `merchant_status`) derivado de `get_merchant_by_user`; el frontend lo parsea en `models/wallet.dart` y **oculta** la entrada de comercio (`wallet-merchant-qr-action`/`-empty`, `qa-qr`) cuando `!isMerchant`. Solo lectura | `dominio` (lectura) + `frontend` | `docs/tasks/E4-T23.md` | `Hecho` |

> **Fronteras (incremento post-cierre):** `E4-T22` es **frontend-only** (`frontend/lib/features/wallet/**`,
> `pubspec.yaml`, iOS `Info.plist`, Android manifest; sin backend). `E4-T23` cambia el contrato de
> **lectura** `GET /wallet` (aditivo, sin migracion) y toca `backend/app/modules/wallet/**` +
> `frontend/lib/features/wallet/**` + `frontend/lib/features/home/**`. Ninguna escribe tablas de
> `transactions`/`ledger`/`accounts` ni mueve dinero; `POST /merchants/qr*` (y su `403`) no se toca.

> **Dependencias (incremento post-cierre):** `E4-T22` reutiliza el seam `onScanned`/`initialContent` y el
> flujo de pago de `E4-T13`/`E4-T14`/`E4-T12` (`Hecho`); `E4-T23` reutiliza `get_wallet_view` (`E4-T01`,
> `Hecho`) y `require_merchant_for_user`/`get_merchant_by_user` (`E4-T08`, `Hecho`); la lectura del
> `qr_content` del escaner tambien sirve al QR personal (`E4-T21`, `Hecho`).

> **Gaps del incremento (`docs/18#9`, continuando tras `G87`):** **G88** (`GET /wallet` no exponia el rol
> de comercio; lo agrega `E4-T23`) y **G89** (dependencia nativa de camara: `mobile_scanner` + permisos;
> `E4-T22`).

## Incremento post-cierre (2026-10-16) - feedback de UX del dueno (`credits` + `wallet`)

**Requisito del dueno (3 mejoras de UX pedidas al probar):** (1) que el **desembolso sea automatico al
firmar** (hoy es manual) y se **notifique** al titular; (2) **nombre del dueno del QR enmascarado**
(primeros 4 caracteres) en el QR personal y en el resumen de pago; (3) **botones de retroceso** en las
pantallas que realmente los necesiten. Son mejoras sobre HU12 (`credits`) y HU13/HU14 (`wallet`) ya
`Hecho`; **solo (1) mueve dinero**, y reusa el desembolso idempotente existente.

| Tarea | Descripcion | Tipo | Brief | Estado |
|---|---|---|---|---|
| `E3-T24` | **Desembolso automatico al firmar + notificacion:** inline en el flujo de firma (`contract_signing._try_auto_disburse` en `savepoint`, decision del dueno) que ejecuta `disburse_application_idempotent` a la **cuenta principal** con clave estable `auto-disburse:<application_id>` (un prestamo por expediente) y devuelve el bloque aditivo `disbursement` en la respuesta de la firma; notificacion dedicada `credit_disbursed` (push+email) como UNICA (generica suprimida para `credit.disbursed`); UI de confirmacion (monto + cuenta enmascarada) con foco al cronograma y CTA manual solo como **reintento**. Sin migracion | `backend` (firma+desembolso) + `frontend` | `docs/tasks/E3-T24.md` | `Hecho` |
| `E4-T24` | **Nombre del dueno del QR enmascarado:** campo `payee_name_masked` (`DIEG••• GONZ•••`, primeros 4 + resto oculto) en `GET /wallet/qr/{id}` y `POST /payments/qr/preview`, resuelto por fachada de `identity` (solo lectura); UI en "Mi QR personal" (dueno) y resumen de pago (pagador). Sin migracion | `dominio` (lectura) + `frontend` | `docs/tasks/E4-T24.md` | `Hecho` |
| `E4-T25` | **Botones de retroceso (UX):** inventario (archivo:pantalla) de las pantallas que carecen de back + back funcional (`pop`) solo donde se requiere; respeta raiz/tab y terminales/de exito; sin botones redundantes a Home | `frontend` | `docs/tasks/E4-T25.md` | `Hecho` |

> **Fronteras (feedback UX):** `E3-T24` toca `backend/app/modules/credits/**` (+ consumo de
> `notifications` por fachada, `composition` y plantillas aditivas) y `frontend/lib/features/credits/**`;
> su unico contacto con el dinero es el caso de uso idempotente existente (`transactions` -> `ledger`
> por fachada). `E4-T24` cambia el contrato de **lectura** de `GET /wallet/qr/{id}` y
> `POST /payments/qr/preview` (aditivo, sin migracion) y toca `backend/app/modules/wallet/**` +
> `frontend/lib/features/wallet/**`. `E4-T25` es **frontend-only** (`frontend/lib/features/**`,
> presentacion). Ninguna escribe tablas de `transactions`/`ledger`/`accounts`/`identity`/`notifications`.

> **Dependencias (feedback UX):** `E3-T24` depende de `E3-T14` (firma + evento `credit.contract.signed`),
> `E3-T17` (caso de uso de desembolso) y `E3-T21` (UI cronograma), todos `Hecho`; reutiliza la infra de
> `notifications` (`E6-T12`/`E6-T13`, `Hecho`). `E4-T24` depende de `E4-T21` (QR personal) y `E4-T13`
> (resumen de pago), mas la fachada `identity.service.profile.get_profile` (`E1-T44`, `Hecho`).
> `E4-T25` no tiene dependencias tecnicas.

> **Gaps del incremento (`docs/18#9`, continuando tras `G89`):** **G90** (desembolso automatico al
> firmar: mecanismo consumidor vs inline + notificacion dedicada vs alerta de seguridad; `E3-T24`),
> **G91** (`payee_name_masked` + formato de mascara + fachada de nombre; `E4-T24`) y **G92** (pantallas
> sin boton de retroceso y criterio de decision; `E4-T25`).

## HU15 - Lectura y Confirmacion de Pago QR (Must, 5 SP) - `wallet`

**Logica** (`docs/09#hu15---lectura-y-confirmacion-de-pago-qr-must-5-sp-sprint-3`): el cliente escanea el
QR; el sistema lo **decodifica** y muestra **comercio y monto** antes de confirmar; si el monto esta
dentro del **limite base**, pago en **un toque**; si lo supera, se exige **biometria adicional**; si el QR
esta **vencido/corrupto/manipulado**, se rechaza **sin descontar fondos** y se alerta.

**Reglas:** lectura/validacion < 2 s; valida firma/hash del QR; el pago pasa por el motor (`docs/04#7.4`).
**CA:** CA-01 (lectura), CA-02 (un toque), CA-03 (biometria sobre limite), CA-04 (QR protegido).

**Decisiones del dueno (sprint-3):** el `qr_content` es un `String` escaneable (sobre `BO-QR1`) que HU15
**decodifica/verifica** (adaptador `E4-T03` + `verify_charge_signature`), no un `payload` objeto; el pago
**consume** el servicio express (`E4-T02`, `daily_limit_reader=wallet_daily_limit_reader`), sin endpoint
de billetera nuevo; `POST /payments/qr` **cierra el cobro E2E** (asiento `docs/04#7.4`, `qr_charge` `PAID`,
abono real `qr_charge_paid`; cierra `G72`).

| Tarea | Descripcion | Tipo | Brief | Estado |
|---|---|---|---|---|
| `E4-T11` | `qr_payments` (migracion `0040_wallet_qr_payments`) + servicio de cobro: decodifica/verifica `qr_content`, valida caducidad y paga por el motor (`docs/04#7.4`), marca `PAID` + abono | `motor-ledger` | `docs/tasks/archive/E4-T11.md` | `Hecho` |
| `E4-T12` | Endpoint `POST /payments/qr` + autorizacion biometrica condicional (un toque vs biometria/PIN) | `backend` | `docs/tasks/archive/E4-T12.md` | `Hecho` |
| `E4-T13` | UI Flutter: escaner QR, resumen (comercio/monto) y confirmacion | `frontend` | `docs/tasks/archive/E4-T13.md` | `Hecho` |
| `E4-T14` | UI Flutter: manejo de errores de QR (vencido/corrupto/manipulado/limite/red) | `frontend` | `docs/tasks/archive/E4-T14.md` | `Hecho` |
| `E4-T15` | QA: seguridad del QR (firma/vencido/manipulado sin fondos), concurrencia de pago y abono E2E | `qa` | `docs/tasks/archive/E4-T15.md` | `Hecho` |

> **Fronteras (HU15):** `wallet` decodifica/verifica el QR y persiste `qr_payments`, pero **no** mueve
> dinero por su cuenta: consume el servicio express (`E4-T02`) y el **motor** (`transactions` ->
> `ledger`) hace el asiento `docs/04#7.4`; el aviso al comercio va por la fachada `notifications.send(...)`.

> **Gaps de HU15 (`docs/18#9`):** **G72** (cierra el E2E del abono en `E4-T15`), **G74** (ruta/ownership
> de `POST /payments/qr`: `docs/05#6.3` la lista bajo `transactions` y `docs/05#6.5` sin detalle),
> **G75** (re-chequeo del limite diario de billetera en `POST /transactions/{id}/authorize`),
> **G76** (sin mockups/spec de las pantallas de HU15).

## HU16 - Pago de Servicios y Recargas Express (Should, 5 SP) - `wallet`

**Logica** (`docs/09#hu16---pago-de-servicios-y-recargas-express-should-5-sp-sprint-3`): el cliente busca
una empresa de servicios por nombre o numero de suministro; puede **recargar celular** (operador, numero,
monto); se envian **recordatorios** de vencimiento de servicios registrados; cada pago/recarga genera
**comprobante estructurado** (numero de transaccion oficial).

**Reglas:** consulta < 2 s; catalogo extensible sin cambios estructurales; recordatorios con **3 dias** de
anticipacion. **CA:** CA-01 (busqueda), CA-02 (recarga), CA-03 (recordatorio), CA-04 (comprobante).

**Decision del dueno (sprint-3):** el pago de servicio/recarga **consume** el servicio express (`E4-T02`,
`daily_limit_reader=wallet_daily_limit_reader`); el asiento contable de `SERVICE_PAYMENT`/`TOPUP` (no
definido aun en `docs/04`) lo **fija `E4-T17`** (`G73`), bloqueante de esa tarea.

| Tarea | Descripcion | Tipo | Brief | Estado |
|---|---|---|---|---|
| `E4-T16` | Catalogo `billers` + `service_payments`/`topups`/`service_subscriptions` (migracion `0042_wallet_services`) + adaptador de servicios (mock) + `GET /services/catalog` | `datos` | `docs/tasks/archive/E4-T16.md` | `Hecho` |
| `E4-T17` | Pago de servicio y recarga via motor (`POST /services/payments`, `POST /topups`) + comprobante | `motor-ledger` | `docs/tasks/archive/E4-T17.md` | `Hecho` |
| `E4-T18` | Job de recordatorios (`run_service_reminders_once`) + parametro `wallet.service_reminder_days_before` (migracion `0043_wallet_service_reminder_param`) | `backend` (jobs) | `docs/tasks/archive/E4-T18.md` | `Hecho` |
| `E4-T19` | UI Flutter: servicios (busqueda/pago) y recargas + comprobante | `frontend` | `docs/tasks/archive/E4-T19.md` | `Hecho` |
| `E4-T20` | QA: pago, recarga, recordatorio y comprobante | `qa` | `docs/tasks/archive/E4-T20.md` | `Hecho` |

> **Fronteras (HU16):** `wallet` es dueno de `billers`/`service_payments`/`topups`/`service_subscriptions`
> **pero no** escribe tablas de `transactions`/`ledger`/`accounts`/`notifications`; el dinero pasa por el
> motor (`E4-T02` -> `transactions` -> `ledger`). El adaptador de servicios vive en
> `backend/app/adapters/` (patron `E4-T03`).

> **Gaps de HU16 (`docs/18#9`):** **G73** (asiento contable de `SERVICE_PAYMENT`/`TOPUP` no definido en
> `docs/04`; lo fija `E4-T17`), **G76** (sin mockups/spec de las pantallas de HU16), **G77** (registro de
> `service_subscriptions`/plantilla `service_reminder`/parametro de ventana sin detalle en `docs/05#6.5`).

## HU22 - Notificaciones de Seguridad Multicanal (Should, 3 SP) - `notifications`

**Logica** (`docs/11#hu22---notificaciones-de-seguridad-multicanal-should-3-sp-sprint-3`): cada
transaccion o login biometrico sensible dispara una **alerta push inmediata**; en paralelo se envia un
**correo de respaldo** con el detalle; la alerta incluye **dispositivo, hora y ubicacion aproximada**
(sin PII innecesaria); desde la notificacion el cliente puede reportar **"no reconozco esta
operacion"**, lo que **bloquea los canales digitales** y **escala a `risk`**.

**Reglas:** entrega **< 5 s**; **tolerancia a la caida de un proveedor (multi-canal)**; el reporte
"no reconozco" escala al modulo `risk` **por fachada/evento** (bloqueo preventivo + alerta), **sin**
que `notifications` escriba tablas de `risk`. Reutiliza lo existente: `notifications` (fachada `send`,
`notification_templates`, sender Gmail/mock), `risk` (E2-T17/E2-T18/E2-T20 y el minimo construido) y
los eventos de negocio (`credit.disbursed`, `qr.payment.confirmed`, `service.paid`, `topup.completed`,
`transfer.settled`, `auth.failed_attempt`, `loan.installment.paid`).

**CA:** CA-01 (push inmediato + respaldo por correo), CA-02 (dispositivo/hora/ubicacion aproximada),
CA-03 (preferencias por canal), CA-04 (multi-canal tolerante).

| Tarea | Descripcion | Tipo | Brief | Estado |
|---|---|---|---|---|
| `E6-T12` | Servicio de notificaciones de seguridad con plantillas y **preferencias** (`user_channel_preferences`, migracion `0044_notifications_preferences`) + centro de notificaciones (listar/leer/detalle/preferencias) | `backend` | `docs/tasks/archive/E6-T12.md` | `Hecho` (fix A1/A2/A3/B2 2026-10-14: cableado E2E + detalle + resolutor; `backend/tests/test_fix_security_alerts_a1.py`, 8 passed) |
| `E6-T13` | Adaptadores **push/correo (mock) desacoplados por canal** (`PUSH_PROVIDER`/`EMAIL_PROVIDER`; aislamiento de fallos) | `adaptador` | `docs/tasks/archive/E6-T13.md` | `Hecho` |
| `E6-T14` | Accion **"no reconozco esta operacion"** -> bloqueo de canales digitales (revocar sesiones) + alerta a `risk` (multi-modulo `notifications`+`risk`+`identity`) | `backend` | `docs/tasks/archive/E6-T14.md` | `Hecho` (fix B3/B4 2026-10-14: marca transaccional + solo `security_*`; `backend/tests/test_fix_security_alerts_a1.py`) |
| `E6-T15` | UI Flutter: **centro de notificaciones**, preferencias por canal y accion "no reconozco" (feature `notifications`) | `frontend` | `docs/tasks/archive/E6-T15.md` | `Hecho` (centro + detalle/"no reconozco" + preferencias + rutas; `flutter analyze` limpio + `flutter test` verde; contrato en `docs/05#6.9` con `device`/`location`/`reference` + detalle) |
| `E6-T16` | QA: entrega **< 5 s**, **multi-canal** (caida de un proveedor), preferencias y reaccion a **"no reconozco"** | `qa` | `docs/tasks/archive/E6-T16.md` | `Hecho` (cierre + regresion del fix en `backend/tests/test_fix_security_alerts_a1.py`, 8 passed) |

> **Fronteras (HU22):** `notifications` es dueno de `notification_templates`/`notifications`/
> `user_channel_preferences` y del adaptador de envio; **no** escribe tablas de `risk`/`identity`/
> `transactions`/`ledger`. El reporte "no reconozco" escala a `risk` **por fachada/evento** (bloqueo
> preventivo + alerta) y el bloqueo de canales digitales reusa `identity.sessions.revoked_at`
> (revocacion, sin borrar). `risk` **no** crea `risk.alerts` (reservada a HU20, D-16). Un modulo por
> brief; `E6-T14` declara los tres modulos que toca (precedente E2-T20/D-10).

> **Gaps de HU22 (`docs/18#9`, continuando tras `G77b`):** **G78** (`docs/05` sin subseccion de
> `notifications`; la fija `E6-T12` en `docs/05#6.9`), **G79** (`user_channel_preferences` estaba
> DIFERIDA; la implementa `E6-T12` con migracion `0044`), **G80** (los eventos de pago no siempre
> traen dispositivo/ubicacion; coordinacion `E6-T12`/`E6-T14`), **G81** (endpoint "no reconozco" sin
> registrar; lo fija `E6-T14`), **G82** (nombre del evento de alerta `fraud.alert.raised` vs
> `risk.alert.raised`), **G83** (`alerts`/`blocked_entities` son HU20/HU21; HU22 escala sin
> escribirlas).

> **Dependencias externas:** la ruta de alertas reutiliza la fachada `notifications.send(...)` y las
> plantillas de seguridad ya `Hecho` (`E2-T20`, `E3-T14`/`E3-T19`, `E4-T08`); no duplica
> infraestructura.

### Orden de construccion recomendado (HU22)

1. `E6-T13` (adaptadores push/correo desacoplados por canal): cimiento sin dependencias; puede ir en
   paralelo con `E6-T12` (el servicio usa el `sender_for_channel` vigente hasta que aterrice el push
   mock).
2. `E6-T12` (servicio + plantillas + preferencias + centro de notificaciones; migracion
   `0044_notifications_preferences`, encadenada tras `0043_wallet_service_reminder`).
3. `E6-T14` (accion "no reconozco" -> bloqueo de canales + alerta a `risk`) sobre `E6-T12`.
4. `E6-T15` (UI Flutter del centro y de "no reconozco") con los endpoints estables o mocks de `docs/05`.
5. `E6-T16` (QA de entrega < 5 s y reaccion a "no reconozco") al cierre de HU22.

### Dependencias criticas (HU22)

- `E6-T13` y `E6-T12` pueden ir en paralelo (seam `sender_for_channel`); `E6-T12` consume el routing.
- `E6-T12` antes de `E6-T14`/`E6-T15`/`E6-T16` (fachada, plantillas, preferencias y centro).
- `E6-T14` reutiliza el minimo de `risk` (`E2-T17`/`E2-T18`/`E2-T20`, `Hecho`) y las sesiones de
  `identity` (`E1-T15`/`E1-T46`, `Hecho`); **no** escribe `risk.alerts` (HU20, D-16).
- Migraciones encadenadas (un solo head): `0044_notifications_preferences` (`E6-T12`) tras
  `0043_wallet_service_reminder` (head vigente). Sin migracion nueva en `E6-T14` (bloqueo por
  `sessions.revoked_at` + evento/auditoria).
- `E6-T15` puede avanzar con mocks de `docs/05` antes de que el backend este listo.
- `E6-T16` al cierre, cuando `E6-T12`/`E6-T13`/`E6-T14` esten `Hecho`.

## Orden de construccion recomendado (HU12)

0. `E5-T23` (correccion del catalogo contable `4200`/`5000`; cierra `docs/18` G60): prerequisito de
   `ledger` **antes** de `E3-T17`/`E3-T20`; migracion `0035_ledger_catalog_fix` + `alembic check`.
1. `E3-T18` (esquema `loans`/`loan_schedules`/`loan_payments` + cronograma definitivo): prerequisito
   de datos; `alembic check` limpio.
2. `E3-T17` (desembolso): crea `loans` + cronograma y mueve el dinero por la fachada del motor
   (asiento `7.5`); expone `GET /credits/loans` y `.../schedule`.
3. `E3-T20` (pago de cuota; asiento `7.6`) sobre prestamos desembolsados.
4. `E3-T19` (jobs de debito automatico/recordatorios) reutilizando `E3-T20` y la fachada de
   `notifications`.
5. `E3-T21` (UI) con los endpoints ya estables (puede avanzar con mocks de `docs/05`).
6. `E3-T22` (QA) al cierre de HU12.
7. El bloque **HU22** ya esta planificado (`E6-T12`..`E6-T16`, seccion propia abajo); se ejecuta
   despues de HU15-HU16, que ya estan `Hecho`.

## Orden de construccion recomendado (HU13-HU14, `wallet`)

1. `E4-T01` (esquema `wallet`: `wallets`/`wallet_links` + tokenizacion): prerequisito de datos;
   migracion `0037_wallet` + `alembic check`.
2. `E4-T02` (pago express + limite base/diario; usa el motor) sobre los vinculos de `E4-T01`.
3. `E4-T03` (adaptador de interoperabilidad, mock/contrato del par) en paralelo con `E4-T02` (no mueve
   dinero); `E4-T04` (UI de billetera) puede avanzar con mocks de `docs/05`.
4. `E4-T06` (dominio + firma del QR + `merchant_accounts`/`qr_charges`; migracion `0039_wallet_qr`,
   encadenada tras `0038_wallet_express_params`).
5. `E4-T07` (job/validacion de caducidad) sobre `qr_charges`.
6. `E4-T08` (endpoints `/merchants/qr` + estado + plantilla de abono) sobre `E4-T06`/`E4-T07`;
   `E4-T09` (UI de comercio) con los endpoints ya estables o mocks.
7. `E4-T05` (QA de HU13) y `E4-T10` (QA de HU14) al cierre de cada HU.

> Dependencia externa: la **notificacion de abono** de HU14 (CA-04) depende del pago de **HU15**
> (`qr.payment.confirmed`; `docs/18` `G72`); `E4-T10` la valido con un pago simulado y el **E2E real** lo
> cierra HU15 (`E4-T15`).

## Orden de construccion recomendado (HU15-HU16, `wallet`)

1. `E4-T11` (esquema `qr_payments` + servicio de cobro: decodifica/verifica el QR y paga por el motor):
   prerequisito de datos; migracion `0040_wallet_qr_payments`, encadenada tras `0039_wallet_qr`.
   Fix guarda (sin brief nuevo, 2026-10-13): migracion `0041_wallet_qr_payment_guard` tras `0040`
   (indice unico parcial anti doble cobro + reintento tras `REJECTED` + `POST /payments/qr/preview`).
2. `E4-T12` (endpoint `POST /payments/qr` + autorizacion biometrica condicional) sobre `E4-T11`.
3. `E4-T13` (UI escaner/resumen/confirmacion) y `E4-T14` (errores de QR) con el endpoint estable o mocks.
4. `E4-T16` (catalogo `billers` + `service_payments`/`topups`/`service_subscriptions` + adaptador mock;
   migracion `0042_wallet_services`, encadenada tras `0041_wallet_qr_payment_guard`).
5. `E4-T17` (pago de servicio/recarga por el motor + comprobante) sobre `E4-T16`/`E4-T02`; fija el asiento
   de `SERVICE_PAYMENT`/`TOPUP` (`G73`).
6. `E4-T18` (job de recordatorios + parametro `wallet.service_reminder_days_before`; migracion
   `0043_wallet_service_reminder_param`) sobre `service_subscriptions`; `E4-T19` (UI) con los endpoints
   estables.
7. `E4-T15` (QA de HU15) y `E4-T20` (QA de HU16) al cierre de cada HU.

## Dependencias criticas (HU12)

- `E3-T18` antes de `E3-T17`/`E3-T20`/`E3-T19` (esquema y repositorio).
- `E3-T12`/`E3-T13`/`E3-T14` (`SIGNED`) y `E3-T08` (`APPROVED`) antes de `E3-T17`.
- `E3-T17` antes de `E3-T20`/`E3-T19`/`E3-T21` (prestamos desembolsados).
- `E3-T20` antes de `E3-T19` (el debito automatico reutiliza el caso de uso de pago).
- **`E5-T23` (correccion del catalogo contable, cierra G60)** antes de `E3-T17`/`E3-T20`: deja
  `5000`=`asset` "Cartera de prestamos" y `4200`="Ingresos por seguro" (dominio + migracion `0035`).
  Es el prerequisito **BLOQUEANTE** de HU12; brief en `docs/tasks/archive/E5-T23.md` (modulo `ledger`).
- `E5-T03`/`E5-T09` (motor/ledger, `Hecho`) son la base del desembolso y del pago.
- `ledger` y `transactions` no se paralelizan entre si (`docs/16#9`).

## Criterios de salida del sprint (HU11-HU16 y HU22)

- El ciclo **simular -> solicitar -> dictamen -> firmar -> desembolsar -> pagar** corre de punta a
  punta (`docs/08`, criterios de salida de la epica).
- **Desembolso atomico** (`docs/04#7.5`) y **pago de cuotas** (`docs/04#7.6`) contabilizados en
  partida doble y cuadrados; sin acreditaciones parciales.
- Ninguna operacion de dinero sin asiento; ninguna escritura de `credits` a `ledger`/`transactions`
  directamente (solo por fachada).
- Desembolso < 30 s tras la firma; debito automatico y recordatorios operativos e idempotentes.
- **Billetera:** fuente de fondos **tokenizada** (nunca PAN); pagos **express** bajo el limite base sin
  clave y **limite diario** respetado (rechazo sin tocar saldos al superarlo).
- **QR:** QR **firmado** y con **caducidad** (vencido invalido sin descontar fondos); formato
  interoperable; **cobro** (`POST /payments/qr`) con **abono < 5 s** al comercio (HU15, cierra `G72`).
- **Pago QR (HU15):** QR vencido/corrupto/manipulado -> **rechazo sin descontar fondos**; monto `<=`
  limite base -> **un toque**; sobre el base -> **biometria adicional**; sin doble pago del mismo cargo.
- **Servicios/recargas (HU16):** busqueda < 2 s y catalogo extensible sin cambios estructurales;
  pago/recarga por el motor con **comprobante estructurado**; recordatorios a **3 dias** e idempotentes.
- **Notificaciones de seguridad (HU22):** cada transaccion/login sensible dispara **alerta push
  < 5 s** con **correo de respaldo en paralelo**; contenido con **dispositivo, hora y ubicacion
  aproximada** sin PII innecesaria; **caida de un proveedor** no impide la entrega del otro canal;
  **preferencias por canal** respetadas; **"no reconozco"** bloquea los canales digitales (sesiones
  revocadas) y escala a `risk` (alerta trazable) **sin** escribir `risk.alerts`.
- Ninguna operacion de dinero sin asiento; ninguna escritura de `wallet` a
  `transactions`/`ledger`/`accounts`/`notifications` directamente (solo por fachada/evento).
- `alembic check` limpio (un solo head); `pytest`/`ruff`/`black` y `flutter analyze`/`flutter test`
  en verde; sin PII/secretos en logs ni respuestas.

## Dependencias criticas (HU13-HU14)

- `E4-T01` antes de `E4-T02`/`E4-T03`/`E4-T04` (billetera/vinculos).
- `E4-T02` reutiliza el **motor** (`E5-T03`/`E5-T09`, `Hecho`) y el gate biometrico
  (`E2-T17`/`E2-T19`/`E2-T31`, `Hecho`); `wallet` no contabiliza por su cuenta.
- `E4-T06` antes de `E4-T07`/`E4-T08`/`E4-T09`/`E4-T10` (QR/`qr_charges`).
- `E4-T07` antes de `E4-T08` (estado efectivo del QR vencido).
- `E4-T05` al cierre de HU13; `E4-T10` al cierre de HU14.
- La **notificacion de abono** (`E4-T08`/`E4-T10`) depende del pago de **HU15** (`G72`).
- La migracion `0039_wallet_qr` encadena `down_revision` tras `0038_wallet_express_params` (un solo head).

## Dependencias criticas (HU15-HU16)

- `E4-T11` antes de `E4-T12`/`E4-T13`/`E4-T14`/`E4-T15` (servicio de cobro/`qr_payments`).
- `E4-T12` antes de `E4-T13`/`E4-T14`/`E4-T15` (endpoint `POST /payments/qr`).
- `E4-T11` reutiliza la **firma** (`E4-T06`), la **caducidad** (`E4-T07`), el **adaptador** (`E4-T03`) y
  el **pago express** (`E4-T02`, `docs/04#7.4`); `wallet` no contabiliza por su cuenta.
- `E4-T15` cierra el E2E del abono (`docs/18` `G72`) que `E4-T10` dejo con pago simulado.
- `E4-T16` antes de `E4-T17`/`E4-T18`/`E4-T19`/`E4-T20` (catalogo/tablas/adaptador).
- `E4-T17` **BLOQUEADO** por el asiento de `SERVICE_PAYMENT`/`TOPUP` (`docs/18` `G73`, se fija en el
  propio brief y en `docs/04`).
- `E4-T18` reutiliza la fachada `notifications.send(...)` y el patron de jobs/wiring M-2 (`E3-T19`).
- Migraciones encadenadas (un solo head): `0040_wallet_qr_payments` (`E4-T11`) ->
  `0041_wallet_qr_payment_guard` (fix guarda ALTO-2/MEDIO-2/MEDIO-3) ->
  `0042_wallet_services` (`E4-T16`) -> `0043_wallet_service_reminder_param` (`E4-T18`), tras
  `0039_wallet_qr`.

## Reglas transversales embebidas en cada brief

Cliente delgado (el frontend no calcula cuotas/saldos/intereses ni decide cobros); todo dinero pasa
por el motor y se contabiliza en `ledger` (partida doble, append-only, proyeccion no es fuente de
verdad); idempotencia obligatoria (`Idempotency-Key`) en desembolso y pago; saldo insuficiente ->
rechazo sin tocar saldos; sin PII/secretos en logs; prohibido tocar `kyc-service/`; fronteras por
fachada/evento (sin importar modelos ajenos); cambios de ORM con migracion Alembic y `alembic check`
limpio; no borrar pruebas ni debilitar aserciones; tasas/parametros configurables sin despliegue
(dueno `shared/config`); un modulo por brief.
**Creditos ademas:** calculos financieros en backend, puros y testeables; el simulador no genera
registro crediticio real; el cronograma definitivo reutiliza el motor `E3-T01`.
**Wallet/QR ademas:** nunca se guarda el PAN (solo `token_ref`/`last4`); el QR se firma/hashea con el
secreto del servidor y caduca (default 5 min); la firma impide manipulacion; `wallet` no debita sin el
motor; el aviso de abono viaja por la fachada `notifications` (sin PII).
**HU15/HU16 ademas:** el QR se **decodifica/verifica** desde el `qr_content` (`BO-QR1`), nunca se confia
en el cliente; pago de servicio/recarga con **comprobante estructurado**; catalogo de servicios
**extensible sin cambios estructurales**; un mismo `qr_charge` no se paga dos veces (idempotencia por
`Idempotency-Key` + estado del cargo).
**HU22 ademas:** entrega **< 5 s** y **multi-canal tolerante** (la caida de un proveedor deja solo su
fila `FAILED`/reimpulsable y no afecta al otro canal); alerta con **dispositivo, hora y ubicacion
aproximada** y **sin PII innecesaria**; **preferencias por canal** respetadas (con respaldo de
seguridad); el reporte **"no reconozco"** escala a `risk` **por fachada/evento** (bloqueo preventivo +
alerta, reusando `identity.sessions.revoked_at`) y **no** escribe `risk.alerts` (HU20, D-16); la fila
`notifications` es la garantia de no perdida.
