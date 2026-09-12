# 16 - Guia para agentes constructores

Este documento es el **manual de trabajo** para cualquier modelo o desarrollador que reciba una
tarea. Leelo completo antes de escribir o modificar codigo.

## 1. Contexto minimo obligatorio antes de una tarea

1. `01-hallazgos-y-decisiones.md` (fuente de verdad si hay contradiccion).
2. `02-arquitectura.md` (donde vive la logica y que se puede tocar).
3. `03-modelo-de-datos.md` (tablas y reglas de integridad).
4. `04-motor-transaccional-y-ledger.md` **si la tarea mueve dinero o estados**.
5. `05-contratos-api.md` (formato de respuestas, errores, auth).
6. El documento de la epica correspondiente (`06`..`12`).

## 2. Reglas de oro (no negociables)

1. Toda operacion de dinero pasa por `transactions` y termina en `ledger`.
2. Nunca se edita ni borra un asiento, transaccion o registro de auditoria; se revierte.
3. El dinero es entero en centimos; **nunca** `float`.
4. Ningun modulo accede a las tablas de otro; se usan fachadas o eventos.
5. Todo endpoint de dinero exige `Idempotency-Key`.
6. Toda regla variable (umbral, tasa, limite) va en `config.parameters`.
7. No guardar frames biometricos ni PII en logs.
8. No publicar eventos dentro de la transaccion de negocio; usar `outbox`.
9. Respetar la matriz de accesos; enmascarar por defecto.
10. Sin secretos en el repositorio.

## 3. Plantilla de tarea (como se describe cada unidad de trabajo)

```
ID:            <E#-T##>
Modulo:        <identity|accounts|transactions|...>
Objetivo:      <resultado verificable en una frase>
Entradas:      <entidades, eventos o endpoints que recibe>
Salidas:       <entidades, eventos o endpoints que produce>
Reglas:        <reglas de negocio y parametros involucrados>
Dependencias:  <tareas/HU que deben estar listas>
CA cubiertos:  <CA-01, CA-02...>
Pruebas:       <casos felices y de error>
DoD:           <ademas del general, criterios especificos>
```

## 4. Flujo de trabajo recomendado

1. Leer los documentos de la seccion 1 y la tarea asignada.
2. Confirmar dependencias y que no se pisa trabajo de otro modulo.
3. Escribir primero el **dominio puro** (testeable sin base de datos).
4. Implementar repositorio y caso de uso.
5. Exponer el endpoint con su esquema y contrato OpenAPI.
6. Escribir pruebas (camino feliz + errores + concurrencia si aplica).
7. Actualizar documentacion de la tarea y `README.md` del modulo.
8. Abrir PR siguiendo `15-calidad-seguridad-devops.md`.

## 5. Definition of Done (DoD)

Una tarea esta lista solo si:

- [ ] Cumple todos sus criterios de aceptacion (`CA`).
- [ ] Cubre caminos feliz y de error con pruebas automatizadas.
- [ ] Respeta la matriz de accesos y el enmascaramiento.
- [ ] Genera auditoria en los eventos sensibles.
- [ ] Si mueve dinero: deja asiento contable y cuadra.
- [ ] Es idempotente si mueve dinero o crea recursos.
- [ ] Reglas configurables fuera del codigo.
- [ ] Contrato/OpenAPI actualizado y validado.
- [ ] Sin secretos, sin PII en logs, sin `TODO` bloqueantes.
- [ ] CI verde y revision de otra persona.
- [ ] `README.md` del modulo actualizado.

## 6. Prompt modelo para asignar una tarea

> "Trabajas en el modulo `<modulo>` del proyecto Banca Online Integral. Lee
> `docs/01`, `docs/02`, `docs/03`, `docs/04` (si aplica), `docs/05` y
> `docs/<documento-de-epica>`. Implementa la tarea `<E#-T##>` siguiendo la plantilla de
> `docs/16`. No accedas a tablas de otros modulos. Si mueves dinero, usa el motor y el ledger.
> Entrega: cambio + pruebas + actualizacion de OpenAPI y del README del modulo. Indica que CA
> cubriste y como lo verificaste."

## 7. Errores comunes a evitar

| Error | Correccion |
|---|---|
| Actualizar saldo sin asiento | Pasar siempre por el motor. |
| Implementar KYC de nuevo | Consumir el microservicio existente. |
| Validar contra RENIEC/buro reales | Usar los adaptadores simulados. |
| Cablear umbrales y tasas | Moverlos a `config.parameters`. |
| Hacer `JOIN` entre modulos | Usar IDs y eventos / fachadas. |
| Guardar el PAN o frames | Tokenizar / guardar solo el resultado. |
| Bloquear el hilo en llamadas externas | Timeouts, reintentos y circuit breaker. |
| Publicar evento dentro de la transaccion | Patron outbox. |

## 8. Protocolo de entrega (reporte de la tarea)

Al terminar, el ejecutor debe reportar:

1. Tarea y modulo.
2. Que se implemento (1-3 lineas).
3. CA cubiertos y evidencia (pruebas).
4. Decisiones tomadas y supuestos.
5. Pendientes/riesgos descubiertos (registrar en `17`).
6. Archivos y endpoints/eventos afectados.

## 9. Como dividir trabajo en paralelo

- Un modulo = un responsable principal (evita colisiones).
- `ledger` y `transactions` no se paralelizan entre si: van juntos y primero.
- Frontend puede ir en paralelo usando mocks de la API.
- Datos y QA acompanan cada modulo desde el inicio, no al final.
- DevOps prepara CI y entornos en la fase de preparacion.
