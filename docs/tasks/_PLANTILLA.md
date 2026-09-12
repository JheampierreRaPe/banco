# <ID> - <Titulo corto de la tarea>

> Copiar este archivo a `tasks/<ID>.md` y rellenar. El brief debe ser **autosuficiente**.

## Meta

| Campo | Valor |
|---|---|
| ID | `<E#-T## / F-T## / Q-T##>` |
| Modulo | `<identity / accounts / ledger / ...>` |
| Tipo | `dominio / motor-ledger / adaptador / frontend / datos / qa / devops` |
| Sprint | `<1..4>` |
| HU relacionadas | `<HU0X>` |
| Prioridad | `Must / Should / Could` |
| Estado | `Pendiente / En progreso / Hecho` |
| Dependencias | `<IDs de tareas que deben estar listas>` |

## Objetivo

<Resultado verificable en 1-3 frases.>

## Alcance

- **Incluye:** <...>
- **No incluye:** <...>

## Contexto embebido (decisiones que aplican)

<Copiar aqui las reglas exactas del proyecto que el agente necesita, sin obligarlo a inferirlas.>

- <Decision / regla 1>
- <Decision / regla 2>

## Context pack (leer solo esto)

- L0: este brief.
- L1: `docs/modules/README.md` -> seccion `<modulo>`.
- L2:
  - `docs/<archivo>#<ancla>`
  - `docs/<archivo>#<ancla>`
- Prohibido leer: `<...>`.

## Frontera de codigo

- **Puede tocar:** `<rutas de este modulo>`
- **Prohibido tocar:** `<tablas/imports de otros modulos>`

## Modelo de datos

<Tablas y columnas que usa. Referencia a `docs/03b` seccion del modulo.>

## API / eventos

- **Endpoints:** `<metodo ruta -> proposito>`
- **Eventos emitidos:** `<dominio.sustantivo.accion>`
- **Eventos consumidos:** `<...>`
- **Errores de negocio:** `<codigos>`

## Reglas de negocio

- <Regla 1 (con parametro configurable si aplica)>
- <Regla 2>

## Criterios de aceptacion (CA)

- `CA-01`: <...>
- `CA-02`: <...>

## Pruebas requeridas

- Camino feliz: <...>
- Errores: <...>
- Concurrencia/idempotencia (si mueve dinero): <...>

## Entregables

- [ ] Codigo del modulo.
- [ ] Pruebas automatizadas.
- [ ] Contrato OpenAPI actualizado.
- [ ] `docs/modules/README.md` (seccion del modulo) actualizado si cambio su frontera.

## Entorno / comandos

- Rama: `feature/<modulo>-<tema>`
- Levantar entorno: `docker compose up`
- Pruebas: `<comando>`
- Migraciones: `<comando Alembic>`

## Reporte esperado

<Seguir el formato de `docs/tasks/README.md` seccion 6.>
