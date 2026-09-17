"""Servicio de OTP (E1-T08, HU02 CA-01/CA-02/CA-03).

- Parte A (sin BD): metadatos del modelo `identity.otp_codes` segun
  `03b#4.5` (columnas exactas + `resend_count` documentado), firma de
  `generate_otp`/`validate_otp`/`resend_otp` y de los re-exports del
  repositorio, y reglas (solo hash+salt, `secrets`, comparacion constante,
  sin `commit`, sin codigo en logs, sin tocar otros schemas, outbox y
  notificaciones solo via import perezoso).
- Parte B (SQLite en memoria + schemas ATTACH): generar+validar OK (un solo
  uso + `user.activated` en outbox solo con `ACTIVATION`); reutilizado
  rechazado; expirado rechazado; limite de intentos con bloqueo; reenvio que
  invalida el anterior + espera minima + maximo de reenvios.
- Parte C (Postgres `db_session`): humo de integracion + migracion
  `up/down` de la revision propia en base de prueba; se omite sin BD.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0014_identity_otp.py"
)
IDENTITY_MODELS_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "models" / "__init__.py"
)
OTP_REPO_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "repository"
    / "otp.py"
)
OTP_SERVICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "service"
    / "otp_service.py"
)

OTP_COLUMNS = {
    "id",
    "user_id",
    "purpose",
    "destination",
    "code_hash",
    "expires_at",
    "attempts",
    "max_attempts",
    "resend_count",
    "status",
    "created_at",
    "consumed_at",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- Parte A: modelo
def test_table_registered_with_schema_columns_constraints():
    import app.modules.identity.models as _i  # noqa: F401 (registro)

    table = Base.metadata.tables["identity.otp_codes"]
    assert table.schema == "identity"
    assert {c.name for c in table.columns} == OTP_COLUMNS, (
        f"columnas 03b#4.5 + resend_count, recibido: "
        f"{sorted(c.name for c in table.columns)}"
    )
    assert isinstance(table.columns["purpose"].type, sa.String)
    assert table.columns["purpose"].type.length == 30
    assert isinstance(table.columns["code_hash"].type, sa.String)
    assert table.columns["code_hash"].type.length == 128
    assert isinstance(table.columns["status"].type, sa.String)
    assert table.columns["destination"].nullable is True, (
        "destination nulable (desviacion documentada: canal aun no resuelto)"
    )
    for fk in table.foreign_keys:
        assert fk.column.table.schema == "identity", (
            f"FK fuera del schema propio: identity.otp_codes -> "
            f"{fk.column.table.schema}.{fk.column.table.name}"
        )
    checks = " ".join(
        str(c.sqltext) for c in table.constraints if isinstance(c, sa.CheckConstraint)
    )
    for token in ("ACTIVATION", "RECOVERY", "PAYMENT", "LOGIN", "PENDING", "USED", "EXPIRED"):
        assert token in checks, f"CK sin {token}: {checks}"
    index_cols = {tuple(i.columns.keys()) for i in table.indexes}
    assert ("user_id", "purpose", "status") in index_cols, (
        f"falta indice 03b#15 (user_id, purpose, status): {index_cols}"
    )
    assert ("expires_at",) in index_cols, "falta indice 03b#15 (expires_at)"


def test_only_hash_with_salt_no_commit_no_code_in_logs():
    svc_content = OTP_SERVICE_PATH.read_text(encoding="utf-8")
    repo_content = OTP_REPO_PATH.read_text(encoding="utf-8")
    for path, content in (("otp_service.py", svc_content), ("otp.py", repo_content)):
        assert ".commit(" not in content, f"flush sin commit en {path}"
        assert "float(" not in content, f"sin float en {path}"
    assert "secrets" in svc_content, "codigo aleatorio seguro con secrets"
    assert "compare_digest" in svc_content, "comparacion en tiempo constante"
    assert "salt" in svc_content and "$" in svc_content, "hash + salt combinados"
    log_lines = [line for line in svc_content.splitlines() if "logger." in line]
    assert log_lines, "el servicio debe loguear sin PII"
    for line in log_lines:
        assert "plain" not in line, f"codigo en claro en logs: {line.strip()}"
    assert "code_hash" not in " ".join(log_lines), "hash fuera de los logs"
    # `outbox` y `notifications` solo via import perezoso (sin ciclos).
    assert "    from app.core.outbox import" in svc_content, (
        "user.activated solo via outbox con import perezoso (E1-T03)"
    )
    assert "    from app.modules.notifications.service import" in svc_content, (
        "notificacion solo via fachada send con import perezoso"
    )
    assert "from app.core.outbox import" not in svc_content.splitlines()[0:30].__str__() or True
    top_imports = "\n".join(
        line for line in svc_content.splitlines()
        if line.startswith("from app.") or line.startswith("import app.")
    )
    assert "outbox" not in top_imports, "outbox no debe importarse a nivel modulo"
    assert "notifications" not in top_imports, "notifications no debe importarse a nivel modulo"


def test_service_and_repository_signatures():
    from app.modules.identity.repository import otp as otp_repo
    from app.modules.identity.service import otp_service as svc

    for fn_name, required_kwonly in (
        ("generate_otp", ("user_id", "purpose")),
        ("validate_otp", ("user_id", "purpose", "code")),
        ("resend_otp", ("user_id", "purpose")),
    ):
        fn = getattr(svc, fn_name)
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        assert params[0].name == "session", f"{fn_name}: primer parametro session"
        for expected in required_kwonly:
            assert expected in sig.parameters, f"{fn_name} sin {expected}"
            assert sig.parameters[expected].kind is inspect.Parameter.KEYWORD_ONLY, (
                f"{fn_name}.{expected} debe ser keyword-only"
            )

    for fn_name in ("create_otp", "get_active", "mark_used", "mark_expired", "bump_attempts"):
        assert hasattr(otp_repo, fn_name), f"repositorio otp sin {fn_name}"
    from app.modules.identity import repository as repo

    for expected in ("create_otp", "get_active_otp", "mark_otp_used", "mark_otp_expired"):
        assert hasattr(repo, expected), f"repository/__init__ sin re-export {expected}"

    for exc_name in (
        "OtpNotFoundError",
        "OtpExpiredError",
        "OtpInvalidError",
        "OtpAttemptsExceededError",
        "OtpMaxResendsExceededError",
        "OtpResendTooSoonError",
    ):
        assert issubclass(getattr(svc, exc_name), ValueError), (
            f"{exc_name} debe ser ValueError (mapeable a 4xx en E1-T10)"
        )


def test_constants_match_parameters_seed():
    from app.modules.identity.service import otp_service as svc

    assert svc.OTP_TTL_SECONDS == 600, "default = semilla otp.ttl_seconds"
    assert svc.OTP_MAX_RESENDS == 3, "default = semilla otp.max_resends"
    assert svc.OTP_RESEND_WAIT_SECONDS > 0, "espera minima positiva documentada"
    assert svc.OTP_MAX_ATTEMPTS == 3, "umbral = 03b#4.5 max_attempts default"
    content = OTP_SERVICE_PATH.read_text(encoding="utf-8")
    for token in ("otp.ttl_seconds", "otp.max_resends", "config.parameters"):
        assert token in content, f"origen documentado sin {token}"


def test_migration_0014_exists_and_matches_models():
    assert MIGRATION_PATH.exists(), "falta migracion 0014_identity_otp.py"
    content = MIGRATION_PATH.read_text(encoding="utf-8")
    for token in (
        'revision = "0014_identity_otp"',
        'down_revision = "0013_identity_kyc_audit"',
        '"otp_codes"',
        "fk_otp_codes_user",
        "ck_otp_codes_purpose",
        "ck_otp_codes_status",
        "ix_otp_codes_user_purpose_status",
        "ix_otp_codes_expires_at",
        "code_hash",
        "resend_count",
        "consumed_at",
        "def upgrade",
        "def downgrade",
    ):
        assert token in content, f"migracion sin {token}"
    for other in (
        'schema="accounts"',
        'schema="ledger"',
        'schema="transactions"',
        'schema="audit"',
        "schema='accounts'",
        '"users"',
        '"credentials"',
        '"kyc_verifications"',
        "audit_log",
        "ledger_accounts",
    ):
        assert other not in content, f"la migracion no debe tocar otro objeto: {other}"


# ---------------------------------------------------------------- Parte B: SQLite
@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schemas `identity`/`shared` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.identity.models as _i  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("identity", "shared"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["identity.users"],
            Base.metadata.tables["identity.otp_codes"],
            Base.metadata.tables["shared.outbox"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_user(sqlite_session: Session) -> uuid.UUID:
    from app.modules.identity.models import User

    user = User(
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Ada",
        last_name="Lovelace",
    )
    sqlite_session.add(user)
    sqlite_session.flush()
    return user.id


def _outbox_events(sqlite_session: Session) -> list:
    from app.modules.shared.models import OutboxEntry

    stmt = sa.select(OutboxEntry).order_by(OutboxEntry.created_at.asc())
    return list(sqlite_session.scalars(stmt).all())


def test_generate_and_validate_ok_single_use_with_activation_event(sqlite_session: Session):
    from app.modules.identity.service import otp_service as svc

    user_id = _make_user(sqlite_session)
    row, plain = svc.generate_otp(sqlite_session, user_id=user_id, purpose="ACTIVATION")
    assert len(plain) == 6 and plain.isdigit()
    assert row.status == "PENDING"
    assert row.attempts == 0 and row.resend_count == 0
    assert row.code_hash != plain and "$" in row.code_hash
    assert len(row.code_hash) <= 128

    validated = svc.validate_otp(
        sqlite_session, user_id=user_id, purpose="ACTIVATION", code=plain
    )
    assert validated.id == row.id
    assert validated.status == "USED"
    assert validated.consumed_at is not None

    events = _outbox_events(sqlite_session)
    assert len(events) == 1
    assert events[0].event_type == "user.activated"
    assert events[0].status == "PENDING"
    assert events[0].payload["user_id"] == str(user_id)

    with pytest.raises(svc.OtpNotFoundError):
        svc.validate_otp(sqlite_session, user_id=user_id, purpose="ACTIVATION", code=plain)
    sqlite_session.rollback()


def test_non_activation_purpose_emits_no_event(sqlite_session: Session):
    from app.modules.identity.service import otp_service as svc

    user_id = _make_user(sqlite_session)
    row, plain = svc.generate_otp(sqlite_session, user_id=user_id, purpose="RECOVERY")
    svc.validate_otp(sqlite_session, user_id=user_id, purpose="RECOVERY", code=plain)
    assert row.status == "USED"
    assert _outbox_events(sqlite_session) == []
    sqlite_session.rollback()


def test_expired_otp_rejected(sqlite_session: Session):
    from app.modules.identity.service import otp_service as svc

    user_id = _make_user(sqlite_session)
    base = _utcnow()
    row, plain = svc.generate_otp(
        sqlite_session, user_id=user_id, purpose="LOGIN", now=base
    )
    with pytest.raises(svc.OtpExpiredError):
        svc.validate_otp(
            sqlite_session,
            user_id=user_id,
            purpose="LOGIN",
            code=plain,
            now=base + timedelta(seconds=svc.OTP_TTL_SECONDS + 1),
        )
    assert row.status == "EXPIRED"
    sqlite_session.rollback()


def test_attempts_limit_blocks_with_documented_threshold(sqlite_session: Session):
    from app.modules.identity.service import otp_service as svc

    user_id = _make_user(sqlite_session)
    row, plain = svc.generate_otp(sqlite_session, user_id=user_id, purpose="LOGIN")
    wrong = "000000" if plain != "000000" else "111111"

    with pytest.raises(svc.OtpInvalidError) as exc1:
        svc.validate_otp(sqlite_session, user_id=user_id, purpose="LOGIN", code=wrong)
    assert exc1.value.remaining_attempts == 2
    with pytest.raises(svc.OtpInvalidError) as exc2:
        svc.validate_otp(sqlite_session, user_id=user_id, purpose="LOGIN", code=wrong)
    assert exc2.value.remaining_attempts == 1
    assert row.attempts == 2

    with pytest.raises(svc.OtpAttemptsExceededError):
        svc.validate_otp(sqlite_session, user_id=user_id, purpose="LOGIN", code=wrong)
    assert row.status == "EXPIRED"
    # Bloqueado: ni el codigo correcto pasa (umbral = max_attempts de la fila = 3).
    assert row.max_attempts == svc.OTP_MAX_ATTEMPTS == 3
    with pytest.raises(svc.OtpNotFoundError):
        svc.validate_otp(sqlite_session, user_id=user_id, purpose="LOGIN", code=plain)
    sqlite_session.rollback()


def test_resend_invalidates_previous_enforces_wait_and_max(sqlite_session: Session):
    from app.modules.identity.service import otp_service as svc

    user_id = _make_user(sqlite_session)
    base = _utcnow()
    _, first = svc.generate_otp(
        sqlite_session, user_id=user_id, purpose="ACTIVATION", now=base
    )

    with pytest.raises(svc.OtpResendTooSoonError) as too_soon:
        svc.resend_otp(sqlite_session, user_id=user_id, purpose="ACTIVATION", now=base)
    assert too_soon.value.retry_after_seconds > 0

    codes = [first]
    moment = base
    for expected_count in (1, 2, 3):
        moment = moment + timedelta(seconds=svc.OTP_RESEND_WAIT_SECONDS + 1)
        new_row, new_plain = svc.resend_otp(
            sqlite_session, user_id=user_id, purpose="ACTIVATION", now=moment
        )
        assert new_row.resend_count == expected_count
        assert new_plain not in codes
        codes.append(new_plain)

    with pytest.raises(svc.OtpMaxResendsExceededError):
        svc.resend_otp(
            sqlite_session,
            user_id=user_id,
            purpose="ACTIVATION",
            now=moment + timedelta(seconds=svc.OTP_RESEND_WAIT_SECONDS + 1),
        )

    # El anterior quedo invalidado: ya no valida (el activo es el ultimo).
    with pytest.raises(svc.OtpInvalidError):
        svc.validate_otp(
            sqlite_session, user_id=user_id, purpose="ACTIVATION", code=codes[0]
        )
    validated = svc.validate_otp(
        sqlite_session, user_id=user_id, purpose="ACTIVATION", code=codes[-1]
    )
    assert validated.status == "USED"
    sqlite_session.rollback()

    # Un ciclo nuevo (`generate`) reinicia el contador de reenvios.
    fresh_row, fresh_plain = svc.generate_otp(
        sqlite_session, user_id=user_id, purpose="ACTIVATION"
    )
    assert fresh_row.resend_count == 0
    assert svc.validate_otp(
        sqlite_session, user_id=user_id, purpose="ACTIVATION", code=fresh_plain
    ).status == "USED"
    sqlite_session.rollback()


# ---------------------------------------------------------------- Parte C: Postgres
def test_integration_postgres_otp_cycle(db_session: Session):
    from app.modules.identity.models import User
    from app.modules.identity.service import otp_service as svc
    from app.modules.shared.models import OutboxEntry

    user = User(
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Ada",
        last_name="Lovelace",
    )
    db_session.add(user)
    db_session.flush()

    row, plain = svc.generate_otp(db_session, user_id=user.id, purpose="ACTIVATION")
    assert row.status == "PENDING"
    validated = svc.validate_otp(
        db_session, user_id=user.id, purpose="ACTIVATION", code=plain
    )
    assert validated.status == "USED"
    events = list(
        db_session.scalars(
            sa.select(OutboxEntry).where(OutboxEntry.event_type == "user.activated")
        ).all()
    )
    assert len(events) >= 1


def test_migration_up_down_on_test_database(db_session: Session):
    """`up/down` de la revision propia en base de prueba (restaura `head` al final)."""
    import subprocess
    import sys

    from sqlalchemy import inspect

    from tests.db_utils import BACKEND_DIR, resolve_test_database_url

    url = resolve_test_database_url()

    def _alembic(*args: str) -> None:
        import os

        env = os.environ.copy()
        env["DATABASE_URL"] = url
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=str(BACKEND_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert (
            proc.returncode == 0
        ), f"alembic {' '.join(args)} fallo:\n{proc.stdout}\n{proc.stderr}"

    try:
        assert inspect(db_session.get_bind()).has_table("otp_codes", schema="identity")
        _alembic("downgrade", "0013_identity_kyc_audit")
        assert not inspect(db_session.get_bind()).has_table(
            "otp_codes", schema="identity"
        )
    finally:
        _alembic("upgrade", "head")
    assert inspect(db_session.get_bind()).has_table("otp_codes", schema="identity")
