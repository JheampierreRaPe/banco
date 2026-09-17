# INIT-T02 - Migraciones multi-esquema y esquemas base

## Meta
| Campo | Valor |
|---|---|
| ID | `INIT-T02` |
| Modulo | `infra` |
| Tipo | `datos` |
| Sprint | `1` (fase de preparacion) |
| HU | `Transversal` |
| Dependencias | `INIT-T01` |
| Estado | `Hecho` |

## Objetivo
Configurar Alembic para PostgreSQL con un esquema por modulo, crear todos los esquemas vacios, la
tabla transversal `config.parameters` y su semilla, y dejar las convenciones de migraciones.

## Contexto embebido
- Un schema por modulo con su nombre (`identity`, `accounts`, ...).
- **No hay FK entre schemas**; las referencias cruzadas son logicas (`REF`).
- Dinero en `*_minor BIGINT`; IDs `UUID`; `created_at`/`updated_at`.
- Toda regla configurable vive en `config.parameters`.

## Context pack
- L0: este brief.
- L1: `docs/modules/README.md#shared--config-infraestructura`.
- L2: `docs/03b-diccionario-de-datos.md#0-convenciones-y-leyenda`,
  `docs/03b-diccionario-de-datos.md#3-schema-config`,
  `docs/02-arquitectura.md#52-reglas-de-modularidad-obligatorias`.

## Alcance
- **Incluye:** Alembic configurado, creacion de los 13 esquemas, tabla `config.parameters` + seed
  de parametros iniciales, y convenciones (`docs/03b` §0).
- **No incluye:** tablas de negocio (las crea cada modulo en su tarea).

## Frontera de codigo
- **Puede tocar:** `backend/migrations/`, `backend/app/core/db.py`, `config/`.
- **Prohibido:** crear tablas de negocio aca.

## Modelo de datos
`config.parameters` (key, value_json, module, description, updated_by, created_at, updated_at).

## Reglas
- Migraciones reversibles (`up`/`down`) y revisadas por el responsable de datos.
- Nombres de esquema exactamente iguales al modulo.
- Parametros semilla: `transfer.biometric_threshold_minor`, `transfer.daily_limit_minor`,
  `qr.express_limit_minor`, `otp.ttl_seconds`, `otp.max_resends`, `auth.max_failed_attempts`,
  `session.inactivity_seconds`, `kyc.match_threshold`, `kyc.max_attempts`, `loan.max_dti_ratio`,
  `fx.quote_ttl_seconds`, `fx.spread_base`, `pocket.yield_rate`.

## CA
- Transversal; base de todas las tareas de datos.

## Pruebas
- `alembic upgrade head` y `downgrade` en base limpia; esquemas creados; seed cargado.

## Entregables
- [x] Alembic multi-esquema + esquemas vacios.
- [x] `config.parameters` + seed.
- [x] Documento de convenciones de migraciones.

## Verificación de cierre (orquestador)
`alembic upgrade head` ejecutado OK en Postgres local
(`0001` → `0002` → `0003` → `0004`). Coletilla cerrada.

## Entorno
- Rama: `feature/init-migrations`
- Comandos: `alembic upgrade head` / `alembic downgrade base`
