# Prueba de carga del motor (E5-T08)

Arnes reproducible con stdlib (`threading` + `time`): `locust`/`k6` no estan
instalados en el entorno y no se instalan a la fuerza (ver `REPORT.md`).

## Ejecutar

Desde `backend/`:

```bash
pytest tests/load -q -s        # suite (rampa + pico + helper)
python -m tests.load.harness --ramp "1x10,2x10,4x10" --spike 8x10
python -m tests.load.harness --help
```

Volumenes moderados y acotados en tiempo por diseno (decenas de operaciones,
segundos de ejecucion) para no saturar la maquina.

## Que mide

Camino de escritura `transactions.service.execute_transfer` (hold + asientos
+ estados + idempotencia) sobre SQLite en memoria, con lock global que emula
el bloqueo pesimista de filas por cuenta del adaptador productivo (mismo
patron que `test_e5_t07_concurrency.py`). Cada operacion usa cuentas
sinteticas propias (`uuid4`, sin PII) fondeadas de sobra: el foco es la
latencia, no el saldo (cubierto en E5-T07).

Reporta por escalon: total/ok/errores, `p50/p95/p99`, media, min/max,
throughput y veredicto contra el SLA (< 200 ms en p95).
