# Q-T05: reproduce el pipeline CI en Windows (paridad con run-local.sh).
# Uso desde la raiz del repo:
#   .\infra\ci\run-local.ps1 [-SkipBuild]
param([switch]$SkipBuild)
$ErrorActionPreference = "Stop"
$ROOT = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$BackendVenv = Join-Path $ROOT "backend\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $BackendVenv)) { $BackendVenv = "python" }

if (-not $env:DATABASE_URL) { $env:DATABASE_URL = "postgresql+psycopg://banca:banca@localhost:5432/banca" }
if (-not $env:TEST_DATABASE_URL) { $env:TEST_DATABASE_URL = "postgresql+psycopg://banca:banca@localhost:5432/banca_test" }
if (-not $env:REDIS_URL) { $env:REDIS_URL = "redis://localhost:6379/0" }

Write-Output "==> [1/5] lint (ruff + black + secretos)"
& $BackendVenv -m ruff check "$ROOT\backend"; if ($LASTEXITCODE -ne 0) { exit 1 }
& $BackendVenv -m black --check "$ROOT\backend"; if ($LASTEXITCODE -ne 0) { exit 1 }
# secrets-check.sh requiere Git Bash; alternativa PowerShell si git-bash no existe:
$bash = (Get-Command bash -ErrorAction SilentlyContinue)
if ($bash) { & bash "$ROOT/infra/ci/secrets-check.sh"; if ($LASTEXITCODE -ne 0) { exit 1 } }
else {
  $tracked = (git ls-files | Select-String '(^|/)\.env($|\.)' | Where-Object { $_ -notmatch '\.env\.example$' })
  if ($tracked) { Write-Output "ERROR: .env versionados:`n$tracked"; exit 1 }
  Write-Output "OK: ningun .env real trackeado (bash no disponible; chequeo de patrones solo en CI)."
}

Write-Output "==> [2/5] pruebas"
& $BackendVenv -m pytest "$ROOT\backend\tests" -q; if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Output "==> [3/5] migraciones (up/down en base limpia)"
if (-not $env:CI_DB_URL) { $env:CI_DB_URL = "postgresql+psycopg://banca:banca@localhost:5432/ci_migrations" }
Push-Location "$ROOT\backend"
try {
  $env:DATABASE_URL = $env:CI_DB_URL
  if ($bash) { & bash "$ROOT/infra/ci/check-migrations.sh"; if ($LASTEXITCODE -ne 0) { exit 1 } }
  else { Write-Output "bash no disponible: migraciones se validan en CI (job migrations)."; }
} finally { Pop-Location }

if ($SkipBuild) { Write-Output "==> [4/5] build: omitido (-SkipBuild)" }
else {
  Write-Output "==> [4/5] build (imagenes Docker)"
  docker build -f "$ROOT/infra/ci/Dockerfile.backend" -t banca-backend:ci "$ROOT"; if ($LASTEXITCODE -ne 0) { exit 1 }
  docker build -f "$ROOT/infra/ci/Dockerfile.worker" -t banca-worker:ci "$ROOT"; if ($LASTEXITCODE -ne 0) { exit 1 }
}

Write-Output "==> [5/5] escaneo de dependencias"
& $BackendVenv -m pip_audit; if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Output "CI local OK: todo verde."
