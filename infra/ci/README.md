# CI - Q-T05

Pipeline de integracion continua (GitHub Actions): lint, pruebas, migraciones, build y
escaneo. **Cualquier error hace fallar el pipeline** (ningun job usa `continue-on-error` y
todos los scripts usan `set -euo pipefail` / `$ErrorActionPreference = "Stop"`).

## Que hace (`.github/workflows/ci.yml`)

Se dispara con `push` a `main` y `feature/**`, y con `pull_request` a `main`.

| Job | Que valida | Como |
|---|---|---|
| `lint` | Formato y estilo + secretos | `ruff check backend`, `black --check backend`, `infra/ci/secrets-check.sh` |
| `test` | Pruebas unitarias e integracion | `pytest backend/tests -v` con Postgres 16 y Redis 7 como servicios |
| `migrations` | Migraciones `up/down` en base limpia | `infra/ci/check-migrations.sh`: crea `ci_migrations` desde cero, `upgrade head`, verifica cabeza unica, `downgrade -1`, `upgrade head` de nuevo |
| `build` | Imagenes Docker | `docker build` de `Dockerfile.backend` y `Dockerfile.worker` (corre solo si `lint`, `test` y `migrations` estan verdes) |
| `scan` | Dependencias y secretos | `pip-audit` (falla ante vulnerabilidades conocidas; antes actualiza `setuptools>=83` porque los runners pueden traer un toolchain con CVEs ya publicados) + `gitleaks` sobre el historial |
| `ci-success` | Agregador para proteccion de rama | Falla si algun job anterior no termino en `success` |

Verde = los 6 jobs en `success`. Rojo = basta un test fallido (o lint, migracion, build o
hallazgo de escaneo) para que el pipeline falle: `pytest` retorna codigo != 0 y el job
`test` se tiñe de rojo, igual que el resto de pasos ante cualquier error.

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
- Exigir status checks: `ci-success` (cubre lint, test, migrations, build y scan).
- Exigir ramas actualizadas antes de merge.

## Secretos

- Nunca hay secretos en el repo: `.env` y `.env.*` estan en `.gitignore` (solo se versiona
  `backend/.env.example` con valores `change-me`).
- `secrets-check.sh` falla si hay un `.env` real trackeado o patrones de alta confianza
  (llaves privadas, `AKIA…`, tokens `ghp_`/`gho_`, `github_pat_`, `xox…`, `AIza…`).
- `scan` corre `gitleaks` sobre el historial en cada push/PR.

## Alcance y pendientes

- Las imagenes `Dockerfile.backend`/`Dockerfile.worker` son de **verificacion de build**;
  la imagen demo definitiva (backend/worker/panel + `docker-compose` + healthchecks) la
  define **Q-T06**. La imagen del panel llega con el panel (`admin-web`, tarea `F-T12`).
- El despliegue a demo (manual o por tag) tambien es de Q-T06 en adelante.
