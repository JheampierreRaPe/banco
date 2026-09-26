"""Arnes de carga del motor (E5-T08, HU17 CA-04).

Mide el camino de escritura (`transactions.service.execute_transfer`) bajo
rampa de carga y pico de fin de mes. Solo stdlib + SQLAlchemy (sin
locust/k6: no instalados en el entorno; ver README). Sin red, sin BD
externa, sin PII (cuentas sinteticas `uuid4`, montos enteros en centimos),
sin secretos en logs (solo agregados).

Uso:
    python -m tests.load.harness --ramp "1x10,2x10,4x10" --spike 8x10
    pytest tests/load -q
"""

from __future__ import annotations

import argparse
import contextlib
import statistics
import sys
import threading
import time
import types
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from types import SimpleNamespace

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

LATENCY_BUDGET_S = 0.200  # docs/04#12 y E5-T08: escritura < 200 ms
DEFAULT_CURRENCY = "PEN"
DEFAULT_AMOUNT_MINOR = 1000
DEFAULT_FUNDS_MINOR = 1_000_000

HEADER = (
    "stage | workers | total | ok | errores | p50 ms | p95 ms | p99 ms | "
    "tps | veredicto(p95 menor a 200ms)"
)
SEPARATOR = "---|---|---|---|---|---|---|---|---|---"


# ------------------------------------------------------------- configuracion
@dataclass(frozen=True)
class LoadStage:
    """Un escalon de carga: `workers` hilos x `ops_per_worker` operaciones."""

    name: str
    workers: int
    ops_per_worker: int
    simultaneous: bool = False  # True = salida sincronizada (pico)


@dataclass
class OpSample:
    latency_s: float
    ok: bool
    status: str = ""
    error: str = ""


@dataclass
class StageReport:
    stage: LoadStage
    samples: list[OpSample] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def total(self) -> int:
        return len(self.samples)

    @property
    def errors(self) -> int:
        return sum(1 for s in self.samples if not s.ok)

    @property
    def latencies(self) -> list[float]:
        return sorted(s.latency_s for s in self.samples if s.ok)

    def percentile(self, pct: float) -> float:
        return percentile(self.latencies, pct)

    @property
    def throughput(self) -> float:
        return self.total / self.elapsed_s if self.elapsed_s > 0 else 0.0

    def summary(self) -> dict:
        ok_lat = self.latencies
        return {
            "stage": self.stage.name,
            "workers": self.stage.workers,
            "total": self.total,
            "ok": len(ok_lat),
            "errors": self.errors,
            "throughput_ops": round(self.throughput, 1),
            "elapsed_s": round(self.elapsed_s, 3),
            "min_ms": round(min(ok_lat) * 1000, 2) if ok_lat else None,
            "mean_ms": round(statistics.mean(ok_lat) * 1000, 2) if ok_lat else None,
            "p50_ms": round(self.percentile(50) * 1000, 2) if ok_lat else None,
            "p95_ms": round(self.percentile(95) * 1000, 2) if ok_lat else None,
            "p99_ms": round(self.percentile(99) * 1000, 2) if ok_lat else None,
            "max_ms": round(max(ok_lat) * 1000, 2) if ok_lat else None,
            "budget_ms": LATENCY_BUDGET_S * 1000,
        }


def percentile(data: list[float], pct: float) -> float:
    """Percentil por rango mas cercano (datos ya ordenados o no)."""
    if not data:
        raise ValueError("sin datos")
    if not 0 <= pct <= 100:
        raise ValueError(f"percentil fuera de rango: {pct}")
    ordered = sorted(data)
    rank = max(1, int(-(-pct * len(ordered) // 100)))  # techo, base 1
    return ordered[min(rank, len(ordered)) - 1]


# ------------------------------------------------------------------ entorno
class FakeBalances:
    """Port de saldos en memoria (E2-T01 pendiente: el adaptador real lo provee).

    Cada operacion usa cuentas propias, asi que no hay contienda entre hilos
    en este dict; el lock global del arnes emula el bloqueo pesimista de
    filas por cuenta del adaptador productivo (mismo patron que E5-T07).
    """

    def __init__(self, funds: dict[str, dict]):
        self.funds = funds

    def lock_and_get(self, session: Session, account_id):
        cur = self.funds.get(str(account_id), {"available_minor": 0, "currency": "PEN"})
        return SimpleNamespace(
            available_minor=cur["available_minor"],
            currency=cur["currency"],
            account_id=account_id,
        )

    def apply_delta(self, session: Session, account_id, delta_minor: int, currency: str):
        assert isinstance(delta_minor, int) and not isinstance(delta_minor, bool)
        cur = self.funds[str(account_id)]
        cur["available_minor"] += delta_minor


@contextlib.contextmanager
def fake_outbox():
    """Inyecta `app.core.outbox` fake y lo restaura al salir.

    La restauracion es obligatoria: sin ella el fake quedaria en
    `sys.modules` y contaminaria al resto de la suite pytest (mismo proceso).
    """
    key = "app.core.outbox"
    sentinel = object()
    previous = sys.modules.get(key, sentinel)
    fake = types.ModuleType("app.core.outbox")

    def record(session, *, aggregate_type, aggregate_id, event_type, payload):
        return SimpleNamespace(id=uuid.uuid4())

    fake.record = record  # type: ignore[attr-defined]
    sys.modules[key] = fake
    try:
        yield fake
    finally:
        if previous is sentinel:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = previous


def make_engine():
    """Engine SQLite en memoria con las tablas del camino de escritura."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.ledger.models as _lm
    import app.modules.shared.models as _sm
    import app.modules.transactions.models as _tm

    assert _lm is not None and _sm is not None and _tm is not None
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS transactions")
        cur.execute("ATTACH DATABASE ':memory:' AS ledger")
        cur.execute("ATTACH DATABASE ':memory:' AS shared")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["transactions.transactions"],
            Base.metadata.tables["transactions.transaction_status_history"],
            Base.metadata.tables["transactions.holds"],
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["ledger.journal_entries"],
            Base.metadata.tables["ledger.postings"],
            Base.metadata.tables["shared.idempotency_keys"],
        ],
    )
    return engine


# ------------------------------------------------------------------ ejecucion
def run_stage(stage: LoadStage) -> StageReport:
    """Ejecuta un escalon y devuelve latencias p50/p95/p99 + errores."""
    from app.modules.transactions import service as svc

    with fake_outbox():
        engine = make_engine()
        total_ops = stage.workers * stage.ops_per_worker
        # Cuentas sinteticas propias por operacion, fondeadas de sobra: el foco
        # es la latencia del camino de escritura, no el saldo (ya cubierto en E5-T07).
        accounts = [(uuid.uuid4(), uuid.uuid4()) for _ in range(total_ops)]
        funds = {
            str(a): {"available_minor": DEFAULT_FUNDS_MINOR, "currency": DEFAULT_CURRENCY}
            for pair in accounts
            for a in pair
        }
        port = FakeBalances(funds)
        gate = threading.Lock()  # emula bloqueo pesimista de filas por cuenta
        barrier = threading.Barrier(stage.workers) if stage.simultaneous else None
        samples: list[OpSample] = []
        samples_lock = threading.Lock()

        def _one(idx: int) -> None:
            src, dst = accounts[idx]
            if barrier is not None:
                try:
                    barrier.wait(timeout=30)
                except threading.BrokenBarrierError:
                    with samples_lock:
                        samples.append(OpSample(0.0, False, error="barrier-broken"))
                    return
            started = time.perf_counter()
            sess = Session(bind=engine, autoflush=False, expire_on_commit=False)
            try:
                with gate:
                    tx = svc.execute_transfer(
                        sess,
                        source_account_id=src,
                        target_account_id=dst,
                        amount_minor=DEFAULT_AMOUNT_MINOR,
                        currency=DEFAULT_CURRENCY,
                        idempotency_key=f"e5-t08-{stage.name}-{idx}-{uuid.uuid4().hex[:8]}",
                        balance_port=port,
                    )
                    sess.commit()
                latency = time.perf_counter() - started
                ok = tx.status == "SETTLED"
                with samples_lock:
                    samples.append(OpSample(latency, ok, status=tx.status))
            except Exception as exc:  # noqa: BLE001 - se agrega como error
                with samples_lock:
                    samples.append(
                        OpSample(time.perf_counter() - started, False, error=type(exc).__name__)
                    )
            finally:
                sess.close()

        report = StageReport(stage=stage)
        wall_start = time.perf_counter()
        try:
            with ThreadPoolExecutor(
                max_workers=stage.workers, thread_name_prefix=f"load-{stage.name}"
            ) as pool:
                list(pool.map(_one, range(total_ops)))
        finally:
            report.elapsed_s = time.perf_counter() - wall_start
            report.samples = samples
            engine.dispose()
    return report


def run_load(stages: list[LoadStage]) -> list[StageReport]:
    return [run_stage(stage) for stage in stages]


def format_table(reports: list[StageReport]) -> str:
    lines = [HEADER, SEPARATOR]
    for rep in reports:
        s = rep.summary()
        verdict = "PASA" if s["errors"] == 0 and (s["p95_ms"] or 0) < s["budget_ms"] else "REVISAR"
        lines.append(
            f"{s['stage']} | {s['workers']} | {s['total']} | {s['ok']} | "
            f"{s['errors']} | {s['p50_ms']} | {s['p95_ms']} | {s['p99_ms']} | "
            f"{s['throughput_ops']} | {verdict}"
        )
    return "\n".join(lines)


def parse_stages(ramp: str, spike: str) -> list[LoadStage]:
    stages: list[LoadStage] = []
    for token in ramp.split(","):
        token = token.strip()
        if not token:
            continue
        workers, ops = token.lower().split("x")
        stages.append(
            LoadStage(
                name=f"rampa-{workers}w",
                workers=int(workers),
                ops_per_worker=int(ops),
            )
        )
    token = spike.strip()
    if token:
        workers, ops = token.lower().split("x")
        stages.append(
            LoadStage(
                name="pico-fin-de-mes",
                workers=int(workers),
                ops_per_worker=int(ops),
                simultaneous=True,
            )
        )
    return stages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Arnes de carga del motor (E5-T08).")
    parser.add_argument(
        "--ramp", default="1x10,2x10,4x10", help='Rampa "workersxops" (defecto: 1x10,2x10,4x10)'
    )
    parser.add_argument(
        "--spike", default="8x10", help='Pico sincronizado "workersxops" (defecto: 8x10)'
    )
    args = parser.parse_args(argv)
    reports = run_load(parse_stages(args.ramp, args.spike))
    print(format_table(reports))
    ok = all(r.errors == 0 and (r.summary()["p95_ms"] or 0) < 200.0 for r in reports)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
