"""Reglas antifraude `risk_rules` + API auditada (E6-T01, HU20).

Cubre el brief `docs/tasks/E6-T01.md#pruebas-requeridas`:

- Camino feliz: listar la semilla; actualizar una regla y releerla
  sin reinicio.
- Errores: codigo inexistente -> `404`; accion invalida -> `422`;
  sin rol -> `403` (y sin token -> `401`).
- Semilla idempotente: `upsert_rule` dos veces no duplica (espejo del
  `ON CONFLICT DO NOTHING` de `0048_risk_rules`, verificado ademas
  estaticamente).
- Determinismo: `priority ASC, code ASC` (empate -> `code`).
- Auditoria: `updated_by`/`updated_at` + fila `risk.rule.updated`
  sin PII.
- Umbrales globales por `shared/config` con fallback.
- No mueve dinero.

SQLite en memoria + schemas ATTACH + HTTP real (TestClient) con JWT
reales (`create_access_token`, mismos roles que el guard).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.modules.audit.models as _audit_models
import app.modules.risk.models as _risk_models
import app.modules.shared.models as _shared_models
from app.core.db import Base, get_db
from app.main import app
from app.modules.risk import repository as risk_repo
from app.modules.risk import service as risk_service

assert _audit_models is not None
assert _risk_models is not None
assert _shared_models is not None

BACKEND_DIR = Path(__file__).resolve().parents[1]
MIGRATION_PATH = BACKEND_DIR / "migrations" / "versions" / "0048_risk_rules.py"

TABLES = [
    "risk.risk_rules",
    "audit.audit_log",
]

SCHEMAS = ("risk", "audit", "config")

SEED = (
    (
        "FACIAL_SPOOFING",
        "Suplantacion / spoofing facial",
        True,
        10,
        "ALERT",
        {"failed_attempts_threshold": 3, "block_on_lock": True},
    ),
    (
        "ATYPICAL_GEO",
        "Geolocalizacion atipica",
        True,
        20,
        "ALERT",
        {"mode": "country_mismatch", "score_bump": 10},
    ),
    (
        "UNUSUAL_AMOUNT",
        "Monto inusual",
        True,
        30,
        "ALERT",
        {"factor": 3.0, "min_amount_minor": 10000},
    ),
    (
        "VELOCITY",
        "Velocidad de operaciones",
        True,
        40,
        "ALERT",
        {"max_operations": 10, "window_seconds": 600},
    ),
    ("NEW_DEVICE", "Dispositivo nuevo", True, 50, "ALERT", {"score_bump": 5}),
)


@pytest.fixture()
def db():
    """SQLite en memoria con `risk.risk_rules` + `audit` + `config.parameters`."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in SCHEMAS:
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
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        for code, name, enabled, priority, action, params in SEED:
            risk_service.upsert_rule(
                session,
                code,
                name=name,
                enabled=enabled,
                priority=priority,
                action=action,
                params_json=params,
            )
        session.commit()
        yield engine, session
        session.rollback()
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def api_client(db, monkeypatch):
    """TestClient con BD SQLite (workers desactivados)."""
    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_QR_JOBS_ENABLED", "0")
    monkeypatch.setenv("WALLET_SERVICE_JOBS_ENABLED", "0")
    _, session = db

    def _override():
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _bearer(user_id: uuid.UUID, roles: list[str] | None = None) -> dict:
    from app.core.security import create_access_token

    token = create_access_token(subject=str(user_id), roles=roles)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def analyst_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def analyst_headers(analyst_id: uuid.UUID) -> dict:
    return _bearer(analyst_id, roles=["fraud_analyst"])


# ------------------------------------------------- migracion (estatica)
def test_migration_chains_head_and_seeds_idempotently():
    """`0048` encadena `0047`, crea `risk_rules` y siembra idempotente."""
    text = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "0048_risk_rules"' in text
    assert 'down_revision = "0047_wallet_personal_qr"' in text
    assert "0049" not in text, "sin ramas extra (un solo head)"
    for token in (
        "create_table",
        "risk_rules",
        "ck_risk_rules_action",
        "ix_risk_rules_enabled",
        "ix_risk_rules_priority",
        "ON CONFLICT (code) DO NOTHING",
        "ON CONFLICT (key) DO NOTHING",
        "risk.velocity_window_seconds",
        "risk.unusual_amount_factor",
    ):
        assert token in text, f"migracion incompleta: {token}"
    for code in ("FACIAL_SPOOFING", "ATYPICAL_GEO", "UNUSUAL_AMOUNT", "VELOCITY", "NEW_DEVICE"):
        assert code in text, f"semilla sin {code}"
    assert text.count("HOLD") >= 1 and "BLOCK" in text, "CHECK ALERT/HOLD/BLOCK"
    assert "'ALERT'" in text


# ------------------------------------------------- camino feliz
def test_list_seed_rules_ordered_and_enabled(api_client, analyst_headers):
    resp = api_client.get("/api/v1/risk/rules", headers=analyst_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"data", "meta"}
    assert body["meta"]["total"] == 5
    assert "request_id" in body["meta"]
    codes = [item["code"] for item in body["data"]]
    assert codes == [
        "FACIAL_SPOOFING",
        "ATYPICAL_GEO",
        "UNUSUAL_AMOUNT",
        "VELOCITY",
        "NEW_DEVICE",
    ], "orden determinista priority ASC, code ASC"
    for item in body["data"]:
        assert set(item) == {
            "code",
            "name",
            "enabled",
            "priority",
            "action",
            "params_json",
            "updated_at",
        }
        assert item["enabled"] is True, "semilla conservadora: todo habilitado"
        assert item["action"] == "ALERT", "semilla sin HOLD/BLOCK (E6-T06 calibra)"
        assert isinstance(item["params_json"], dict), "params_json siempre objeto"
        assert item["updated_at"] is not None


def test_update_rule_reflected_without_restart(api_client, analyst_headers, analyst_id, db):
    _, session = db
    resp = api_client.put(
        "/api/v1/risk/rules/VELOCITY",
        json={"enabled": False, "priority": 5, "params_json": {"max_operations": 3}},
        headers=analyst_headers,
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()["data"]
    assert updated["code"] == "VELOCITY"
    assert updated["enabled"] is False
    assert updated["priority"] == 5
    assert updated["params_json"] == {"max_operations": 3}
    assert updated["action"] == "ALERT", "no informado = conserva"
    # Relectura sin reinicio: el cambio ya rige (y lidera el orden).
    reread = api_client.get("/api/v1/risk/rules", headers=analyst_headers).json()["data"]
    by_code = {item["code"]: item for item in reread}
    assert by_code["VELOCITY"]["enabled"] is False
    assert by_code["VELOCITY"]["priority"] == 5
    assert next(item["code"] for item in reread) == "VELOCITY", "nuevo orden sin reinicio"
    # Trazabilidad en fila.
    row = risk_repo.require_by_code(session, "VELOCITY")
    assert str(row.updated_by) == str(analyst_id)
    assert row.updated_at is not None


def test_update_action_hold_and_block_allowed(api_client, analyst_headers):
    for action in ("HOLD", "BLOCK"):
        resp = api_client.put(
            "/api/v1/risk/rules/NEW_DEVICE",
            json={"action": action},
            headers=analyst_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["action"] == action
    # Code de la ruta insensible a mayusculas; la accion es estricta.
    resp = api_client.put(
        "/api/v1/risk/rules/new_device",
        json={"action": "ALERT"},
        headers=analyst_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["action"] == "ALERT"


def test_admin_role_also_authorized(api_client, analyst_id):
    headers = _bearer(analyst_id, roles=["admin"])
    resp = api_client.get("/api/v1/risk/rules", headers=headers)
    assert resp.status_code == 200, resp.text


# ------------------------------------------------- errores
def test_update_unknown_code_404(api_client, analyst_headers):
    resp = api_client.put(
        "/api/v1/risk/rules/NO_SUCH_RULE", json={"enabled": False}, headers=analyst_headers
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_update_invalid_action_422(api_client, analyst_headers):
    resp = api_client.put(
        "/api/v1/risk/rules/VELOCITY", json={"action": "FREEZE"}, headers=analyst_headers
    )
    assert resp.status_code == 422, resp.text


def test_update_invalid_priority_422(api_client, analyst_headers):
    for bad in (-1, 99999, "alta"):
        resp = api_client.put(
            "/api/v1/risk/rules/VELOCITY", json={"priority": bad}, headers=analyst_headers
        )
        assert resp.status_code == 422, resp.text


def test_update_non_object_params_422(api_client, analyst_headers):
    for bad in (["max_operations", 3], "no-objeto", 42):
        resp = api_client.put(
            "/api/v1/risk/rules/VELOCITY", json={"params_json": bad}, headers=analyst_headers
        )
        assert resp.status_code == 422, resp.text


def test_forbidden_without_role(api_client, analyst_id):
    headers = _bearer(analyst_id, roles=["analyst"])
    assert api_client.get("/api/v1/risk/rules", headers=headers).status_code == 403
    assert (
        api_client.put(
            "/api/v1/risk/rules/VELOCITY", json={"enabled": False}, headers=headers
        ).status_code
        == 403
    )


def test_unauthenticated_401(api_client):
    assert api_client.get("/api/v1/risk/rules").status_code == 401
    assert api_client.put("/api/v1/risk/rules/VELOCITY", json={"enabled": False}).status_code == 401


# ------------------------------------------------- semilla / orden / auditoria / config
def test_seed_upsert_idempotent(db):
    _, session = db
    before = risk_service.list_rules(session)
    assert len(before) == 5
    for code, name, enabled, priority, action, params in SEED:
        risk_service.upsert_rule(
            session,
            code,
            name=name,
            enabled=enabled,
            priority=priority,
            action=action,
            params_json=params,
        )
    session.commit()
    after = risk_service.list_rules(session)
    assert len(after) == 5, "repetir la semilla no duplica"
    assert {(r.code, r.priority, r.action) for r in after} == {
        (code, priority, action) for code, _, _, priority, action, _ in SEED
    }


def test_upsert_preserves_customization(db):
    _, session = db
    risk_service.update_rule(session, "VELOCITY", actor=uuid.uuid4(), enabled=False)
    session.commit()
    row = risk_service.upsert_rule(session, "VELOCITY", name="Velocidad", enabled=True)
    session.commit()
    assert row.enabled is False, "la semilla no pisa personalizaciones"


def test_priority_tie_breaks_by_code(api_client, analyst_headers):
    api_client.put("/api/v1/risk/rules/VELOCITY", json={"priority": 10}, headers=analyst_headers)
    codes = [
        item["code"]
        for item in api_client.get("/api/v1/risk/rules", headers=analyst_headers).json()["data"]
    ]
    assert codes == [
        "FACIAL_SPOOFING",
        "VELOCITY",
        "ATYPICAL_GEO",
        "UNUSUAL_AMOUNT",
        "NEW_DEVICE",
    ], "empate de prioridad -> code ascendente"


def test_rule_change_audited_without_pii(api_client, analyst_headers, analyst_id, db):
    _, session = db
    api_client.put(
        "/api/v1/risk/rules/UNUSUAL_AMOUNT",
        json={"params_json": {"factor": 2.5}},
        headers=analyst_headers,
    )
    rows = session.execute(
        sa.text(
            "SELECT actor_id, action, after_json FROM audit.audit_log "
            "WHERE action = 'risk.rule.updated' ORDER BY id DESC LIMIT 1"
        )
    ).all()
    assert len(rows) == 1, "auditoria best-effort registrada"
    actor_id, action, after_json = rows[0]
    assert action == "risk.rule.updated"
    assert str(actor_id).replace("-", "") == str(analyst_id).replace("-", "")
    import json as _json

    meta = _json.loads(after_json) if isinstance(after_json, str) else dict(after_json or {})
    assert meta["code"] == "UNUSUAL_AMOUNT"
    assert set(meta) <= {"code", "enabled", "priority", "action", "params_keys"}
    blob = _json.dumps(meta)
    for pii in ("@", "analyst", "token", "password", "pin"):
        assert pii.lower() not in blob.lower(), f"posible PII en auditoria: {pii}"


def test_evaluate_config_reads_globals_with_fallback(db):
    _, session = db
    cfg = risk_service.evaluate_config(session)
    assert cfg == {
        "velocity_window_seconds": 600,
        "unusual_amount_factor": 3.0,
    }, "sin claves rige el fallback conservador"
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, 'risk')"
        ),
        {"id": uuid.uuid4().hex, "key": "risk.velocity_window_seconds", "value": "120"},
    )
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, 'risk')"
        ),
        {"id": uuid.uuid4().hex, "key": "risk.unusual_amount_factor", "value": "2.5"},
    )
    session.commit()
    cfg = risk_service.evaluate_config(session)
    assert cfg == {"velocity_window_seconds": 120, "unusual_amount_factor": 2.5}


def test_evaluate_config_invalid_values_fall_back(db):
    _, session = db
    for key, value in (
        ("risk.velocity_window_seconds", '"no-numero"'),
        ("risk.unusual_amount_factor", "-1"),
    ):
        session.execute(
            sa.text(
                "INSERT INTO config.parameters (id, key, value_json, module) "
                "VALUES (:id, :key, :value, 'risk')"
            ),
            {"id": uuid.uuid4().hex, "key": key, "value": value},
        )
    session.commit()
    cfg = risk_service.evaluate_config(session)
    assert cfg == {"velocity_window_seconds": 600, "unusual_amount_factor": 3.0}


def test_no_money_movement():
    """`risk` no importa el motor ni el ledger (regla de oro 1/4)."""
    for rel in (
        "app/modules/risk/service/__init__.py",
        "app/modules/risk/repository/__init__.py",
        "app/modules/risk/api/__init__.py",
        "app/modules/risk/models/__init__.py",
    ):
        text = (BACKEND_DIR / rel).read_text(encoding="utf-8")
        for forbidden in (
            "app.modules.transactions",
            "app.modules.ledger",
            "app.modules.accounts",
            "app.modules.identity",
            "post_entry",
        ):
            assert forbidden not in text, f"frontera rota en {rel}: {forbidden}"
