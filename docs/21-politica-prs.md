# Auditoría de entrega: el orquestador está produciendo PRs de 280.000 líneas

**Para:** orquestador del proyecto `C:\Users\Jheampierre\Desktop\proyecto integrador`
**De:** revisión externa + política vigente
**Fecha:** 2026-11-09
**Alcance:** auditoría del repo (solo lectura) + política de entrega aplicada.
**Estado:** Fase 0 aplicada en el repo; ver "Estado de implementación" en la sección 5.

> Este documento es una auditoría y un contrato de trabajo. No es una opinión de estilo.
> El proceso que se exige acá **ya está escrito** en `docs/15-calidad-seguridad-devops.md`
> (línea 237), `docs/tasks/README.md` (sección 4.5) y `docs/16-guia-para-agentes.md`.
> El problema es que el orquestador **no lo está cumpliendo**.

---

## 0. Cómo leer esto

1. **Secciones 1-3**: qué pasó y por qué (evidencia + diagnóstico).
2. **Sección 4**: reglas obligatorias desde el próximo sprint.
3. **Sección 5**: arreglos concretos que el orquestador debe ejecutar (Fase 0).
4. **Sección 6**: qué hacer con el PR #9 (decisión recomendada).
5. **Secciones 7-9**: protocolo de sesión, checklist y forma de medir.
6. **Anexos**: mensaje listo para pegar y plantilla de PR.

Si el orquestador solo va a leer una sección, que lea **las secciones 2 y 4**.

---

## 1. Resumen ejecutivo

El proyecto tiene una base documental y de pruebas excelente. El problema es de **entrega**:

- El PR #9 tiene **1.190 archivos y +280.983 líneas**. No es un PR del Sprint 3: es el PR de **todo el proyecto**.
- La causa inmediata es que se abrió contra `feature/qa-bootstrap` (el esqueleto inicial), no contra `sprint-2`.
- La causa de fondo es que **se documenta por tarea pero se entrega por sprint**: un commit por bloque de 5 briefs, un PR por sprint acumulado.
- El CI **no se ejecuta en los PRs** porque `pull_request` solo apunta a `main`, y `main` no existe.
- El proceso escrito exige `feature/<modulo>-<tema>` + PR obligatorio + `main` protegida. Se usan ramas `sprint-2` / `sprint-3` y **0 merge commits**.

Consecuencia: el PR dejó de ser una unidad de revisión. Nadie puede decir "esto está bien" leyendo 280.000 líneas.

---

## 2. Evidencia reproducible

### 2.1 El PR #9 no es incremental

```text
$ git diff --shortstat feature/qa-bootstrap...sprint-3
 1190 files changed, 280983 insertions(+), 1839 deletions(-)

$ git rev-list --count feature/qa-bootstrap..sprint-3
131
```

Y `sprint-2` **es ancestro** de `sprint-3`:

```text
$ git merge-base --is-ancestor sprint-2 sprint-3   # exit 0 = SÍ
```

Es decir: el PR #9 incluye **Sprint 1 + Sprint 2 + Sprint 3**. La base elegida fue el esqueleto, no la última entrega.

### 2.2 El incremento real del Sprint 3 sigue siendo enorme

Con la base correcta (`sprint-2`):

```text
$ git diff --shortstat sprint-2...sprint-3
 292 files changed, 72782 insertions(+), 720 deletions(-)

$ git rev-list --count sprint-2..sprint-3
11
```

**72.782 líneas en 11 commits.** Sigue siendo ~145 veces el presupuesto razonable de un PR.

### 2.3 Commits que agrupan 5 tareas

| Commit | Alcance | Archivos | Líneas |
|---|---|---|---|
| `ae9a4d9` | `E3-T17..T22` + `E5-T23` (créditos) | 49 | +12.960 |
| `65e1ad2` | `E4-T16..T20` (servicios/recargas) | 26 | +11.198 |
| `4b6a316` | `E4-T01..T05` (billetera) | 31 | +9.351 |
| `e8a7ead` | `E4-T11..T15` (cobro QR) | 23 | +9.203 |
| `55f21e3` | `E6-T12..T16` (notificaciones) | 33 | +8.130 |
| `4f16296` | `E4-T06..T10` (QR) | 22 | +6.692 |
| `24b0c41` | **6 puntos de feedback distintos** | 79 | +5.818 |
| `73adb37` | 5 tareas UX + nginx | 59 | +4.886 |

`24b0c41` es el caso de estudio: mezcla bug crítico de recargas, desembolso, correos, QR personal, descubribilidad y tests. Son **6 PRs**, no uno.

### 2.4 Composición del diff (PR #9)

| Área | Archivos | Líneas añadidas |
|---|---|---|
| `backend/` | 423 | +132.279 |
| `frontend/` | 374 | +95.989 |
| `admin-web/` | 25 | +5.445 |
| `docs/` | 287 | +45.014 |
| `infra/` | 14 | +735 |
| `.opencode/` | 4 | +111 |

Tests vs. producción (líneas cambiadas):

- **tests:** 106.812
- **código de producto** (`backend/app` + `frontend/lib` + `admin-web/src`): 112.325

Los tests son ~49 % del cambio. Eso es **bueno** y no se debe castigar: el presupuesto de PR debe excluirlos, no separarlos del código.

### 2.5 El CI no valida los PRs

`.github/workflows/ci.yml`:

```yaml
on:
  push:
    branches: [main, "feature/**", "sprint-*"]
  pull_request:
    branches: [main]
```

Pero `main` **no existe**: el default del repo es `feature/qa-bootstrap`. Resultado: el push al branch corre CI, pero **el PR #9 no tiene checks**. Se revisa y mergea sin validación del PR.

### 2.6 Proceso escrito vs. proceso ejecutado

| Fuente | Regla escrita | Realidad |
|---|---|---|
| `docs/15` línea 237 | `main` protegida; trabajo en `feature/<modulo>-<tema>`; PR obligatorio | ramas `sprint-2`, `sprint-3`; sin `main` |
| `docs/tasks/README.md` sección 4.5 | rama `feature/<modulo>-<tema>` | no se usa |
| `docs/tasks/README.md` sección 5.8 | "Reportar y abrir PR" | PRs abiertos al final, contra base incorrecta |
| `docs/16` línea 52 | abrir PR siguiendo `docs/15` | 0 merge commits en 133 commits |
| `docs/sesiones/2026-10-16.md` | "PR pendiente de crear" | el cierre de sesión no incluye abrir PR |

El orquestador no inventó un proceso malo: **ignoró el que el proyecto ya tenía**.

### 2.7 Deuda estructural que alimenta PRs grandes

Archivos actuales (no del diff) por encima de 1.000 líneas:

| Archivo | Líneas |
|---|---|
| `backend/app/modules/transactions/service/interbank.py` | 1.794 |
| `frontend/lib/features/transfers/presentation/confirm_page.dart` | 1.466 |
| `frontend/lib/features/home/presentation/home_page.dart` | 1.426 |
| `frontend/lib/features/movements/presentation/movements_page.dart` | 1.368 |
| `frontend/lib/features/wallet/presentation/qr_pay_page.dart` | 1.224 |
| `backend/app/modules/transactions/api/__init__.py` | 1.152 |
| `backend/app/modules/transactions/service/biometric.py` | 1.072 |
| `frontend/lib/features/profile/presentation/profile_page.dart` | 1.065 |
| `backend/app/modules/wallet/service/services.py` | 1.005 |

Un cambio de 30 líneas en `interbank.py` obliga a un diff de contexto enorme. Esto no se arregla con la política de PRs, pero explica parte del ruido.

### 2.8 Otros defectos de orquestación

1. **Agentes con rama hardcodeada obsoleta.** Los 4 archivos `.opencode/agent/*.md` dicen
   `Ruta: ... (rama pruebas-flutter-nuevoagente)`. La rama de trabajo real es `sprint-3`.
   Un delegado puede razonar sobre el estado equivocado.
2. **13 ramas locales**, varias de septiembre, sin limpieza tras integrar.
3. **`HANDOFF.md` se reescribe completo cada sesión** (`24b0c41`: +/-176/158 líneas).
   Genera conflictos y un diff de documentación que no aporta revisión.
4. **Artefactos en la raíz del repo**: `Acta_Constitucion_Banca_Online.docx`,
   `HU-integradorII.xlsx`, `informe.docx` versionados; `eliminar-movements.jpeg` y
   `.opencode/agent/diseno-frontend.md` sin trackear. Higiene, no bloqueante.
5. **`gh` no instalado** y el PR se crea "a mano" o por API: por eso el cierre de sesión
   termina con "PR pendiente". Si no hay `gh`, el orquestador debe usar la API con el token
   del credential helper **en el mismo cierre**, no dejarlo para después.
6. **209 briefs** (9 activos, 198 archivados, promedio 159 líneas, máximo 442).
   Es un activo enorme y **la unidad natural de PR ya existe**: no hay que inventarla.

---

## 3. Diagnóstico

Cinco causas, en orden de impacto:

1. **Unidad de entrega equivocada.** Se entrega por sprint/HU completa, no por tarea o corte.
2. **Base de PR equivocada.** Cada PR apunta al esqueleto, así que es acumulativo y no incremental.
3. **Sin integración continua real en PRs.** El pipeline corre en push, no en la revisión.
4. **El orquestador hace commits "paraguas".** Agrupa tareas no relacionadas por conveniencia de cierre.
5. **El cierre de sesión no tiene puerta de calidad.** No se mide el diff, no se abre PR, no se declara alcance.

La causa 1 es la única que requiere disciplina; las otras cuatro son configuración y checklist.

---

## 4. Reglas obligatorias desde ahora

### 4.1 Unidad de entrega

Un PR debe cumplir **todo** esto:

- [ ] **1 brief** (`docs/tasks/<ID>.md`) o un corte declarado de ese brief.
- [ ] **1 módulo** (`wallet`, `credits`, `notifications`, `shared`...). Si cruza dos, se justifica en la descripción.
- [ ] **1 motivo.** Si el título necesita "y", son dos PRs.
- [ ] **Reversible solo**: se puede revertir sin deshacer otra cosa.
- [ ] **Verde por sí mismo**: compila, tests pasan, sin código muerto ni endpoints a medio cablear.
- [ ] **Diff declarado**: el PR dice cuántas líneas semánticas trae.

### 4.2 Presupuesto

| Tipo de cambio | Objetivo | Tope duro | ¿Cuenta? |
|---|---|---|---|
| Feature (código de producto) | 300-500 líneas | 700 | Sí |
| Tests del feature | mismo PR | sin tope | **No** |
| Bugfix | <= 150 | 250 | Sí |
| Migración / lockfile / generado / rename mecánico | commit separado | sin tope | **No** |
| Refactor puro (sin cambio de comportamiento) | PR aislado | sin tope | Parcial |
| Docs / briefs | PR propio | 500 | No |

**Línea semántica** = líneas añadidas + eliminadas en `backend/app`, `frontend/lib`, `admin-web/src`,
excluyendo tests, `pubspec.lock`, `GeneratedPluginRegistrant`, migraciones autogeneradas y docs.

Regla de alarma: si un PR supera el tope, **no se parte por líneas**; se parte por
"¿qué parte se puede mergear sola sin dejar el repo roto?".

### 4.3 Cómo cortar una tarea que no entra

Por orden de preferencia:

1. **Cortes verticales aditivos**: dominio + tests -> repositorio/migración + tests -> servicio/API + tests -> frontend + tests -> wiring final. Cada PR deja `main` verde porque solo añade código aún no referenciado.
2. **Refactor primero, comportamiento después**: un PR mueve/extrae, otro cambia lógica.
3. **Por escenario**: camino feliz -> errores/seguridad -> QA.
4. **Stacked PRs**: si es indivisible, 3 PRs con base en el anterior, merge en orden.

### 4.4 Prohibiciones

- Mezclar feature + fix + docs + bump de dependencias + formateo en el mismo PR.
- Commitear tests de otra tarea en un PR ajeno.
- Abrir un PR contra una base que no sea la última entrega integrada.
- Dejar un PR sin checks de CI.
- Cerrar sesión con "PR pendiente de crear".
- Reescribir `HANDOFF.md` completo en cada sesión.
- Commits con más de un brief si no son estrictamente inseparables (ej. migración + modelo).

### 4.5 Excepciones admitidas (con justificación escrita en el PR)

- Renombres/movimientos masivos.
- Migraciones + modelo que no compilan por separado.
- Tests que solo tienen sentido con su código (siempre van juntos).
- Release PR de cierre de sprint (ver sección 6), declarado como tal.

---

## 5. Fase 0: lo que el orquestador debe arreglar

| # | Archivo | Cambio | Criterio de aceptación |
|---|---|---|---|
| 1 | GitHub | Crear `main` desde `feature/qa-bootstrap` y ponerlo como default | `git remote show origin` muestra `HEAD branch: main` |
| 2 | `.github/workflows/ci.yml` | `pull_request: branches: ["**"]` (o al menos `main` + default real) | Un PR de prueba dispara lint/test/build |
| 3 | `.github/pull_request_template.md` | Crear con el checklist del Anexo B | Todo PR nuevo nace con el checklist |
| 4 | `docs/18-gestion-de-cambios.md` | Nueva sección 10 "Política de PRs" con la sección 4 de este documento | Referenciada desde `docs/16` |
| 5 | `docs/tasks/_PLANTILLA.md` | Nueva sección "Plan de PRs" (2-5 cortes) | El siguiente brief la trae completa |
| 6 | `.opencode/agent/planificador.md` | Debe producir el "Plan de PRs"; quitar rama hardcodeada | Brief nuevo incluye cortes |
| 7 | `.opencode/agent/worker.md` | "Ejecuta UN corte, no el brief entero"; quitar rama hardcodeada | Reporte indica el corte |
| 8 | `.opencode/agent/fixer.md` | "Un fix = un PR"; quitar rama hardcodeada | - |
| 9 | `.opencode/agent/validador.md` | Verificar tamaño del diff y base del PR; quitar rama hardcodeada | Dictamen incluye tamaño |
| 10 | `HANDOFF.md` sección 7 | En `adios`: medir diff, abrir PRs, no dejar pendientes | El cierre muestra `git diff --shortstat` |
| 11 | Repo | Instalar `gh` o documentar creación de PR por API en el cierre | `adios` termina con URL de PR |
| 12 | Repo | Limpiar ramas integradas y mover binarios de la raíz a `docs/entregables/` | `git branch` sin ramas muertas |

**Orden recomendado:** 1 -> 2 -> 3 -> 4 -> 5 -> 6/7/8/9 -> 10/11 -> 12.
Los puntos 1-3 son de una hora y desbloquean todo lo demás.

### Estado de implementación (2026-11-09)

- **Aplicado en el repo:** 2 (CI en todos los PRs), 3 (plantilla de PR), 4 (`docs/18` sección 10),
  5 (`_PLANTILLA` con "Plan de PRs"), 6-9 (los 4 agentes) y 10 (`HANDOFF` cierre de sesión).
- **Parcial:** 1 (rama local `main` creada en `2c56ad6`; falta `git push -u origin main` y cambiar
  el default en GitHub: Settings > Branches).
- **Pendiente manual:** 11 (instalar `gh` o definir la creación de PR por API) y 12 (limpieza de
  ramas integradas y binarios de la raíz).

---

## 6. Qué hacer con el PR #9

La historia ya está acumulada. Se puede dividir retroactivamente en 7-11 PRs encadenados
(los commits ya están agrupados por HU), pero seguirían siendo de 5.000-13.000 líneas.
**No arregla el problema; solo lo maquilla.**

Recomendación:

1. **No reescribir historia.**
2. Cambiar la base a `sprint-2` si GitHub lo permite (baja de 281k a 73k) **o** cerrar #9 y abrirlo como **PR de release**, declarando la excepción en la descripción.
3. Aplicar la política **desde el Sprint 4**.
4. Para la evaluación: los 2-3 PRs chicos reales del Sprint 4 demuestran el hábito mejor que un retrofit.

Si el dueño insiste en el retrofit, hacerlo solo con los 11 commits de `sprint-2..sprint-3` como
stacked PRs (base de cada uno = commit anterior). Aun así, declararlo como deuda histórica, no como ejemplo.

---

## 7. Protocolo de sesión (reemplaza lo vigente en `HANDOFF.md` sección 7)

### `hola` (arranque)

1. Contenedores healthy (sin cambios).
2. ADB con exactamente un dispositivo (sin cambios).
3. **Nuevo:** verificar rama y base:
   - `git status -sb`
   - `git log --oneline <base>..HEAD`
   - Si HEAD ya tiene commits de más de un brief sin PR: avisar antes de seguir.

### `adios` (cierre)

1. Detener con `docker compose stop` (sin cambios).
2. **Medir:** `git diff --shortstat <base>...HEAD` y el conteo semántico (sección 9).
3. **Clasificar:** ¿el trabajo es 1 brief/corte? Si no, abrir **varios PRs apilados**, no uno.
4. **Abrir el PR** (o los PRs) en la misma sesión. Prohibido "PR pendiente".
5. Verificar que el PR tiene checks de CI.
6. Generar `docs/sesiones/<fecha>.md` **append-only** (no reescribir `HANDOFF.md`).
7. Preguntar por commit solo si hay autorización.
8. Cerrar con "Descansa bello".

---

## 8. Checklist de PR (usar en la plantilla)

```markdown
## Qué
<!-- 1-3 líneas. Si necesitás "y", son dos PRs. -->

## Brief / corte
- Brief: `docs/tasks/<ID>.md`
- Corte: <ej. "dominio + tests" / "API + tests" / "wiring">
- Base: `<rama>` (debe ser la última integrada)

## Alcance
- Módulos tocados: <uno>
- Archivos principales: <lista corta>
- **No incluye**: <explícito>

## Tamaño
- `git diff --shortstat <base>...HEAD`: <N archivos, +X/-Y>
- Líneas semánticas (sin tests/docs/lock): <N>
- Dentro del presupuesto: [ ] Sí [ ] No -> justificación:

## Evidencia
- [ ] `pytest` / `flutter test` / `npm test` en verde
- [ ] `ruff` + `black` / `flutter analyze` / `tsc` limpios
- [ ] `alembic check` + un solo head (si aplica)
- [ ] CI del PR en verde

## Riesgo y rollback
- Riesgo: <bajo/medio/alto>
- Rollback: revert del PR (¿es independiente? sí/no)
- Migración reversible: sí/no/No aplica

## Qué NO se tocó
- Otros módulos, `kyc-service/`, secretos, PII en logs.
```

---

## 9. Cómo medir

Desde la raíz del repo, en PowerShell:

```powershell
# Base integrada (ajustar cuando exista main)
$base = "main"

# Diff total
git diff --shortstat "$base...HEAD"

# Líneas semánticas (solo producto)
git diff --numstat "$base...HEAD" -- backend/app frontend/lib admin-web/src |
  ForEach-Object {
    $p = $_ -split "`t"
    if ($p[0] -match '^\d+$') { [int]$p[0] + [int]$p[1] }
  } |
  Measure-Object -Sum |
  Select-Object -ExpandProperty Sum

# Archivos tocados
(git diff --name-only "$base...HEAD" | Measure-Object).Count
```

**Objetivo de Sprint 4:**

- 0 PRs por encima de **700** líneas semánticas.
- >= 80 % de PRs por debajo de **500**.
- 1 PR = 1 brief o 1 corte declarado.
- CI verde en el PR, no solo en el push.
- 0 sesiones cerradas con "PR pendiente".

---

## 10. Criterio de éxito

El orquestador habrá corregido el rumbo cuando, al final del Sprint 4, se pueda responder **sí** a:

- [ ] ¿Cada PR se revisa en menos de 30 minutos?
- [ ] ¿Cada PR dice qué brief y qué corte implementa?
- [ ] ¿Cada PR tiene checks de CI?
- [ ] ¿Ningún commit mezcla dos tareas no relacionadas?
- [ ] ¿El cierre de sesión deja PRs abiertos, no deuda?
- [ ] ¿El diff de `docs/` va separado del de producto cuando es planificación?

---

## 11. Reproducir la auditoría

```powershell
cd "C:\Users\Jheampierre\Desktop\proyecto integrador"

git diff --shortstat feature/qa-bootstrap...sprint-3
git rev-list --count feature/qa-bootstrap..sprint-3
git merge-base --is-ancestor sprint-2 sprint-3; "ancestro: $LASTEXITCODE"
git diff --shortstat sprint-2...sprint-3
git rev-list --count sprint-2..sprint-3
git log --merges --oneline
git remote show origin
Get-Content .github/workflows/ci.yml -TotalCount 20
Get-ChildItem .opencode\agent -Filter *.md | Select-String "rama "
```

---

## Anexo A: mensaje para pegarle al orquestador

> Antes de seguir con el Sprint 4, lee
> `docs/21-politica-prs.md`.
>
> Resumen: el PR #9 tiene 1.190 archivos y +280.983 líneas porque se abrió contra
> `feature/qa-bootstrap`, que es ancestro de `sprint-2`. El incremento real del Sprint 3
> (`sprint-2...sprint-3`) sigue siendo +72.782 en 11 commits. Cada commit agrupa 5 briefs.
> El CI no corre en PRs porque `pull_request` solo apunta a `main`, y `main` no existe.
> El proceso de `docs/15` y `docs/tasks/README.md` ya exigía ramas `feature/<modulo>-<tema>`
> y PR obligatorio; no se cumplió.
>
> Desde ahora: **1 brief o 1 corte = 1 rama = 1 PR**, con presupuesto de 300-500 líneas
> semánticas (700 de tope), tests incluidos en el mismo PR pero excluidos del conteo, y
> CI verde en el PR. Nada de commits paraguas. Nada de "PR pendiente" al cerrar sesión.
>
> La Fase 0 ya está aplicada en el repo (ver "Estado de implementación" en la sección 5).
> Verifica en tu próximo PR: base correcta, CI en verde, brief/corte declarado y tamaño dentro
> del presupuesto. Si un brief no entra en un PR, el `planificador` debe entregar el
> **Plan de PRs** y el `worker` ejecuta un corte por vez.
>
> El PR #9 no se reescribe: se declara como release histórico con base corregida o se cierra.
> La prueba del cambio son los PRs del Sprint 4.

---

## Anexo B: por qué esta política no frena el proyecto

No se trata de trabajar más lento. Se trata de que cada PR sea **mergeable, reversible y explicable**.
Con IA, el riesgo no es escribir poco: es escribir mucho sin que nadie lo entienda.
Un PR de 400 líneas con tests se revisa; uno de 12.000 se aprueba por fe. La diferencia no es
burocracia: es la posibilidad de detectar que el agente tocó algo que no debía.

El proyecto ya tiene todo lo necesario para hacerlo bien: briefs autosuficientes, fronteras por
módulo, tests al 50 %, CI con migraciones y gitleaks, y un validador solo-lectura.
La única pieza que falta es **entregar en la misma granularidad en la que se planifica**.

---

_Documento de política del proyecto. La auditoría se hizo en modo Plan (sin modificar el repo)._
_Referencia: `docs/21-politica-prs.md`; citado desde `docs/15`, `docs/16` y `docs/18` sección 10._
