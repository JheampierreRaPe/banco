"""Backfill de `entry_type` en el historial de movimientos (E2-T37, HU05).

Revision ID: 0028_backfill_entry_type
Revises: 0027_movements_entry_type
Create Date: 2026-10-09

Fuente: `docs/03b-diccionario-de-datos.md#5.4-movements_view` (columna
`entry_type VARCHAR(30)` nullable) y `docs/03b#7.2-journal_entries`
(`entry_type`, concepto contable del asiento, fuente de verdad).

`upgrade()` / `backfill_data(bind)`: copia por fila
`ledger.journal_entries.entry_type` hacia `accounts.movements_view.entry_type`
haciendo join por `movements_view.journal_entry_id = journal_entries.id`,
solo sobre filas con `entry_type IS NULL` (idempotente y re-ejecutable:
misma base -> mismo resultado). Filas huerfanas (sin asiento asociado) o
con `entry_type` nulo/vacio en el asiento (defensivo) quedan `NULL`; el
cliente ya aplica el fallback ("Movimiento") sin mostrar texto crudo.

Excepcion documentada a la regla de oro 4 (`docs/16#2`): un modulo no lee
tablas de otro en runtime, pero esta migracion es capa de datos/esquema
(corre con Alembic, sin imports de `app.modules.*`) y solo hace `SELECT`
read-only sobre `ledger.journal_entries`; no escribe en `ledger`, no toca
`postings`/asientos/cuadre y no mueve dinero. Autorizada por el brief
`docs/tasks/E2-T37.md`.

Portabilidad: Postgres usa `UPDATE ... FROM` (el `UPDATE` correlacionado
con `FROM` no existe en SQLite, que se usa en las pruebas); en cualquier
otro dialecto se usa la variante equivalente con subconsulta correlacionada
(`SET entry_type = (SELECT ...)` + `EXISTS`), que tambien corre en Postgres.

`downgrade()`: revierte solo el dato (`UPDATE ... SET entry_type = NULL`,
estado inmediatamente posterior a `0027`; el esquema no cambia). El
`upgrade` re-ejecutable repuebla; `0027.downgrade()` es quien retira la
columna.

Nota de nombre: `alembic_version.version_num` es `VARCHAR(32)` (ver
`0026_drop_preapproved_limit`); `0028_backfill_entry_type` (24 chars)
respeta el limite.
"""

import sqlalchemy as sa
from alembic import op

revision = "0028_backfill_entry_type"
down_revision = "0027_movements_entry_type"
branch_labels = None
depends_on = None

_PG_UPDATE = """UPDATE accounts.movements_view AS mv
SET entry_type = je.entry_type
FROM ledger.journal_entries AS je
WHERE mv.journal_entry_id = je.id
AND mv.entry_type IS NULL
AND je.entry_type IS NOT NULL
AND je.entry_type <> ''"""

_GENERIC_UPDATE = """UPDATE accounts.movements_view
SET entry_type = (SELECT je.entry_type FROM ledger.journal_entries AS je
WHERE je.id = accounts.movements_view.journal_entry_id)
WHERE accounts.movements_view.entry_type IS NULL
AND EXISTS (SELECT 1 FROM ledger.journal_entries AS je
WHERE je.id = accounts.movements_view.journal_entry_id
AND je.entry_type IS NOT NULL AND je.entry_type <> '')"""

_RESET_UPDATE = "UPDATE accounts.movements_view SET entry_type = NULL"


def backfill_data(bind) -> int:
    """Puebla `entry_type` NULL desde `ledger.journal_entries`; retorna filas tocadas.

    Idempotente: el filtro `entry_type IS NULL` hace que una segunda
    ejecucion no altere valores ni duplique nada. Solo lectura sobre
    `ledger` (ningun `INSERT`/`UPDATE`/`DELETE` fuera de `accounts`).
    """
    if bind.dialect.name == "postgresql":
        result = bind.execute(sa.text(_PG_UPDATE))
    else:  # SQLite de pruebas (y cualquier otro dialecto sin UPDATE..FROM).
        result = bind.execute(sa.text(_GENERIC_UPDATE))
    return int(result.rowcount or 0)


def reset_data(bind) -> int:
    """Revierte el dato (`entry_type = NULL` en todas las filas); retorna filas tocadas.

    Solo se usa desde `downgrade()` (y pruebas): deja la columna en el
    estado inmediatamente posterior a `0027` sin cambiar el esquema.
    """
    result = bind.execute(sa.text(_RESET_UPDATE))
    return int(result.rowcount or 0)


def upgrade() -> None:
    backfill_data(op.get_bind())


def downgrade() -> None:
    # Reversion solo del dato (E2-T37): no toca el esquema ni `ledger.*`.
    reset_data(op.get_bind())
