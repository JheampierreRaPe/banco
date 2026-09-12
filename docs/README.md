# Banca Online Integral - Plan Rector de Construccion

Este directorio contiene **el razonamiento y la logica de construccion** del proyecto, no
codigo. Su objetivo es que cualquier desarrollador (humano o modelo de IA) pueda tomar una
tarea, entender que debe hacer, por que, con que reglas y como verificar que quedo bien.

> Estado: el proyecto esta **recien creado**. Todo lo que aparezca descrito en los documentos
> de `Desktop\proyecto integrador` se considera **por construir**, aunque la documentacion lo
> redacte como si existiera. La unica pieza ya real es el microservicio KYC
> (`validacion-id-liveness-selfie`) que se integra como servicio externo.

## 1. Documentos fuente leidos

| Archivo | Contenido |
|---|---|
| `Acta_Constitucion_Banca_Online.docx` | Project Charter, alcance, exclusiones, equipo, sprints. |
| `informe.docx` | Analisis de negocio, epicas, backlog, riesgos, SLA/KPI. |
| `HU-integradorII.xlsx` | 11 hojas: HUs, criterios Gherkin, RF/RNF, backlog, sprints, Gantt, riesgos, flujo del dinero, matriz de accesos, equipo. |
| `validacion-id-liveness-selfie` (rama `eliminar`) | Microservicio KYC real (FastAPI + DeepFace + MediaPipe). |

## 2. Decisiones ya tomadas

| Tema | Decision |
|---|---|
| Backend | **Python + FastAPI + PostgreSQL** |
| Frontend cliente | **Flutter (movil)** con biometria del dispositivo |
| Frontend interno | **Panel web admin** (operaciones, fraude, cumplimiento, auditoria, creditos) |
| Arquitectura | **Monolito modular**, con cada modulo disenado para poder extraerse y publicarse como microservicio reutilizable en otro repositorio. |
| KYC | Se consume el microservicio existente por HTTP; no se reimplementa. |
| Plan de sprints | **4 sprints / 12 semanas** (se unifica la documentacion dispersa). |
| Dinero | Simulado, en **enteros (centimos)**, jamas decimal flotante. |
| Entrega | Esta carpeta `docs/`. |

## 3. Orden de lectura recomendado

1. `01-hallazgos-y-decisiones.md` - que dice cada documento, que se contradice y que se fija.
2. `02-arquitectura.md` - como se organiza el sistema y los repositorios.
3. `03-modelo-de-datos.md` - entidades, tablas y reglas de integridad.
   - `03b-diccionario-de-datos.md` - detalle columna por columna (tipos, claves, indices, enums).
   - `03c-modelo-er.md` - relaciones, agregados y referencias entre modulos.
4. `04-motor-transaccional-y-ledger.md` - **el corazon**: como se mueve y contabiliza el dinero.
5. `05-contratos-api.md` - convenciones de API, auth, errores e idempotencia.
6. `06` a `12` - una guia de tareas por epica (la parte que ejecutan los constructores).
7. `13-frontend-flutter-panel.md` - pantallas y flujos de UI.
8. `14-plan-sprints-y-ejecucion.md` - orden de construccion e hitos.
9. `15-calidad-seguridad-devops.md` - pruebas, seguridad, entornos y despliegue.
10. `16-guia-para-agentes.md` - **leer antes de escribir una linea**: plantilla de tarea, DoD y reglas.
11. `17-riesgos-y-decisiones-abiertas.md` - lo que falta decidir.
12. `tasks/README.md` y `tasks/SPRINT-1.md` - protocolo de handoff y briefs del primer sprint.

## 4. Mapa documento - epica

| Epica | HU | Documento de tareas |
|---|---|---|
| 1. Identidad, Biometria y KYC | HU01-HU04 | `06-epica-1-identidad-kyc.md` |
| 2. Cuentas y Transacciones | HU05-HU08 | `07-epica-2-cuentas-transferencias.md` |
| 3. Creditos Digitales | HU09-HU12 | `08-epica-3-creditos.md` |
| 4. Billetera y Pagos QR | HU13-HU16 | `09-epica-4-billetera-qr.md` |
| 5. Motor Transaccional | HU17-HU19 | `04` (logica) + `10-epica-5-motor-transaccional.md` (tareas) |
| 6. Fraude y Cumplimiento | HU20-HU23 | `11-epica-6-fraude-cumplimiento.md` |
| 7. Productos Avanzados | HU24-HU26 | `12-epica-7-productos-avanzados.md` |

## 5. Convenciones del plan

- Cada HU se describe con: objetivo, flujo feliz, excepciones, reglas configurables, datos,
  API, dependencias y criterios de aceptacion (CA heredados del Excel como `CA-n`).
- El lenguaje es de **especificacion**: nombres de campos, tablas, endpoints y estados son
  validos; no se escribe codigo de implementacion.
- Toda regla que en los documentos originales aparezca "cableada" debe convertirse en
  **parametro configurable** (umbrales, tasas, limites).
- Si un documento fuente se contradice con otro, manda `01-hallazgos-y-decisiones.md`.
- Lo que en la documentacion original requeria entidades reales (RENIEC, centrales de riesgo,
  OFAC, redes interbancarias) se implementa contra **adaptadores simulados** con el mismo
  contrato que tendria el real.

## 6. Definicion de "listo" (resumen)

Una tarea esta lista cuando: cumple sus CA, tiene pruebas automatizadas de los caminos feliz y
de error, respeta la matriz de accesos, queda auditada, no rompe el ledger, esta documentada en
OpenAPI y fue revisada por otra persona del equipo. El detalle esta en
`16-guia-para-agentes.md`.
