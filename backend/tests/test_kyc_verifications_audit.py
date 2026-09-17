"""Persistencia KYC + auditoria (E1-T04, HU01 CA-02/CA-04).

- Parte A (sin BD): metadatos de los modelos y migracion segun
  `03b#4.4 kyc_verifications` y `03b#14.1 audit_log` (columnas exactas +
  `failure_reason` documentado), firma de `save_verification` y de la
  fachada `audit.record`, y reglas (sin `float`, sin `commit`, sin escribir
  `audit_log` fuera de la fachada, sin `update`/`delete` de auditoria, sin
  FK entre schemas, sin columnas ni material biometrico).
- Parte B (SQLite en memoria + schemas ATTACH): exito y fallo con motivo;
  intento con frame/base64 rechazado y nada persiste; `audit_log` recibe
  ambas filas con cadena de hash valida (cada hash referencia al anterior);
  `audit` no expone `update`/`delete`; fallo sin motivo y exito con motivo
  rechazados.
- Parte C (Postgres `db_session`): humo de integracion + migracion
  `up/down` de la revision propia en base de prueba; se omite sin BD.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0013_identity_kyc_audit.py"
)
IDENTITY_MODELS_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "models" / "__init__.py"
)
KYC_REPO_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "repository"
    / "kyc_verifications.py"
)
AUDIT_MODELS_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "audit" / "models" / "__init__.py"
)
AUDIT_SERVICE_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "audit" / "service" / "__init__.py"
)
AUDIT_REPO_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "audit"
    / "repository"
    / "__init__.py"
)

KYC_COLUMNS = {
    "id",
    "user_id",
    "provider",
    "overall_result",
    "document_json",
    "liveness_json",
    "face_match_json",
    "challenge_token_hash",
    "failure_reason",
    "created_at",
}

AUDIT_COLUMNS = {
    "id",
    "seq",
    "actor_type",
    "actor_id",
    "action",
    "entity_type",
    "entity_id",
    "before_json",
    "after_json",
    "ip",
    "device_id",
    "request_id",
    "created_at",
    "prev_hash",
    "hash",
}


def _ok_payload() -> dict:
    return {
        "document_json": {"number": "****5678", "valid": True, "issuer": "RENIEC"},
        "liveness_json": {"passed": True, "score": 0.97},
        "face_match_json": {"distance": 0.31, "threshold": 0.68, "confidence": 0.94},
        "challenge_token_hash": hashlib.sha256(b"challenge-token").hexdigest(),
    }


# ---------------------------------------------------------------- Parte A: modelo
def test_tables_registered_with_schema_columns_constraints():
    import app.modules.audit.models as _a  # noqa: F401 (registro)
    import app.modules.identity.models as _i  # noqa: F401 (registro)

    kyc = Base.metadata.tables["identity.kyc_verifications"]
    assert kyc.schema == "identity"
    assert {c.name for c in kyc.columns} == KYC_COLUMNS, (
        f"columnas 03b#4.4 + failure_reason, recibido: {sorted(c.name for c in kyc.columns)}"
    )
    assert isinstance(kyc.columns["provider"].type, sa.String)
    assert kyc.columns["provider"].type.length == 50
    assert isinstance(kyc.columns["overall_result"].type, sa.Boolean)
    assert isinstance(kyc.columns["challenge_token_hash"].type, sa.String)
    assert kyc.columns["challenge_token_hash"].type.length == 128
    for fk in kyc.foreign_keys:
        assert fk.column.table.schema == "identity", (
            f"FK fuera del schema propio: identity.kyc_verifications -> "
            f"{fk.column.table.schema}.{fk.column.table.name}"
        )

    audit = Base.metadata.tables["audit.audit_log"]
    assert audit.schema == "audit"
    assert {c.name for c in audit.columns} == AUDIT_COLUMNS, (
        f"columnas 03b#14.1, recibido: {sorted(c.name for c in audit.columns)}"
    )
    assert isinstance(audit.columns["hash"].type, sa.String)
    assert audit.columns["hash"].type.length == 64
    uq_cols = [
        {c.name for c in con.columns}
        for con in audit.constraints
        if isinstance(con, sa.UniqueConstraint)
    ]
    assert {"seq"} in uq_cols, "seq debe ser UQ"
    checks = " ".join(
        str(c.sqltext) for c in audit.constraints if isinstance(c, sa.CheckConstraint)
    )
    assert "actor_type" in checks
    assert len(audit.foreign_keys) == 0, "audit_log no lleva FK fisicas (UUID logicos)"


def test_no_biometric_columns_or_material():
    import app.modules.audit.models as _a  # noqa: F401 (registro)
    import app.modules.identity.models as _i  # noqa: F401 (registro)

    for key in ("identity.kyc_verifications", "audit.audit_log"):
        for col in Base.metadata.tables[key].columns:
            lowered = col.name.lower()
            assert "frame" not in lowered and "image" not in lowered, (
                f"columna biometrica prohibida: {key}.{col.name}"
            )
    for path in (KYC_REPO_PATH, AUDIT_SERVICE_PATH, AUDIT_REPO_PATH):
        content = path.read_text(encoding="utf-8")
        assert "float(" not in content
        assert ".commit(" not in content, f"flush sin commit en {path.name}"
    kyc_content = KYC_REPO_PATH.read_text(encoding="utf-8")
    for forbidden in ("face_image", "frame_base64", "data:image"):
        assert forbidden not in kyc_content or "prohibido" in kyc_content.lower(), (
            f"material biometrico fuera de la guardia: {forbidden}"
        )
    assert "import base64" not in kyc_content and "b64decode" not in kyc_content, (
        "jamas decodificar/guardar frames base64"
    )


def test_save_verification_signature_and_audit_facade():
    from app.modules.audit import service as audit_svc
    from app.modules.identity.repository import kyc_verifications as kyc_repo

    params = list(inspect.signature(kyc_repo.save_verification).parameters)
    assert params[0] == "session", f"primer parametro debe ser session: {params}"
    for expected in (
        "user_id",
        "overall_result",
        "document_json",
        "liveness_json",
        "face_match_json",
        "challenge_token_hash",
        "failure_reason",
    ):
        assert expected in params, f"save_verification sin {expected}: {params}"

    record_params = list(inspect.signature(audit_svc.record).parameters)
    assert record_params[0] == "session"
    for expected in ("actor", "action", "entity", "entity_id", "metadata"):
        assert expected in record_params, f"record sin {expected}: {record_params}"

    kyc_content = KYC_REPO_PATH.read_text(encoding="utf-8")
    assert "audit" in kyc_content and "record" in kyc_content, (
        "identity llama a audit via fachada record (misma sesion)"
    )
    assert "AuditLog" not in kyc_content, "identity no toca el modelo AuditLog"
    assert "audit.models" not in kyc_content and "audit_log\"" not in kyc_content, (
        "identity no escribe audit_log directamente"
    )


def test_audit_append_only_without_update_delete():
    from app.modules.audit import repository as audit_repo
    from app.modules.audit import service as audit_svc

    for module in (audit_repo, audit_svc):
        for forbidden in ("update", "delete", "remove", "purge"):
            assert not hasattr(module, forbidden), (
                f"audit append-only: {module.__name__} no debe exponer {forbidden}"
            )
    for path in (AUDIT_SERVICE_PATH, AUDIT_REPO_PATH):
        content = path.read_text(encoding="utf-8")
        assert "def update" not in content and "def delete" not in content, (
            f"sin update/delete en {path.name}"
        )


def test_audit_hash_is_deterministic_and_chained():
    from app.modules.audit.service import compute_audit_hash, verify_chain

    actor = uuid.uuid4()
    entity = uuid.uuid4()
    first = compute_audit_hash(
        actor_type="USER",
        actor_id=actor,
        action="kyc.verified",
        entity_type="kyc_verification",
        entity_id=entity,
        after_json={"overall_result": True},
        prev_hash=None,
    )
    assert len(first) == 64
    assert first == compute_audit_hash(
        actor_type="USER",
        actor_id=actor,
        action="kyc.verified",
        entity_type="kyc_verification",
        entity_id=entity,
        after_json={"overall_result": True},
        prev_hash=None,
    )
    second = compute_audit_hash(
        actor_type="USER",
        actor_id=actor,
        action="kyc.failed",
        entity_type="kyc_verification",
        entity_id=entity,
        after_json={"overall_result": False},
        prev_hash=first,
    )
    assert second != first

    from app.modules.audit.models import AuditLog

    genesis = AuditLog(
        seq=1, actor_type="USER", actor_id=actor, action="kyc.verified",
        entity_type="kyc_verification", entity_id=entity,
        after_json={"overall_result": True}, prev_hash=None, hash=first,
    )
    chained = AuditLog(
        seq=2, actor_type="USER", actor_id=actor, action="kyc.failed",
        entity_type="kyc_verification", entity_id=entity,
        after_json={"overall_result": False}, prev_hash=first, hash=second,
    )
    assert verify_chain([genesis, chained]) is True
    broken = AuditLog(
        seq=2, actor_type="USER", actor_id=actor, action="kyc.failed",
        entity_type="kyc_verification", entity_id=entity,
        after_json={"overall_result": False}, prev_hash="0" * 64, hash=second,
    )
    assert verify_chain([genesis, broken]) is False


def test_migration_0013_exists_and_matches_models():
    assert MIGRATION_PATH.exists(), "falta migracion 0013_identity_kyc_audit.py"
    content = MIGRATION_PATH.read_text(encoding="utf-8")
    for token in (
        'revision = "0013_identity_kyc_audit"',
        'down_revision = "0012_identity_core"',
        '"kyc_verifications"',
        '"audit_log"',
        "fk_kyc_verifications_user",
        "uq_audit_log_seq",
        "ck_audit_log_actor_type",
        "prev_hash",
        "failure_reason",
        "ix_kyc_verifications_user",
        "ix_audit_log_entity",
        "def upgrade",
        "def downgrade",
    ):
        assert token in content, f"migracion sin {token}"
    for other in (
        'schema="accounts"',
        'schema="ledger"',
        'schema="transactions"',
        "schema='accounts'",
        "schema='ledger'",
        '"users"',
        '"credentials"',
        "ledger_accounts",
    ):
        assert other not in content, f"la migracion no debe tocar otro objeto: {other}"


# ---------------------------------------------------------------- Parte B: SQLite
@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schemas `identity`/`audit` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.audit.models as _a  # noqa: F401
    import app.modules.identity.models as _i  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("identity", "audit"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["identity.users"],
            Base.metadata.tables["identity.kyc_verifications"],
            Base.metadata.tables["audit.audit_log"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _audit_rows(session: Session) -> list:
    from app.modules.audit.models import AuditLog

    stmt = sa.select(AuditLog).order_by(AuditLog.seq.asc())
    return list(session.scalars(stmt).all())


def test_save_success_and_failure_with_reason(sqlite_session: Session):
    from app.modules.audit.service import verify_chain
    from app.modules.identity.repository import save_verification

    user_id = uuid.uuid4()
    ok_payload = _ok_payload()
    ok_row = save_verification(sqlite_session, user_id=user_id, overall_result=True, **ok_payload)
    assert ok_row.id is not None
    assert ok_row.overall_result is True
    assert ok_row.failure_reason is None
    assert ok_row.provider == "facial-kyc-service"

    fail_row = save_verification(
        sqlite_session,
        user_id=user_id,
        overall_result=False,
        document_json={"number": "****5678", "valid": False, "reason": "expired"},
        liveness_json={"passed": False, "score": 0.12},
        face_match_json=None,
        challenge_token_hash=None,
        failure_reason="documento vencido",
    )
    assert fail_row.overall_result is False
    assert fail_row.failure_reason == "documento vencido"

    rows = _audit_rows(sqlite_session)
    assert len(rows) == 2
    assert rows[0].action == "kyc.verified"
    assert rows[1].action == "kyc.failed"
    for row, verification in zip(rows, (ok_row, fail_row)):
        assert row.entity_type == "kyc_verification"
        assert row.entity_id == verification.id
        assert row.actor_id == user_id
        assert row.actor_type == "USER"
    assert rows[0].prev_hash is None
    assert rows[1].prev_hash == rows[0].hash
    assert verify_chain(rows) is True


def test_biometric_frame_is_rejected_and_nothing_persists(sqlite_session: Session):
    from app.modules.identity.models import KycVerification
    from app.modules.identity.repository import save_verification

    user_id = uuid.uuid4()
    frame = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+P+/HgAFhAJ/wlX1BwAAAABJRU5ErkJggg=="
    with pytest.raises(ValueError, match="biometrico"):
        save_verification(
            sqlite_session,
            user_id=user_id,
            overall_result=True,
            document_json={"valid": True, "frame_base64": frame},
        )
    with pytest.raises(ValueError, match="biometrico"):
        save_verification(
            sqlite_session,
            user_id=user_id,
            overall_result=True,
            liveness_json={"passed": True, "snapshot": "data:image/jpeg;base64,/9j/4AAQ"},
        )
    with pytest.raises(ValueError, match="biometrico"):
        save_verification(
            sqlite_session,
            user_id=user_id,
            overall_result=True,
            face_match_json={"confidence": 0.9, "face_image": frame},
            challenge_token_hash="data:image/png;base64," + frame,
        )
    count = sqlite_session.scalar(sa.select(sa.func.count()).select_from(KycVerification))
    assert count == 0
    assert _audit_rows(sqlite_session) == []
    sqlite_session.rollback()


def test_failure_without_reason_and_success_with_reason_rejected(sqlite_session: Session):
    from app.modules.identity.repository import save_verification

    user_id = uuid.uuid4()
    with pytest.raises(ValueError, match="failure_reason es obligatorio"):
        save_verification(sqlite_session, user_id=user_id, overall_result=False)
    with pytest.raises(ValueError, match="failure_reason solo aplica"):
        save_verification(
            sqlite_session,
            user_id=user_id,
            overall_result=True,
            failure_reason="motivo sobrante",
            **_ok_payload(),
        )
    sqlite_session.rollback()


def test_audit_chain_links_every_consecutive_hash(sqlite_session: Session):
    from app.modules.audit.service import record, verify_chain
    from app.modules.identity.repository import save_verification

    user_id = uuid.uuid4()
    for _ in range(3):
        save_verification(sqlite_session, user_id=user_id, overall_result=True, **_ok_payload())
    record(
        sqlite_session,
        actor=None,
        action="kyc.reviewed",
        entity="kyc_verification",
        entity_id=uuid.uuid4(),
        metadata={"note": "revision manual"},
    )
    rows = _audit_rows(sqlite_session)
    assert len(rows) == 4
    assert [row.seq for row in rows] == [1, 2, 3, 4]
    assert rows[3].actor_type == "SYSTEM"
    for previous, current in zip(rows, rows[1:]):
        assert current.prev_hash == previous.hash
    assert verify_chain(rows) is True


def test_audit_has_no_update_or_delete_api(sqlite_session: Session):
    from app.modules.audit import repository as audit_repo
    from app.modules.audit import service as audit_svc

    assert audit_repo.latest_hash(sqlite_session) is None
    assert audit_repo.latest_seq(sqlite_session) == 0
    for module in (audit_repo, audit_svc):
        for forbidden in ("update", "delete"):
            assert not hasattr(module, forbidden)


# ---------------------------------------------------------------- Parte C: Postgres
def test_integration_postgres_kyc_and_audit(db_session: Session):
    from app.modules.audit.service import verify_chain
    from app.modules.identity.repository import get_verification, save_verification

    # `user_id=None` (admitido por `save_verification` y `03b#4.4`: null si aun
    # no existe usuario): evita la FK intra-schema `fk_kyc_verifications_user`
    # contra `identity.users` sin crear filas ajenas al test.
    user_id = None
    ok_row = save_verification(db_session, user_id=user_id, overall_result=True, **_ok_payload())
    fail_row = save_verification(
        db_session,
        user_id=user_id,
        overall_result=False,
        document_json={"valid": False},
        failure_reason="rostro no coincide",
    )
    assert get_verification(db_session, ok_row.id) is not None
    assert get_verification(db_session, fail_row.id).failure_reason == "rostro no coincide"

    rows = _audit_rows(db_session)
    assert len(rows) >= 2
    assert verify_chain(rows) is True


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
        assert inspect(db_session.get_bind()).has_table("kyc_verifications", schema="identity")
        assert inspect(db_session.get_bind()).has_table("audit_log", schema="audit")
        _alembic("downgrade", "0012_identity_core")
        assert not inspect(db_session.get_bind()).has_table(
            "kyc_verifications", schema="identity"
        )
        assert not inspect(db_session.get_bind()).has_table("audit_log", schema="audit")
    finally:
        _alembic("upgrade", "head")
    assert inspect(db_session.get_bind()).has_table("kyc_verifications", schema="identity")
    assert inspect(db_session.get_bind()).has_table("audit_log", schema="audit")
