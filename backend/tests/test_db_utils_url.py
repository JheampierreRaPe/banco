"""URL por defecto de la BD de prueba (infra QA, sin BD).

Regresion: el default debe usar `127.0.0.1` (no `localhost`, que resuelve
primero a IPv6 `::1` y el Postgres de Docker solo publica `127.0.0.1:5432`)
con `connect_timeout=5` defensivo. Sin esta prueba, un cambio a `localhost`
reintroduce ~130 s de espera muerta por invocacion de alembic en subproceso.
"""

from sqlalchemy.engine.url import make_url

from tests.db_utils import DEFAULT_TEST_DATABASE_URL, resolve_test_database_url


def test_default_apunta_a_loopback_ipv4() -> None:
    assert "localhost" not in DEFAULT_TEST_DATABASE_URL
    assert make_url(DEFAULT_TEST_DATABASE_URL).host == "127.0.0.1"


def test_default_con_timeout_de_conexion_defensivo() -> None:
    query = make_url(DEFAULT_TEST_DATABASE_URL).query
    assert query.get("connect_timeout") == "5"


def test_resolve_respeta_test_database_url(monkeypatch) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:5432/otro_test")
    assert resolve_test_database_url() == "postgresql+psycopg://u:p@127.0.0.1:5432/otro_test"


def test_resolve_sin_env_devuelve_default(monkeypatch) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    assert resolve_test_database_url() == DEFAULT_TEST_DATABASE_URL
