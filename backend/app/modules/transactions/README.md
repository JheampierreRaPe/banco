# Modulo transactions

Contrato y fronteras: ver `docs/modules/README.md#transactions`.

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
