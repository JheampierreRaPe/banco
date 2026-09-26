# 18 - Gestion de cambios (añadir cosas al proyecto)

> Regla de oro: **primero se cambia el plan (documento), luego el codigo**. Nunca al reves.
> No esperes al final del proyecto para agregar algo que ya sabes ahora: el costo crece con el
> tiempo (rework, migraciones, pruebas rotas).

## 1. Decision rapida: ¿cuando entra el cambio?

| Tipo de cambio | Cuando | Que actualizar |
|---|---|---|
| Nueva funcionalidad / HU | Desde ya: registrar y planificar en un sprint | `01` (backlog), estimar SP, decidir MVP, crear brief en `tasks/`, `14` (sprint) |
| Ajuste a algo **aun no construido** | Desde ya, antes de que se tome esa tarea | El brief/CA de esa HU (`06`..`12`) y, si aplica, `03b`/`05` |
| Ajuste a algo **ya construido** | Registrar ya; implementar en el sprint actual si es critico, si no en estabilizacion | Nuevo brief de cambio + el doc afectado |
| Regla tecnica / arquitectura (dependencia, patron) | Desde ya (impacta todo lo que venga) | `02`, `03b`, `17` |
| Cambio de alcance o exclusion | Desde ya | `01` (y el acta de constitucion si aplica) |
| Idea descartada | Registrarla como descartada | Este documento (para no reevaluarla) |

Resumen: **lo que no bloquea -> backlog; lo que invalida lo que se esta construyendo -> ahora.**

## 2. Flujo de 5 pasos

1. **Registrar** la solicitud (SCR) en la seccion 4 con contexto y motivo.
2. **Evaluar impacto**: HU/modulos afectados, story points, dependencias, si es MVP o post-MVP.
3. **Decidir**: `entra ahora`, `se planifica en sprint N`, `post-MVP` o `descartado`.
4. **Actualizar la fuente de verdad** (docs) y crear/editar los briefs en `docs/tasks/`.
5. **Comunicar y re-planificar** el sprint (`14`) si cambia el alcance comprometido.

## 3. Reglas para no romper el trabajo en curso

- No inyectar cambios dentro de una tarea activa sin actualizar su brief: **se para, se actualiza
  el brief y se reanuda**.
- Si el cambio rompe algo ya entregado: crear un brief de cambio que incluya **reverso/ajuste
  contable** y compatibilidad.
- Los cambios de dinero nunca editan asientos: se compensan con asiento nuevo.
- Todo cambio de alcance se refleja en `01-hallazgos-y-decisiones.md` (fuente de verdad).
- Un cambio sin CA ni pruebas no se acepta.

## 4. Registro de solicitudes de cambio (SCR)

| ID | Fecha | Solicitud | Motivo | Tipo | Impacto (HU/SP) | Decision | Sprint | Estado |
|---|---|---|---|---|---|---|---|---|
| SCR-001 | 2026-09-12 | Ejecucion en dos campos (PC servidor + movil por LAN) con cliente delgado estricto | Formalizar el reparto cliente-servidor pedido por el equipo | Tecnico / Arquitectura | `02`, `05`, `13`, `19`, briefs frontend | entra ahora | Sprint 1 | hecho |
| SCR-002 | 2026-09-12 | Guia de diseno UI y tokens para los dos frontends | Definir como especificar el diseno (tokens + mockups; sin CSS para Flutter) | Tecnico / Documentacion | `docs/20`, `docs/design/`, briefs UI | entra ahora | Sprint 1 | hecho |
| SCR-003 | 2026-09-16 | Integrar el design system "Eucalipto y Ocre" (YAML) en la documentacion y definir `success`/`warning` | Fijar la paleta para que el frontend de prueba se construya con esos estilos | Diseno / Documentacion | `docs/20`, `docs/design/mockups.md`, briefs UI | entra ahora | Sprint 1 | hecho |
| SCR-004 | 2026-09-20 | Version visible de la app en entry, login y crear cuenta, con fuente unica y esquema de versionado documentado | Confirmar que build esta instalado en el movil y fijar el esquema acordado | Ajuste / Documentacion | `13`, `18`, `README`, brief `F-T30`, `frontend` | entra ahora | Sprint 1 | resuelto/aplicado (2026-09-26: brief `F-T30` en `docs/tasks/archive/` con `Estado: Hecho`) |
| SCR-005 | 2026-09-21 | Regeneracion total de la UI Flutter con el diseno canonico "Eucalipto y Ocre" (`pantallas.fig`) + ajustes de identidad en backend | Unificar la app al diseno oficial, fusionar onboarding y simplificar el flujo de sesion por `user_ref` | Nueva funcionalidad / Ajuste | Fases 1-6 (rama `refactorizacion-ui`); briefs `F-T34`..`F-T42`, `E1-T32`..`E1-T34`, `Q-T11` | entra ahora | Sprint 1 | resuelto/aplicado (2026-09-26: briefs `F-T34`..`F-T42`, `E1-T32`..`E1-T34` y `Q-T11` en `docs/tasks/archive/`, todos con `Estado: Hecho`) |

> **SCR-005 - decisiones del dueno (2026-09-21, rama `refactorizacion-ui`):**
> 1. Diseno canonico = `pantallas.fig` (Eucalipto y Ocre) + tokens de `docs/20`. Las
>    pantallas SIN pagina en el `.fig` (recovery, pin-reset, activation, home/dashboard)
>    se **disenan primero** en el `.fig` antes de implementarlas.
> 2. Backend: `recovery.verify` deja de abrir sesion y devuelve `{user_ref, device_bound}`;
>    la unica sesion la abre `POST /auth/login/pin`. OTP queda **solo email** (se quita
>    el fallback SMS) y `POST /auth/activate` se **deprecia pero se mantiene vivo**
>    (header `Deprecation` + OpenAPI `deprecated`).
> 3. Extras backend incluidos: endpoints `/pin-reset` (email+DNI+OTP), migracion para
>    `users.email` NOT NULL, y mover constantes (lockout, espera de reenvio, intentos de
>    OTP, rate-limit de recovery) a `config.parameters`.
> 4. Alcance de la rama: **bloque identidad/onboarding primero** (splash -> onboarding ->
>    registro -> sesion -> home). Cuentas/creditos/divisas quedan para una fase posterior.
> 5. El flag local `welcome.hasSeenWelcome` se elimina; el guard del router depende solo de
>    `SessionIdentityStore.userRef` (+ sesion activa). `SessionIdentityStore` pasa a
>    `Listenable`.

Plantilla para agregar una fila:

```
| SCR-00X | <fecha> | <que se quiere agregar> | <por que> | <Nueva HU / Ajuste / Tecnico / Alcance> | <modulos, SP> | <entra ahora / sprint N / post-MVP / descartado> | <sprint> | <pendiente/en curso/hecho> |
```

## 5. Ejemplos

- **Agregar "pago de matriculas"**: nueva HU -> registrar SCR, estimar, decidir MVP, crear brief,
  planificar en el sprint con capacidad. No esperar al final.
- **Cambiar el umbral de biometria de S/10,000 a S/5,000**: es un parametro -> cambiar
  `config.parameters`, no el codigo, no requiere HU nueva.
- **Reemplazar el proveedor de notificaciones mock por uno real**: cambio tecnico -> actualizar
  `02` y crear tarea de adaptador; se hace cuando toque ese modulo.
- **Cambiar el estandar de QR**: cambio de arquitectura -> `02` + `06`..`09` (HU14/HU15) + un brief.

## 6. Cuando el proyecto ya esta en marcha

- Cambio en una **HU no iniciada**: se edita el brief directamente (barato).
- Cambio en una **HU en curso**: se actualiza el brief y el agente re-planifica; si ya hay codigo
  hecho que contradice el cambio, se agrega una subtarea de ajuste.
- Cambio en una **HU terminada**: se abre una tarea de mejora/correccion; no se reescribe la
  historia ya cerrada.

## 7. Definition of Done de un cambio

- [ ] SCR registrada y decidida.
- [ ] Docs fuente de verdad actualizados.
- [ ] Brief creado o actualizado.
- [ ] CA y pruebas incluidos.
- [ ] Impacto en ledger/saldos evaluado si aplica.
- [ ] Sprint re-planificado si cambia el alcance.

## 8. Versionado visible de la app

Esquema de versionado acordado por el dueño (literal), para la app Flutter:

- Arrancar en `1.1.010`.
- Mientras se corrige el error actual (camara KYC), cada nueva version sube
  `+0.0.001` (`1.1.011`, `1.1.012`, ...).
- Una vez funcional y se pase a otros apartados del Sprint 1 que deben verse en
  Flutter, se varia a `0.1.0`.
- Al acabar el sprint, `1.0.0`.

Reglas de implementacion:

- **Fuente unica:** la version se define en un solo lugar,
  `frontend/lib/core/app_version.dart` (`const String kAppVersion =
  String.fromEnvironment('APP_VERSION', defaultValue: '1.1.010');`),
  sobreescribible por `--dart-define=APP_VERSION=<valor>`. Cambiar la version
  por build no toca ninguna pantalla.
- **Widget compartido:** `frontend/lib/core/widgets/app_version_label.dart`
  (`AppVersionLabel`, `Key('app-version')`) renderiza `Version <kAppVersion>`.
  Se monta en las TRES pantallas: `/entry` (`entry_page.dart`), `/login`
  (`login_page.dart`) y `/kyc` (`kyc_start_page.dart`). Es discreto
  (`label-sm` + color de texto secundario) y no rompe los estados de cada
  pantalla.
- **`pubspec.yaml` y semver:** el `version:` de pubspec no acepta cero a la
  izquierda en el patch, por lo que se usa `version: 1.1.10+1`; el TEXTO visible
  (el que identifica el build) sigue siendo `1.1.010` desde `kAppVersion`. Sin
  dependencias nuevas.
- Registrado como `SCR-004` (fila de la seccion 4).
