# Modulo accounts

Contrato y fronteras: ver `docs/modules/README.md#accounts`.

Estado: repositorio de cuentas y proyeccion de saldos (E2-T01, Hecho).
Tablas propias: `accounts`, `account_balances`, `daily_balance_snapshots`
(sin FK entre schemas; `user_id`/`ledger_*` son UUID logicos).
Adaptadores fase 3: `AccountsBalanceAdapter` (satisface `BalancePort` de
`transactions`) y `AccountsLedgerProjectionAdapter` + consumidor
idempotente `handle_ledger_entry_posted` (satisface `AccountProjectionPort`
de `ledger`).

Endpoints (E2-T02, Hecho): `GET /api/v1/accounts` (consolidado) y
`GET /api/v1/accounts/{id}` (detalle con disponible/retenido/contable).
Esquemas en `schemas/` (Pydantic, envoltorio `{"data", "meta"}` segun
05#4); casos de uso en `service/` (lee la proyeccion `account_balances`,
`contable = disponible + retenido`, enmascarado `****1234`, RBAC por
`user_id` del JWT via `core.security.decode_token`). El `router` de `api/`
lo monta `app.main` via `iter_routers` (sin registro extra).

Movimientos y export (E2-T03, Hecho): `GET /api/v1/accounts/{id}/movements`
(paginado `page`/`page_size` default 20/max 100, filtros fecha valor
desde/hasta y `direction`, orden recientes primero, <2 s) y
`GET /api/v1/accounts/{id}/movements/export` (`format=csv|xlsx|pdf`, rango de
fechas obligatorio con 422 si falta y 400 si invertido). `format=csv` siempre
disponible (UTF-8 con BOM, cabeceras `03b#5.4`, anti-formula-injection);
`format=xlsx` exige `openpyxl` y `format=pdf` exige `reportlab`/`fpdf2`/
`weasyprint` (sin ellas -> 501 `EXPORT_FORMAT_NOT_SUPPORTED`); el PDF real
lleva titulo con cuenta/rango, tabla `03b#5.4`, montos enteros, fechas ISO y
cabecera repetida por pagina. Casos de uso en `service/` (`list_account_
movements`, `get_export_movements` sobre `get_export_movements` via E2-T04
`list_movements` sin recalcular, techos `MOVEMENTS_FETCH_LIMIT`/`EXPORT_MAX_
ROWS` 10000); misma auth/RBAC E2-T02 (401/403/404); solo lectura, dinero int
`amount_minor`, sin numeros en claro (solo UUIDs en movimientos).

Proyeccion de movimientos (E2-T04, Hecho): tabla `accounts.movements_view`
(columnas exactas `03b#5.4`, proyeccion de lectura de `ledger.postings`, sin
FK entre schemas) + consumidor idempotente `handle_movement_event` del evento
`ledger.entry.posted` (clave `processed_events` "accounts-movements", UQ
natural compuesta, re-entrega no duplica) + `rebuild_entry_movements` (borra
las filas del `entry_id` y las re-deriva; ante diferencia gana el origen
`03c#14`). Lectura via `list_movements` (recientes primero); migracion
`0010_accounts_movements.py`.
