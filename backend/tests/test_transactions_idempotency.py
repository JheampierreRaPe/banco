"""Idempotencia Idempotency-Key (E5-T04, HU17 CA-03).

- Parte A (sin BD): metadatos del modelo y migracion segun `03b#2.1`,
  hash canonico SHA-256, TTL (default 24 h + env), cabecera obligatoria
  solo en dinero / opcional fuera de dinero, y reglas (sin `commit`,
  sin publicar eventos, sin `float`, sin tocar `service` ni `jobs/`).
- Parte B (SQLite en memoria + schema ATTACH): repeticion exacta devuelve
  el snapshot sin re-ejecutar (contador == 1); misma clave + distinto
  cuerpo -> 409; clave expirada se renueva y re-ejecuta; doble claim
  simultaneo no duplica (claim atomico por UQ).
- Parte C (Postgres `db_session`): humo de integracion; se omite si no hay BD.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base


# ---------------------------------------------------------------- Parte A: modelo
def test_table_registered_with_schema_columns_constraints_indexes():
    import app.modules.shared.models as m  # noqa: F401 (registro)

    assert "shared.idempotency_keys" in Base.metadata.tables
    table = Base.metadata.tables["shared.idempotency_keys"]
    assert table.schema == "shared"
    cols = {c.name for c in table.columns}
    for expected in (
        "id",
        "key",
        "user_id",
        "endpoint",
        "method",
        "request_hash",
        "response_snapshot",
        "transaction_id",
        "created_at",
        "expires_at",
    ):
        assert expected in cols, f"falta columna idempotency_keys.{expected}"
    assert isinstance(table.columns["key"].type, sa.String)
    assert table.columns["key"].type.length == 80
    assert isinstance(table.columns["request_hash"].type, sa.String)
    assert table.columns["request_hash"].type.length == 64
    assert isinstance(table.columns["response_snapshot"].type, sa.JSON)
    uniques = {
        c.name
        for c in table.constraints
        if isinstance(c, sa.UniqueConstraint)
    }
    assert "uq_idempotency_key_user" in uniques, "UQ(key, user_id)"
    idx = {i.name for i in table.indexes}
    assert "ix_idempotency_created_at" in idx
    assert "ix_idempotency_expires_at" in idx


def test_no_foreign_keys_to_other_schemas_and_outbox_untouched():
    table = Base.metadata.tables["shared.idempotency_keys"]
    assert not list(table.foreign_keys), "sin FK fisicas a otros schemas"
    assert not table.columns["user_id"].foreign_keys
    assert not table.columns["transaction_id"].foreign_keys
    # E5-T05 intacto: outbox/inbox siguen registrados sin cambios.
    assert "shared.outbox" in Base.metadata.tables
    assert "shared.processed_events" in Base.metadata.tables


def test_migration_0007_exists_and_matches_models():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0007_shared_idempotency.py"
    )
    assert path.exists(), "falta migracion 0007_shared_idempotency.py"
    content = path.read_text(encoding="utf-8")
    for token in (
        'revision = "0007_shared_idempotency"',
        'down_revision = "0006_ledger_balances"',
        '"idempotency_keys"',
        '"shared"',
        "uq_idempotency_key_user",
        "ix_idempotency_created_at",
        "ix_idempotency_expires_at",
        "request_hash",
        "response_snapshot",
        "expires_at",
    ):
        assert token in content, f"migracion sin {token}"
    for foreign in ("ledger.", "transactions.", "accounts.", "identity."):
        assert foreign not in content, f"la migracion no debe tocar {foreign}"


def test_request_hash_is_canonical_sha256():
    from app.modules.shared.repository import compute_request_hash

    same_a = compute_request_hash({"b": 2, "a": 1})
    same_b = compute_request_hash({"a": 1, "b": 2})
    assert same_a == same_b, "orden de claves no cambia el hash"
    assert len(same_a) == 64
    assert compute_request_hash({"a": 1}) != compute_request_hash({"a": 2})
    assert compute_request_hash(None) == compute_request_hash(b"")
    with pytest.raises(ValueError):
        compute_request_hash({"bad": object()})
    with pytest.raises(ValueError):
        compute_request_hash(123)


def test_ttl_defaults_24h_and_reads_env(monkeypatch):
    from app.modules.shared.repository import DEFAULT_TTL_SECONDS, resolve_ttl_seconds

    assert DEFAULT_TTL_SECONDS == 24 * 3600
    monkeypatch.delenv("IDEMPOTENCY_TTL_SECONDS", raising=False)
    assert resolve_ttl_seconds() == 24 * 3600
    monkeypatch.setenv("IDEMPOTENCY_TTL_SECONDS", "60")
    assert resolve_ttl_seconds() == 60
    assert resolve_ttl_seconds(explicit=10) == 10
    monkeypatch.setenv("IDEMPOTENCY_TTL_SECONDS", "no-numero")
    with pytest.raises(ValueError):
        resolve_ttl_seconds()
    monkeypatch.setenv("IDEMPOTENCY_TTL_SECONDS", "0")
    with pytest.raises(ValueError):
        resolve_ttl_seconds()
    with pytest.raises(ValueError):
        resolve_ttl_seconds(explicit=0)


def test_header_required_only_for_money():
    from app.modules.transactions.api.idempotency import (
        IdempotencyKeyMissingError,
        require_idempotency_key,
    )

    with pytest.raises(IdempotencyKeyMissingError):
        require_idempotency_key({}, endpoint="/transfers")
    with pytest.raises(IdempotencyKeyMissingError):
        require_idempotency_key({"Idempotency-Key": "  "}, endpoint="/payments/qr")
    assert require_idempotency_key({"Idempotency-Key": "k-1"}, endpoint="/transfers") == "k-1"
    # Fuera de dinero: opcional (marca explicita) pero respetada si viene.
    assert require_idempotency_key({}, endpoint="/health") is None
    assert require_idempotency_key({}, endpoint="/accounts") is None
    assert (
        require_idempotency_key({"idempotency-key": "k-2"}, endpoint="/health") == "k-2"
    )


def test_new_files_have_no_commit_publish_float_nor_service_touch():
    base = Path(__file__).resolve().parents[1]
    for rel in (
        Path("app/modules/shared/repository/idempotency.py"),
        Path("app/modules/transactions/api/idempotency.py"),
        Path("app/modules/shared/models/__init__.py"),
    ):
        content = (base / rel).read_text(encoding="utf-8")
        assert ".commit(" not in content, f"{rel} no debe hacer commit"
        assert "float(" not in content, f"{rel} sin atajos float"
    mw = (base / "app/modules/transactions/api/idempotency.py").read_text(encoding="utf-8")
    assert "publish" not in mw.lower(), "el middleware no publica eventos"
    assert "transactions/service" not in mw and "modules.transactions.service" not in mw, (
        "sin tocar transactions/service (E5-T03)"
    )
    service_dir = base / "app/modules/transactions/service"
    assert service_dir.exists()


# ---------------------------------------------------------------- Parte B: SQLite
@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schema `shared` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.shared.models as m  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS shared")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine, tables=[Base.metadata.tables["shared.idempotency_keys"]]
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _money_handler(calls: list, tx_id: uuid.UUID):
    def _run():
        calls.append(1)
        return ({"status": "SETTLED", "amount_minor": 10_000}, tx_id)

    return _run


def test_exact_replay_returns_snapshot_without_reexecution(sqlite_session: Session):
    from app.modules.transactions.api.idempotency import execute_with_idempotency

    calls: list = []
    tx_id = uuid.uuid4()
    kwargs = dict(
        key="k-exacta",
        user_id=uuid.uuid4(),
        endpoint="/transfers",
        method="POST",
        body={"to": "B", "amount_minor": 10_000},
    )
    first = execute_with_idempotency(
        sqlite_session, handler=_money_handler(calls, tx_id), **kwargs
    )
    assert first.replayed is False
    assert first.response == {"status": "SETTLED", "amount_minor": 10_000}
    assert first.transaction_id == tx_id
    second = execute_with_idempotency(
        sqlite_session, handler=_money_handler(calls, tx_id), **kwargs
    )
    assert second.replayed is True, "repeticion exacta: replay sin re-ejecutar"
    assert second.response == first.response
    assert len(calls) == 1, "el handler se ejecuto una sola vez (sin doble debito)"


def test_same_key_different_body_conflicts(sqlite_session: Session):
    from app.modules.transactions.api.idempotency import (
        IdempotencyConflictError,
        execute_with_idempotency,
    )

    calls: list = []
    base = dict(
        key="k-conflicto",
        user_id=uuid.uuid4(),
        endpoint="/payments",
        method="POST",
    )
    execute_with_idempotency(
        sqlite_session,
        handler=_money_handler(calls, uuid.uuid4()),
        body={"amount_minor": 100},
        **base,
    )
    with pytest.raises(IdempotencyConflictError):
        execute_with_idempotency(
            sqlite_session,
            handler=_money_handler(calls, uuid.uuid4()),
            body={"amount_minor": 200},
            **base,
        )
    assert len(calls) == 1


def test_expired_key_is_treated_as_new(sqlite_session: Session):
    from datetime import timezone

    from app.modules.shared.models import IdempotencyKey
    from app.modules.shared.repository import compute_request_hash
    from app.modules.transactions.api.idempotency import execute_with_idempotency

    calls: list = []
    user_id = uuid.uuid4()
    body = {"amount_minor": 50}
    first = execute_with_idempotency(
        sqlite_session,
        key="k-ttl",
        user_id=user_id,
        endpoint="/transfers",
        method="POST",
        body=body,
        handler=_money_handler(calls, uuid.uuid4()),
        ttl_seconds=3600,
    )
    assert first.replayed is False
    # Vencer la clave manipulando `expires_at` (equivale a TTL corto).
    row = sqlite_session.scalar(
        sa.select(IdempotencyKey).where(
            IdempotencyKey.key == "k-ttl", IdempotencyKey.user_id == user_id
        )
    )
    assert row is not None
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    sqlite_session.flush()
    tx_id = uuid.uuid4()
    again = execute_with_idempotency(
        sqlite_session,
        key="k-ttl",
        user_id=user_id,
        endpoint="/transfers",
        method="POST",
        body=body,
        handler=_money_handler(calls, tx_id),
    )
    assert again.replayed is False, "clave expirada: se re-ejecuta como nueva"
    assert again.transaction_id == tx_id
    assert len(calls) == 2
    assert row.request_hash == compute_request_hash(body)
    assert row.response_snapshot == {"status": "SETTLED", "amount_minor": 10_000}


def test_concurrent_double_claim_does_not_duplicate(tmp_path):
    # Nota: con `user_id` autenticado (caso de los endpoints de dinero) la
    # UQ (`key`, `user_id`) hace el claim atomico. Con `user_id` NULL
    # (servicios tecnicos) los NULL no colisionan en UQ (semantica SQL
    # estandar en SQLite y Postgres): ver pendientes del reporte E5-T04.
    #
    # BD en archivos (no `:memory:` + `StaticPool`): cada hilo obtiene su
    # propia conexion, como en produccion; compartir una sola conexion
    # DBAPI entre hilos genera entrelazados invalidos fuera del alcance.
    from sqlalchemy import create_engine

    from app.modules.shared.repository import compute_request_hash, try_claim

    main_db = tmp_path / "race_main.db"
    shared_db = tmp_path / "race_shared.db"
    engine = create_engine(
        f"sqlite:///{main_db}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute(f"ATTACH DATABASE '{shared_db}' AS shared")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["shared.idempotency_keys"]])
    user_id = uuid.uuid4()
    results: dict = {}

    def _claim(name: str):
        session = Session(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            _, created = try_claim(
                session,
                key="k-carrera",
                user_id=user_id,
                endpoint="/transfers",
                method="POST",
                request_hash=compute_request_hash({"amount_minor": 5}),
                ttl_seconds=3600,
            )
            session.commit()
            results[name] = created
        finally:
            session.close()

    threads = [threading.Thread(target=_claim, args=(f"t{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results.values(), key=str) == [False, True], f"un solo claim gana: {results}"

    # El perdedor no re-ejecuta: marca en vuelo (snapshot None) -> 409.
    from app.modules.transactions.api.idempotency import (
        IdempotencyConflictError,
        execute_with_idempotency,
    )

    other = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        with pytest.raises(IdempotencyConflictError):
            execute_with_idempotency(
                other,
                key="k-carrera",
                user_id=user_id,
                endpoint="/transfers",
                method="POST",
                body={"amount_minor": 5},
                handler=lambda: ({"status": "SETTLED", "amount_minor": 5}, uuid.uuid4()),
            )
    finally:
        other.close()
        engine.dispose()


# ---------------------------------------------------------------- Parte C: Postgres
def test_integration_postgres_idempotency_smoke(db_session: Session):
    from app.modules.shared.repository import compute_request_hash, find_key
    from app.modules.transactions.api.idempotency import execute_with_idempotency

    calls: list = []
    tx_id = uuid.uuid4()
    kwargs = dict(
        key=f"k-smoke-{uuid.uuid4().hex[:8]}",
        user_id=uuid.uuid4(),
        endpoint="/transfers",
        method="POST",
        body={"amount_minor": 100},
    )
    first = execute_with_idempotency(
        db_session, handler=_money_handler(calls, tx_id), **kwargs
    )
    assert first.replayed is False
    second = execute_with_idempotency(
        db_session, handler=_money_handler(calls, tx_id), **kwargs
    )
    assert second.replayed is True and len(calls) == 1
    stored = find_key(db_session, key=kwargs["key"], user_id=kwargs["user_id"])
    assert stored is not None
    assert stored.request_hash == compute_request_hash(kwargs["body"])
    assert stored.transaction_id == tx_id
