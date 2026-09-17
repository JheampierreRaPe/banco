# Modulo ledger

Contrato y fronteras: ver `docs/modules/README.md#ledger`.

Estado: catalogo contable (E5-T09) implementado; asientos y balances en tareas
posteriores del sprint.

## E5-T09 - Catalogo y subcuentas por cliente

- Tabla propia: `ledger.ledger_accounts` (`code` unico).
- Catalogo semilla (11): `1000`, `2000`, `2100`, `2200`, `3000`, `4000`,
  `4100`, `4200`, `5000`, `6000`, `9100` (migracion `0003`, idempotente por
  `ON CONFLICT (code) DO NOTHING`).
- Subcuentas por cuenta de cliente (`04#2.1`): `2000-<cuenta>` (disponible) y
  `2100-<cuenta>` (retenido), con `parent_account_id` al catalogo.
- Fachada interna: `service.ensure_customer_accounts(account_ref, currency)`
  (delega a `repository`; idempotente por `code` unico + `SAVEPOINT`).
- Desviacion de `03b#7.1`: `code` es `VARCHAR(50)` (no 30) porque
  `2000-<uuid>` ocupa 41 caracteres; el ejemplo de `03b` manda.
