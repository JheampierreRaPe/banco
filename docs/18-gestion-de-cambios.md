# 18 - Gestion de cambios (añadir cosas al proyecto)

> Regla de oro: **primero se cambia el plan (documento), luego el codigo**. Nunca al reves.
> No esperes al final del proyecto para agregar algo que ya sabes ahora: el costo crece con el
> tiempo (rework, migraciones, pruebas rotas).

## 1. Decision rapida: ¿cuando entra el cambio?

| Tipo de cambio | Cuando | Que actualizar |
|---|---|---|
| Nueva funcionalidad / HU | Desde ya: registrar y planificar en un sprint | `01` (backlog), estimar SP, decidir MVP, crear brief en `tasks/`, `14` (sprint) |
| Ajuste a algo **aun no construido** | Desde ya, antes de que se tome esa tarea | El brief/CA de esa HU (`06`..`12`) y, si aplica, `03b`/`05` |
| Ajuste a algo **ya construido** | Registrar ya; implementar en el sprint actual si es critico, si no en estabilizacion | Nuevo brief de cambio + el doc afectado |
| Regla tecnica / arquitectura (dependencia, patron) | Desde ya (impacta todo lo que venga) | `02`, `03b`, `17` |
| Cambio de alcance o exclusion | Desde ya | `01` (y el acta de constitucion si aplica) |
| Idea descartada | Registrarla como descartada | Este documento (para no reevaluarla) |

Resumen: **lo que no bloquea -> backlog; lo que invalida lo que se esta construyendo -> ahora.**

## 2. Flujo de 5 pasos

1. **Registrar** la solicitud (SCR) en la seccion 4 con contexto y motivo.
2. **Evaluar impacto**: HU/modulos afectados, story points, dependencias, si es MVP o post-MVP.
3. **Decidir**: `entra ahora`, `se planifica en sprint N`, `post-MVP` o `descartado`.
4. **Actualizar la fuente de verdad** (docs) y crear/editar los briefs en `docs/tasks/`.
5. **Comunicar y re-planificar** el sprint (`14`) si cambia el alcance comprometido.

## 3. Reglas para no romper el trabajo en curso

- No inyectar cambios dentro de una tarea activa sin actualizar su brief: **se para, se actualiza
  el brief y se reanuda**.
- Si el cambio rompe algo ya entregado: crear un brief de cambio que incluya **reverso/ajuste
  contable** y compatibilidad.
- Los cambios de dinero nunca editan asientos: se compensan con asiento nuevo.
- Todo cambio de alcance se refleja en `01-hallazgos-y-decisiones.md` (fuente de verdad).
- Un cambio sin CA ni pruebas no se acepta.

## 4. Registro de solicitudes de cambio (SCR)

| ID | Fecha | Solicitud | Motivo | Tipo | Impacto (HU/SP) | Decision | Sprint | Estado |
|---|---|---|---|---|---|---|---|---|
| SCR-001 | 2026-09-12 | Ejecucion en dos campos (PC servidor + movil por LAN) con cliente delgado estricto | Formalizar el reparto cliente-servidor pedido por el equipo | Tecnico / Arquitectura | `02`, `05`, `13`, `19`, briefs frontend | entra ahora | Sprint 1 | hecho |
| SCR-002 | 2026-09-12 | Guia de diseno UI y tokens para los dos frontends | Definir como especificar el diseno (tokens + mockups; sin CSS para Flutter) | Tecnico / Documentacion | `docs/20`, `docs/design/`, briefs UI | entra ahora | Sprint 1 | hecho |
| SCR-003 | 2026-09-16 | Integrar el design system "Eucalipto y Ocre" (YAML) en la documentacion y definir `success`/`warning` | Fijar la paleta para que el frontend de prueba se construya con esos estilos | Diseno / Documentacion | `docs/20`, `docs/design/mockups.md`, briefs UI | entra ahora | Sprint 1 | hecho |
| SCR-004 | 2026-09-20 | Version visible de la app en entry, login y crear cuenta, con fuente unica y esquema de versionado documentado | Confirmar que build esta instalado en el movil y fijar el esquema acordado | Ajuste / Documentacion | `13`, `18`, `README`, brief `F-T30`, `frontend` | entra ahora | Sprint 1 | resuelto/aplicado (2026-09-26: brief `F-T30` en `docs/tasks/archive/` con `Estado: Hecho`) |
| SCR-005 | 2026-09-21 | Regeneracion total de la UI Flutter con el diseno canonico "Eucalipto y Ocre" (`pantallas.fig`) + ajustes de identidad en backend | Unificar la app al diseno oficial, fusionar onboarding y simplificar el flujo de sesion por `user_ref` | Nueva funcionalidad / Ajuste | Fases 1-6 (rama `refactorizacion-ui`); briefs `F-T34`..`F-T42`, `E1-T32`..`E1-T34`, `Q-T11` | entra ahora | Sprint 1 | resuelto/aplicado (2026-09-26: briefs `F-T34`..`F-T42`, `E1-T32`..`E1-T34` y `Q-T11` en `docs/tasks/archive/`, todos con `Estado: Hecho`) |

> **SCR-005 - decisiones del dueno (2026-09-21, rama `refactorizacion-ui`):**
> 1. Diseno canonico = `pantallas.fig` (Eucalipto y Ocre) + tokens de `docs/20`. Las
>    pantallas SIN pagina en el `.fig` (recovery, pin-reset, activation, home/dashboard)
>    se **disenan primero** en el `.fig` antes de implementarlas.
> 2. Backend: `recovery.verify` deja de abrir sesion y devuelve `{user_ref, device_bound}`;
>    la unica sesion la abre `POST /auth/login/pin`. OTP queda **solo email** (se quita
>    el fallback SMS) y `POST /auth/activate` se **deprecia pero se mantiene vivo**
>    (header `Deprecation` + OpenAPI `deprecated`).
> 3. Extras backend incluidos: endpoints `/pin-reset` (email+DNI+OTP), migracion para
>    `users.email` NOT NULL, y mover constantes (lockout, espera de reenvio, intentos de
>    OTP, rate-limit de recovery) a `config.parameters`.
> 4. Alcance de la rama: **bloque identidad/onboarding primero** (splash -> onboarding ->
>    registro -> sesion -> home). Cuentas/creditos/divisas quedan para una fase posterior.
> 5. El flag local `welcome.hasSeenWelcome` se elimina; el guard del router depende solo de
>    `SessionIdentityStore.userRef` (+ sesion activa). `SessionIdentityStore` pasa a
>    `Listenable`.

Plantilla para agregar una fila:

```
| SCR-00X | <fecha> | <que se quiere agregar> | <por que> | <Nueva HU / Ajuste / Tecnico / Alcance> | <modulos, SP> | <entra ahora / sprint N / post-MVP / descartado> | <sprint> | <pendiente/en curso/hecho> |
```

## 5. Ejemplos

- **Agregar "pago de matriculas"**: nueva HU -> registrar SCR, estimar, decidir MVP, crear brief,
  planificar en el sprint con capacidad. No esperar al final.
- **Cambiar el umbral de biometria de S/10,000 a S/5,000**: es un parametro -> cambiar
  `config.parameters`, no el codigo, no requiere HU nueva.
- **Reemplazar el proveedor de notificaciones mock por uno real**: cambio tecnico -> actualizar
  `02` y crear tarea de adaptador; se hace cuando toque ese modulo.
- **Cambiar el estandar de QR**: cambio de arquitectura -> `02` + `06`..`09` (HU14/HU15) + un brief.

## 6. Cuando el proyecto ya esta en marcha

- Cambio en una **HU no iniciada**: se edita el brief directamente (barato).
- Cambio en una **HU en curso**: se actualiza el brief y el agente re-planifica; si ya hay codigo
  hecho que contradice el cambio, se agrega una subtarea de ajuste.
- Cambio en una **HU terminada**: se abre una tarea de mejora/correccion; no se reescribe la
  historia ya cerrada.

## 7. Definition of Done de un cambio

- [ ] SCR registrada y decidida.
- [ ] Docs fuente de verdad actualizados.
- [ ] Brief creado o actualizado.
- [ ] CA y pruebas incluidos.
- [ ] Impacto en ledger/saldos evaluado si aplica.
- [ ] Sprint re-planificado si cambia el alcance.

## 8. Versionado visible de la app

Esquema de versionado acordado por el dueño (literal), para la app Flutter:

- Arrancar en `1.1.010`.
- Mientras se corrige el error actual (camara KYC), cada nueva version sube
  `+0.0.001` (`1.1.011`, `1.1.012`, ...).
- Una vez funcional y se pase a otros apartados del Sprint 1 que deben verse en
  Flutter, se varia a `0.1.0`.
- Al acabar el sprint, `1.0.0`.

Reglas de implementacion:

- **Fuente unica:** la version se define en un solo lugar,
  `frontend/lib/core/app_version.dart` (`const String kAppVersion =
  String.fromEnvironment('APP_VERSION', defaultValue: '1.1.010');`),
  sobreescribible por `--dart-define=APP_VERSION=<valor>`. Cambiar la version
  por build no toca ninguna pantalla.
- **Widget compartido:** `frontend/lib/core/widgets/app_version_label.dart`
  (`AppVersionLabel`, `Key('app-version')`) renderiza `Version <kAppVersion>`.
  Se monta en las TRES pantallas: `/entry` (`entry_page.dart`), `/login`
  (`login_page.dart`) y `/kyc` (`kyc_start_page.dart`). Es discreto
  (`label-sm` + color de texto secundario) y no rompe los estados de cada
  pantalla.
- **`pubspec.yaml` y semver:** el `version:` de pubspec no acepta cero a la
  izquierda en el patch, por lo que se usa `version: 1.1.10+1`; el TEXTO visible
  (el que identifica el build) sigue siendo `1.1.010` desde `kAppVersion`. Sin
  dependencias nuevas.
- Registrado como `SCR-004` (fila de la seccion 4).

## 9. Supuestos y gaps detectados por el planificador (Sprint 2)

> Registro de supuestos/gaps levantados al generar los briefs `E2-T07`..`E2-T23` y ampliado en la
> revision de auditoria a `E2-T07`..`E2-T26` (2026-09-26), al 2o lote de creditos
> `E3-T01`..`E3-T11` (2026-09-26), al cierre de `E2-T10`/`E2-T13`/`E2-T18` (2026-09-26), a la
> validacion de `E2-T17` (**2026-09-27, `Hecho`**, con observaciones **OBS-1..OBS-4**) y al cierre
> de `E2-T14`/`E2-T19`/`E2-T20`/`E2-T24`/`E2-T25`/`E2-T26` (**2026-09-27, `Hecho`**): se resuelven
> **G01 (authorize)**, **G16 (listado)**, **G17 (reverso)** y **G18 (parametros admin)** y se
> **cierra OBS-1** (perfil de riesgo end-to-end); siguen **abiertas OBS-2** (umbral vs limite
> diario), **OBS-3** (CAS del contador de fallos) y **OBS-4**. Cada
> fila indica su severidad, el brief que la resuelve o el doc a corregir. Si un
> supuesto contradice `01-hallazgos-y-decisiones.md`, manda `01` y se corrige el brief
> (`docs/tasks/README.md#7`).

| # | Supuesto / gap | Brief que lo resuelve | Doc a actualizar |
|---|---|---|---|
| G01 | **RESUELTO (E2-T19 `Hecho`, 2026-09-27):** `docs/05#6.3` fija ahora tambien el contrato de `POST /transactions/{id}/authorize` (request `{nonce, signature, device_id}`, `Idempotency-Key`, estados, `EXPIRED_NONCE`/`CONFLICT` y el `403` generico sin oraculo), completando `/transfers/*`, `GET /transactions/{id}` y `GET /transactions/{id}/receipt` (E2-T07/T09/T10). | `E2-T19` (`Hecho`) | `docs/05` |
| G02 | **PARCIAL/RESUELTO (E2-T13 `Hecho`, 2026-09-26):** `docs/05#6.2` ya detalla `GET/POST/PUT/DELETE /beneficiaries` (body, `?q=`, `preapproved_limit_minor`, soft-delete `?confirm=true`, enmascaramiento y codigos `DUPLICATE_BENEFICIARY`/`IDEMPOTENCY_CONFLICT`/`VALIDATION_ERROR`). Queda abierta la pantalla de agenda (`E2-T15`). | `E2-T13` (`Hecho`), `E2-T15` | `docs/05` |
| G03 | `ledger.entry.posted` **no se emite** en el codigo hoy (ni `ledger.repository.entries` ni `balances` publican); el consumidor `handle_movement_event` de `E2-T04` nunca recibe eventos en produccion (`docs/17#6`). Se fija el payload del evento y el **handler `on_ledger_entry_posted(entry)`** que abre su propia sesion y deriva `event_id=entry.id` (D-04; el bus `app.core.bus` entrega solo el `entry`, sin sesion). | `E2-T23` | `docs/02#7`, `docs/17#6`, `docs/modules/README.md#accounts` |
| G04 | `handle_ledger_entry_posted` (retenidos) sigue siendo stub; `held` lo sincroniza hoy `AccountsLedgerProjectionAdapter`. Suscribirlo al bus sin reconciliar duplicaria el efecto. Se documenta la no doble aplicacion. | `E2-T23` | `docs/modules/README.md#accounts` |
| G05 | **CERRADO/APLICADO (E2-T07 `Hecho`, 2026-09-26):** la cuenta canonica de comision es **`4100-Ingresos por comisiones`**. El catalogo semilla mapea `4000`="Ingresos por intereses" (`0003`, `ledger/domain/chart.py`). El motor define `FEE_ACCOUNT_CODE="4100"` (`transactions/service/__init__.py:83`) y lo consume con `get_by_code` (antes leia `4000` en `:311`). Alineados `docs/04#7.2/#7.3`, `docs/03b#7.1` y `docs/03` (catalogo). | `E2-T07` (`Hecho`) | `docs/04`, `docs/03b`, `docs/03` |
| G06 | **RESUELTO (E2-T18 `Hecho`, 2026-09-26):** `shared/config/parameters.py` implementa la fachada duena de `config.parameters` (lectura best-effort sin cache + `set_parameter` transaccional/auditado); `shared/config` es la unica fuente duena del esquema y ciclo de vida. | `E2-T18` (`Hecho`) | `docs/modules/README.md#shared--config-infraestructura` |
| G07 | **PARCIAL (E2-T18 `Hecho`, 2026-09-26):** `risk` ya tiene `models/repository/service` con el minimo de `risk_profiles` (`03b#112`, migracion `0023`); `alerts`/`risk_rules` y la ampliacion del perfil siguen siendo HU20 (`E6-T03`/`E6-T04`, Sprint 4, D-16). El bloqueo `CA-02` lo aplica `E2-T17` y `E2-T20` consume la alerta (D-05). | `E2-T18` (`Hecho`), `E2-T20` (coordinado con `E6-T03`/`E6-T04`) | `docs/modules/README.md#risk` |
| G08 | `transactions` no tiene router real (solo `APIRouter` vacio) ni integracion del middleware de idempotencia a nivel app; solo existe el helper `idempotency_dependency`. Los endpoints deben cablearlo. | `E2-T07`, `E2-T09`, `E2-T19`, `E2-T24`, `E2-T25` | `docs/modules/README.md#transactions` |
| G09 | **PARCIAL (E2-T18 `Hecho`, 2026-09-26):** los datos/config ya existen (claves `transfer.daily_limit_{PERFIL}_minor`, con `STANDARD` sembrado en `0023`; `users.risk_profile` sigue default `STANDARD`); la **aplicacion** del limite diario es `E2-T17` (`CA-03`) y la taxonomia en `docs/03b#31`/`#112` aun no se actualiza. | `E2-T18` (`Hecho`, datos), `E2-T17` (aplicacion) | `docs/03b#31`, `docs/03b#112` |
| G10 | **RESUELTO/APLICADO (E2-T09 `Hecho`, 2026-09-26):** contrato interbancario real con el equipo par aun inexistente (D01); el simulador de `docs/05#7` define el contrato interno de la saga. Integridad de concurrencia cubierta: CAS atomico en `transition_transaction`, reordenamiento claim->post en las ramas `FUNDS_HELD`/`ACCEPTED` y exclusion por transaccion (`RLock` + `SELECT ... FOR UPDATE` + `_set_meta` CAS por `version`). | `E2-T09` (`Hecho`) | `docs/05#7` |
| G11 | **RESUELTO (E2-T13 `Hecho`, 2026-09-26):** `beneficiaries_crypto` implementa AES-256-GCM (clave `BENEFICIARY_CRYPTO_KEY`) e indice ciego HMAC (`BENEFICIARY_INDEX_PEPPER`), gestionadas por entorno como `DOC_HASH_PEPPER` (placeholders en `backend/.env.example`; fail-fast fuera de `local`); `cryptography>=41` declarada. | `E2-T13` (`Hecho`) | `docs/02#10`, `.env.example` |
| G12 | **RESUELTO (E2-T20 `Hecho`, 2026-09-27):** se decidio **no emitir `parameter.updated`** (trazabilidad por `updated_by`/`updated_at`, E2-T18) y se fijo el nombre del evento de fallo en **`auth.failed_attempt`** (`transfer.authorization_failed` descartado). | `E2-T20` (`Hecho`); `E2-T18` (`Hecho`, sin evento) | `docs/02#7`, `app/core/events.py` |
| G13 | HU04 (Recuperacion de cuenta) queda fuera del Sprint 2 salvo rediseno: el flujo vigente (`recovery/request` + `pin-reset` + login en dispositivo nuevo `E1-T45`/`E1-T46`) la supera. Sin briefs nuevos. | — (decision de planificacion) | `docs/05#61`, `docs/14#3` |
| G14 | HU09/HU10 (`credits`, `E3-T01`..`E3-T11`) estan planificadas en el Sprint 2 (`docs/14#3`) pero son **2o lote sin briefs aun**; no se crean en esta tanda. | — (decision de planificacion) | `docs/tasks/SPRINT-2.md` |
| G15 | **RESUELTO (E2-T10 `Hecho`, 2026-09-26):** `GET /transactions/{id}/receipt` emite PDF real con `reportlab` (ya dependencia) y degrada a `501 RECEIPT_FORMAT_NOT_SUPPORTED` si no hay backend PDF (patron `E2-T03`); documentado en `docs/05#6.3`. | `E2-T10` (`Hecho`) | `docs/05#6.3` |
| G16 | **RESUELTO (E2-T24 `Hecho`, 2026-09-27):** `docs/05#6.3` fija `GET /transactions` (paginacion `page`/`page_size` default 20/max 100, filtros `date_from`/`date_to`/`type`/`status`, orden `created_at DESC, id`, enmascarado `****1234`, `422` por entrada invalida y aislamiento por titular). | `E2-T24` (`Hecho`) | `docs/05#6.3` |
| G17 | **RESUELTO (E2-T25 `Hecho`, 2026-09-27):** `docs/05#6.3` fija `POST /transactions/{id}/reverse` (request opcional `{reason?}`, `Idempotency-Key`, response con `reversal_entry_id`/`reverses_entry_id`), los **estados permitidos** `POSTED`/`SETTLED`/`CONCILIATED` -> `REVERSED` (G17 confirmado) y el evento `transfer.reversed`. | `E2-T25` (`Hecho`) | `docs/05#6.3` |
| G18 | **RESUELTO (E2-T26 `Hecho`, 2026-09-27):** `docs/05#6.8` fija `GET/PUT /admin/parameters` (listado con filtros `module`/`prefix`, `PUT {value_json}` con validacion de tipo, rol admin, `404`/`422`); la fachada es de `admin` sobre `shared/config` (unico dueno) con auditoria y sin evento `parameter.updated`. | `E2-T26` (`Hecho`) | `docs/05#6.8`, `docs/modules/README.md#admin` |
| G19 | **RESUELTO/APLICADO (E2-T08 `Hecho`, 2026-09-26; D-07):** el endpoint `POST /transfers/interbank/validate` era **inventado** (no esta en `docs/05`). Queda **retirado**: la validacion titular/CCI es una **funcion de fachada** (`validate_holder`) consumida por `E2-T07`/`E2-T09`; sin endpoint ni OpenAPI nuevo. | `E2-T08` (`Hecho`) | `docs/modules/README.md#transactions` |
| G20 | **Briefs del 2o lote de creditos generados (2026-09-26):** `E3-T01`..`E3-T11` (HU09/HU10) ya existen en `docs/tasks/`; cierra G14. | `E3-T01`..`E3-T11` | `docs/tasks/SPRINT-2.md` |
| G21 | `docs/05#64` lista `/credits/*` como rutas + proposito, sin request/response ni codigos (mismo caso que G01). Se fijan el body/response de `GET /credits/products`, `POST /credits/simulate`, `POST /credits/applications`, `GET /credits/applications/{id}` al implementar; el **export del simulador** y las rutas de **revision del analista** (bandeja + dictamen) **no estan en `docs/05`** y se agregan. | `E3-T03`, `E3-T08` | `docs/05#64` |
| G22 | Los contratos de `/credits/*` **no estan especificados en `docs/05`** mas alla de las filas de rutas; no hay `docs/05` para simulacion, consentimiento ni dictamen. Se asume que `docs/05#64` es el contrato minimo y que el detalle se fija en los briefs. | `E3-T03`, `E3-T08` | `docs/05#64` |
| G23 | **Tablas de `credits` ausentes:** el schema `credits` no existe en migraciones (solo `identity`, `transactions`, `ledger`, `accounts`, `notifications`, `shared`); `loan_products`/`loan_applications` se crean con migracion Alembic en este lote (`E3-T02`, `E3-T08`). Coordinar el numero de revision con `alembic heads`. | `E3-T02`, `E3-T08` | `docs/03b#8-schema-credits` |
| G24 | **`CreditBureau` inexistente:** `backend/app/adapters/credit_bureau.py` no existe (solo `kyc_provider`, `document_lookup_provider`, `notification_sender`); el adaptador se crea en este lote con interfaz + mock + HTTP, timeouts/reintentos/circuit breaker y `CREDIT_BUREAU_PROVIDER` (mock por defecto). | `E3-T06` | `docs/02#9-adaptadores-externos-simulados`, `backend/app/adapters/README.md` |
| G25 | Nombres de eventos de credito por decidir/registrar: `app/core/events.py` solo tiene `credit.disbursed`; faltan `credit.approved` (listado en `docs/modules/README.md#credits`), `credit.application.submitted`, `credit.rejected`, `credit.review.required` y `bureau.score.received`. Se fijan al implementar. | `E3-T06`, `E3-T08` | `docs/02#7`, `app/core/events.py`, `docs/modules/README.md#credits` |
| G26 | Claves de `config.parameters` del motor de reglas de credito: `docs/03b#31` solo siembra `loan.max_dti_ratio`; faltan `loan.max_amount_minor`, `loan.min_score`, `loan.review_margin`, `loan.max_debt_minor`. Se agregan en `E3-T07` (dueno `shared/config`). | `E3-T07` | `docs/03b#31` |
| G27 | **Panel web sin base:** `admin-web/` solo tiene `README.md`; el brief del panel es `F-T12` en `docs/13#24` pero **no existe brief `F-T12`** (no esta en `docs/tasks/` ni en `archive/`). `E3-T10` asume que puede incluir el bootstrap minimo del panel y se coordina con `F-T12` para no duplicar. | `E3-T10` | `docs/13#24`, `docs/18` (crear `F-T12` si se confirma) |
| G28 | Criterios de redondeo, base del desgravamen y convencion de tasa (TEA vs TCEA) del motor financiero no estan fijados en docs; se documentan en `credits/domain` y se validan contra casos de referencia. | `E3-T01`, `E3-T05` | `docs/08#hu09...`, `backend/app/modules/credits/README.md` |
| G29 | No hay mockup `creditos-simulador.png` (esta listado como ejemplo en `docs/design/mockups.md` pero el archivo no existe); `E3-T04` avanza con tokens de `docs/20` y se registra/crea el mockup con el dueno. | `E3-T04` | `docs/design/mockups.md` |
| G30 | **Decision de routing de comision en el settle (E2-T07/E2-T09):** la comision de transferencia (tercero e interbancaria) se carga al **liquidar** desde la cuenta de fondos retenidos (`2100-A`) hacia `4100-Ingresos por comisiones`, en lugar del `2000-A` literal de `docs/04#7.3` paso 2. Una operacion fallida/timeout nunca cobra; el reverso la anula. No rompe la partida doble. | `E2-T07`/`E2-T09` (`Hecho`) | `docs/04#7.3` |
| G31 | **Concepto `INTERBANK_RETURN` (devolucion post-settle, E2-T09):** tras revertir una interbancaria ya `SETTLED` (hold `CAPTURED`), se devuelve al disponible lo capturado con el asiento `2100-A -> 2000-A` (`entry_type=INTERBANK_RETURN`), idempotente via `meta.entries["return_entry"]`; el original queda intacto. | `E2-T09` (`Hecho`) | `docs/04#7.3` |
| G32 | **Registro de codigos nuevos (E2-T10/E2-T13 `Hecho`, 2026-09-26):** `RECEIPT_FORMAT_NOT_SUPPORTED` (501, comprobante sin backend PDF) y `DUPLICATE_BENEFICIARY` (409, alias/CCI `ACTIVE` duplicado del titular); tambien `IDEMPOTENCY_CONFLICT` (409, alta de beneficiario con `Idempotency-Key` y cuerpo distinto). Documentados en `docs/05#4` y `docs/05#6.2`/`#6.3`. | `E2-T10`, `E2-T13` (`Hecho`) | `docs/05#4`, `docs/05#6.2`, `docs/05#6.3` |
| G33 | **Decision: NO se emite `parameter.updated` (E2-T18 `Hecho`, 2026-09-26).** Se decidio no introducir el evento de cambio de parametro; la trazabilidad la dan `updated_at`/`updated_by`. **Coordinacion D-16 con HU20** (`E6-T03`/`E6-T04`, Sprint 4): `E2-T18` crea solo el minimo de `risk.risk_profiles` (`03b#112`, migracion `0023`), sin tocar `alerts`/`risk_rules`. Cierra parte de G12. | `E2-T18` (`Hecho`); `E6-T03`/`E6-T04` | `docs/modules/README.md#shared--config-infraestructura`, `docs/modules/README.md#risk`, `docs/02#7` |
| G34 | **Dependencia `cryptography` declarada + fail-fast de claves (E2-T13 `Hecho`, 2026-09-26):** `cryptography>=41` en `backend/pyproject.toml` (AES-256-GCM); fuera de `local` el arranque exige `cryptography` + `BENEFICIARY_CRYPTO_KEY`/`BENEFICIARY_INDEX_PEPPER` (`core/config` y `beneficiaries_crypto.assert_production_ready`); placeholders en `backend/.env.example`. Cierra G11. | `E2-T13` (`Hecho`) | `backend/pyproject.toml`, `backend/.env.example`, `docs/modules/README.md#accounts` |
| G35 | **Evento `auth.failed_attempt` (E2-T17 emisor / E2-T20 `Hecho`, 2026-09-27):** el motor lo registra por `outbox` en cada intento biometrico fallido (y en el bloqueo preventivo con `blocked:true`); payload `{transaction_id, user_id, device_id, attempt, blocked, reason}` (sin `nonce`, firma, claves ni material biometrico). Consumidores: `risk` (`on_auth_failed_attempt` -> severidad + auditoria + push; idempotente por `processed_events`, consumidor `risk-auth-failures`) y `notifications`. Nombre fijado en `app/core/events.py` (`AUTH_FAILED_ATTEMPT`). | `E2-T20` (`Hecho`) | `docs/02#7`, `docs/modules/README.md#risk`, `docs/modules/README.md#notifications` |
| G36 | **Evento `transfer.reversed` (E2-T25 `Hecho`, 2026-09-27):** emitido por `outbox` al compensar con asiento inverso; payload `{transaction_id, status, reversal_entry_ids, reverses_entry_ids}`. **Estados reversibles (G17 confirmado):** `POSTED`/`SETTLED`/`CONCILIATED` -> `REVERSED`; el resto no se reversa (`409`). El `ledger.entry.posted` de cada asiento inverso lo emite `ledger` (E2-T23). | `E2-T25` (`Hecho`) | `docs/05#6.3`, `docs/modules/README.md#transactions` |
| G37 | **`metadata.beneficiary_id` + `PREAPPROVED_LIMIT` (E2-T14 `Hecho`, 2026-09-27):** el tope pre-aprobado por beneficiario se resuelve por `beneficiary_id` explicito o `metadata["beneficiary_id"]` (fachada `accounts`); con `preapproved_limit_minor >= amount_minor + fee_minor` y misma moneda la operacion se exime del reto biometrico (omite `PENDING_AUTHORIZATION`) y `transaction.initiated` viaja con `exemption_reason="PREAPPROVED_LIMIT"` + `beneficiary_id` (sin PII). | `E2-T14` (`Hecho`) | `docs/05#6.3`, `docs/modules/README.md#transactions` |
| G38 | **Entorno demo de beneficiarios (E2-T13/E2-T14, 2026-09-27):** `docker-compose.yml` inyecta `BENEFICIARY_CRYPTO_KEY`/`BENEFICIARY_INDEX_PEPPER` al `backend` y al `worker`; con `APP_ENV: demo` es obligatorio definirlas (fail-fast fuera de `local`), por lo que la demo debe exportar ambas en el `.env` de la raiz o usar `APP_ENV: local` si no se van a ejercitar beneficiarios. | `E2-T13` (`Hecho`), `E2-T14` (`Hecho`) | `docker-compose.yml`, `backend/.env.example` |
| OBS-1 | **CERRADA (E2-T19 `Hecho`, 2026-09-27):** CA-03 "por perfil" ya llega al runtime — el router resuelve el `risk_profile` del titular (`biometric_service.resolve_risk_profile`, lectura acotada en `SAVEPOINT`, best-effort) y lo pasa end-to-end al motor; queda como fallback el limite generico/`STANDARD` cuando no se puede resolver. | `E2-T19` (`Hecho`) | `docs/modules/README.md#transactions`, `backend/app/modules/transactions/service/biometric.py` |
| OBS-2 | **(media) Observacion abierta - coherencia de config (E2-T17 `Hecho`, validacion 2026-09-27):** con las semillas por defecto el gate biometrico es **inalcanzable**: `transfer.biometric_threshold_minor`=`1_000_000` > `transfer.daily_limit_*`=`500_000`, y `evaluate_gate` prioriza `limit_exceeded` (rechaza) sobre `requires_biometry`, por lo que CA-01 no se dispara sin cambiar config. Ajustar umbral vs limite. | `E2-T18` (semilla) / `E2-T19` | `migrations/versions/0023_risk_profiles_params.py`, `docs/03b#31` |
| OBS-3 | **(media) Observacion ABIERTA/PARCIAL (E2-T17 `Hecho`, validacion 2026-09-27; E2-T19 no la endurece):** `biometric.save_block` persiste el conteo de fallos con `flush` sin `commit` (depende de que el llamante commitee el fallo) y **sin CAS por `version`**, de modo que dos fallos concurrentes pueden subcontar `failed_attempts`. `E2-T19` la deja **parcial** (el test de concurrencia de doble `authorize` va serializado por lock: evidencia debil). | endurecimiento | `docs/modules/README.md#transactions` |
| OBS-4 | **(baja) Observacion abierta (E2-T17 `Hecho`, validacion 2026-09-27):** el `nonce` solo se marca usado (`used=true`) en el exito, por lo que es **reintentable** tras un fallo mientras no venza (no es estrictamente de un solo uso); el test de concurrencia de doble `authorize` va serializado por lock (evidencia debil). | `E2-T19` / endurecimiento | `backend/tests/test_e2_t17_biometric.py`, `docs/modules/README.md#transactions` |

> **Nota (E2-T07 `Hecho`, 2026-09-26):** en la validacion (APTO) se detecto un defecto **ALTA**:
> la idempotencia del motor buscaba `idempotency_key` de forma global, de modo que un segundo
> titular que reutilizaba una clave ajena recibia la transaccion del primero (fuga de
> UUIDs/montos) sin debito propio. Resuelto acotando la busqueda al `initiator_user_id`
> (`transactions/repository.get_by_idempotency_key(..., initiator_user_id=...)`); regresion en
> `backend/tests/test_transactions_idempotency_user_scope.py` (falla sin el fix).
