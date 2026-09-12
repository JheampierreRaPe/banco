# 01 - Hallazgos, contradicciones y decisiones (fuente de verdad)

Este documento manda sobre los originales cuando haya conflicto. Si un constructor encuentra una
duda, se resuelve aqui; si no esta, se registra en `17-riesgos-y-decisiones-abiertas.md`.

## 1. Que describe cada fuente

- **Acta de Constitucion**: vision, alcance, exclusiones, equipo, 18 semanas, 6 sprints,
  7 epicas, 28 HU / 25 MVP / 174 SP. Es el documento mas formal.
- **informe.docx**: analisis de negocio, 7 epicas, 26 HU / 4 sprints / 173 SP / 24 MVP,
  matriz de riesgos y SLA/KPI.
- **HU-integradorII.xlsx**: detalle real de HU, criterios Gherkin, RF/RNF, backlog priorizado,
  plan de sprints, Gantt, riesgos, flujo del dinero, matriz de accesos y equipo.
- **Microservicio KYC** (rama `eliminar`): servicio real, reutilizable, con contrato documentado.

## 2. Contradicciones detectadas y resolucion

| # | Contradiccion | Resolucion adoptada |
|---|---|---|
| 1 | Charter: 6 sprints; informe/Excel: 4 sprints | **4 sprints de desarrollo**. Se documenta tambien el encaje en 18 semanas (4 preparacion + 12 desarrollo + 2 cierre). |
| 2 | Charter: 28 HU / 25 MVP; informe/Excel: 26 HU / 24 MVP | **26 HU**, **24 en MVP**, 2 `Could` fuera (`HU23`, `HU26`). |
| 3 | SP: 174 / 173 / 160 MVP | **173 total**, **160 del MVP** (excluye HU23=5 y HU26=8). |
| 4 | Hoja Requerimientos tiene IDs corridos (`HU27`, luego `HU02`, `HU04`...) | Remapear: fila `HU27` = **HU02** (OTP); fila `HU02` = **HU03** (autenticacion); el resto se mantiene. La fuente de HU y CA es la hoja "Historias de Usuario". |
| 5 | Rol de HU18/HU19 difiere entre hojas | HU18 -> **Sistema**; HU19 -> **Analista de operaciones**. El backlog tenia los roles invertidos. |
| 6 | `HU22` aparece duplicada en la hoja Backlog | Se conserva una sola fila: "Notificaciones de Seguridad Multicanal". |
| 7 | Los CA exigen RENIEC, centrales de riesgo, OFAC, World-Check, PEP reales | El alcance las excluye. Se implementan como **adaptadores simulados** con el mismo contrato (ver `02-arquitectura.md`). |
| 8 | CA hablan de MFA, huella y biometria facial en login, recuperacion, pagos y firma; el charter dice un solo factor | Factor unico por operacion con **biometria del dispositivo** (Face ID/huella del telefono) y PIN de contingencia. El **liveness del microservicio KYC se usa unicamente en HU01 (creacion de cuenta)**. HU03 (login), HU04 (recuperacion), HU08 (pagos sensibles) y HU11 (firma) usan biometria del dispositivo. MFA queda fuera del MVP. |
| 9 | Transferencia interbancaria "real" con equipo homologo | Se construye una **pasarela interbancaria** con contrato OpenAPI propio y un **simulador**; cuando exista el contrato del equipo par, se implementa el adaptador. |
| 10 | informe menciona "programar en Java/Kotlin"; el microservicio KYC es Python | Backend **Python/FastAPI**. Kotlin queda descartado. |
| 11 | "central de riesgo / score real" | Motor de reglas simple (ratio deuda/ingreso, tope por perfil, historial interno) + proveedor simulado. |
| 12 | Gantt dice ejecucion semanas 1-17 y "semana actual 5"; plan dice 12 semanas | Cronograma unico de **12 semanas de desarrollo** mas preparacion y cierre (ver doc 14). |
| 13 | SLA hablan de caudales reales (99.9% uptime, etc.) | Se usan como **objetivos de diseno medibles** en el entorno del curso, no como SLA contractual. |

## 3. Backlog canonico (26 HU)

> Fuente unica. Los `CA-n` de cada HU se conservan del Excel (hoja "Criterios Aceptacion").

| ID | Epica | Modulo | Rol | Prioridad | SP | MVP | Sprint base |
|---|---|---|---|---|---|---|---|
| HU01 | 1 | Registro y KYC Biometrico | Cliente | Must | 8 | Si | 1 |
| HU02 | 1 | Verificacion y Activacion (OTP) | Cliente | Must | 3 | Si | 1 |
| HU03 | 1 | Autenticacion BioFacial | Cliente | Must | 5 | Si | 1 |
| HU04 | 1 | Recuperacion de Cuenta | Cliente | Should | 5 | Si | 1 |
| HU05 | 2 | Consolidado de Cuentas y Saldos | Cliente | Must | 5 | Si | 1 |
| HU06 | 2 | Transferencias Propias e Interbancarias | Cliente | Must | 8 | Si | 2 |
| HU07 | 2 | Beneficiarios Frecuentes | Cliente | Should | 3 | Si | 2 |
| HU08 | 2 | Autorizacion Biometrica de Pagos | Cliente | Must | 8 | Si | 2 |
| HU09 | 3 | Simulador de Creditos | Cliente | Should | 5 | Si | 2 |
| HU10 | 3 | Solicitud y Evaluacion Crediticia | Cliente | Must | 8 | Si | 3 |
| HU11 | 3 | Firma Digital de Contrato | Cliente | Must | 8 | Si | 3 |
| HU12 | 3 | Desembolso y Cuotas | Cliente | Must | 5 | Si | 3 |
| HU13 | 4 | Billetera Digital | Cliente | Must | 8 | Si | 3 |
| HU14 | 4 | Generacion y Cobro QR | Comercio | Must | 5 | Si | 3 |
| HU15 | 4 | Lectura y Pago QR | Cliente | Must | 5 | Si | 3 |
| HU16 | 4 | Pago de Servicios y Recargas | Cliente | Should | 5 | Si | 4 |
| HU17 | 5 | Motor Transaccional | Sistema | Must | 13 | Si | 1 |
| HU18 | 5 | Ledger de Partida Doble | Sistema | Must | 13 | Si | 1 |
| HU19 | 5 | Conciliacion Bancaria | Analista de operaciones | Must | 8 | Si | 4 |
| HU20 | 6 | Motor Antifraude | Analista de fraude | Must | 8 | Si | 4 |
| HU21 | 6 | Cumplimiento PLDFT / AML | Oficial de cumplimiento | Must | 8 | Si | 4 |
| HU22 | 6 | Notificaciones de Seguridad | Cliente | Should | 3 | Si | 4 |
| HU23 | 6 | Bitacora de Auditoria | Auditor | Could | 5 | **No** | 4 |
| HU24 | 7 | Multidivisa y Meta de Ahorro | Cliente | Should | 5 | Si | 4 |
| HU25 | 7 | Cambio de Divisas | Cliente | Should | 8 | Si | 4 |
| HU26 | 7 | Distribucion de Ingresos | Cliente | Could | 8 | **No** | 4 |

Totales: **26 HU, 17 Must, 7 Should, 2 Could, 173 SP, 160 SP en MVP.**

> El sprint base es una primera aproximacion; el rebalanceo final por dependencia y capacidad
> esta en `14-plan-sprints-y-ejecucion.md`.

## 4. Actores del sistema

| Actor | Descripcion |
|---|---|
| Cliente | Persona natural con cuenta digital. |
| Comercio | Genera QR y recibe abonos. |
| Sistema | Procesos automaticos (motor, ledger, conciliacion). |
| Analista de operaciones | Concilia y gestiona excepciones. |
| Analista de fraude | Gestiona alertas y bloqueos. |
| Oficial de cumplimiento | PLDFT/AML, ROS, listas. |
| Auditor | Consulta evidencia, solo lectura. |
| Administrador de seguridad | Gestiona identidades, roles y permisos. |
| Servicio transaccional | Cuenta tecnica sin acceso interactivo. |

## 5. Exclusiones del MVP (recordatorio)

- Nada de dinero real: **saldos simulados**.
- Sin integracion bancaria real: interoperabilidad solo entre los dos sistemas del curso.
- Sin validacion contra RENIEC ni centrales de riesgo reales: adaptadores simulados.
- Sin huella dactilar fisica: se usa la biometria del dispositivo movil.
- Sin modelos de ML de riesgo/fraude: reglas configurables.
- Sin alta disponibilidad ni escalado productivo.

## 6. Glosario minimo

| Termino | Significado en este proyecto |
|---|---|
| KYC | Conocimiento del cliente; aqui = documento + liveness + match facial. |
| Liveness | Prueba de vida (que sea una persona real, no foto/video). |
| Ledger | Libro mayor; registro contable de partida doble, inmutable. |
| Asiento | Conjunto de movimientos (debitos/creditos) de una operacion. |
| Hold / retencion | Fondos reservados que no estan disponibles pero tampoco se han entregado. |
| Idempotencia | Repetir la misma peticion no duplica el efecto. |
| Conciliacion | Cruzar lo registrado internamente con lo informado por terceros. |
| Clearing | Archivo de compensacion/liquidacion que envia una red de pago. |
| CCI | Codigo de cuenta interbancario. |
| TCEA | Tasa de costo efectivo anual. |
| Spread | Diferencia entre precio de compra y venta de divisas. |
| ROS | Reporte de operacion sospechosa. |
| Reverso | Asiento compensatorio que anula un efecto; **nunca se borra** el original. |

## 7. Supuestos

1. El microservicio KYC estara disponible durante todo el proyecto.
2. Los datos de prueba son simulados (sin PII real).
3. Existira una API gratuita de tipo de cambio; si no, se usan tasas semilla.
4. Se confirma un responsable DevOps antes del Sprint 1.
5. El contrato interbancario con el equipo par puede llegar tarde; el simulador lo cubre.
