# Modulo transactions

Contrato y fronteras: ver `docs/modules/README.md#transactions`.

## E5-T01 - Maquina de estados del motor (HU17)

- Dominio puro (`domain/state_machine.py`): sin BD ni HTTP; solo el motor
  cambia de estado (cada cambio deja historia en `transaction_status_history`
  en la capa de persistencia).
- Estados (11): `INITIATED`, `VALIDATED`, `PENDING_AUTHORIZATION`,
  `AUTHORIZED`, `FUNDS_HELD`, `POSTED`, `SETTLED`, `CONCILIATED`, `REJECTED`,
  `FAILED`, `REVERSED`.
- Transiciones validas: `INITIATED` -> `VALIDATED`/`REJECTED`; `VALIDATED` ->
   `PENDING_AUTHORIZATION`/`AUTHORIZED`/`REJECTED`; `PENDING_AUTHORIZATION` ->
   `AUTHORIZED`/`REJECTED`; `AUTHORIZED` -> `FUNDS_HELD`/`POSTED` (atomica propia
   E2-T07)/`REJECTED`; `FUNDS_HELD` -> `POSTED`/`FAILED`; `POSTED` ->
  `SETTLED`/`FAILED`/`REVERSED`; `SETTLED` -> `CONCILIATED`/`REVERSED`;
  `CONCILIATED` -> `REVERSED`. Toda transicion no listada lanza
  `InvalidTransitionError` (incluye reintentos/doble procesamiento).
- Actores por transicion (`SYSTEM`/`USER`/`ANALYST` segun `actor_type` de
  `03b` 6.2): casi todo `SYSTEM`; `PENDING_AUTHORIZATION` -> `AUTHORIZED`
  admite `USER`/`SYSTEM`; `POSTED`/`SETTLED`/`CONCILIATED` -> `REVERSED`
  admite `SYSTEM`/`ANALYST`. Actor no autorizado lanza
  `UnauthorizedActorError`.
- Terminales: `REJECTED`, `FAILED`, `REVERSED` (`is_terminal`); `CONCILIATED`
  no es estrictamente terminal (puede ir a `REVERSED` ante descuadre).
- `FAILED` es tecnico y siempre libera holds (`requires_hold_release`);
  `REJECTED` es de negocio (sin movimiento, solo antes de retener).
- `guarded_transition` con optimistic locking (`expected_version` vs
  `current_version`; lanza `ConcurrencyError` ante doble procesamiento
  concurrente) y validaciones `amount_minor` entero positivo / moneda
  ISO-4217. Sin endpoints ni eventos en esta tarea.

## E5-T02 - Persistencia del motor (HU17)

- Modelos (`models/`): `transactions`, `transaction_status_history`
  (append-only), `holds` en el schema `transactions` segun
  `docs/03b-diccionario-de-datos.md#6-schema-transactions`.
  Dinero en centimos (`amount_minor > 0`, `fee_minor >= 0`); columna DB
  `metadata` expuesta como atributo `meta`. Sin FK a otros schemas.
- Repositorio (`repository/`): CRUD, `get_by_idempotency_key`,
  `transition_transaction` (valida con la maquina de E5-T01, historial por
  transicion, optimistic locking por `version`, `settled_at` en `SETTLED`) y
  holds (`create/update/expire_due_holds`; solo se sale de `ACTIVE`).
  Hace `flush`, no `commit`; no publica eventos (usa `outbox` la capa de
  servicio).
- Migracion: `migrations/versions/0002_transactions_persistence.py`.
- Sin endpoints (capa de datos). Sin acceso a `ledger` ni `accounts`.

## E5-T03 - Caso de uso central del motor (HU17)

- `service/execute_transfer` atomico (solo `flush`, nunca `commit`): valida entrada pura,
  crea `INITIATED` -> `VALIDATED` -> `AUTHORIZED`, bloquea saldos en orden estable via
  `BalancePort`, retiene (`create_hold` + asiento `TRANSFER_HOLD` `2000-S -> 2100-S` por
  fachada `ledger`), avanza a `FUNDS_HELD` -> `POSTED` y, si `settle=True`, liquida
  (`TRANSFER_SETTLE` `2100-S -> 2000-D` + fee a `4100`, E2-T07) a `SETTLED`; sin fondos -> `REJECTED`.
  Fallo tecnico: holds a `RELEASED` + `FAILED` best-effort y re-lanza (rollback total).

## E5-T04 - Holds e idempotencia (HU17)

- Holds `ACTIVE` -> `CAPTURED`/`RELEASED`/`EXPIRED` (`expire_due_holds`); idempotencia por
  `idempotency_key` (`get_by_idempotency_key`: clave repetida retorna la operacion original).
  Eventos solo por `outbox` (`funds.held`/`transfer.settled`/`funds.released`, import perezoso).

## E5-T06 - Reverso autorizado (HU17)

- Reverso como asiento compensatorio nuevo (nunca borra el original); transiciones
  `POSTED`/`SETTLED`/`CONCILIATED` -> `REVERSED` con actor `SYSTEM`/`ANALYST` (ver E5-T01).

## E2-T07 - Transferencias propia y a tercero (HU06)

- Casos de uso (`service/transfer_own`, `service/transfer_third_party`): validan
  origen/destino por la fachada `accounts.service.get_account` (D-01: existen,
  `ACTIVE`, moneda coherente, origen del titular; `NOT_FOUND`/`NOT_AUTHORIZED`/
  `VALIDATION_ERROR`) y delegan en `execute_transfer`.
- `OWN_TRANSFER` = ruta atomica SIN hold (`atomic=True`, `04#7.1`): un solo asiento
  `OWN_TRANSFER` `2000-S -> 2000-D` sin comision (`fee` distinto de 0 se rechaza),
  sin fila en `holds`, `AUTHORIZED -> POSTED -> SETTLED` (nueva transicion
  `AUTHORIZED -> POSTED` en la maquina de E5-T01, actor `SYSTEM`) y outbox
  `transaction.initiated` + `transfer.settled` (nunca `funds.held`).
- `THIRD_PARTY_TRANSFER` = camino hold + liquidacion (`04#7.2`); la comision se
  acredita a **`4100-Ingresos por comisiones`** (`FEE_ACCOUNT_CODE`, cierra G05;
  antes `4000`). `fee_minor` explicita o `transfer.third_party_fee_minor` de
  `config.parameters` (best-effort con fallback `0`; sin regla cableada).
- Endpoints (`api/`, esquemas en `schemas/`): `POST /transfers/own` y
  `POST /transfers/third-party` (`Idempotency-Key` obligatoria; validacion de
  fachada antes del reclamo para no consumir la clave; insuficiente -> `201` con
  `status=REJECTED` + `failure_code=INSUFFICIENT_FUNDS`, sin tocar saldos).
- Pruebas: `tests/test_e2_t07_transfers.py` (propia sin hold, tercero con fee a
  `4100`, insuficiente, errores, idempotencia, concurrencia sin sobregiro, HTTP).
