# infra

Infraestructura, contenedores y automatizacion.

- `docker/`: Dockerfiles y `docker-compose` del entorno de demo (Postgres, Redis, backend, worker,
  panel). Ver tarea `Q-T06`.
- `ci/`: workflows de integracion continua (lint, pruebas, migraciones, build). Ver tarea `Q-T05`.
- Un solo comando debe levantar el entorno de sustentacion.

Variables en `.env` (plantilla `.env.example` en `backend/`). Nunca secretos en el repositorio.
