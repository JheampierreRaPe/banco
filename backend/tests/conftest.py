"""Fixtures transversales de QA (Q-T01).

- `client`: TestClient sin BD (humo de API, como antes).
- `seed_dataset`: dataset determinista en memoria (tests/factories.py).
- `test_engine`: engine a la BD de prueba; crea BD + migraciones 1 vez/sesion.
- `db_session`: sesion con rollback -> cada prueba arranca en estado conocido.
- `db_client`: TestClient con `get_db` apuntando a `db_session`.
- `seeded_snapshot`: semilla cargada en `qa.seed_snapshots` (idempotente).

Si Postgres de prueba no esta disponible, las pruebas de integracion se
omiten (`skip`); las unitarias (factories, dataset) siempre corren.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.main import app
from tests import seed as seed_mod
from tests.db_utils import (
    ensure_test_database,
    make_test_engine,
    resolve_test_database_url,
    run_migrations,
)
from tests.factories import DEFAULT_SEED, build_seed_dataset


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="session")
def seed_dataset() -> dict:
    return build_seed_dataset(DEFAULT_SEED)


@pytest.fixture(scope="session")
def test_engine(seed_dataset):
    """Engine a `banca_test` (TEST_DATABASE_URL). Crea BD + migra 1 vez."""
    url = resolve_test_database_url()
    try:
        ensure_test_database(url)
        run_migrations(url)
        engine = make_test_engine(url)
        seed_mod.load_seed_into_db(engine, seed_dataset)
        yield engine
    except Exception as exc:  # noqa: BLE001 - sin BD no hay integracion
        pytest.skip(f"Postgres de prueba no disponible ({exc})")
    finally:
        try:
            engine.dispose()  # type: ignore[possibly-used-before-assignment]
        except Exception:  # noqa: BLE001, S110 - limpieza best-effort
            pass


@pytest.fixture()
def db_session(test_engine) -> Session:
    """Sesion con rollback: cada prueba arranca en estado conocido."""
    connection = test_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def db_client(db_session: Session) -> TestClient:
    """Cliente API cableado a la sesion de prueba (override de `get_db`)."""

    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture(scope="session")
def seeded_snapshot(test_engine, seed_dataset) -> dict:
    """Reporte de la semilla cargada (fingerprint + baseline verificado)."""
    return seed_mod.load_seed_into_db(test_engine, seed_dataset)
