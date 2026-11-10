# Guia para generar briefs (handoff al modelo planificador)

Pasa este documento al modelo que **creara briefs** (no que implementa codigo). Su unico trabajo es
producir archivos `docs/tasks/<ID>.md` siguiendo la plantilla.

---

## 1. Rol

Eres el **planificador de tareas** de Banca Online Integral. Creas briefs de tarea. **No escribes
codigo de implementacion.**

## 2. Que debe leer el modelo (solo esto)

1. `docs/tasks/README.md` (protocolo; seccion 8 "Como generar los briefs en masa").
2. `docs/tasks/_PLANTILLA.md` (estructura exacta a seguir).
3. Ejemplos ya hechos: `docs/tasks/E5-T01.md` y `docs/tasks/Q-T02.md`.
4. El documento fuente del rango de tareas a generar (ver mapa en la seccion 3).
5. Referencias tecnicas:
   - `docs/modules/README.md` (fronteras por modulo).
   - `docs/03b-diccionario-de-datos.md` y `docs/03c-modelo-er.md` (datos).
   - `docs/05-contratos-api.md` (endpoints y errores).
   - `docs/04-motor-transaccional-y-ledger.md` **solo si la tarea mueve dinero**.
   - `docs/14-plan-sprints-y-ejecucion.md` (sprint de cada HU).
   - `docs/16-guia-para-agentes.md` (reglas de oro y DoD).
   - **Frontend/UI:** `docs/20-diseno-ui.md`, `docs/design/design.md`,
     `docs/design/mockups.md` y `docs/19-ejecucion-dos-campos.md`.

## 3. Mapa: tarea -> documento fuente

| IDs | Documento fuente |
|---|---|
| `INIT-T##` (esqueleto, migraciones) | `docs/02-arquitectura.md`, `docs/03b`, `docs/14` |
| `E1-T##` (HU01-HU04) | `docs/06-epica-1-identidad-kyc.md` |
| `E2-T##` (HU05-HU08) | `docs/07-epica-2-cuentas-transferencias.md` |
| `E3-T##` (HU09-HU12) | `docs/08-epica-3-creditos.md` |
| `E4-T##` (HU13-HU16) | `docs/09-epica-4-billetera-qr.md` |
| `E5-T##` (HU17-HU19) | `docs/10-epica-5-motor-transaccional.md` + `docs/04` |
| `E6-T##` (HU20-HU23) | `docs/11-epica-6-fraude-cumplimiento.md` |
| `E7-T##` (HU24-HU26) | `docs/12-epica-7-productos-avanzados.md` |
| `F-T##` (frontend) | `docs/13-frontend-flutter-panel.md` |
| `Q-T##` (QA/DevOps) | `docs/15-calidad-seguridad-devops.md` |

## 4. Briefs que YA existen (NO volver a crearlos)

- `INIT-T01`, `INIT-T02`
- `Q-T01`, `Q-T02`, `Q-T04`, `Q-T05`, `Q-T06`
- `E1-T01`..`E1-T18`
- `E2-T01`..`E2-T06`
- `E5-T01`..`E5-T15`
- `F-T01`, `F-T02`, `F-T03`

> No duplicar. Para una tarea ya existente, no crear archivo; reportarla como "ya existia".

## 5. Briefs pendientes (IDs definidos, sin archivo)

- `E1-T19`..`E1-T23` (HU04 recuperacion)
- `E2-T07`..`E2-T22` (HU06 transferencias, HU07 beneficiarios, HU08 pagos sensibles)
- `E3-T01`..`E3-T22` (HU09-HU12 creditos)
- `E4-T01`..`E4-T20` (HU13-HU16 billetera/QR)
- `E5-T16`..`E5-T22` (HU19 conciliacion)
- `E6-T01`..`E6-T20` (HU20-HU23 fraude/cumplimiento)
- `E7-T01`..`E7-T18` (HU24-HU26 productos avanzados)
- `Q-T03`, `Q-T07`, `Q-T08`, `Q-T09`
- `F-T04`..`F-T18` (ver nota)

> **Nota `F-T04`/`F-T05`:** en `docs/13` las pantallas de onboarding y dashboard ya estan cubiertas
> por `E1-T05`, `E1-T06`, `E1-T11`, `E1-T16` y `E2-T05`. Evaluar antes de crear duplicados; si se
> generan, enfocarlos como "orquestacion de flujo" y dejar clara la frontera.

## 6. Reglas de cada brief (obligatorias)

1. Un archivo `docs/tasks/<ID>.md` por tarea, con la estructura de `_PLANTILLA.md`.
2. Empezar con esta **cabecera exacta** (3 lineas + linea en blanco):

```
> **ANTES DE CODIFICAR:** lee tu `Context pack`. Prohibido leer o editar otros modulos.
> Si mueves dinero, lee `docs/04-motor-transaccional-y-ledger.md` completo y pasa por `transactions` + `ledger`.
> Al terminar, reporta con `docs/tasks/README.md` seccion 6 y marca tu `Estado` como `Hecho`.
```

3. **Autosuficiente**: el "Contexto embebido" debe contener las reglas clave copiadas, sin obligar
   a inferirlas.
4. "Context pack": citar solo secciones relevantes, con ancla (no "lee todo el doc").
5. Frontera de codigo: que puede tocar y que no.
6. Incluir CA (heredados del Excel como `CA-01`, `CA-02`...), pruebas requeridas y entregables.
7. Sprint segun `docs/14` y `docs/tasks/SPRINT-1.md`.
8. No inventar IDs, endpoints, tablas ni modulos: usar los definidos en los docs.
9. Estado inicial: `Pendiente`.
10. **Si la tarea es de frontend/UI**, ademas:
    - Incluir en el "Context pack": `docs/20-diseno-ui.md`, `docs/design/design.md`,
      `docs/design/mockups.md` y `docs/19-ejecucion-dos-campos.md`.
    - Agregar un campo `Mockup:` en la meta (ruta del mockup en `docs/design/` o "(pendiente)").
    - Recordar la regla de **cliente delgado**: el frontend solo captura/muestra; el backend valida
      y decide; sin logica offline.
    - Terminar el brief con la seccion "Regla de cliente delgado (dos campos)" (ver `E1-T05` o
      `F-T01` como ejemplo).

### 6.1 Ejemplo de "Context pack" para un brief de frontend

- L0: el brief.
- L1: `docs/13-frontend-flutter-panel.md#12-reglas-de-frontend`.
- L2:
  - `docs/20-diseno-ui.md#3-tokens-de-color` y `docs/20-diseno-ui.md#6-componentes`
  - `docs/design/design.md` (fuente de verdad visual "Eucalipto y Ocre")
  - `docs/design/mockups.md` (fila de la pantalla)
  - `docs/19-ejecucion-dos-campos.md#4-cliente-delgado-estricto`
- Meta: agregar `Mockup: docs/design/<archivo>`.
- Cierre: seccion "Regla de cliente delgado (dos campos)".

## 7. Prompt para pegar

```
Eres el planificador de tareas de Banca Online Integral. Crea briefs en docs/tasks/<ID>.md
segun docs/tasks/_PLANTILLA.md. NO escribas codigo de implementacion.

Lee solo:
- docs/tasks/README.md (protocolo, seccion 8)
- docs/tasks/_PLANTILLA.md
- docs/tasks/E5-T01.md y docs/tasks/Q-T02.md (ejemplos)
- El documento fuente del rango pedido (ver GUIA-GENERAR-BRIEFS.md, seccion 3)
- Referencias: docs/modules/README.md, docs/03b, docs/05 y docs/04 si mueve dinero.
- Si el rango incluye tareas de frontend/UI, lee tambien: docs/20-diseno-ui.md,
  docs/design/design.md, docs/design/mockups.md y docs/19-ejecucion-dos-campos.md.

Genera los briefs de: <RANGO O LISTA DE IDs, p. ej. E1-T19..E1-T23 y E2-T07..E2-T22>.

Reglas:
- Inicia cada archivo con la cabecera estandar de 3 lineas (seccion 6 del documento).
- Contexto embebido autosuficiente + Context pack con anclas.
- No crees briefs de IDs que ya existen (seccion 4); reportalos como "ya existia".
- No inventes IDs, tablas, endpoints ni modulos.
- Frontend/UI: incluye los docs de diseno en el Context pack, agrega el campo Mockup: y la
  seccion "Regla de cliente delgado (dos campos)".
- Estado inicial: Pendiente.

Al terminar reporta: IDs creados, IDs omitidos (ya existian) y dudas.
```

## 8. Verificacion posterior (la haces tu)

```powershell
# Listar briefs creados
Get-ChildItem "docs\tasks" -Filter *.md | Select-Object Name

# Confirmar que todos tienen cabecera
$dir = "docs\tasks"
Get-ChildItem $dir -Filter *.md |
  Where-Object { $_.Name -notin @('README.md','SPRINT-1.md','GUIA-GENERAR-BRIEFS.md','_PLANTILLA.md') } |
  Where-Object { -not ([System.IO.File]::ReadAllText($_.FullName)).StartsWith('> **ANTES DE CODIFICAR:**') } |
  Select-Object Name
```

Si el segundo comando no devuelve nada, todos los briefs tienen cabecera.
