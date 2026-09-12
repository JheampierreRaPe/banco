"""Script versionado de semilla reproducible (Q-T01).

Uso (desde `backend/`):
    .venv\\Scripts\\python.exe scripts\\seed_demo.py check
    .venv\\Scripts\\python.exe scripts\\seed_demo.py dump
    .venv\\Scripts\\python.exe scripts\\seed_demo.py load [--database-url ...] [--seed 42]

- `check`: la misma semilla genera el mismo dataset (reproducible).
- `dump`:  imprime el snapshot JSON canonico por stdout.
- `load`:  crea la BD de prueba si falta, aplica migraciones, carga la
  semilla (idempotente) y verifica el estado base.

Por seguridad solo opera sobre BDs de prueba (`*test*`), salvo
`--allow-non-test-db`. Nunca usar datos personales reales.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from tests import seed as seed_mod
from tests.db_utils import (
    ensure_test_database,
    is_test_database,
    make_test_engine,
    resolve_test_database_url,
    run_migrations,
)
from tests.factories import (
    DEFAULT_SEED,
    build_seed_dataset,
    dataset_fingerprint,
    validate_dataset,
)


def cmd_check(seed: int) -> int:
    dataset_a = build_seed_dataset(seed)
    dataset_b = build_seed_dataset(seed)
    fa, fb = dataset_fingerprint(dataset_a), dataset_fingerprint(dataset_b)
    errors = validate_dataset(dataset_a)
    print(f"seed={seed} fingerprint={fa}")
    if fa != fb:
        print("ERROR: la semilla no es reproducible (fingerprints difieren)")
        return 1
    if errors:
        print("ERROR: dataset invalido:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("OK: semilla reproducible y dataset valido")
    return 0


def cmd_dump(seed: int) -> int:
    dataset = build_seed_dataset(seed)
    errors = validate_dataset(dataset)
    if errors:
        print("ERROR: dataset invalido:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(json.dumps(dataset, sort_keys=True, indent=2, ensure_ascii=False))
    return 0


def cmd_load(database_url: str, seed: int, allow_non_test: bool) -> int:
    if not is_test_database(database_url) and not allow_non_test:
        print(
            f"ERROR: {database_url!r} no parece BD de prueba. "
            "Pasa --allow-non-test-db para forzar.",
            file=sys.stderr,
        )
        return 1
    allow = allow_non_test or is_test_database(database_url)
    print(f"[1/4] Asegurando BD de prueba: {database_url}")
    ensure_test_database(database_url, allow_non_test=allow)
    print("[2/4] Aplicando migraciones (alembic upgrade head)...")
    run_migrations(database_url)
    dataset = build_seed_dataset(seed)
    path = seed_mod.write_snapshot(dataset)
    print(f"[3/4] Snapshot escrito en {path} (fingerprint={dataset_fingerprint(dataset)})")
    engine = make_test_engine(database_url)
    try:
        print("[4/4] Cargando semilla en la BD...")
        report = seed_mod.load_seed_into_db(engine, dataset)
    finally:
        engine.dispose()
    baseline = report["baseline"]
    print(f"  seed_version={report['seed_version']} roundtrip_ok={report['roundtrip_ok']}")
    print(
        f"  baseline ok={baseline['ok']} "
        f"(esquemas={baseline['schemas_ok']}, parametros={baseline['parameter_count']})"
    )
    if not baseline["ok"]:
        print(f"  faltan esquemas: {baseline['missing_schemas']}", file=sys.stderr)
        print(f"  faltan parametros: {baseline['missing_keys']}", file=sys.stderr)
        return 1
    print("OK: semilla cargada y verificada")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Semilla demo reproducible (solo datos simulados)."
    )
    parser.add_argument("--seed", type=int, default=int(os.getenv("SEED", DEFAULT_SEED)))
    parser.add_argument("--database-url", default=resolve_test_database_url())
    parser.add_argument("--allow-non-test-db", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Verifica reproducibilidad y validez del dataset.")
    sub.add_parser("dump", help="Imprime el snapshot JSON canonico.")
    sub.add_parser("load", help="Crea BD prueba, migra y carga la semilla.")
    args = parser.parse_args(argv)
    if args.command == "check":
        return cmd_check(args.seed)
    if args.command == "dump":
        return cmd_dump(args.seed)
    return cmd_load(args.database_url, args.seed, args.allow_non_test_db)


if __name__ == "__main__":
    raise SystemExit(main())
