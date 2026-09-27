"""Utilidades de base de datos para pruebas (Q-T01).

Solo infraestructura de QA: crear la BD de prueba, aplicar migraciones y
verificar el estado base. No contiene logica de negocio ni toca `app/`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url

BACKEND_DIR = Path(__file__).resolve().parents[1]

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://banca:banca@127.0.0.1:5432/banca_test?connect_timeout=5"
)
MAINTENANCE_DB = "postgres"

# Deben coincidir con migrations/versions/0001_init_schemas.py (solo lectura).
REQUIRED_SCHEMAS = (
    "shared",
    "config",
    "identity",
    "accounts",
    "transactions",
    "ledger",
    "credits",
    "wallet",
    "fx",
    "risk",
    "reconciliation",
    "notifications",
    "audit",
)

EXPECTED_PARAMETER_KEYS = (
    "transfer.biometric_threshold_minor",
    "transfer.daily_limit_minor",
    "transfer.daily_limit_STANDARD_minor",
    "qr.express_limit_minor",
    "otp.ttl_seconds",
    "otp.max_resends",
    "auth.max_failed_attempts",
    "session.inactivity_seconds",
    "kyc.match_threshold",
    "kyc.max_attempts",
    "loan.max_dti_ratio",
    "fx.quote_ttl_seconds",
    "fx.spread_base",
    "pocket.yield_rate",
)


def resolve_test_database_url() -> str:
    """URL de la BD de prueba. Nunca la de desarrollo/produccion por defecto."""
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


def _database_name(database_url: str) -> str:
    return make_url(database_url).database or ""


def is_test_database(database_url: str) -> bool:
    return "test" in _database_name(database_url).lower()


def ensure_test_database(database_url: str, *, allow_non_test: bool = False) -> str:
    """Crea la BD de prueba si no existe. Idempotente y reproducible."""
    if not is_test_database(database_url) and not allow_non_test:
        raise ValueError(
            f"La URL no parece una BD de prueba ({database_url!r}). "
            "Usa TEST_DATABASE_URL apuntando a *_test o pasa allow_non_test=True."
        )
    url = make_url(database_url)
    db_name = url.database or ""
    maintenance_url = url.set(database=MAINTENANCE_DB)
    engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT", future=True)
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": db_name},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        engine.dispose()
    return database_url


def run_migrations(database_url: str, timeout_s: int = 180) -> None:
    """Aplica `alembic upgrade head` contra la BD indicada (via DATABASE_URL)."""
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head fallo (codigo {proc.returncode}):\n"
            f"{proc.stdout}\n{proc.stderr}"
        )


def _make_alembic_config(database_url: str):
    """Config de Alembic in-process (sin subproceso ni `cwd`).

    `script_location` absoluto desde `BACKEND_DIR` y `config_file_name=None`
    para que `migrations/env.py` no reconfigure `logging` en cada llamada.
    """
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.set_main_option("path_separator", "os")
    if database_url:
        cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.config_file_name = None
    return cfg


def _run_alembic_command(database_url: str, operation: str, target: str) -> None:
    """Ejecuta `alembic upgrade/downgrade <target>` en este proceso.

    Equivale al subproceso `python -m alembic <op> <target>` con
    `DATABASE_URL=<database_url>`, sin heredar el entorno al proceso actual
    (restaura `DATABASE_URL` y la caché de settings al salir).
    """
    if operation not in ("upgrade", "downgrade"):
        raise ValueError(f"operacion alembic no soportada: {operation!r}")
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        from app.core.config import get_settings

        get_settings.cache_clear()
        try:
            cfg = _make_alembic_config(database_url)
            from alembic import command

            if operation == "upgrade":
                command.upgrade(cfg, target)
            else:
                command.downgrade(cfg, target)
        finally:
            get_settings.cache_clear()
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def run_migrations_in_process(database_url: str, target: str = "head") -> None:
    """Aplica `alembic upgrade <target>` en este proceso (sin subproceso)."""
    _run_alembic_command(database_url, "upgrade", target)


def downgrade_in_process(database_url: str, target: str) -> None:
    """Aplica `alembic downgrade <target>` en este proceso (sin subproceso)."""
    _run_alembic_command(database_url, "downgrade", target)


def run_alembic_in_process(database_url: str, *args: str) -> None:
    """Compat 1:1 con el `_alembic(*args)` historico de los tests.

    Solo `("upgrade", <rev>)` y `("downgrade", <rev>)`; misma cobertura,
    mismo DDL real, sin arranque de subproceso.
    """
    if len(args) != 2 or args[0] not in ("upgrade", "downgrade"):
        raise ValueError(
            f"args alembic no soportados: {args!r} (usa 'upgrade <rev>' o 'downgrade <rev>')"
        )
    _run_alembic_command(database_url, args[0], args[1])


def get_head_revisions() -> tuple[str, ...]:
    """Revisiones `head` de la cadena (normalmente una sola)."""
    from alembic.script import ScriptDirectory

    return tuple(ScriptDirectory.from_config(_make_alembic_config("")).get_heads())


def get_current_revision(engine: Engine) -> str | None:
    """Version aplicada (`alembic_version`); `None` si no hay tabla/fila."""
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001 - BD fresca sin versionar -> migrar
        return None


def make_test_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, future=True)


def verify_baseline(engine: Engine) -> dict:
    """Verifica el estado base: esquemas + parametros de la migracion 0001."""
    schemas = set(inspect(engine).get_schema_names())
    missing_schemas = [s for s in REQUIRED_SCHEMAS if s not in schemas]
    keys: set[str] = set()
    if "config" in schemas:
        with engine.connect() as conn:
            keys = {row[0] for row in conn.execute(text("SELECT key FROM config.parameters"))}
    missing_keys = [k for k in EXPECTED_PARAMETER_KEYS if k not in keys]
    return {
        "schemas_ok": not missing_schemas,
        "missing_schemas": missing_schemas,
        "parameter_count": len(keys),
        "parameters_ok": not missing_keys,
        "missing_keys": missing_keys,
        "ok": not missing_schemas and not missing_keys,
    }
