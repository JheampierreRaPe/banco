"""Carga reproducible de la semilla en la BD de prueba (Q-T01).

Estrategia (los modulos aun son esqueleto, sin tablas de negocio):
1. El dataset vive en `tests/factories.py` (fuente de verdad, determinista).
2. `load_seed_into_db()` persiste el snapshot en `qa.seed_snapshots`
   (esquema propio de QA, idempotente por `seed_version`) y verifica el
   estado base (esquemas + `config.parameters` de la migracion 0001).
3. Cuando cada modulo cree sus tablas, se agregaran loaders que inserten
   estos mismos diccionarios (upsert por `id`), sin cambiar este contrato.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.db_utils import verify_baseline
from tests.factories import (
    DATASET_VERSION,
    DEFAULT_SEED,
    build_seed_dataset,
    dataset_fingerprint,
    validate_dataset,
)

SNAPSHOT_PATH = Path(__file__).resolve().parent / "data" / "seed_demo.json"

QA_DDL = """
CREATE SCHEMA IF NOT EXISTS qa;
CREATE TABLE IF NOT EXISTS qa.seed_snapshots (
    seed_version TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    dataset JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

UPSERT_SNAPSHOT = """
INSERT INTO qa.seed_snapshots (seed_version, fingerprint, dataset)
VALUES (:version, :fingerprint, CAST(:dataset AS JSONB))
ON CONFLICT (seed_version) DO UPDATE
SET fingerprint = EXCLUDED.fingerprint,
    dataset = EXCLUDED.dataset,
    created_at = now();
"""


def write_snapshot(dataset: dict, path: Path = SNAPSHOT_PATH) -> Path:
    """Escribe el snapshot JSON canonico (determinista byte a byte)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(dataset, sort_keys=True, indent=2, ensure_ascii=False)
    path.write_text(canonical + "\n", encoding="utf-8")
    return path


def read_snapshot(path: Path = SNAPSHOT_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_seed_into_db(engine: Engine, dataset: dict) -> dict:
    """Valida, persiste el snapshot (idempotente) y verifica el baseline."""
    errors = validate_dataset(dataset)
    if errors:
        raise ValueError("Dataset invalido:\n- " + "\n- ".join(errors))
    fingerprint = dataset_fingerprint(dataset)
    payload = json.dumps(dataset, sort_keys=True, ensure_ascii=False)
    with engine.begin() as conn:
        conn.execute(text(QA_DDL))
        conn.execute(
            text(UPSERT_SNAPSHOT),
            {
                "version": dataset["meta"]["version"],
                "fingerprint": fingerprint,
                "dataset": payload,
            },
        )
    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT fingerprint FROM qa.seed_snapshots WHERE seed_version = :v"),
            {"v": dataset["meta"]["version"]},
        ).scalar()
    if stored != fingerprint:
        raise RuntimeError("Roundtrip de semilla fallo: fingerprint no coincide")
    return {
        "seed_version": dataset["meta"]["version"],
        "seed": dataset["meta"]["seed"],
        "fingerprint": fingerprint,
        "roundtrip_ok": True,
        "baseline": verify_baseline(engine),
    }


def build_default_dataset(seed: int = DEFAULT_SEED) -> dict:
    return build_seed_dataset(seed)


__all__ = [
    "DATASET_VERSION",
    "SNAPSHOT_PATH",
    "build_default_dataset",
    "dataset_fingerprint",
    "load_seed_into_db",
    "read_snapshot",
    "write_snapshot",
]
