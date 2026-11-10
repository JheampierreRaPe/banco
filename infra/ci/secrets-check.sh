#!/usr/bin/env bash
# Q-T05: garantiza que no haya secretos en el repositorio.
# Falla (exit 1) si encuentra archivos de entorno trackeados o
# patrones de secretos de alta confianza en archivos versionados.
set -euo pipefail

FAIL=0

# 1. Ningun `.env` real (ni `.env.*`) debe estar trackeado; solo `.env.example`.
tracked_env="$(git ls-files | grep -E '(^|/)\.env($|\.)' | grep -v '\.env\.example$' || true)"
if [ -n "$tracked_env" ]; then
  echo "ERROR: archivos de entorno versionados (deben estar en .gitignore):"
  echo "$tracked_env"
  FAIL=1
else
  echo "OK: ningun .env real trackeado."
fi

# 2. Patrones de secretos en archivos trackeados.
# Se excluye este propio script y el README de infra/ci (documentan los patrones).
PATTERN='-----BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36,}|gho_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}|xox[baprs]-[A-Za-z0-9-]+|AIza[0-9A-Za-z_-]{35}'
matches="$(git grep -n -E -e "$PATTERN" -- . ':!infra/ci/secrets-check.sh' ':!infra/ci/README.md' || true)"
if [ -n "$matches" ]; then
  echo "ERROR: posibles secretos en archivos versionados:"
  echo "$matches"
  FAIL=1
else
  echo "OK: sin patrones de secretos en archivos trackeados."
fi

exit "$FAIL"
