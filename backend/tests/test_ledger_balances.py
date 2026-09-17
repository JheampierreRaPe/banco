"""Proyeccion atomica de saldos `ledger_balances` (E5-T12, HU18, CA-04).

- Parte A (sin BD): metadatos del modelo y migracion segun `03b#7.4`,
  puerto `AccountProjectionPort` (stub E2-T01 pendiente) y reglas
  (sin `float`, sin `commit`, sin publicacion de eventos, sin `accounts`).
- Parte B (SQLite en memoria + schema ATTACH): saldo sube/baja por
  DEBIT/CREDIT, version incrementa + conflicto concurrente rechazado,
  rollback deja proyeccion intacta, consistencia proyeccion-vs-postings.
- Parte C (Postgres `db_session`): humo de integracion; se omite si no hay BD.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base


def _valid_postings(avail_id, hold_id, amount=10_000, currency="PEN"):
    return [
        {
            "ledger_account_id": avail_id,
            "direction": "DEBIT",
            "amount_minor": amount,
            "currency": currency,
        },
        {
            "ledger_account_id": hold_id,
            "direction": "CREDIT",
            "amount_minor": amount,
            "currency": currency,
        },
    ]


class _FakeAccountProjection:
    """Fake del puerto E2-T01: registra llamadas sin tocar `accounts`."""

    def __init__(self):
        self.calls: list[tuple] = []

    def apply_projection(self, session, ledger_account_id, signed_delta_minor, currency):
        self.calls.append(
            (ledger_account_id, signed_delta_minor, currency)
        )


# ---------------------------------------------------------------- Parte A: modelo
def test_balance_table_registered_with_schema_columns_constraints():
    import app.modules.ledger.models as m  # noqa: F401 (registro)

    key = "ledger.ledger_balances"
    assert key in Base.metadata.tables, "falta tabla ledger.ledger_balances"
    table = Base.metadata.tables[key]
    assert table.schema == "ledger"
    cols = {c.name for c in table.columns}
    assert cols == {
        "ledger_account_id",
        "currency",
        "balance_minor",
        "version",
        "updated_at",
    }, f"columnas 03b#7.4, recibido: {sorted(cols)}"
    assert isinstance(table.columns["balance_minor"].type, sa.BigInteger)
    assert isinstance(table.columns["version"].type, sa.Integer)
    assert isinstance(table.columns["currency"].type, sa.String)
    pk = {c.name for c in table.primary_key.columns}
    assert pk == {"ledger_account_id"}, "PK debe ser ledger_account_id"


def test_no_foreign_keys_to_other_schemas():
    for key in (
        "ledger.ledger_balances",
        "ledger.journal_entries",
        "ledger.postings",
    ):
        table = Base.metadata.tables[key]
        for fk in table.foreign_keys:
            target = fk.column.table
            assert target.schema == "ledger", (
                f"FK fuera del schema propio: {key} -> "
                f"{target.schema}.{target.name}"
            )


def test_ledger_account_untouched_by_e5_t12():
    table = Base.metadata.tables["ledger.ledger_accounts"]
    cols = {c.name for c in table.columns}
    assert cols == {
        "id",
        "code",
        "name",
        "type",
        "currency",
        "owner_type",
        "owner_ref",
        "parent_account_id",
        "is_system",
        "created_at",
    }, "E5-T12 no debe modificar LedgerAccount (solo aditivo)"


def test_migration_0006_exists_and_matches_models():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0006_ledger_balances.py"
    )
    assert path.exists(), "falta migracion 0006_ledger_balances.py"
    content = path.read_text(encoding="utf-8")
    for token in (
        'revision = "0006_ledger_balances"',
        'down_revision = "0005_shared_outbox"',
        '"ledger_balances"',
        '"ledger"',
        "fk_ledger_balances_account",
    ):
        assert token in content, f"migracion sin {token}"
    assert 'schema="accounts"' not in content, "la migracion no debe tocar accounts"
    assert "schema='accounts'" not in content, "la migracion no debe tocar accounts"
    assert "account_balances" not in content, (
        "esa tabla es de E2-T01 fase 4: NO crearla aqui"
    )


def test_account_projection_port_defaults_to_stub():
    from app.modules.ledger.repository import balances as b

    stub = b.DefaultAccountProjection()
    with pytest.raises(NotImplementedError, match="E2-T01 pendiente"):
        stub.apply_projection(None, uuid.uuid4(), 100, "PEN")


def test_repository_has_no_float_commit_publish_nor_accounts():
    path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "modules"
        / "ledger"
        / "repository"
        / "balances.py"
    )
    content = path.read_text(encoding="utf-8")
    assert "float(" not in content
    # Sin DDL/ORM sobre la tabla de E2-T01 (mencionarla en comentarios esta bien).
    assert 'create_table(\n        "account_balances"' not in content
    assert '__tablename__ = "account_balances"' not in content
    assert 'schema="accounts"' not in content, "NO tocar el schema accounts"
    assert ".commit(" not in content, "flush sin commit: quien llama decide"
    assert "publish(" not in content, "nada de publish dentro del repositorio"
    assert "outbox" not in content, "la emision queda para outbox/E5-T05"
    for pattern in (
        r"session\.delete",
        r"sa\.delete\s*\(",
        r"sa\.update\s*\(",
        r"def\s+(update|delete|remove|purge)_posting",
    ):
        assert not re.search(pattern, content), f"repositorio con {pattern}"
    assert "post_entry" in content, "debe reutilizar post_entry sin duplicar logica"


# ---------------------------------------------------------------- Parte B: SQLite
@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schema `ledger` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.ledger.models as m  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS ledger")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["ledger.journal_entries"],
            Base.metadata.tables["ledger.postings"],
            Base.metadata.tables["ledger.ledger_balances"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_accounts(session):
    from app.modules.ledger import repository as repo

    return repo.ensure_customer_accounts(session, uuid.uuid4(), "PEN")


def test_balance_up_down_by_direction(sqlite_session: Session):
    from app.modules.ledger import repository as repo

    avail, hold = _seed_accounts(sqlite_session)
    fake = _FakeAccountProjection()
    repo.post_entry_and_update_balances(
        sqlite_session,
        entry_type="OWN_TRANSFER",
        postings=_valid_postings(avail.id, hold.id, 10_000),
        account_projection=fake,
    )
    assert repo.get_balance(sqlite_session, avail.id).balance_minor == 10_000
    assert repo.get_balance(sqlite_session, hold.id).balance_minor == -10_000
    # Segundo asiento acumula: DEBIT sube, CREDIT baja.
    repo.post_entry_and_update_balances(
        sqlite_session,
        entry_type="OWN_TRANSFER",
        postings=_valid_postings(avail.id, hold.id, 4_000),
        account_projection=fake,
    )
    assert repo.get_balance(sqlite_session, avail.id).balance_minor == 14_000
    assert repo.get_balance(sqlite_session, hold.id).balance_minor == -14_000
    # El seam E2-T01 recibe los deltas firmados (2 postings x 2 asientos).
    assert len(fake.calls) == 4
    assert (avail.id, 10_000, "PEN") in fake.calls
    assert (hold.id, -10_000, "PEN") in fake.calls


def test_version_increments_and_conflict_rejected(sqlite_session: Session):
    from app.modules.ledger import repository as repo
    from app.modules.ledger.repository.balances import (
        VersionConflictError,
        apply_balance_delta,
    )

    avail, _ = _seed_accounts(sqlite_session)
    row = apply_balance_delta(sqlite_session, avail.id, 5_000, "PEN")
    assert row.version == 0
    row = apply_balance_delta(sqlite_session, avail.id, 2_000, "PEN")
    assert (row.balance_minor, row.version) == (7_000, 1)
    # Version esperada correcta: aplica e incrementa.
    row = apply_balance_delta(
        sqlite_session, avail.id, 1_000, "PEN", expected_version=1
    )
    assert (row.balance_minor, row.version) == (8_000, 2)
    # Version concurrente obsoleta: rechazada, saldo intacto (el fallo ocurre
    # antes de mutar: no hay nada que revertir de este intento).
    with pytest.raises(VersionConflictError):
        apply_balance_delta(
            sqlite_session, avail.id, 1_000, "PEN", expected_version=1
        )
    row = repo.get_balance(sqlite_session, avail.id)
    assert (row.balance_minor, row.version) == (8_000, 2)


def test_rollback_leaves_projection_intact(sqlite_session: Session):
    import sqlalchemy as sa

    from app.modules.ledger import repository as repo

    avail, hold = _seed_accounts(sqlite_session)

    class _FailingProjection:
        def apply_projection(self, session, ledger_account_id, signed_delta_minor, currency):
            raise RuntimeError("fallo inyectado tras el asiento")

    before_entries = sqlite_session.scalar(
        sa.select(sa.func.count()).select_from(
            Base.metadata.tables["ledger.journal_entries"]
        )
    )
    with pytest.raises(RuntimeError, match="fallo inyectado"):
        repo.post_entry_and_update_balances(
            sqlite_session,
            entry_type="OWN_TRANSFER",
            postings=_valid_postings(avail.id, hold.id, 3_000),
            account_projection=_FailingProjection(),
        )
    sqlite_session.rollback()
    after_entries = sqlite_session.scalar(
        sa.select(sa.func.count()).select_from(
            Base.metadata.tables["ledger.journal_entries"]
        )
    )
    assert after_entries == before_entries, "el asiento tambien se revierte"
    assert repo.get_balance(sqlite_session, avail.id) is None
    assert repo.get_balance(sqlite_session, hold.id) is None
    assert repo.check_projection_consistency(sqlite_session) == []


def test_consistency_projection_vs_postings(sqlite_session: Session):
    from app.modules.ledger.models import LedgerBalance
    from app.modules.ledger import repository as repo

    avail, hold = _seed_accounts(sqlite_session)
    fake = _FakeAccountProjection()
    repo.post_entry_and_update_balances(
        sqlite_session,
        entry_type="OWN_TRANSFER",
        postings=_valid_postings(avail.id, hold.id, 6_000),
        account_projection=fake,
    )
    assert repo.check_projection_consistency(sqlite_session) == []
    # Corrupcion simulada de la proyeccion: el chequeo la detecta.
    row = repo.get_balance(sqlite_session, avail.id)
    row.balance_minor = 1
    sqlite_session.flush()
    diffs = repo.check_projection_consistency(sqlite_session)
    assert len(diffs) == 1
    assert diffs[0]["ledger_account_id"] == avail.id
    assert diffs[0]["postings_total_minor"] == 6_000
    assert diffs[0]["balance_minor"] == 1


def test_default_stub_is_silently_skipped(sqlite_session: Session):
    """Sin adaptador E2-T01, `ledger_balances` igual se actualiza."""
    from app.modules.ledger import repository as repo

    avail, hold = _seed_accounts(sqlite_session)
    repo.post_entry_and_update_balances(
        sqlite_session,
        entry_type="OWN_TRANSFER",
        postings=_valid_postings(avail.id, hold.id, 1_000),
    )
    assert repo.get_balance(sqlite_session, avail.id).balance_minor == 1_000
    assert repo.check_projection_consistency(sqlite_session) == []


# ---------------------------------------------------------------- Parte C: Postgres
def test_integration_postgres_balances(db_session: Session):
    from app.modules.ledger import repository as repo

    avail, hold = repo.ensure_customer_accounts(db_session, uuid.uuid4(), "PEN")
    fake = _FakeAccountProjection()
    repo.post_entry_and_update_balances(
        db_session,
        entry_type="OWN_TRANSFER",
        postings=_valid_postings(avail.id, hold.id, 2_500),
        description="humo E5-T12",
        account_projection=fake,
    )
    assert repo.get_balance(db_session, avail.id).balance_minor == 2_500
    assert repo.get_balance(db_session, hold.id).balance_minor == -2_500
    assert repo.check_projection_consistency(db_session) == []
