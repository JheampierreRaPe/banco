"""Caso de uso: alta de cliente `onboard_customer` (E1-T03, HU01 CA-03).

- Parte A (sin BD): metadatos de los modelos y migracion segun `03b#4`
  (`users`, `credentials`), firma del caso de uso y reglas (sin `float`,
  sin `commit`, sin publicar directo, sin FK entre schemas, fachadas por
  import perezoso, sin frames/PII).
- Parte B (SQLite en memoria + schemas ATTACH): alta exitosa
  (usuario+credencial+cuenta+subcuentas+outbox en una sesion); fallo de
  ledger (fachada que lanza) -> rollback total (sin usuario residual);
  documento duplicado rechazado; `overall_result=false` no crea nada.
- Parte C (Postgres `db_session`): humo de integracion + migracion
  `up/down` de la revision propia en base de prueba; se omite sin BD.
"""

from __future__ import annotations

import inspect
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0012_identity_core.py"
)
MODELS_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "models" / "__init__.py"
)
REPO_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "repository"
    / "__init__.py"
)
SERVICE_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "service" / "__init__.py"
)


def _kyc_result(doc_hash: str, overall: bool = True) -> dict:
    return {"overall_result": overall, "doc_number_hash": doc_hash}


def _customer_kwargs(doc_hash: str, **overrides) -> dict:
    params = {
        "kyc_result": _kyc_result(doc_hash),
        "first_name": "Ana",
        "last_name": "Quispe",
        "doc_type": "DNI",
        "doc_number_masked": "****5678",
        "email": f"ana.{uuid.uuid4().hex[:8]}@example.com",
        "phone": "999888777",
    }
    params.update(overrides)
    return params


# ---------------------------------------------------------------- Parte A: modelo
def test_tables_registered_with_schema_columns_constraints():
    import app.modules.identity.models as m  # noqa: F401 (registro)

    assert "identity.users" in Base.metadata.tables
    assert "identity.credentials" in Base.metadata.tables

    users = Base.metadata.tables["identity.users"]
    assert users.schema == "identity"
    cols = {c.name for c in users.columns}
    assert cols == {
        "id",
        "doc_type",
        "doc_number_hash",
        "doc_number_masked",
        "first_name",
        "last_name",
        "birth_date",
        "email",
        "phone",
        "status",
        "kyc_status",
        "risk_profile",
        "created_at",
        "updated_at",
    }, f"columnas 03b#4.1, recibido: {sorted(cols)}"
    assert isinstance(users.columns["doc_number_hash"].type, sa.String)
    uq_cols = [
        {c.name for c in con.columns}
        for con in users.constraints
        if isinstance(con, sa.UniqueConstraint)
    ]
    assert {"doc_number_hash"} in uq_cols, "doc_number_hash debe ser UQ"
    assert {"email"} in uq_cols, "email debe ser UQ"
    checks = " ".join(
        str(c.sqltext) for c in users.constraints if isinstance(c, sa.CheckConstraint)
    )
    assert "doc_type" in checks
    assert "PENDING_ACTIVATION" in checks

    credentials = Base.metadata.tables["identity.credentials"]
    assert credentials.schema == "identity"
    ccols = {c.name for c in credentials.columns}
    assert ccols == {
        "user_id",
        "password_hash",
        "pin_hash",
        "biometric_enabled",
        "failed_attempts",
        "locked_until",
        "password_updated_at",
        "updated_at",
    }, f"columnas 03b#4.2, recibido: {sorted(ccols)}"
    pk = {c.name for c in credentials.primary_key.columns}
    assert pk == {"user_id"}, "PK debe ser user_id (1:1)"
    cchecks = " ".join(
        str(c.sqltext) for c in credentials.constraints if isinstance(c, sa.CheckConstraint)
    )
    assert "failed_attempts >= 0" in cchecks


def test_no_foreign_keys_to_other_schemas():
    import app.modules.identity.models as m  # noqa: F401 (registro)

    for key in ("identity.users", "identity.credentials"):
        table = Base.metadata.tables[key]
        for fk in table.foreign_keys:
            target = fk.column.table
            assert (
                target.schema == "identity"
            ), f"FK fuera del schema propio: {key} -> {target.schema}.{target.name}"


def test_migration_0012_exists_and_matches_models():
    assert MIGRATION_PATH.exists(), "falta migracion 0012_identity_core.py"
    content = MIGRATION_PATH.read_text(encoding="utf-8")
    for token in (
        'revision = "0012_identity_core"',
        'down_revision = "0011_notifications"',
        '"users"',
        '"credentials"',
        "uq_users_doc_number_hash",
        "uq_users_email",
        "ck_users_doc_type",
        "ck_users_status",
        "fk_credentials_user",
        "ck_credentials_failed_attempts_min",
        "ix_users_status",
        "def upgrade",
        "def downgrade",
    ):
        assert token in content, f"migracion sin {token}"
    for other in (
        'schema="accounts"',
        'schema="ledger"',
        "schema='accounts'",
        "schema='ledger'",
        "ledger_accounts",
        "account_balances",
    ):
        assert other not in content, f"la migracion no debe tocar otro schema: {other}"


def test_service_signature_and_rules():
    from app.modules.identity import service as svc

    params = list(inspect.signature(svc.onboard_customer).parameters)
    assert params[0] == "session", f"primer parametro debe ser session: {params}"
    assert "kyc_result" in params

    for path in (MODELS_PATH, REPO_PATH, SERVICE_PATH):
        content = path.read_text(encoding="utf-8")
        assert "float(" not in content
        assert ".commit(" not in content, f"flush sin commit en {path.name}"
    repo_content = REPO_PATH.read_text(encoding="utf-8")
    assert "publish(" not in repo_content, "nada de publish en el repositorio"
    svc_content = SERVICE_PATH.read_text(encoding="utf-8")
    assert "outbox" in svc_content and "record" in svc_content, "evento solo via outbox.record"
    for forbidden in ("base64", "face_image", "frame_base64"):
        assert forbidden not in svc_content.lower(), f"nada de material biometrico: {forbidden}"
    import app.modules.identity.models as _m  # noqa: F401 (registro)

    for key in ("identity.users", "identity.credentials"):
        for col in Base.metadata.tables[key].columns:
            assert (
                "frame" not in col.name and "image" not in col.name
            ), f"columna biometrica prohibida: {key}.{col.name}"
    # Fachadas por import perezoso (sin ciclo a nivel top).
    assert "ensure_customer_accounts" in svc_content
    assert "create_account" in svc_content
    top_imports = [
        line
        for line in svc_content.splitlines()
        if line.startswith(("from app.modules.", "import app.modules.", "from app.core"))
    ]
    assert all(
        line.startswith(("from app.modules.identity.", "from app.modules.identity "))
        for line in top_imports
    ), f"imports top solo del propio modulo: {top_imports}"
    assert (
        "except " not in svc_content or "raise" in svc_content
    ), "sin capturas que oculten el rollback"


# ---------------------------------------------------------------- Parte B: SQLite
@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schemas `identity`/`accounts`/`ledger`/`shared` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401
    import app.modules.identity.models as _i  # noqa: F401
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.notifications.models as _n  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("identity", "accounts", "ledger", "shared", "notifications"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["identity.users"],
            Base.metadata.tables["identity.credentials"],
            Base.metadata.tables["identity.otp_codes"],
            Base.metadata.tables["accounts.accounts"],
            Base.metadata.tables["accounts.account_balances"],
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["notifications.notifications"],
            Base.metadata.tables["notifications.notification_templates"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _pending_outbox(session: Session, event_type: str) -> list:
    stmt = sa.select(Base.metadata.tables["shared.outbox"]).where(
        Base.metadata.tables["shared.outbox"].c.event_type == event_type
    )
    return list(session.execute(stmt).all())


def test_successful_onboarding_creates_all_in_one_session(sqlite_session: Session):
    from app.modules.identity import repository as repo
    from app.modules.identity.service import onboard_customer

    doc_hash = f"hash-{uuid.uuid4().hex}"
    result = onboard_customer(sqlite_session, **_customer_kwargs(doc_hash))

    assert result["status"] == "ONBOARDED"
    user = repo.get_by_doc_hash(sqlite_session, doc_hash)
    assert user is not None
    assert user.status == "PENDING_ACTIVATION"
    assert result["user_id"] == user.id

    credential = repo.get_credential(sqlite_session, user.id)
    assert credential is not None

    from app.modules.accounts import repository as accounts_repo

    account = accounts_repo.get_account(sqlite_session, result["account_id"])
    assert account is not None
    assert account.user_id == user.id
    assert account.ledger_account_id is not None
    assert account.ledger_hold_account_id is not None
    balance = accounts_repo.get_balance(sqlite_session, account.id)
    assert (balance.available_minor, balance.held_minor) == (0, 0)

    from app.modules.ledger import repository as ledger_repo

    codes = {a.code for a in ledger_repo.get_by_owner(sqlite_session, account.id)}
    assert codes == {f"2000-{account.id}", f"2100-{account.id}"}

    rows = _pending_outbox(sqlite_session, "kyc.completed")
    assert len(rows) == 1
    assert rows[0].status == "PENDING"


def test_ledger_failure_rolls_back_everything(sqlite_session: Session):
    from app.modules.identity import repository as repo
    from app.modules.identity.service import onboard_customer
    from app.modules.ledger import service as ledger_service

    doc_hash = f"hash-{uuid.uuid4().hex}"
    original = ledger_service.ensure_customer_accounts

    def _boom(session, *args, **kwargs):
        raise RuntimeError("ledger caido")

    ledger_service.ensure_customer_accounts = _boom
    try:
        with pytest.raises(RuntimeError, match="ledger caido"):
            onboard_customer(sqlite_session, **_customer_kwargs(doc_hash))
    finally:
        ledger_service.ensure_customer_accounts = original
    sqlite_session.rollback()

    assert repo.get_by_doc_hash(sqlite_session, doc_hash) is None
    count = sqlite_session.scalar(
        sa.select(sa.func.count()).select_from(Base.metadata.tables["identity.users"])
    )
    assert count == 0
    assert _pending_outbox(sqlite_session, "kyc.completed") == []


def test_duplicate_document_rejected(sqlite_session: Session):
    from app.modules.identity.service import onboard_customer

    doc_hash = f"hash-{uuid.uuid4().hex}"
    onboard_customer(sqlite_session, **_customer_kwargs(doc_hash))
    sqlite_session.commit()  # el llamante confirma; el duplicado debe fallar despues
    with pytest.raises(ValueError, match="documento ya registrado|duplicado"):
        onboard_customer(sqlite_session, **_customer_kwargs(doc_hash))
    sqlite_session.rollback()

    count = sqlite_session.scalar(
        sa.select(sa.func.count()).select_from(Base.metadata.tables["identity.users"])
    )
    assert count == 1


def test_overall_false_creates_nothing(sqlite_session: Session):
    from app.modules.identity import repository as repo
    from app.modules.identity.service import onboard_customer

    doc_hash = f"hash-{uuid.uuid4().hex}"
    kwargs = _customer_kwargs(doc_hash)
    kwargs["kyc_result"] = _kyc_result(doc_hash, overall=False)
    result = onboard_customer(sqlite_session, **kwargs)

    assert result["status"] == "REJECTED"
    assert repo.get_by_doc_hash(sqlite_session, doc_hash) is None
    count = sqlite_session.scalar(
        sa.select(sa.func.count()).select_from(Base.metadata.tables["identity.users"])
    )
    assert count == 0
    assert _pending_outbox(sqlite_session, "kyc.completed") == []


def test_onboard_emits_initial_otp_end_to_end_activate_ok(sqlite_session: Session):
    """B1 (fase 5): alta -> OTP `PENDING` emitido -> `POST /auth/activate` -> `ACTIVE`.

    E2E por API publica salvo la lectura del codigo en BD de prueba (el
    codigo en claro solo viaja en la notificacion `otp_code`; se lee del
    `payload` persistido). Sin llamadas directas a `otp_service` entre el
    alta y la activacion: el OTP lo emite `onboard_customer`.
    """
    from fastapi.testclient import TestClient

    from app.core.db import get_db
    from app.main import app
    from app.modules.identity import repository as repo
    from app.modules.identity.service import onboard_customer
    from app.modules.notifications.models import Notification

    doc_hash = f"hash-{uuid.uuid4().hex}"
    result = onboard_customer(sqlite_session, **_customer_kwargs(doc_hash))

    assert result["status"] == "ONBOARDED"
    assert set(result) == {"status", "user_id", "account_id", "event_type"}

    user = repo.get_by_doc_hash(sqlite_session, doc_hash)
    assert user is not None and user.status == "PENDING_ACTIVATION"

    pending = repo.get_active_otp(sqlite_session, user.id, "ACTIVATION")
    assert pending is not None and pending.status == "PENDING"

    note = sqlite_session.scalars(
        sa.select(Notification).where(Notification.user_id == user.id)
    ).first()
    assert note is not None and note.template_code == "otp_code"
    plain = note.payload_json["data"]["code"]
    assert isinstance(plain, str) and len(plain) == 6 and plain.isdigit()

    def _override():
        yield sqlite_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/auth/activate",
                json={"user_ref": str(user.id), "code": plain},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    assert resp.json()["data"] == {"user_id": str(user.id), "status": "ACTIVE"}
    assert plain not in resp.text

    sqlite_session.expire_all()
    assert repo.get_user(sqlite_session, user.id).status == "ACTIVE"


# ---------------------------------------------------------------- Parte C: Postgres
def test_integration_postgres_onboarding(db_session: Session):
    from app.modules.identity import repository as repo
    from app.modules.identity.service import onboard_customer

    doc_hash = f"hash-{uuid.uuid4().hex}"
    result = onboard_customer(
        db_session,
        **_customer_kwargs(doc_hash, initial_pin_hash="argon2:pin-inicial"),
    )
    assert result["status"] == "ONBOARDED"
    user = repo.get_by_doc_hash(db_session, doc_hash)
    assert user is not None and user.status == "PENDING_ACTIVATION"
    assert repo.get_credential(db_session, user.id).pin_hash == "argon2:pin-inicial"


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
        assert inspect(db_session.get_bind()).has_table("users", schema="identity")
        _alembic("downgrade", "0011_notifications")
        assert not inspect(db_session.get_bind()).has_table("users", schema="identity")
        assert not inspect(db_session.get_bind()).has_table("credentials", schema="identity")
    finally:
        _alembic("upgrade", "head")
    assert inspect(db_session.get_bind()).has_table("users", schema="identity")
    assert inspect(db_session.get_bind()).has_table("credentials", schema="identity")
