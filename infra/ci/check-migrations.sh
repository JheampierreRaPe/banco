#!/usr/bin/env bash
# Q-T05: valida las migraciones Alembic (up/down) sobre una base limpia.
#
# Uso en CI (working-directory: backend):
#   DATABASE_URL=postgresql+psycopg://banca:banca@localhost:5432/ci_migrations \
#     bash ../infra/ci/check-migrations.sh
#
# Hace: crea la BD desde cero, `upgrade head`, verifica cabeza unica,
# `downgrade -1`, `upgrade head` de nuevo y verifica el estado final.
# Cualquier error hace fallar el script (`set -euo pipefail`).
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL debe estar definido (BD dedicada, p.ej. ci_migrations)}"

python3 - <<'EOF'
import os
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url

url = make_url(os.environ["DATABASE_URL"])
db_name = url.database
if not db_name:
    raise SystemExit("DATABASE_URL sin nombre de base de datos")

maintenance = url.set(database="postgres")
engine = create_engine(maintenance, isolation_level="AUTOCOMMIT", future=True)
with engine.connect() as conn:
    conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    conn.execute(text(f'CREATE DATABASE "{db_name}"'))
engine.dispose()
print(f"BD limpia creada: {db_name}")
EOF

echo "== upgrade head (base limpia) =="
alembic upgrade head

echo "== cabezas =="
alembic heads
heads="$(alembic heads | grep -c . || true)"
if [ "$heads" -ne 1 ]; then
  echo "ERROR: se esperaba una sola cabeza de migracion, hay $heads"
  exit 1
fi

echo "== downgrade -1 =="
alembic downgrade -1

echo "== upgrade head (de nuevo) =="
alembic upgrade head

echo "== estado final =="
alembic current
echo "Migraciones OK: up/down/up sobre base limpia."
