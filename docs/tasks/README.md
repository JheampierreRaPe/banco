# Handoff para agentes - como repartir una tarea sin que el modelo se pierda

Este directorio contiene **un brief por tarea**. Un agente constructor nunca debe leer todo el
proyecto: se le entrega su brief + el *context pack* indicado, y nada mas.

## 1. Estructura

```
docs/
  tasks/
    README.md            # este archivo (protocolo)
    _PLANTILLA.md        # plantilla para crear un brief
    E1-T01.md            # briefs reales por ID de tarea
    E5-T01.md
    ...
  modules/
    README.md            # contrato de cada modulo (fronteras y responsabilidades)
```

## 2. Escalera de contexto (leer de menos a mas, nunca todo)

| Nivel | Que se lee | Siempre / Condicional |
|---|---|---|
| L0 | El brief de la tarea (`tasks/<ID>.md`) | Siempre. Es autosuficiente. |
| L1 | La seccion de su modulo en `modules/README.md` | Siempre. |
| L2 | Las secciones (con ancla) que el brief lista | Solo las listadas. |
| L3 | Un documento completo | Solo si el brief lo autoriza explicitamente. |

Si el agente necesita mas contexto, **debe pedir el documento o seccion exacta**; no explorar el
repositorio por su cuenta.

## 3. Matriz: context pack segun el tipo de tarea

| Tipo | Context pack (ademas de L0 + su modulo en L1) |
|---|---|
| Dominio de modulo (entidades/CRUD) | `03b` seccion del modulo, `05` seccion del modulo, doc de epica. |
| Motor / ledger (mueve dinero) | `04` completo, `03b` (`transactions`, `ledger`), `05` (transacciones), epica 5. |
| Adaptador / integracion | `02` (§9, §10), `05` (§7/§8), contrato del proveedor. |
| Frontend | `13` (seccion), `05` (solo endpoints que consume). No `03`/`04`. |
| Datos / migraciones | `03b` (modulo completo), `03c`, `02` (§5.2). |
| QA | `15`, doc de epica (CA), `05`. |
| DevOps | `15`, `02` (§11). |

## 4. Paso a paso: que debes entregar al agente (checklist)

1. **ID y modulo** de la tarea.
2. **El brief** `docs/tasks/<ID>.md` (creado con `_PLANTILLA.md`).
3. **Context pack** (L1 + secciones L2 segun la matriz 3).
4. **Frontera de codigo**: "puedes tocar estos archivos; prohibido tocar tablas/imports de otros modulos".
5. **Entorno**: rama `feature/<modulo>-<tema>`, `.env.example`, comando de `docker-compose`, comando de tests.
6. **Entregables**: codigo + pruebas + OpenAPI + `README` del modulo.
7. **DoD** (`16` §5) y **formato de reporte** (§6 de este documento).
8. **Cierre**: indicar que NO explore todo el repo.

## 5. Flujo del agente constructor

1. Leer el brief y el context pack listado.
2. Confirmar dependencias y fronteras.
3. Escribir primero el dominio puro (testeable sin base de datos).
4. Implementar repositorio y caso de uso.
5. Exponer endpoint + esquema + contrato OpenAPI.
6. Escribir pruebas (feliz + error + concurrencia si aplica).
7. Actualizar OpenAPI y `README` del modulo.
8. Reportar y abrir PR.

## 6. Formato de reporte del agente

```
Tarea: <ID> - <modulo>
Implementado: <1-3 lineas>
CA cubiertos: <CA-01...> + evidencia (pruebas)
Decisiones/supuestos: <...>
Pendientes/riesgos: <registrar en docs/17 si aplica>
Archivos/endpoints/eventos afectados: <lista>
```

## 7. Reglas para no perder contexto

- El brief **embebe** las decisiones que aplican (copiadas), no obliga a deducirlas.
- Se citan secciones con ancla (`docs/04#7-asientos-por-tipo-de-operacion`), no "lee el doc 04".
- Un agente trabaja un solo modulo; las tareas de dinero se asignan junto con `ledger`.
- El frontend puede avanzar con mocks de la API descritos en `05`.
- Si un dato del brief contradice un documento, manda `01-hallazgos-y-decisiones.md` y se corrige
  el brief.

## 8. Como generar los briefs en masa

1. Tomar las tareas de los documentos de epica (`06`..`12`) y los IDs `F-T##`/`Q-T##` de `13`/`15`.
2. Copiar `_PLANTILLA.md` a `tasks/<ID>.md`.
3. Rellenar: meta, objetivo, alcance, contexto embebido, context pack, frontera, datos, API,
   reglas, CA, pruebas, entregables.
4. Verificar que el brief sea autosuficiente (que se entienda sin leer otro doc).
5. Registrar estado en el propio brief (`Pendiente/En progreso/Hecho`).

> Primer lote ya generado: **49 briefs del Sprint 1** (indice en `tasks/SPRINT-1.md`). Ver
> tambien `14-plan-sprints-y-ejecucion.md`.
