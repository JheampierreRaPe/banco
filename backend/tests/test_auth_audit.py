"""Auditoria de login e intentos fallidos (E1-T17, HU03 CA-03 + base HU23).

- Sin Postgres: SQLite en memoria con schemas ATTACH
  (`identity`/`shared`/`audit`/`notifications`), patron de
  `tests/test_device_login.py` + `tests/test_pin_login.py`.
- Decision (documentada en cada servicio): best-effort como E1-T03. La
  auditoria va via fachada `audit.record` en la misma sesion (`flush` sin
  `commit`); si `audit` falla se loguea y el login igual se retorna (la
  auditoria es observabilidad: nunca debe romper la disponibilidad del
  login; coherente con `docs/modules/README.md#audit`: prohibido bloquear
  el flujo).
- Sin PII en auditoria: solo IDs (`user_id`, `session_id`, `device_id`,
  `ip`, `at`, `method`, `reason`); jamas PIN/firmas/nonces/tokens.
- Casos: facial exitoso -> `auth.login_succeeded` en cadena valida;
  facial fallido -> `auth.failed_attempt` sin PII; PIN exitoso/fallido
  (sin PIN ni su hash en la fila); refresh exitoso/fallido; cadena valida
  tras N eventos (`verify_chain` existente de `audit.service`); best-effort:
  si `audit.record` revienta, el login igual retorna.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

DEVICE_SERVICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "service"
    / "device_login.py"
)
PIN_SERVICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "service"
    / "pin_login.py"
)
SESSIONS_SERVICE_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "service" / "sessions.py"
)

PIN = "482917"
WRONG_PIN = "000000"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sign_hmac(secret_hex: str, nonce: str) -> str:
    secret = bytes.fromhex(secret_hex)
    return hmac.new(secret, nonce.encode("utf-8"), hashlib.sha256).hexdigest()


@pytest.fixture()
def audit_session():
    """Sesion SQLite aislada (`identity`/`shared`/`audit`/`notifications`)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.audit.models as _a  # noqa: F401 (registro)
    import app.modules.identity.models as _i  # noqa: F401 (registro)
    import app.modules.notifications.models as _n  # noqa: F401 (registro)
    import app.modules.shared.models as _s  # noqa: F401 (registro)

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("identity", "shared", "audit", "notifications"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["identity.users"],
            Base.metadata.tables["identity.credentials"],
            Base.metadata.tables["identity.device_bindings"],
            Base.metadata.tables["identity.sessions"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["audit.audit_log"],
            Base.metadata.tables["notifications.notifications"],
            Base.metadata.tables["notifications.notification_templates"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        from app.modules.identity.domain import nonce as nonce_domain

        nonce_domain.reset_nonces()
        yield session
    finally:
        try:
            from app.modules.identity.domain import nonce as nonce_domain

            nonce_domain.reset_nonces()
        finally:
            session.close()
            engine.dispose()


def _make_hmac_user(session: Session, *, device_id: str = "pixel-8-pro"):
    from app.modules.identity import repository as identity_repo

    suffix = uuid.uuid4().hex[:8]
    user = identity_repo.create_user(
        session,
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Ada",
        last_name="Lovelace",
        email=f"ada.{suffix}@example.com",
        phone="+51999888777",
    )
    secret_hex = secrets.token_hex(24)
    identity_repo.register_binding(
        session,
        user.id,
        device_id,
        f"hmac:{secret_hex}",
        platform="android",
        biometric_type="FACE",
    )
    session.commit()
    return user, secret_hex


def _make_pin_user(session: Session, *, pin: str = PIN):
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import pin_login as pin_login_service

    suffix = uuid.uuid4().hex[:8]
    user = identity_repo.create_user(
        session,
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Ada",
        last_name="Lovelace",
        email=f"ada.{suffix}@example.com",
        phone="+51999888777",
    )
    identity_repo.create_credential(session, user.id, pin_hash=pin_login_service.hash_pin(pin))
    session.commit()
    return user


def _audit_rows(session: Session) -> list:
    from app.modules.audit.models import AuditLog

    stmt = sa.select(AuditLog).order_by(AuditLog.seq.asc())
    return list(session.scalars(stmt).all())


def _audit_dump(rows: list) -> str:
    blob = []
    for row in rows:
        blob.append(
            json.dumps(
                {
                    "action": row.action,
                    "entity_type": row.entity_type,
                    "actor_id": str(row.actor_id),
                    "entity_id": str(row.entity_id),
                    "device_id": row.device_id,
                    "ip": row.ip,
                    "after": row.after_json,
                    "before": row.before_json,
                    "request_id": row.request_id,
                },
                sort_keys=True,
                default=str,
            )
        )
    return "\n".join(blob)


# ---------------------------------------------------------------- Reglas estaticas
def test_static_rules_audit_via_facade_no_direct_write_no_commit():
    for path in (DEVICE_SERVICE_PATH, PIN_SERVICE_PATH, SESSIONS_SERVICE_PATH):
        content = path.read_text(encoding="utf-8")
        assert ".commit(" not in content, f"flush sin commit en {path.name}"
        assert "AuditLog" not in content, f"{path.name} no toca el modelo AuditLog"
        assert "audit.models" not in content, f"{path.name} no importa audit.models"
        assert (
            "audit.service" in content and "record" in content
        ), f"{path.name} debe llamar a la fachada audit.record"
        assert "auth.failed_attempt" in content, f"{path.name} sin failed_attempt"
        assert (
            "auth.login_succeeded" in content or "LOGIN_SUCCEEDED" in content
        ), f"{path.name} sin login_succeeded"


# ---------------------------------------------------------------- Facial
def test_facial_success_audits_login_and_chain_valid(audit_session: Session):
    from app.modules.audit.service import verify_chain
    from app.modules.identity.service import device_login as device_login_service

    user, secret_hex = _make_hmac_user(audit_session)
    challenge = device_login_service.request_challenge(
        audit_session, user_ref=str(user.id), device_id="pixel-8-pro"
    )
    nonce = challenge["nonce"]
    out = device_login_service.login_with_device(
        audit_session,
        nonce=nonce,
        device_id="pixel-8-pro",
        signature=_sign_hmac(secret_hex, nonce),
        ip="10.0.0.1",
    )
    assert out["session_id"]

    rows = _audit_rows(audit_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "auth.login_succeeded"
    assert row.entity_type == "user"
    assert row.entity_id == user.id
    assert row.actor_id == user.id
    assert row.actor_type == "USER"
    assert row.device_id == "pixel-8-pro"
    assert row.ip == "10.0.0.1"
    assert row.after_json["method"] == "facial"
    assert row.after_json["session_id"] == out["session_id"]
    assert row.after_json["at"]
    assert len(row.hash) == 64
    assert verify_chain(rows) is True

    # La firma HMAC del nonce jamas se persiste en auditoria.
    assert _sign_hmac(secret_hex, nonce) not in _audit_dump(rows)
    assert nonce not in _audit_dump(rows)


def test_facial_failure_audits_without_pii(audit_session: Session):
    from app.modules.audit.service import verify_chain
    from app.modules.identity.service import device_login as device_login_service

    user, secret_hex = _make_hmac_user(audit_session)
    challenge = device_login_service.request_challenge(
        audit_session, user_ref=str(user.id), device_id="pixel-8-pro"
    )
    nonce = challenge["nonce"]
    bad_signature = "00" * 32
    with pytest.raises(device_login_service.LoginInvalidError):
        device_login_service.login_with_device(
            audit_session,
            nonce=nonce,
            device_id="pixel-8-pro",
            signature=bad_signature,
            ip="10.0.0.2",
        )

    rows = _audit_rows(audit_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "auth.failed_attempt"
    assert row.entity_id == user.id
    assert row.device_id == "pixel-8-pro"
    assert row.ip == "10.0.0.2"
    assert row.after_json["method"] == "facial"
    assert row.after_json["at"]
    assert verify_chain(rows) is True

    dump = _audit_dump(rows)
    assert bad_signature not in dump, "la firma no debe quedar en auditoria"
    assert nonce not in dump, "el nonce no debe quedar en auditoria"
    assert secret_hex not in dump


# ---------------------------------------------------------------- PIN
def test_pin_success_and_failure_audit_without_pii(audit_session: Session):
    from app.modules.audit.service import verify_chain
    from app.modules.identity.service import pin_login as pin_login_service

    user = _make_pin_user(audit_session)
    out = pin_login_service.login_with_pin(
        audit_session,
        user_ref=str(user.id),
        pin=PIN,
        device_id="dev-1",
        ip="10.0.0.3",
    )
    assert out["session_id"]

    with pytest.raises(pin_login_service.PinInvalidError):
        pin_login_service.login_with_pin(
            audit_session,
            user_ref=str(user.id),
            pin=WRONG_PIN,
            device_id="dev-1",
            ip="10.0.0.3",
        )

    rows = _audit_rows(audit_session)
    assert len(rows) == 2
    assert rows[0].action == "auth.login_succeeded"
    assert rows[0].after_json["method"] == "pin"
    assert rows[0].after_json["session_id"] == out["session_id"]
    assert rows[0].device_id == "dev-1"
    assert rows[0].ip == "10.0.0.3"
    assert rows[1].action == "auth.failed_attempt"
    assert rows[1].after_json["method"] == "pin"
    assert rows[1].entity_id == user.id
    assert verify_chain(rows) is True

    dump = _audit_dump(rows)
    assert PIN not in dump, "el PIN en claro jamas va a auditoria"
    assert "pbkdf2" not in dump, "ni el hash del PIN va a auditoria"
    for row in rows:
        assert row.after_json is not None
        assert set(row.after_json) <= {
            "method",
            "at",
            "session_id",
            "reason",
        }, f"metadata minima sin PII: {sorted(row.after_json)}"
        assert row.after_json.get("method") == "pin"


# ---------------------------------------------------------------- Refresh
def test_refresh_success_audits_login(audit_session: Session):
    import secrets as _secrets
    from datetime import timedelta

    from app.modules.audit.service import verify_chain
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import sessions as sessions_service

    user = _make_pin_user(audit_session)
    refresh = _secrets.token_urlsafe(32)
    identity_repo.create_session(
        audit_session,
        user.id,
        identity_repo.hash_refresh_token(refresh),
        _utcnow() + timedelta(seconds=3600),
        device_id="pixel-8-pro",
        ip="10.0.0.4",
    )
    audit_session.commit()

    out = sessions_service.refresh_session(audit_session, refresh_token=refresh)
    assert out["session_id"]

    rows = _audit_rows(audit_session)
    assert len(rows) == 1
    assert rows[0].action == "auth.login_succeeded"
    assert rows[0].after_json["method"] == "refresh"
    assert rows[0].after_json["session_id"] == out["session_id"]
    assert rows[0].device_id == "pixel-8-pro"
    assert rows[0].ip == "10.0.0.4"
    assert rows[0].entity_id == user.id
    assert verify_chain(rows) is True
    dump = _audit_dump(rows)
    assert refresh not in dump, "el refresh en claro jamas va a auditoria"


def test_refresh_failure_audits_without_pii(audit_session: Session):
    import secrets as _secrets

    from app.modules.audit.service import verify_chain
    from app.modules.identity.service import sessions as sessions_service

    _make_pin_user(audit_session)
    unknown = _secrets.token_urlsafe(32)
    with pytest.raises(sessions_service.RefreshInvalidError):
        sessions_service.refresh_session(audit_session, refresh_token=unknown)

    rows = _audit_rows(audit_session)
    assert len(rows) == 1
    assert rows[0].action == "auth.failed_attempt"
    assert rows[0].after_json["method"] == "refresh"
    assert verify_chain(rows) is True
    assert unknown not in _audit_dump(rows)


# ---------------------------------------------------------------- Cadena tras N eventos
def test_chain_valid_after_n_events(audit_session: Session):
    from app.modules.audit.service import verify_chain
    from app.modules.identity.service import device_login as device_login_service
    from app.modules.identity.service import pin_login as pin_login_service

    user, secret_hex = _make_hmac_user(audit_session)
    pin_user = _make_pin_user(audit_session)

    for _ in range(3):
        challenge = device_login_service.request_challenge(
            audit_session, user_ref=str(user.id), device_id="pixel-8-pro"
        )
        nonce = challenge["nonce"]
        device_login_service.login_with_device(
            audit_session,
            nonce=nonce,
            device_id="pixel-8-pro",
            signature=_sign_hmac(secret_hex, nonce),
            ip="10.1.0.1",
        )
    for _ in range(2):
        with pytest.raises(pin_login_service.PinInvalidError):
            pin_login_service.login_with_pin(
                audit_session,
                user_ref=str(pin_user.id),
                pin=WRONG_PIN,
                device_id="dev-9",
                ip="10.1.0.2",
            )

    rows = _audit_rows(audit_session)
    assert len(rows) == 5
    assert [row.seq for row in rows] == [1, 2, 3, 4, 5]
    for previous, current in pairwise(rows):
        assert current.prev_hash == previous.hash
    assert rows[0].prev_hash is None
    assert verify_chain(rows) is True


# ---------------------------------------------------------------- Best-effort
def test_audit_best_effort_does_not_break_login(
    audit_session: Session, monkeypatch: pytest.MonkeyPatch
):
    from app.modules.identity.service import pin_login as pin_login_service

    def _boom(*args, **kwargs):
        raise RuntimeError("audit down")

    monkeypatch.setattr("app.modules.audit.service.record", _boom)

    user = _make_pin_user(audit_session)
    out = pin_login_service.login_with_pin(
        audit_session, user_ref=str(user.id), pin=PIN, device_id="dev-1"
    )
    assert out["session_id"], "el login exitoso no se rompe si audit falla"

    with pytest.raises(pin_login_service.PinInvalidError):
        pin_login_service.login_with_pin(audit_session, user_ref=str(user.id), pin=WRONG_PIN)

    assert _audit_rows(audit_session) == [], "nada de audit persiste si la fachada falla"
