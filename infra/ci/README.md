# CI - Q-T05 + Q-T12

Pipeline de integracion continua (GitHub Actions): lint, pruebas **con cobertura y
umbral**, migraciones, build, escaneo, **frontend Flutter y panel admin-web**.
**Cualquier error hace fallar el pipeline** (ningun job usa `continue-on-error` y
todos los scripts usan `set -euo pipefail` / `$ErrorActionPreference = "Stop"`).

## Que hace (`.github/workflows/ci.yml`)

Se dispara con `push` a `main`, `feature/**` y `sprint-*` (cubre `sprint-2`), y con `pull_request` a `main`.

| Job | Que valida | Como |
|---|---|---|
| `lint` | Formato y estilo + secretos | `ruff check backend`, `black --check backend`, `infra/ci/secrets-check.sh` |
| `test` | Pruebas unitarias e integracion **con cobertura y umbral** | `pytest --cov=app --cov-report=term-missing --cov-fail-under=80 -q` (desde `backend/`) con Postgres 16 y Redis 7 como servicios |
| `migrations` | Migraciones `up/down` en base limpia | `infra/ci/check-migrations.sh`: crea `ci_migrations` desde cero, `upgrade head`, verifica cabeza unica, `downgrade -1`, `upgrade head` de nuevo |
| `build` | Imagenes Docker | `docker build` de `Dockerfile.backend` y `Dockerfile.worker` (corre solo si `lint`, `test` y `migrations` estan verdes) |
| `scan` | Dependencias, SAST y secretos | `pip-audit` (falla ante vulnerabilidades conocidas; antes actualiza `setuptools>=83` porque los runners pueden traer un toolchain con CVEs ya publicados) + `npm audit --omit=dev --audit-level=high` en `admin-web/` (solo dependencias de producción, que son las que se despliegan; falla ante high/critical) + `bandit --severity-level high --confidence-level high -r backend/app` (SAST; falla ante HIGH severidad + HIGH confianza) + `gitleaks` sobre el historial |
| `frontend` | App Flutter | `flutter pub get`, `flutter analyze` (falla ante cualquier issue) y `flutter test` sobre `frontend/` (Flutter 3.47.1 estable, cache activado) |
| `panel` | Panel `admin-web` | `npm ci` (lockfile), `npm run lint` (`tsc --noEmit`), `npm test` (`vitest run`) y `npm run build` sobre `admin-web/` (Node 22, cache npm) |
| `ci-success` | Agregador para proteccion de rama | Falla si algun job anterior (`lint`, `test`, `migrations`, `build`, `scan`, `frontend`, `panel`) no termino en `success` |

Verde = los 8 jobs en `success`. Rojo = basta un test fallido (o lint, migracion, build,
hallazgo de escaneo, issue de `flutter analyze`, fallo de `vitest`/`tsc`/`vite build` o
cobertura bajo el umbral) para que el pipeline falle: `pytest` retorna codigo != 0 y el
job `test` se tiñe de rojo, igual que el resto de pasos ante cualquier error.

## Reproducir en local

```bash
# Linux/macOS/Git Bash, desde la raiz del repo (Postgres local + Docker arriba):
bash infra/ci/run-local.sh [--skip-build]
```

```powershell
# Windows PowerShell, desde la raiz del repo:
.\infra\ci\run-local.ps1 [-SkipBuild]
```

Variables (con defecto local): `DATABASE_URL`, `TEST_DATABASE_URL`, `REDIS_URL` y `CI_DB_URL`
(base dedicada `ci_migrations` para la validacion de migraciones).

## Rama `main` protegida

En GitHub: *Settings > Branches > Add rule* para `main`:

- Exigir PR antes de merge (sin pushes directos).
- Exigir status checks: `ci-success` (cubre lint, test, migrations, build, scan, frontend y panel).
- Exigir ramas actualizadas antes de merge.

## Cobertura: valor medido y umbral (Q-T12)

Dueño: DevOps transversal. Dependencia de desarrollo: `pytest-cov` en
`backend/pyproject.toml` (`[dev]`). Alcance: paquete `app`
(`[tool.coverage.run] source = ["app"]`, `omit = tests/migrations`).

| Dato | Valor |
|---|---|
| Medido (2026-10-11, local sin BD: 1484 passed, 2 skipped) | **86 %** total sobre `app` |
| Umbral (`--cov-fail-under`) | **80** (= medido redondeado a la baja a multiplo de 5, ~6 pp de margen) |

Comandos locales (desde `backend/`, con el venv activo):

```bash
pip install -e ".[dev]"
python -m pytest --cov=app --cov-report=term-missing -q   # medir
python -m pytest --cov=app --cov-report=term-missing --cov-fail-under=80 -q  # umbral -> verde
```

En CI el job `test` corre el comando con umbral (paridad total con local); si la
cobertura baja de 80 %, `pytest` retorna != 0 y el pipeline se tiñe de rojo. No se
borra ni se desactiva ninguna prueba para alcanzar el umbral, y el umbral solo se
sube/baja de forma justificada y documentada aqui.

## Secretos

- Nunca hay secretos en el repo: `.env` y `.env.*` estan en `.gitignore` (solo se versiona
  `backend/.env.example` con valores `change-me`).
- `secrets-check.sh` falla si hay un `.env` real trackeado o patrones de alta confianza
  (llaves privadas, `AKIA…`, tokens `ghp_`/`gho_`, `github_pat_`, `xox…`, `AIza…`).
- `scan` corre `gitleaks` sobre el historial en cada push/PR.

## Escaneo: umbrales y comando local (Q-T08)

Dueño: DevOps/Seguridad transversal. Ningun paso usa `continue-on-error`: un hallazgo por
encima del umbral retorna codigo != 0 y tiñe el pipeline de rojo.

| Herramienta | Que cubre | Umbral / fallo | Comando local (desde la raiz del repo) |
|---|---|---|---|
| `pip-audit` | Vulnerabilidades conocidas de dependencias instaladas | Falla ante cualquier vulnerabilidad conocida reportada por la base (OSV/PyPI) | `pip install -U "setuptools>=83" && pip-audit` |
| `npm audit` (`admin-web/`, solo producción) | Vulnerabilidades conocidas de dependencias de producción del panel | Falla ante vulnerabilidades high/critical (`--audit-level=high` retorna != 0) | `cd admin-web && npm audit --omit=dev --audit-level=high` |
| `bandit` (SAST, solo `backend/app`) | Patrones inseguros en Python (p. ej. `subprocess` con shell, `pickle`, asserts) | Falla ante hallazgos HIGH severidad + HIGH confianza (flags explicitos `--severity-level high --confidence-level high`; el atajo `-ll` solo eleva severidad, por eso se usan los flags largos) | `python -m bandit --severity-level high --confidence-level high -r backend/app` (o desde `backend/`: `python -m bandit --severity-level high --confidence-level high -r app`) |
| `secrets-check.sh` + `gitleaks` | Secretos en archivos trackeados / historial git | Falla ante `.env` real trackeado o patron de alta confianza; `gitleaks detect` falla ante cualquier secreto detectado | `bash infra/ci/secrets-check.sh` y `gitleaks detect --source . --no-banner` |

Reproducir el `scan` completo en local (paridad con CI):

```bash
# Desde la raiz del repo, con el venv de backend/ activo:
pip install -e "./backend[dev]"
pip install -U "setuptools>=83" && pip-audit
npm audit --omit=dev --audit-level=high --prefix admin-web
python -m bandit --severity-level high --confidence-level high -r backend/app
bash infra/ci/secrets-check.sh
gitleaks detect --source . --no-banner  # si esta instalado; en CI via gitleaks-action
```

O con los wrappers (Windows omite el build con `-SkipBuild`):

```bash
bash infra/ci/run-local.sh [--skip-build]
```

```powershell
.\infra\ci\run-local.ps1 [-SkipBuild]
```

## Alcance y pendientes

- Las imagenes `Dockerfile.backend`/`Dockerfile.worker` son de **verificacion de build**;
  la imagen demo definitiva (backend/worker/panel + `docker-compose` + healthchecks) la
  define **Q-T06**. La imagen del panel llega con el panel (`admin-web`, tarea `F-T12`).
- El despliegue a demo (manual o por tag) tambien es de Q-T06 en adelante.
