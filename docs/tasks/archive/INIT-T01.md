# INIT-T01 - Esqueleto del monorepo y proyecto backend base

## Meta
| Campo | Valor |
|---|---|
| ID | `INIT-T01` |
| Modulo | `infra` |
| Tipo | `devops` |
| Sprint | `1` (fase de preparacion) |
| HU | `Transversal` |
| Dependencias | `Ninguna` |
| Estado | `Hecho` |

## Objetivo
Crear la estructura fisica del monorepo y el proyecto backend base (FastAPI) con la estructura de
modulos vacia, configuracion, healthcheck y logging, SIN logica de negocio.

## Contexto embebido
- Arquitectura: monolito modular, un paquete por bounded context; schema Postgres = nombre del modulo.
- Un modulo no importa modelos/repositorios de otro; solo fachadas o eventos.
- Dinero en centimos; sin floats.

## Context pack
- L0: este brief.
- L1: `docs/modules/README.md`.
- L2: `docs/02-arquitectura.md#5-estructura-del-repositorio-monorepo`,
  `docs/02-arquitectura.md#51-estructura-interna-de-cada-modulo`,
  `docs/02-arquitectura.md#52-reglas-de-modularidad-obligatorias`,
  `docs/16-guia-para-agentes.md#2-reglas-de-oro-no-negociables`.

## Alcance
- **Incluye:** carpetas `backend/`, `packages/`, `frontend/` (placeholder), `admin-web/`
  (placeholder), `infra/`; `pyproject.toml`, `app/main.py`, `app/core/` (config, db, errores,
  logging, seguridad), y un paquete vacio por modulo con su `README.md`.
- **No incluye:** modelos, endpoints ni migraciones con tablas (van en `INIT-T02` y demas tareas).

## Frontera de codigo
- **Puede tocar:** raiz del repo, `backend/app/`, `pyproject.toml`, README raiz.
- **Prohibido:** implementar logica de negocio aqui.

## Estructura a crear

```
banca-online/
  backend/app/{core,shared,modules,adapters}
  backend/app/modules/{identity,accounts,transactions,ledger,credits,wallet,fx,risk,reconciliation,notifications,audit,admin}
  packages/
  frontend/   (placeholder)
  admin-web/  (placeholder)
  infra/
  docs/       (ya existe)
```

Cada modulo con `api/ schemas/ domain/ service/ repository/ models/ events/ jobs/ README.md`.

## Reglas
- `app/main.py` ensambla los routers de los modulos; sin logica.
- Config por variables de entorno (`.env.example`).
- Logging JSON con `request_id`; healthcheck `/health` y `/health/ready`.
- Endpoint `GET /health` responde aunque no haya modulos implementados.

## CA
- Transversal; habilita todas las tareas del sprint.

## Pruebas
- La app levanta; `/health` responde 200; un import por modulo no rompe.

## Entregables
- [x] Monorepo + backend base + paquetes de modulos vacios.
- [x] `.env.example` y README raiz con instrucciones.
- [x] Prueba de humo del healthcheck.

## Verificación de cierre (orquestador)
Deps instaladas (`pip check` limpio), suite completa `65 passed` con
`backend/.venv`, imports base OK. Coletilla cerrada.

## Entorno
- Rama: `feature/init-monorepo`
- Ejecutar: `uvicorn app.main:app` / `pytest`
