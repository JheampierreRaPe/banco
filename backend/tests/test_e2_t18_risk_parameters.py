"""Umbral y perfiles de riesgo configurables sin despliegue (E2-T18, HU08).

Cubre (brief `docs/tasks/E2-T18.md#pruebas-requeridas`):

- Camino feliz: leer umbral/limite desde `config.parameters`; cambiar el
  valor y verificar que la fachada lo refleja sin reiniciar.
- Errores: clave ausente -> fallback; valor invalido -> fallback sin
  romper; escritura con valor negativo -> rechazo (`VALIDATION_ERROR`);
  escritura de clave inexistente -> `NOT_FOUND`.
- Idempotencia/concurrencia: `set_parameter` repetido no duplica filas
  (`key` UQ); escrituras intercaladas no corrompen el valor.
- `risk_profiles`: alta/lectura/actualizacion, upsert idempotente y
  validaciones.

Parte A: pura (sin BD). Parte B: SQLite en memoria + schemas ATTACH
(`config`/`risk`). Parte C: Postgres de prueba (`db_session`, con
rollback; se omite si no hay BD).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

BACKEND_DIR = Path(__file__).resolve().parents[1]
MIGRATION_PATH = BACKEND_DIR / "migrations" / "versions" / "0023_risk_profiles_params.py"


# ---------------------------------------------------------- Parte A: pura
def test_defaults_match_seeds():
    """Defaults = semillas (`0001`/`0019`/`0023`/`0025`): migrar no cambia conducta."""
    from app.modules.shared.config import parameters as params

    assert params.DEFAULT_BIOMETRIC_THRESHOLD_MINOR == 100_000
    assert params.DEFAULT_DAILY_LIMIT_MINOR == 500_000
    assert params.DEFAULT_MAX_FAILED_ATTEMPTS == 5
    assert params.BIOMETRIC_THRESHOLD_KEY == "transfer.biometric_threshold_minor"
    assert params.DAILY_LIMIT_KEY == "transfer.daily_limit_minor"
    assert params.AUTH_MAX_FAILED_ATTEMPTS_KEY == "auth.max_failed_attempts"
    assert "STANDARD" in params.SEEDED_PROFILES


def test_daily_limit_key_for_profile_normalizes_and_rejects():
    from app.modules.shared.config import parameters as params

    assert params.daily_limit_key_for_profile("standard") == "transfer.daily_limit_STANDARD_minor"
    assert params.daily_limit_key_for_profile("  standard  ") == (
        "transfer.daily_limit_STANDARD_minor"
    )
    for bad in (None, "", "   ", "standard; DROP", "a-b", "(Action)", 123):
        assert params.daily_limit_key_for_profile(bad) is None, bad


def test_error_types_map_to_4xx():
    from app.modules.shared.config import parameters as params

    assert issubclass(params.ParameterNotFoundError, ValueError)
    assert issubclass(params.ParameterValidationError, ValueError)
    assert issubclass(params.ParameterNotFoundError, params.ParameterError)
    assert issubclass(params.ParameterValidationError, params.ParameterError)


def test_facade_respects_boundaries():
    """La fachada no importa modelos ajenos (frontera `shared/config`)."""
    content = (BACKEND_DIR / "app" / "modules" / "shared" / "config" / "parameters.py").read_text(
        encoding="utf-8"
    )
    for token in ("app.modules.identity", "app.modules.accounts", "app.modules.transactions"):
        assert token not in content, f"frontera rota: {token}"
    assert "config.parameters" in content


def test_migration_0023_exists_and_matches_models():
    assert MIGRATION_PATH.exists(), "falta migracion 0023_risk_profiles_params.py"
    content = MIGRATION_PATH.read_text(encoding="utf-8")
    for token in (
        'revision = "0023_risk_profiles_params"',
        'down_revision = "0022_beneficiaries_active_uq"',
        '"risk_profiles"',
        "avg_amount_minor",
        "usual_countries",
        "usual_hours",
        "last_device_id",
        "transfer.daily_limit_STANDARD_minor",
        "ON CONFLICT (key) DO NOTHING",
        "def upgrade",
        "def downgrade",
    ):
        assert token in content, f"migracion sin {token}"
    for other in (
        '"alerts"',
        '"risk_rules"',
        'schema="identity"',
        'schema="accounts"',
        'schema="transactions"',
        '"users"',
        '"credentials"',
    ):
        assert other not in content, f"la migracion no debe tocar otro objeto: {other}"


# --------------------------------------------------------- Parte B: SQLite
@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schemas `config`/`risk` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.risk.models as _r  # noqa: F401 - registra risk.risk_profiles
    from app.core.db import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("config", "risk"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE config.parameters (id TEXT PRIMARY KEY, "
                "key TEXT NOT NULL UNIQUE, value_json TEXT NOT NULL, "
                "module TEXT NOT NULL, description TEXT, updated_by TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
        )
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["risk.risk_profiles"]])
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_param(session: Session, key: str, value: str, module: str = "transactions") -> None:
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, :module)"
        ),
        {"id": uuid.uuid4().hex, "key": key, "value": value, "module": module},
    )
    session.flush()


def _seed_defaults(session: Session) -> None:
    from app.modules.shared.config import parameters as params

    _seed_param(session, params.BIOMETRIC_THRESHOLD_KEY, "100000")
    _seed_param(session, params.DAILY_LIMIT_KEY, "500000")
    _seed_param(session, "transfer.daily_limit_STANDARD_minor", "500000")
    _seed_param(session, params.AUTH_MAX_FAILED_ATTEMPTS_KEY, "5", module="identity")


def test_read_threshold_limits_and_profiles(sqlite_session: Session):
    from app.modules.shared.config import parameters as params

    _seed_defaults(sqlite_session)
    assert params.get_biometric_threshold_minor(sqlite_session) == 100_000
    assert params.get_daily_limit_minor(sqlite_session) == 500_000
    assert params.get_daily_limit_minor(sqlite_session, "STANDARD") == 500_000
    assert params.get_max_failed_attempts(sqlite_session) == 5
    assert params.get_parameter(sqlite_session, params.DAILY_LIMIT_KEY, "X") == "500000"


def test_hot_change_reflected_without_restart(sqlite_session: Session):
    """Cambiar el valor en caliente: la fachada refleja el nuevo valor."""
    from app.modules.shared.config import parameters as params

    _seed_defaults(sqlite_session)
    assert params.get_biometric_threshold_minor(sqlite_session) == 100_000
    assert (
        params.set_parameter(sqlite_session, key=params.BIOMETRIC_THRESHOLD_KEY, value=2_000_000)
        == 2_000_000
    )
    assert params.get_biometric_threshold_minor(sqlite_session) == 2_000_000
    assert (
        params.set_parameter(
            sqlite_session, key="transfer.daily_limit_STANDARD_minor", value=750_000
        )
        == 750_000
    )
    assert params.get_daily_limit_minor(sqlite_session, "STANDARD") == 750_000
    # El perfil por defecto es STANDARD; un perfil sin clave usa el generico.
    assert params.get_daily_limit_minor(sqlite_session) == 750_000
    assert params.get_daily_limit_minor(sqlite_session, "VIP") == 500_000


def test_missing_key_falls_back(sqlite_session: Session):
    from app.modules.shared.config import parameters as params

    assert params.get_biometric_threshold_minor(sqlite_session) == 100_000
    assert params.get_daily_limit_minor(sqlite_session, "VIP") == 500_000
    assert params.get_daily_limit_minor(sqlite_session, "STANDARD") == 500_000
    assert params.get_max_failed_attempts(sqlite_session) == 5
    assert params.get_parameter(sqlite_session, "no.existe", "FB") == "FB"


def test_invalid_stored_value_falls_back_without_raising(sqlite_session: Session):
    from app.modules.shared.config import parameters as params

    _seed_param(session=sqlite_session, key=params.BIOMETRIC_THRESHOLD_KEY, value="-10")
    _seed_param(session=sqlite_session, key=params.DAILY_LIMIT_KEY, value="abc")
    _seed_param(session=sqlite_session, key="transfer.daily_limit_STANDARD_minor", value="12.5")
    _seed_param(
        session=sqlite_session,
        key=params.AUTH_MAX_FAILED_ATTEMPTS_KEY,
        value="0",
        module="identity",
    )
    assert params.get_biometric_threshold_minor(sqlite_session) == 100_000
    assert params.get_daily_limit_minor(sqlite_session) == 500_000
    assert params.get_daily_limit_minor(sqlite_session, "STANDARD") == 500_000
    assert params.get_max_failed_attempts(sqlite_session) == 5


def test_read_without_table_falls_back():
    """Sin `config.parameters` (tabla ausente) la lectura no lanza."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from app.modules.shared.config import parameters as params

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        assert params.get_biometric_threshold_minor(session) == 100_000
        assert params.get_daily_limit_minor(session, "STANDARD") == 500_000
        assert params.get_max_failed_attempts(session) == 5
    finally:
        session.close()
        engine.dispose()


def test_set_negative_rejected_and_row_unchanged(sqlite_session: Session):
    from app.modules.shared.config import parameters as params

    _seed_defaults(sqlite_session)
    with pytest.raises(params.ParameterValidationError):
        params.set_parameter(sqlite_session, key=params.DAILY_LIMIT_KEY, value=-1)
    with pytest.raises(params.ParameterValidationError):
        params.set_parameter(sqlite_session, key=params.AUTH_MAX_FAILED_ATTEMPTS_KEY, value=0)
    for bad in (True, "500000", 12.5, None):
        with pytest.raises(params.ParameterValidationError):
            params.set_parameter(sqlite_session, key=params.DAILY_LIMIT_KEY, value=bad)
    assert params.get_daily_limit_minor(sqlite_session) == 500_000
    assert params.get_max_failed_attempts(sqlite_session) == 5


def test_set_unknown_key_not_found(sqlite_session: Session):
    from app.modules.shared.config import parameters as params

    _seed_defaults(sqlite_session)
    with pytest.raises(params.ParameterNotFoundError):
        params.set_parameter(sqlite_session, key="transfer.no_existe_minor", value=1)
    with pytest.raises(params.ParameterNotFoundError):
        params.set_parameter(sqlite_session, key="", value=1)
    with pytest.raises(params.ParameterNotFoundError):
        params.set_parameter(sqlite_session, key="transfer.daily_limit_VIP_minor", value=1)


def test_set_repeated_does_not_duplicate(sqlite_session: Session):
    """Idempotencia: `set_parameter` repetido no duplica filas (`key` UQ)."""
    from app.modules.shared.config import parameters as params

    _seed_defaults(sqlite_session)
    params.set_parameter(sqlite_session, key=params.DAILY_LIMIT_KEY, value=600_000)
    params.set_parameter(sqlite_session, key=params.DAILY_LIMIT_KEY, value=600_000)
    params.set_parameter(sqlite_session, key=params.DAILY_LIMIT_KEY, value=610_000)
    count = sqlite_session.execute(
        sa.text("SELECT COUNT(*) FROM config.parameters WHERE key = :key"),
        {"key": params.DAILY_LIMIT_KEY},
    ).scalar()
    assert count == 1
    # Perfil sin clave propia ("VIP") cae al generico actualizado.
    assert params.get_daily_limit_minor(sqlite_session, "VIP") == 610_000


def test_interleaved_writes_do_not_corrupt(sqlite_session: Session):
    """Dos escrituras intercaladas: una sola fila y gana el ultimo valor."""
    from app.modules.shared.config import parameters as params

    _seed_defaults(sqlite_session)
    other = Session(bind=sqlite_session.bind, autoflush=False, expire_on_commit=False)
    try:
        params.set_parameter(sqlite_session, key=params.BIOMETRIC_THRESHOLD_KEY, value=1_500_000)
        params.set_parameter(other, key=params.BIOMETRIC_THRESHOLD_KEY, value=2_500_000)
        other.flush()
        count = sqlite_session.execute(
            sa.text("SELECT COUNT(*) FROM config.parameters WHERE key = :key"),
            {"key": params.BIOMETRIC_THRESHOLD_KEY},
        ).scalar()
        assert count == 1
    finally:
        other.close()


def test_set_records_actor_without_pii(sqlite_session: Session):
    from app.modules.shared.config import parameters as params

    _seed_defaults(sqlite_session)
    actor = uuid.uuid4()
    params.set_parameter(
        sqlite_session, key=params.DAILY_LIMIT_KEY, value=600_000, updated_by=actor
    )
    row = (
        sqlite_session.execute(
            sa.text("SELECT updated_by, updated_at FROM config.parameters WHERE key = :key"),
            {"key": params.DAILY_LIMIT_KEY},
        )
        .mappings()
        .one()
    )
    assert row["updated_by"] == str(actor)
    assert row["updated_at"] is not None
    with pytest.raises(params.ParameterValidationError):
        params.set_parameter(
            sqlite_session, key=params.DAILY_LIMIT_KEY, value=1, updated_by="no-uuid"
        )


def test_risk_profile_crud_and_upsert_idempotent(sqlite_session: Session):
    from sqlalchemy import func, select

    from app.modules.risk import repository as risk_repo
    from app.modules.risk import service as risk_svc
    from app.modules.risk.models import RiskProfile

    user_id = uuid.uuid4()
    assert risk_repo.get_by_user(sqlite_session, user_id) is None
    row = risk_svc.get_or_create_profile(sqlite_session, user_id)
    assert row.avg_amount_minor == 0 and row.score == 0
    same = risk_svc.get_or_create_profile(sqlite_session, user_id)
    assert same.user_id == row.user_id
    updated = risk_svc.update_behavior(
        sqlite_session,
        user_id,
        avg_amount_minor=25_000,
        usual_countries=["pe", "cl"],
        usual_hours=[9, 18],
        last_device_id="dev-1",
        score=10,
    )
    assert updated.avg_amount_minor == 25_000
    assert updated.usual_countries == ["PE", "CL"]
    assert updated.usual_hours == [9, 18]
    assert updated.last_device_id == "dev-1"
    assert updated.score == 10
    rows = sqlite_session.execute(
        select(func.count()).select_from(RiskProfile).where(RiskProfile.user_id == user_id)
    ).scalar()
    assert rows == 1


def test_risk_profile_validation(sqlite_session: Session):
    from app.modules.risk import repository as risk_repo

    user_id = uuid.uuid4()
    with pytest.raises(risk_repo.RiskProfileValidationError):
        risk_repo.update_behavior(sqlite_session, user_id, avg_amount_minor=-1)
    with pytest.raises(risk_repo.RiskProfileValidationError):
        risk_repo.update_behavior(sqlite_session, user_id, score=-1)
    with pytest.raises(risk_repo.RiskProfileValidationError):
        risk_repo.update_behavior(sqlite_session, user_id, usual_hours=[25])
    with pytest.raises(risk_repo.RiskProfileValidationError):
        risk_repo.update_behavior(sqlite_session, user_id, usual_countries=[""])
    with pytest.raises(risk_repo.RiskProfileValidationError):
        risk_repo.get_by_user(sqlite_session, "no-uuid")
    assert issubclass(risk_repo.RiskProfileValidationError, ValueError)


# -------------------------------------------------------- Parte C: Postgres
def test_postgres_seeds_and_hot_change(db_session: Session):
    """Semillas reales (`0001`/`0019`/`0023`/`0025`) + cambio en caliente."""
    from app.modules.shared.config import parameters as params

    assert params.get_biometric_threshold_minor(db_session) == 100_000
    assert params.get_daily_limit_minor(db_session) == 500_000
    assert params.get_daily_limit_minor(db_session, "STANDARD") == 500_000
    assert params.get_max_failed_attempts(db_session) == 5
    params.set_parameter(db_session, key="transfer.daily_limit_STANDARD_minor", value=600_000)
    assert params.get_daily_limit_minor(db_session, "STANDARD") == 600_000


def test_postgres_risk_profile_roundtrip(db_session: Session):
    """`risk.risk_profiles` real (ARRAY de Postgres) + upsert idempotente."""
    from sqlalchemy import func, select

    from app.modules.risk import service as risk_svc
    from app.modules.risk.models import RiskProfile

    user_id = uuid.uuid4()
    row = risk_svc.update_behavior(
        db_session,
        user_id,
        avg_amount_minor=40_000,
        usual_countries=["PE"],
        usual_hours=[8, 20],
        score=5,
    )
    assert row.user_id == user_id
    again = risk_svc.get_or_create_profile(db_session, user_id)
    assert again.avg_amount_minor == 40_000
    assert again.usual_countries == ["PE"]
    count = db_session.execute(
        select(func.count()).select_from(RiskProfile).where(RiskProfile.user_id == user_id)
    ).scalar()
    assert count == 1
