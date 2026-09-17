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
  `AUTHORIZED`/`REJECTED`; `AUTHORIZED` -> `FUNDS_HELD`/`REJECTED`;
  `FUNDS_HELD` -> `POSTED`/`FAILED`; `POSTED` ->
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
