# Sprint 4 - Indice de briefs (conciliacion, fraude, cumplimiento, multidivisa y panel)

> **Estado del indice (2026-11-09):** Sprint 4 **planificado**, 38 briefs en `Pendiente`.
> Base: `main` (`d2d038d`, Sprints 1-3 cerrados). Alcance canonico: `docs/14#5` (HU19, HU20,
> HU21, HU24, HU25 = 37 SP) + las vistas de panel de `docs/13#2.4` (`F-T12`..`F-T18`).
> Regla de entrega: **1 brief o 1 corte = 1 rama = 1 PR** (`docs/21`).

Objetivo del sprint: **blindar el banco y ampliar la propuesta de valor** con conciliacion con
excepciones, motor antifraude, cotejo PLDFT/AML, bolsillos/multidivisa y cambio de divisas, mas el
panel interno por rol.

## 1. Alcance y SP

| HU | Descripcion | Modulo | SP | Briefs |
|---|---|---|---:|---|
| HU19 | Conciliacion bancaria y redes de pago | `reconciliation` | 8 | `E5-T16`..`E5-T22` |
| HU20 | Motor antifraude por reglas y anomalias | `risk` | 8 | `E6-T01`..`E6-T06` |
| HU21 | Cumplimiento PLDFT/AML y listas restrictivas | `risk` | 8 | `E6-T07`..`E6-T11` |
| HU24 | Cuentas multidivisa y meta de ahorro | `fx` | 5 | `E7-T01`..`E7-T07` |
| HU25 | Cambio de divisas preferencial | `fx` | 8 | `E7-T08`..`E7-T13` |
| Panel | Base, RBAC y vistas internas por rol | `admin-web` (+ backend de lectura) | - | `F-T12`..`F-T18` |

**Total HU: 37 SP** (HU23/HU26 siguen fuera del MVP, `docs/01#3`). El panel no suma SP de HU: es
infraestructura de entrega de las vistas internas de HU19/HU20/HU21 y de administracion/auditoria.

## 2. Tabla de briefs

| ID | Titulo corto | Modulo | Tipo | PRs | Depende de |
|---|---|---|---|---:|---|
| `E5-T16` | Adaptador `ClearingSource` (mock + http) | `reconciliation` | adaptador | 1 | - |
| `E5-T17` | Esquema e ingesta de `clearing_files`/`clearing_items` | `reconciliation` | datos | 1 | `E5-T16` |
| `E5-T18` | Motor de matching con tolerancias | `reconciliation` | backend | 1 | `E5-T17` |
| `E5-T19` | Excepciones de conciliacion + reporte | `reconciliation` | backend | 1 | `E5-T18` |
| `E5-T20` | Reclamos y asiento de ajuste autorizado | `reconciliation` | motor-ledger | 2 | `E5-T19` |
| `E5-T21` | Panel: corridas, excepciones y reclamos | `reconciliation` + `admin-web` | frontend | 1 | `E5-T19`, `E5-T20`, `F-T12` |
| `E5-T22` | QA: match total, diferencias, huerfana, ajuste | `reconciliation` | qa | 1 | `E5-T16`..`E5-T21` |
| `E6-T01` | Modelo `risk_rules` + parametros por regla | `risk` | datos | 1 | - |
| `E6-T02` | Motor de evaluacion sincrono y asincrono | `risk` | backend | 2 | `E6-T01` |
| `E6-T03` | Perfil de comportamiento alimentado por historial | `risk` | backend | 1 | `E6-T01` |
| `E6-T04` | `alerts` + bloqueo preventivo | `risk` | backend | 1 | `E6-T02`, `E6-T03` |
| `E6-T05` | Panel: tablero de alertas del analista | `risk` + `admin-web` | frontend | 1 | `E6-T04`, `F-T12` |
| `E6-T06` | QA: calibracion, falsos positivos y bloqueo | `risk` | qa | 1 | `E6-T01`..`E6-T05` |
| `E6-T07` | Adaptador `SanctionsLists` (mock + http) con matching difuso | `risk` | adaptador | 1 | - |
| `E6-T08` | Versionado y actualizacion de listas | `risk` | datos | 1 | `E6-T07` |
| `E6-T09` | Cotejo en onboarding y transaccion + `screening_results` | `risk` | backend | 2 | `E6-T07`, `E6-T08` |
| `E6-T10` | ROS + bandeja del oficial de cumplimiento | `risk` + `admin-web` | backend + frontend | 1 | `E6-T09`, `F-T12` |
| `E6-T11` | QA: exacta, difusa, falso positivo, auditoria | `risk` | qa | 1 | `E6-T07`..`E6-T10` |
| `E7-T01` | Esquema `fx` (`fx_rates`/`fx_quotes`/`fx_operations`/`savings_pockets`/`pocket_movements`) | `fx` | datos | 1 | - |
| `E7-T02` | Apertura de bolsillos y cuentas multidivisa | `fx` + `accounts` (fachada) | backend | 2 | `E7-T01` |
| `E7-T03` | Redondeo automatico integrado al flujo de pago | `fx` | backend | 1 | `E7-T02`, `E7-T05` |
| `E7-T04` | Job de rendimientos de bolsillos | `fx` + `transactions` (fachada) | backend | 2 | `E7-T02` |
| `E7-T05` | Traspasos entre bolsillos/cuentas via motor | `fx` | motor-ledger | 1 | `E7-T02` |
| `E7-T06` | UI Flutter: bolsillos y metas | `frontend` | frontend | 1 | `E7-T02`, `E7-T05` |
| `E7-T07` | QA: redondeo, rendimiento y traspaso | `fx` | qa | 1 | `E7-T01`..`E7-T06` |
| `E7-T08` | Adaptador `FxRateProvider` con cache y TTL | `fx` | adaptador | 1 | - |
| `E7-T09` | `fx_quotes` con temporizador y vigencia | `fx` | backend | 1 | `E7-T01`, `E7-T08` |
| `E7-T10` | Spread dinamico configurable por volumen | `fx` | backend | 1 | `E7-T09` |
| `E7-T11` | Ejecucion del cambio con dos asientos ligados | `fx` + `transactions` (fachada) | motor-ledger | 2 | `E7-T09`, `E7-T10`, `E7-T02` |
| `E7-T12` | UI Flutter: cotizacion y confirmacion con temporizador | `frontend` | frontend | 1 | `E7-T11` |
| `E7-T13` | QA: expiracion, spread y cuadre por moneda | `fx` | qa | 1 | `E7-T08`..`E7-T12` |
| `F-T12` | Base del panel, sesion y RBAC por rol | `admin-web` | frontend | 2 | - |
| `F-T13` | Integracion del panel de operaciones/conciliacion | `admin-web` | frontend | 1 | `F-T12`, `E5-T21` |
| `F-T14` | Integracion del panel de fraude | `admin-web` | frontend | 1 | `F-T12`, `E6-T05` |
| `F-T15` | Integracion del panel de cumplimiento | `admin-web` | frontend | 1 | `F-T12`, `E6-T10` |
| `F-T16` | Panel de credito: bandeja, detalle y dictamen completos | `admin-web` | frontend | 1 | `F-T12` |
| `F-T17` | Panel de parametros y roles | `admin-web` + `admin`/`identity` | frontend + backend | 2 | `F-T12` |
| `F-T18` | Vista de auditoria y ledger (solo lectura) | `audit` + `ledger` + `admin-web` | backend + frontend | 2 | `F-T12` |

## 3. Orden de construccion propuesto

### Fase 0 - cimientos (paralelizables)

1. `F-T12` (shell + RBAC): desbloquea **todas** las vistas de panel.
2. `E5-T16` (adaptador de clearing): sin dependencias.
3. `E6-T01` (`risk_rules`): desbloquea `E6-T02`/`E6-T03`.
4. `E6-T07` (adaptador de listas): desbloquea `E6-T08`/`E6-T09`.
5. `E7-T01` (esquema `fx`): desbloquea HU24/HU25.
6. `E7-T08` (adaptador FX): puede ir en paralelo con `E7-T01`.

### Fase 1 - datos y dominio

7. `E5-T17` -> `E5-T18` -> `E5-T19` -> `E5-T20`.
8. `E6-T02` -> `E6-T03` -> `E6-T04`.
9. `E6-T08` -> `E6-T09` -> `E6-T10`.
10. `E7-T02` -> `E7-T05` -> `E7-T03`/`E7-T04`.
11. `E7-T09` -> `E7-T10` -> `E7-T11`.

### Fase 2 - UI y QA

12. `E5-T21`, `E6-T05`, `E6-T10` (pantalla), `E7-T06`, `E7-T12`.
13. `F-T13`, `F-T14`, `F-T15`, `F-T16`, `F-T17`, `F-T18` sobre `F-T12`.
14. `E5-T22`, `E6-T06`, `E6-T11`, `E7-T07`, `E7-T13` al cierre de cada HU.

## 4. Dependencias criticas

- **Panel:** `F-T12` antes de `F-T13`..`F-T18`; sin shell/RBAC no se integran vistas.
- **Conciliacion:** `E5-T16` -> `E5-T17` -> `E5-T18` -> `E5-T19` -> `E5-T20`; `E5-T21` necesita
  `E5-T19`/`E5-T20` y `F-T12`. `E5-T20` cruza `reconciliation` y una **fachada aditiva** de
  `transactions` (asiento de ajuste); se declara como excepcion de 2 modulos.
- **Fraude:** `E6-T01` -> `E6-T02`; `E6-T03` puede ir en paralelo con `E6-T02`; `E6-T04` necesita
  ambos. `E6-T02` agrega un **hook sincrono** en `transactions` (pre-transaccion).
- **Serializacion de `transactions`:** las fachadas/hook de `E6-T02`, `E7-T04` y `E7-T11` tocan
  `backend/app/modules/transactions/service/`; **no** se ejecutan en paralelo. Orden recomendado:
  `E6-T02` -> `E7-T04` -> `E7-T11` (o el que el orquestador decida, uno por vez).
- **Cumplimiento:** `E6-T07` -> `E6-T08` -> `E6-T09` -> `E6-T10`. `E6-T09` reutiliza el hook de
  `E6-T02` para el cotejo sincrono; si el hook no existe, lo crea en su primer corte.
- **Multidivisa:** `E7-T01` -> `E7-T02` -> `E7-T05` -> `E7-T03`/`E7-T04`. `E7-T02` agrega una
  **fachada aditiva** `accounts.service.open_customer_account` (solo `accounts` escribe `accounts`).
- **Divisas:** `E7-T01` -> `E7-T09` -> `E7-T10` -> `E7-T11`. `E7-T11` agrega una **fachada aditiva**
  `transactions.service.post_fx_exchange` (dos asientos ligados, `docs/04#7.7`).
- **Migraciones:** un solo head. Orden tentativo: `0048_reconciliation` (`E5-T17`) ->
  `0049_risk_rules` (`E6-T01`) -> `0050_risk_alerts` (`E6-T04`) -> `0051_risk_screening`
  (`E6-T09`) -> `0052_risk_ros` (`E6-T10`) -> `0053_fx` (`E7-T01`) -> `0054_fx_params`
  (`E7-T04`; `E7-T10` reutiliza la semilla si ya existe). Cada worker debe comprobar
  `alembic heads` y **renumerar** si otro brief tomo el numero; `alembic check` limpio.
- **Regla de dinero:** ningun brief escribe `transactions`/`ledger`/`accounts` de otro modulo; los
  ajustes y cambios de divisa pasan por fachadas del motor y terminan en `ledger`.

## 5. Regla anti-duplicacion de UI de panel

`E5-T21`, `E6-T05` y `E6-T10` construyen la **pantalla de su dominio** en
`admin-web/src/features/<dominio>/` (tablas, hooks, detalle, acciones y pruebas de componente).
`F-T13`, `F-T14` y `F-T15` **no reconstruyen** esas pantallas: integran rutas, guards por rol,
menu y pruebas E2E de acceso, y declaran explicitamente "no incluye la pantalla de dominio".
Si el dueno prefiere que las `F-T` sean duenas de la UI, se intercambia el alcance antes de
empezar (decision abierta D-S4-01).

## 6. Riesgos del sprint

| Riesgo | Impacto | Mitigacion |
|---|---|---|
| Reglas antifraude con falsos positivos que rompen flujos existentes | Alto | Reglas con umbrales conservadores + `enabled` configurable; regresion de suites E2/E4 en `E6-T06`. |
| Hook sincrono de `risk` en `transactions` acopla modulos | Alto | Fachada `risk.service.evaluate_pre_transaction`; `transactions` solo la invoca; sin imports de modelos. |
| Ajuste de conciliacion tocando el asiento original | Alto | Solo asiento nuevo (`docs/04#7.9`); prohibido editar/borrar. |
| Cotejo de listas con PII en logs o tablas | Alto | Solo hash/referencia; narrativa ROS sin PII; enmascarar en panel. |
| Dos asientos FX descuadrados o cruzando monedas en uno solo | Alto | `docs/04#7.7`: un asiento por moneda, mismo `fx_operation_id`; QA `E7-T13`. |
| Migraciones divergentes | Medio | Orden unico + `alembic heads`/`alembic check`; renumerar al integrar. |
| Panel sin autenticacion real de internos | Medio | `F-T12` mantiene el JWT pegado del bootstrap y RBAC; login de panel es decision abierta D-S4-02. |
| HU24/HU25 son `Should` y pueden recortarse | Medio | Orden de recorte `docs/14#8`: primero `HU24`/`HU25`; el resto del sprint no depende de ellas. |

## 7. Decisiones abiertas que requieren al dueno

| ID | Decision | Recomendacion |
|---|---|---|
| D-S4-01 | Dueno de la UI de panel: `E5-T21`/`E6-T05`/`E6-T10` vs `F-T13`/`F-T14`/`F-T15` | Mantener la regla anti-duplicacion de la seccion 5 (E construye pantalla; F integra rutas/RBAC/E2E). |
| D-S4-02 | Autenticacion del panel interno | Mantener el JWT pegado del bootstrap `E3-T10` + RBAC por rol para el MVP; no hay endpoint de login de panel en `docs/05`. |
| D-S4-03 | Ajuste de conciliacion que afecta una cuenta de cliente | Fachada aditiva `transactions.service.post_reconciliation_adjustment`; si el dueno prefiere solo cuentas internas, se restringe a `2200`/`6000`/`4100`. |
| D-S4-04 | Versionado de listas restrictivas sin tabla en `docs/03b` | Archivos semilla versionados en `backend/app/adapters/data/sanctions/` + version en `config.parameters` y auditoria; sin tabla nueva. |
| D-S4-05 | Cuenta contable del rendimiento de bolsillos | Usar `6000-Gastos operativos` como gasto y `2000-<pocket>` como abono; si tesoreria define otra, sembrarla en `config.parameters`. |
| D-S4-06 | Endpoints de usuarios/roles del panel | `F-T17` implementa parametros + consulta/asignacion por `user_id` con las rutas de `docs/05#6.8`; el listado de usuarios internos queda como decision abierta. |
| D-S4-07 | Lectura de ledger para auditor | Agregar `GET /ledger/entries` (solo lectura, rol auditor) en `F-T18`; no existe en `docs/05` y se documentara como aditivo. |

## 8. Criterio de cierre del Sprint 4

- Conciliacion: clearing procesado, cruce con diferencias documentadas, excepciones y reclamos con
  ajuste contable **nuevo** (nunca edicion); `E5-T22` en verde.
- Fraude: reglas configurables sin reiniciar, alertas < 1 s, bloqueo preventivo funcional y tablero
  del analista; `E6-T06` en verde y suites previas intactas.
- Cumplimiento: onboarding y transacciones cotejadas contra listas simuladas, `screening_results`
  persistidos, ROS generado y auditado; `E6-T11` en verde.
- Multidivisa: bolsillos y cuentas USD/EUR, redondeo, rendimientos y traspasos por el motor;
  `E7-T07` en verde.
- Divisas: cotizacion congelada 30 s, spread por volumen, ejecucion con dos asientos ligados y
  rechazo por expiracion; `E7-T13` en verde.
- Panel: cada rol ve solo sus vistas, datos enmascarados, acciones auditadas y sin edicion de
  asientos; `F-T12`..`F-T18` con pruebas.
- `alembic check` limpio (un solo head), `pytest`/`ruff`/`black` y `flutter analyze`/`flutter test`
  en verde; sin PII/secretos en logs; 0 PRs > 700 lineas semanticas (`docs/21`).

## 9. Reglas transversales embebidas en cada brief

- Dinero entero en centimos; nunca `float`.
- Toda operacion de dinero pasa por `transactions` y termina en `ledger`; no se edita ni borra un
  asiento/transaccion/auditoria, se revierte.
- Ningun modulo accede a tablas de otro: fachadas o eventos (`outbox`/`processed_events`).
- `Idempotency-Key` obligatoria en endpoints que mueven dinero o crean recursos.
- Reglas/umbrales/tasas en `config.parameters` via `shared/config` (regla de oro 6).
- Sin PII ni secretos en logs, respuestas, eventos ni auditoria; cuentas/documentos enmascarados.
- Cambio de ORM => migracion Alembic + un solo head + `alembic check` limpio.
- Un brief o un corte declarado = una rama = un PR; presupuesto 300-500 lineas semanticas (tope 700).
- No debilitar pruebas existentes; no tocar `kyc-service/`.
