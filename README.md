# Banca Online Integral

Proyecto academico: plataforma de banca online con billetera, prestamos y control transaccional.
Monolito modular (FastAPI + PostgreSQL) con modulos disenados para poder extraerse como
microservicios. App cliente Flutter y panel web interno.

> El dinero es **simulado**. Los terceros reales (RENIEC, centrales de riesgo, redes de pago) se
> representan con adaptadores simulados.

## Estructura

```
.
  docs/          Plan rector, arquitectura, modelo de datos y briefs de tareas
  backend/       API FastAPI (monolito modular) + migraciones Alembic
  packages/      Librerias transversales publicables (p.ej. ledger-core)
  frontend/      App Flutter (cliente y comercio)
  admin-web/     Panel web interno (operaciones, fraude, cumplimiento, auditoria)
  infra/         Docker, CI y scripts
```

## Documentacion

- Empezar por `docs/README.md` (indice).
- Logica del dinero: `docs/04-motor-transaccional-y-ledger.md`.
- Modelo de datos: `docs/03b-diccionario-de-datos.md` y `docs/03c-modelo-er.md`.
- Como trabajar una tarea: `docs/16-guia-para-agentes.md` y `docs/tasks/`.

## Arranque rapido (backend)

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

Health: `http://localhost:8000/health` - Swagger: `http://localhost:8000/docs`.

## Estado

Esqueleto inicial (fase de preparacion del Sprint 1). Los modulos estan vacios; la logica se
implementa siguiendo los briefs de `docs/tasks/SPRINT-1.md`.
