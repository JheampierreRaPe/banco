"""Test de humo de la semilla + BD de prueba (Q-T01).

- Sin BD: validaciones del dataset en memoria (siempre corren).
- Con BD (`TEST_DATABASE_URL`, defecto `banca_test`): baseline de esquemas +
  `config.parameters`, carga idempotente y roundtrip del snapshot.
"""

from sqlalchemy import text

from tests import seed as seed_mod
from tests.db_utils import verify_baseline
from tests.factories import (
    build_seed_dataset,
    dataset_fingerprint,
    validate_dataset,
)


def test_dataset_valido_y_sin_pii_real(seed_dataset: dict) -> None:
    assert validate_dataset(seed_dataset) == []
    for user in seed_dataset["users"]:
        assert user["email"].endswith("@example.com")
        assert user["doc_number"].startswith("000")
    for account in seed_dataset["accounts"]:
        assert isinstance(account["balance_minor"], int)
        assert not isinstance(account["balance_minor"], float)


def test_snapshot_en_disco_coincide_con_factories(seed_dataset: dict) -> None:
    path = seed_mod.write_snapshot(seed_dataset)
    assert path.exists()
    assert dataset_fingerprint(seed_mod.read_snapshot(path)) == dataset_fingerprint(seed_dataset)


def test_baseline_esquemas_y_parametros(test_engine) -> None:
    report = verify_baseline(test_engine)
    assert report["schemas_ok"], f"Esquemas faltantes: {report['missing_schemas']}"
    assert report["parameters_ok"], f"Parametros faltantes: {report['missing_keys']}"
    assert report["parameter_count"] == 13


def test_carga_semilla_idempotente_y_roundtrip(test_engine, seed_dataset: dict) -> None:
    first = seed_mod.load_seed_into_db(test_engine, seed_dataset)
    second = seed_mod.load_seed_into_db(test_engine, seed_dataset)
    assert first["fingerprint"] == second["fingerprint"]
    assert first["roundtrip_ok"] and second["roundtrip_ok"]
    with test_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT fingerprint FROM qa.seed_snapshots WHERE seed_version = :v"),
            {"v": seed_dataset["meta"]["version"]},
        ).scalar()
    assert stored == dataset_fingerprint(seed_dataset)


def test_health_y_ready_sobre_bd_de_prueba(db_client) -> None:
    assert db_client.get("/health").json()["status"] == "ok"
    ready = db_client.get("/health/ready").json()
    assert ready["status"] == "ok"
    assert ready["database"] is True


def test_cada_prueba_arranca_en_estado_conocido(db_session) -> None:
    """El rollback de `db_session` aisla: lo escrito aqui no persiste."""
    db_session.execute(text("CREATE TEMP TABLE qa_probe (id INT)"))
    db_session.execute(text("INSERT INTO qa_probe VALUES (1)"))
    assert db_session.execute(text("SELECT count(*) FROM qa_probe")).scalar() == 1
    # Al salir, conftest hace rollback: la tabla temporal desaparece con la sesion.
    assert build_seed_dataset()["meta"]["version"] == "seed-v1"
