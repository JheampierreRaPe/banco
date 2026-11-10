# Reporte de carga del motor (E5-T08) — 2026-09-25 UTC

Objetivo: verificar el SLA (< 200 ms en el camino de escritura) bajo carga
normal y pico de fin de mes (HU17 CA-04).

## Resultado (arnes `python -m tests.load.harness --ramp "1x10,2x10,4x10" --spike 8x10`)

| stage | workers | total | ok | errores | p50 ms | p95 ms | p99 ms | tps | veredicto |
|---|---|---|---|---|---|---|---|---|---|
| rampa-1w | 1 | 10 | 10 | 0 | 6.54 | 22.37 | 22.37 | 121.4 | PASA |
| rampa-2w | 2 | 20 | 20 | 0 | 12.45 | 13.48 | 17.89 | 150.7 | PASA |
| rampa-4w | 4 | 40 | 40 | 0 | 24.99 | 26.39 | 31.20 | 144.8 | PASA |
| pico-fin-de-mes | 8 | 80 | 80 | 0 | 29.61 | 53.37 | 73.00 | 146.8 | PASA |

Suite `pytest tests/load -q`: **3 passed** (rampa + pico + helper de percentiles).

## Alcance y honestidad

- **Hecho:** rampa, pico sincronizado (barrera de salida simultanea) y
  reporte p50/p95/p99 + errores + throughput, reproducibles con stdlib.
- **No hecho:** `locust`/`k6` no estan instalados en el entorno y no se
  instalaron a la fuerza; carga contra Postgres real no disponible en el
  entorno de test (el arnes usa SQLite en memoria con lock global que emula
  el bloqueo pesimista de filas, mismo patron que E5-T07). Los numeros miden
  el camino de escritura del motor, no la latencia de red ni de Postgres
  productivo: el SLA < 200 ms queda verificado a nivel de motor, pendiente
  revalidar contra Postgres/API en CI con servicios (Q-T05).
- Sin PII ni secretos en logs: solo agregados (percentiles, conteos).
