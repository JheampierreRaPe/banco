"""E5-T08 (HU17 CA-04): prueba de carga del motor.

Rampa de carga + pico de fin de mes sobre el camino de escritura
(`execute_transfer`) con el arnes reproducible de `tests.load.harness`.
Volumenes moderados y acotados en tiempo (decenas de operaciones, segundos)
para no saturar la maquina. Dinero solo de juguete (cuentas sinteticas,
centimos enteros, sin PII); sin red ni BD externa.

- Rampa: verifica el SLA (< 200 ms en p95) bajo carga normal.
- Pico: salida sincronizada de N hilos; verifica que el flujo de escritura
  no se degrada (cero errores, todo `SETTLED`) y reporta p50/p95/p99.
"""

from __future__ import annotations

from tests.load import harness as H

RAMP_STAGES = [
    H.LoadStage(name="rampa-1w", workers=1, ops_per_worker=10),
    H.LoadStage(name="rampa-2w", workers=2, ops_per_worker=10),
    H.LoadStage(name="rampa-4w", workers=4, ops_per_worker=10),
]
SPIKE_STAGE = H.LoadStage(name="pico-fin-de-mes", workers=8, ops_per_worker=5, simultaneous=True)


def test_e5_t08_percentile_helper():
    """El calculo de percentiles cubre bordes (vacio/rango)."""
    import pytest

    assert H.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    assert H.percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0
    with pytest.raises(ValueError):
        H.percentile([], 50)
    with pytest.raises(ValueError):
        H.percentile([1.0], 101)


def test_e5_t08_rampa_latencia_bajo_sla():
    """Rampa 1/2/4 hilos: cero errores y p95 < 200 ms en cada escalon."""
    reports = H.run_load(RAMP_STAGES)
    print("\n" + H.format_table(reports))
    assert reports, "la rampa debe producir reportes"
    for rep in reports:
        summary = rep.summary()
        assert summary["total"] == rep.stage.workers * rep.stage.ops_per_worker
        assert summary["errors"] == 0, f"{rep.stage.name}: errores bajo carga"
        assert summary["ok"] == summary["total"]
        assert (
            summary["p95_ms"] is not None and summary["p95_ms"] < 200.0
        ), f"{rep.stage.name}: p95 {summary['p95_ms']} ms >= SLA 200 ms"


def test_e5_t08_pico_fin_de_mes_sin_degradacion():
    """Pico sincronizado (8 hilos): el flujo de escritura no se degrada."""
    report = H.run_stage(SPIKE_STAGE)
    print("\n" + H.format_table([report]))
    summary = report.summary()
    assert summary["errors"] == 0, f"pico con errores: {summary}"
    assert summary["ok"] == summary["total"] == 40
    assert summary["p50_ms"] is not None
    assert summary["p95_ms"] is not None
    assert summary["p99_ms"] is not None
    assert summary["p95_ms"] < 200.0, f"pico degrada la escritura: {summary}"
    assert all(s.status == "SETTLED" for s in report.samples if s.ok)
