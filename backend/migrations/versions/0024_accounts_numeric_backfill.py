"""Backfill de numeros de cuenta a 14 digitos con digito verificador (E2-T28, HU05/HU06).

Revision ID: 0024_accounts_numeric_backfill
Revises: 0023_risk_profiles_params
Create Date: 2026-10-04

Reasigna un numero valido de 14 digitos (`001` + cuerpo de 10 digitos + DV
modulo 11 con pesos ciclicos 2..7, `10`/`11` -> `0`; decision del dueno
2026-10-03) a cada fila de `accounts.accounts` cuyo `account_number` actual
NO cumpla ya el formato/DV (p. ej. el hex heredado `001-<12 hex>` como
`001-01a7124e8ebe`: letras y guion nunca validan). Las filas que ya cumplen
se conservan intactas (idempotente y re-ejecutable).

Asignacion determinista y unica: se recorre el universo ordenado por
`(created_at, id)` y se asigna un cuerpo secuencial desde `0000000001`
(saltando los cuerpos ya ocupados por numeros conservados), garantizando
unicidad sin colisionar con lo conservado. Solo toca la columna
`account_number` (conserva `user_id`, `type`, `currency`; no toca saldos,
asientos, `movements_view` —que referencia por UUID— ni beneficiarios —que
guardan cifrado/hash + `last4`—). Sin cambio de tipo: la columna sigue
`String(20)` UQ (14 digitos caben).

`downgrade` es no-op documentado: el valor hex original se sobreescribe y no
es reconstruible (no hay tabla auxiliar: el numero anterior no se necesita
para operar —todas las referencias son por UUID—); no borra ni altera datos.
"""

import re

import sqlalchemy as sa
from alembic import op

revision = "0024_accounts_numeric_backfill"
down_revision = "0023_risk_profiles_params"
branch_labels = None
depends_on = None

_PREFIX = "001"
_BODY_LEN = 10
_FULL_RE = re.compile(r"^001[0-9]{11}$")
_FIRST_BODY = 1
_LAST_BODY = 10**_BODY_LEN - 1


def _check_digit(first13: str) -> int:
    """DV modulo 11 (copia congelada del algoritmo de E2-T28, no importar `app`)."""
    total = 0
    weight = 2
    for digit in reversed(first13):
        total += int(digit) * weight
        weight = weight + 1 if weight < 7 else 2
    check = 11 - (total % 11)
    return 0 if check >= 10 else check


def is_conforming(value: object) -> bool:
    """`True` si ya es cuenta de 14 digitos con prefijo `001` y DV valido."""
    if not isinstance(value, str) or _FULL_RE.fullmatch(value) is None:
        return False
    return _check_digit(value[:13]) == int(value[13])


def plan_backfill(existing: list[tuple[str, str]]) -> dict[str, str]:
    """Plan puro `{id: nuevo_numero}` solo para filas no conformes.

    `existing`: pares `(id, account_number)` ya ordenados por
    `(created_at, id)`. Determinista: misma entrada -> mismo plan; unico:
    ningun numero asignado repite uno conservado ni otro asignado.
    """
    used = {number for _, number in existing if is_conforming(number)}
    plan: dict[str, str] = {}
    body = _FIRST_BODY
    for row_id, number in existing:
        if is_conforming(number):
            continue
        while body <= _LAST_BODY:
            candidate = (
                f"{_PREFIX}{body:0{_BODY_LEN}d}{_check_digit(f'{_PREFIX}{body:0{_BODY_LEN}d}')}"
            )
            body += 1
            if candidate not in used:
                break
        else:  # pragma: no cover - espacio de 10^10 cuerpos, inagotable en la practica
            raise RuntimeError("espacio de cuerpos de 10 digitos agotado en el backfill")
        used.add(candidate)
        plan[str(row_id)] = candidate
    return plan


def backfill_data(bind) -> int:
    """Aplica el backfill sobre la conexion dada; retorna filas reasignadas."""
    rows = bind.execute(
        sa.text("SELECT id, account_number FROM accounts.accounts ORDER BY created_at, id")
    ).all()
    plan = plan_backfill([(str(row[0]), row[1]) for row in rows])
    for row_id, new_number in plan.items():
        bind.execute(
            sa.text("UPDATE accounts.accounts SET account_number = :number WHERE id = :id"),
            {"number": new_number, "id": row_id},
        )
    return len(plan)


def upgrade() -> None:
    backfill_data(op.get_bind())


def downgrade() -> None:
    # No-op documentado (E2-T28): el hex original se sobreescribio y no es
    # reconstruible; no hay nada que revertir sin inventar datos. No borra
    # ni altera filas (el downgrade de migraciones vecinas sigue operando).
    pass
