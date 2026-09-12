#!/usr/bin/env bash
# Q-T05: reproduce el pipeline CI en local (paridad con .github/workflows/ci.yml).
# Requiere: Python 3.11 con el venv de backend/ activo, Postgres local y Docker.
# Uso desde la raiz del repo:  bash infra/ci/run-local.sh [--skip-build]
set -euo pipefail

SKIP_BUILD=0
for arg in "$@"; do
  case "$arg" in
    --skip-build) SKIP_BUILD=1 ;;
    *) echo "Flag desconocido: $arg (solo --skip-build)"; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://banca:banca@localhost:5432/banca}"
export TEST_DATABASE_URL="${TEST_DATABASE_URL:-postgresql+psycopg://banca:banca@localhost:5432/banca_test}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"

echo "==> [1/5] lint (ruff + black + secretos)"
ruff check "$ROOT/backend"
black --check "$ROOT/backend"
bash "$ROOT/infra/ci/secrets-check.sh"

echo "==> [2/5] pruebas"
pytest "$ROOT/backend/tests" -q

echo "==> [3/5] migraciones (up/down en base limpia)"
CI_DB_URL="${CI_DB_URL:-postgresql+psycopg://banca:banca@localhost:5432/ci_migrations}"
(
  cd "$ROOT/backend"
  DATABASE_URL="$CI_DB_URL" bash "$ROOT/infra/ci/check-migrations.sh"
)

if [ "$SKIP_BUILD" -eq 1 ]; then
  echo "==> [4/5] build: omitido (--skip-build)"
else
  echo "==> [4/5] build (imagenes Docker)"
  docker build -f "$ROOT/infra/ci/Dockerfile.backend" -t banca-backend:ci "$ROOT"
  docker build -f "$ROOT/infra/ci/Dockerfile.worker" -t banca-worker:ci "$ROOT"
fi

echo "==> [5/5] escaneo de dependencias"
if command -v pip-audit >/dev/null 2>&1; then
  pip-audit
else
  echo "pip-audit no instalado; instalalo con: pip install pip-audit"
  exit 1
fi

echo "CI local OK: todo verde."
