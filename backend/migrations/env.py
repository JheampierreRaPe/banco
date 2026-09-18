"""Entorno Alembic multi-esquema (un schema por modulo)."""

import os
import sys
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.dialects import postgresql

# Permitir importar `app` al ejecutar alembic desde backend/
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import importlib

import app.modules
from app.core.config import get_settings
from app.core.db import Base

# Registrar todos los modelos en Base.metadata para autogenerate/check:
# `app.modules` solo hace imports perezosos de `.api`, asi que se importa
# aqui cada `app.modules.<m>.models` (`shared` no tiene router y no esta en
# MODULES, pero si tiene modelos). Se tolera ModuleNotFoundError para
# modulos sin paquete `models` (p. ej. futuros modulos solo-API).
for _mod_name in (*app.modules.MODULES, "shared"):
    try:
        importlib.import_module(f"app.modules.{_mod_name}.models")
    except ModuleNotFoundError:
        continue

# `accounts.movements_view` es modelo ORM pero vive en el repositorio
# (`accounts/repository/movements.py`, sin efectos colaterales al importar:
# solo define la clase + indice); se registra aqui para que la metadata
# quede completa.
import app.modules.accounts.repository.movements

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Esquemas del sistema (deben existir antes de crear tablas de negocio).
SCHEMAS = (
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


# `config.parameters` la gestiona la migracion 0001 (tabla semilla sin modelo
# ORM): se excluye de autogenerate/check para no reportar `remove_table`.
def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table":
        return not (getattr(object, "schema", None) == "config" and name == "parameters")
    if type_ == "index":
        table = getattr(object, "table", None)
        if (
            table is not None
            and getattr(table, "schema", None) == "config"
            and table.name == "parameters"
        ):
            return False
    return True


# `shared` usa `sa.JSON()` generico por portabilidad SQLite (ver
# `shared/models`); en Postgres las migraciones usan `JSONB`: equivalencia
# intencional, no drift. `audit.audit_log.ip` usa `sa.String(45)` portable
# en el modelo y `INET` en la migracion 03b: equivalencia intencional, no
# drift. Resto de tipos: comparacion por defecto.
def compare_type(context, inspected_column, metadata_column, inspected_type, metadata_type):
    if isinstance(inspected_type, postgresql.JSONB) and type(metadata_type) is sa.JSON:
        return False
    if isinstance(inspected_type, postgresql.INET) and (
        isinstance(metadata_type, sa.String) and metadata_type.length == 45
    ):
        return False
    return None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        include_object=include_object,
        compare_type=compare_type,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_object=include_object,
            compare_type=compare_type,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
