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

## E5-T10 - Asientos de partida doble (HU18)

- Tablas propias: `ledger.journal_entries` + `ledger.postings` append-only
  (sin `UPDATE`/`DELETE`; el reverso marca el original `POSTED` -> `REVERSED`
  y crea un asiento compensatorio nuevo con postings intactos).
- Cuadre por moneda (`04#2`): `sum(DEBIT) == sum(CREDIT)` por moneda, dinero
  entero en centimos (nunca `float`).
- Hash encadenado SHA-256 en hex (64 chars) sobre campos canonicos del asiento
  + postings ordenados + `prev_hash` (genesis: `prev_hash None`), unidos por
  `"\n"`; punta determinista (asiento cuyo `hash` nadie referencia como
  `prev_hash`), sin depender de `created_at`.
- Fachada interna: `repository.post_entry(...)` / `reverse_entry(entry_id)`
  (a la que `service` delega); hacen `flush`, no `commit`.
- Sin eventos (regla de oro 8: la emision de `ledger.entry.posted` queda para
  outbox/E5-T05).

## E5-T11 - Validador de cuadre contable (HU18)

- Validador explicito del agregado: `domain.validate_balanced(items)` exige
  `sum(DEBIT) == sum(CREDIT)` por moneda con igualdad exacta de enteros en
  centimos (sin tolerancias, sin redondeos, sin flags de desactivacion);
  lanza `ValueError` si alguna moneda descuadra.
- `domain.validate_postings` delega siempre en `validate_balanced` (sin opcion
  de omitirlo); `repository.post_entry` valida antes de agregar filas (sin
  filas residuales al rechazar).
- Auditoria de rutas: la unica creacion es `repository.post_entry` (a la que
  `reverse_entry` y la fachada `service` delegan); ninguna ruta saltea la
  validacion, sin `float`/`round`, sin eventos.
