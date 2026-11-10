# Entorno Docker de demo y healthchecks (Q-T06)

Levanta todo el entorno de sustentacion con **un solo comando**: Postgres, Redis,
backend, worker y panel admin-web. El microservicio KYC es opcional (perfil `kyc`).

## Requisitos

- Docker Engine con **Docker Compose v2** (`docker compose version`).
- Puerto libres: `5432` (Postgres), `6379` (Redis), `8000` (backend), `8080` (panel).
  Si alguno ya esta en uso (p. ej. un Postgres/Redis local), sobreescribir los puertos
  desde `.env`: `POSTGRES_PORT`, `REDIS_PORT`, `BACKEND_PORT`, `ADMIN_WEB_PORT`. La
  comunicacion entre servicios usa los nombres internos del compose y no cambia.

## Levantar

Desde la raiz del repositorio:

```bash
cp .env.example .env          # (PowerShell: Copy-Item .env.example .env)
docker compose up --build -d
```

Esperar a que todos los servicios esten `healthy`:

```bash
docker compose ps
```

| Servicio  | Healthcheck                                          | URL demo                       |
|-----------|------------------------------------------------------|--------------------------------|
| postgres  | `pg_isready`                                         | `localhost:5432`               |
| redis     | `redis-cli ping`                                     | `localhost:6379`               |
| backend   | `GET /health/ready` (readiness, incluye BD)          | `http://localhost:8000/health` |
| worker    | `GET /health` (puerto interno `WORKER_HEALTH_PORT`)  | -                              |
| admin-web | HTTP 200 sobre la pagina (espera al backend)         | `http://localhost:8080`        |

`GET /health` es liveness (siempre 200 si el proceso esta vivo).
Healthcheck **global** (`docs/02-arquitectura.md`): todos los servicios `healthy`
en `docker compose ps` y `http://localhost:8000/health/ready` responde
`{"status":"ok","database":true}`.

## Configuracion

- Plantilla: `.env.example` (raiz, para compose). Los valores por defecto funcionan sin `.env`.
- `POSTGRES_*` / `REDIS_PORT` / `BACKEND_PORT` / `ADMIN_WEB_PORT`: puertos y
  credenciales de ejemplo (sin secretos reales). Solo cambian el puerto publicado
  al host; la comunicacion interna sigue en `postgres:5432` y `redis:6379`.
- `WORKER_HEALTH_PORT`: puerto interno del `/health` del worker (defecto 8100).
- Jobs de cuotas HU12 (E3-T19): `CREDIT_LOAN_JOBS_ENABLED` (interruptor
  general, `0` = inertes), `CREDIT_LOANS_AUTO_DEBIT_ENABLED` /
  `CREDIT_LOANS_REMINDERS_ENABLED` (flags por job) y
  `CREDIT_LOANS_JOBS_INTERVAL_SECONDS` (periodicidad, defecto 3600 s).
  Corren tanto en el `lifespan` del backend como en el contenedor
  `worker` (mismo mecanismo que el worker del outbox: hilo daemon con
  sesion/`commit` propios por pasada).
- Jobs de QR HU14 (E4-T07): `WALLET_QR_JOBS_ENABLED` (interruptor
  general, `0` = inertes), `WALLET_QR_EXPIRY_ENABLED` (flag de la
  caducidad `ACTIVE` -> `EXPIRED`) y `WALLET_QR_JOBS_INTERVAL_SECONDS`
  (periodicidad, defecto 60 s). Mismo mecanismo y runtimes que los de
  cuotas (lifespan del backend + contenedor `worker`).
- `JWT_*`, `LOG_LEVEL`, `CORS_ORIGINS`, `KYC_*`: mismos nombres que usa el backend.
  No confundir con `backend/.env.example`, que es para correr el backend en local
  fuera de Docker (`localhost`) en vez de nombres de servicio (`postgres`, `redis`, `kyc`).
- Password de Postgres con caracteres especiales: URL-encodear en `POSTGRES_PASSWORD`.

El backend corre `alembic upgrade head` al arrancar, asi que `docker compose up` deja
la base migrada.

## KYC (opcional)

```bash
docker compose --profile kyc up -d
```

Requiere definir `KYC_IMAGE` (adaptador desplegado o simulador) en `.env`; sin ello el
resto del entorno funciona igual y solo el onboarding (HU01) falla al momento de llamar.

## Validar / detener

```bash
docker compose ps                                   # estado por modulo
curl http://localhost:8000/health/ready             # healthcheck global
docker compose down                                 # baja todo (conserva volumenes)
docker compose down -v                              # baja y borra datos de demo
```

## Notas

- El worker valida Postgres/Redis, expone `/health` y corre el worker
  periodico de cuotas (debito automatico + recordatorios, E3-T19; inerte
  con `CREDIT_LOAN_JOBS_ENABLED=0`). El consumidor real de otras colas
  llega con su propia tarea.
- El panel es un placeholder estatico; se reemplaza por la imagen real con la tarea
  `F-T12` (`admin-web/Dockerfile`).
- Volumenes `postgres_data` y `redis_data` para persistencia local de la demo.
- Todos los servicios usan `restart: unless-stopped`: si el worker o el backend
  fallan en el primer arranque (p. ej. `alembic upgrade head` con la BD aun
  migrando), Docker los reintenta sin intervencion.
- `.dockerignore` excluye `.env`, `.git`, `docs`, `frontend`, etc. del contexto de
  build: nada de secretos dentro de las imagenes.