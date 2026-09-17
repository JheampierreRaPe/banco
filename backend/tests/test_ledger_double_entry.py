"""Asientos de partida doble append-only con hash (E5-T10, HU18, CA-01/02/03).

- Parte A (sin BD): metadatos de modelos y migracion segun `03b#7.2/#7.3`,
  dominio puro (`cuadre`, `hash`, `reverso`) y reglas (sin `float`,
  sin mutacion de postings, sin publicacion de eventos).
- Parte B (SQLite en memoria + schema ATTACH): asiento cuadrado, descuadrado
  rechazado sin filas residuales, reverso, UUID unico, cadena de hashes,
  fachada `post_entry`/`reverse_entry`.
- Parte C (Postgres `db_session`): humo de integracion; se omite si no hay BD.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from itertools import pairwise
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


# ---------------------------------------------------------------- Parte A: modelo
def test_tables_registered_with_schema_columns_constraints_indexes():
    import app.modules.ledger.models as m  # noqa: F401 (registro)

    for key in ("ledger.journal_entries", "ledger.postings"):
        assert key in Base.metadata.tables, f"falta tabla {key}"

    entry = Base.metadata.tables["ledger.journal_entries"]
    assert entry.schema == "ledger"
    cols = {c.name for c in entry.columns}
    for expected in (
        "id",
        "transaction_id",
        "entry_type",
        "description",
        "value_date",
        "status",
        "reverses_entry_id",
        "prev_hash",
        "hash",
        "created_at",
    ):
        assert expected in cols, f"falta columna journal_entries.{expected}"
    assert isinstance(entry.columns["value_date"].type, sa.Date)
    assert isinstance(entry.columns["hash"].type, sa.String)
    checks = {c.name for c in entry.constraints if isinstance(c, sa.CheckConstraint)}
    assert "ck_journal_entries_status" in checks
    idx = {i.name for i in entry.indexes}
    for expected in (
        "ix_journal_entries_transaction_id",
        "ix_journal_entries_value_date",
        "ix_journal_entries_status",
        "ix_journal_entries_created_at",
    ):
        assert expected in idx, f"falta indice {expected}"

    posting = Base.metadata.tables["ledger.postings"]
    assert posting.schema == "ledger"
    cols = {c.name for c in posting.columns}
    for expected in (
        "id",
        "journal_entry_id",
        "ledger_account_id",
        "direction",
        "amount_minor",
        "currency",
        "account_ref",
        "created_at",
    ):
        assert expected in cols, f"falta columna postings.{expected}"
    assert isinstance(posting.columns["amount_minor"].type, sa.BigInteger)
    checks = {c.name for c in posting.constraints if isinstance(c, sa.CheckConstraint)}
    assert "ck_postings_direction" in checks
    assert "ck_postings_amount_positive" in checks
    idx = {i.name for i in posting.indexes}
    for expected in (
        "ix_postings_journal_entry_id",
        "ix_postings_ledger_account_id",
        "ix_postings_account_ref",
        "ix_postings_created_at",
        "ix_postings_ledger_account_created_at",
    ):
        assert expected in idx, f"falta indice {expected}"


def test_no_foreign_keys_to_other_schemas_and_tx_id_is_logical():
    for key in ("ledger.journal_entries", "ledger.postings"):
        table = Base.metadata.tables[key]
        for fk in table.foreign_keys:
            target = fk.column.table
            assert target.schema == "ledger", (
                f"FK fuera del schema propio: {key} -> " f"{target.schema}.{target.name}"
            )
    entry = Base.metadata.tables["ledger.journal_entries"]
    assert not entry.columns[
        "transaction_id"
    ].foreign_keys, "transaction_id debe ser UUID logico sin FK fisica a otro schema"


def test_ledger_account_untouched_by_e5_t10():
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
    }, "E5-T10 no debe modificar LedgerAccount de E5-T09"


def test_migration_0004_exists_and_matches_models():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0004_ledger_double_entry.py"
    )
    assert path.exists(), "falta migracion 0004_ledger_double_entry.py"
    content = path.read_text(encoding="utf-8")
    for token in (
        'down_revision = "0003_ledger_chart_of_accounts"',
        '"journal_entries"',
        '"postings"',
        '"ledger"',
        "ck_journal_entries_status",
        "ck_postings_direction",
        "ck_postings_amount_positive",
        "fk_postings_journal_entry",
        "fk_postings_ledger_account",
        "fk_journal_entries_reverses",
        "ix_journal_entries_transaction_id",
        "ix_postings_journal_entry_id",
    ):
        assert token in content, f"migracion sin {token}"
    assert "transactions." not in content, "la migracion no debe referenciar otro schema"


def test_domain_balanced_entry_validates():
    from app.modules.ledger.domain.entries import validate_postings

    a, b = uuid.uuid4(), uuid.uuid4()
    items = validate_postings(_valid_postings(a, b))
    assert len(items) == 2
    # Varias monedas cuadran cada una por separado.
    multi = validate_postings(
        _valid_postings(a, b, 1_000, "PEN") + _valid_postings(a, b, 200, "USD")
    )
    assert len(multi) == 4


def test_domain_unbalanced_entry_rejected():
    from app.modules.ledger.domain.entries import validate_postings

    a, b = uuid.uuid4(), uuid.uuid4()
    postings = _valid_postings(a, b)
    postings[1]["amount_minor"] = 9_999
    with pytest.raises(ValueError, match="descuadrado"):
        validate_postings(postings)
    with pytest.raises(ValueError):
        validate_postings([postings[0]])  # menos de 2 movimientos


def test_domain_rejects_float_zero_negative_and_bad_enums():
    from app.modules.ledger.domain.entries import validate_postings

    a, b = uuid.uuid4(), uuid.uuid4()
    base = _valid_postings(a, b)
    for bad in (0, -5):
        bad_postings = [dict(base[0], amount_minor=bad), base[1]]
        with pytest.raises(ValueError):
            validate_postings(bad_postings)
    for bad_type in (10_000.0, True, "10000"):
        bad_postings = [dict(base[0], amount_minor=bad_type), base[1]]
        with pytest.raises(TypeError):
            validate_postings(bad_postings)
    with pytest.raises(ValueError):
        validate_postings([dict(base[0], direction="debit"), base[1]])
    with pytest.raises(ValueError):
        validate_postings([dict(base[0], currency="pen"), base[1]])
    with pytest.raises(ValueError):
        validate_postings([dict(base[0], currency="PENS"), base[1]])


def test_domain_hash_is_chained_hex():
    from app.modules.ledger.domain.entries import compute_entry_hash, validate_postings

    a, b = uuid.uuid4(), uuid.uuid4()
    items = validate_postings(_valid_postings(a, b))
    kwargs = {
        "entry_type": "OWN_TRANSFER",
        "description": "test",
        "value_date": date(2026, 9, 17),
        "transaction_id": uuid.uuid4(),
        "postings": items,
    }
    genesis = compute_entry_hash(**kwargs, prev_hash=None)
    assert re.fullmatch(r"[0-9a-f]{64}", genesis), "hash debe ser hex de 64 chars"
    # Determinista e independiente del orden de los postings.
    assert compute_entry_hash(**kwargs, prev_hash=None) == genesis
    assert compute_entry_hash(**kwargs, prev_hash=None) == compute_entry_hash(
        **{**kwargs, "postings": list(reversed(items))}, prev_hash=None
    )
    chained = compute_entry_hash(**kwargs, prev_hash=genesis)
    assert chained != genesis
    with pytest.raises(ValueError):
        compute_entry_hash(**kwargs, prev_hash="corto")


def test_domain_reversal_inverts_directions():
    from app.modules.ledger.domain.entries import (
        build_reversal_inputs,
        validate_postings,
    )

    a, b = uuid.uuid4(), uuid.uuid4()
    items = validate_postings(_valid_postings(a, b, 5_000))
    reversed_items = build_reversal_inputs(items)
    assert [(p.direction, p.amount_minor) for p in reversed_items] == [
        ("CREDIT", 5_000),
        ("DEBIT", 5_000),
    ]
    # El compensatorio tambien cuadra.
    assert validate_postings(list(reversed_items)) == reversed_items


def test_repository_has_no_posting_mutation_nor_event_publish():
    path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "modules"
        / "ledger"
        / "repository"
        / "entries.py"
    )
    content = path.read_text(encoding="utf-8")
    for pattern in (
        r"session\.delete",
        r"sa\.delete\s*\(",
        r"sa\.update\s*\(",
        r"def\s+(update|delete|remove|purge)_",
        r"\.execute\s*\(\s*sa\.(delete|update)",
    ):
        assert not re.search(pattern, content), f"repositorio con {pattern}"
    assert "publish(" not in content, "nada de publish dentro del repositorio"
    assert "outbox" not in content, "la emision queda para outbox/E5-T05"
    assert "float(" not in content


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


def _count(session, key):
    return session.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[key]))


def test_post_balanced_entry(sqlite_session: Session):
    from app.modules.ledger import repository as repo

    avail, hold = _seed_accounts(sqlite_session)
    tx_id = uuid.uuid4()
    entry = repo.post_entry(
        sqlite_session,
        entry_type="OWN_TRANSFER",
        postings=_valid_postings(avail.id, hold.id),
        transaction_id=tx_id,
        description="cuadre",
        value_date=date(2026, 9, 17),
    )
    assert entry.status == "POSTED"
    assert entry.prev_hash is None  # genesis
    assert re.fullmatch(r"[0-9a-f]{64}", entry.hash)
    assert entry.transaction_id == tx_id
    assert entry.value_date == date(2026, 9, 17)
    rows = repo.list_postings(sqlite_session, entry.id)
    assert len(rows) == 2
    debit = sum(r.amount_minor for r in rows if r.direction == "DEBIT")
    credit = sum(r.amount_minor for r in rows if r.direction == "CREDIT")
    assert debit == credit == 10_000
    assert repo.get_entry(sqlite_session, entry.id).id == entry.id
    assert repo.get_entry(sqlite_session, uuid.uuid4()) is None


def test_post_unbalanced_rejected_without_residue(sqlite_session: Session):
    from app.modules.ledger import repository as repo

    avail, hold = _seed_accounts(sqlite_session)
    postings = _valid_postings(avail.id, hold.id)
    postings[1]["amount_minor"] = 9_999
    before_entries, before_postings = (
        _count(sqlite_session, "ledger.journal_entries"),
        _count(sqlite_session, "ledger.postings"),
    )
    with pytest.raises(ValueError, match="descuadrado"):
        repo.post_entry(sqlite_session, entry_type="OWN_TRANSFER", postings=postings)
    sqlite_session.rollback()
    assert _count(sqlite_session, "ledger.journal_entries") == before_entries
    assert _count(sqlite_session, "ledger.postings") == before_postings


def test_post_rejects_unknown_account(sqlite_session: Session):
    from app.modules.ledger import repository as repo

    avail, _ = _seed_accounts(sqlite_session)
    with pytest.raises(ValueError, match="inexistentes"):
        repo.post_entry(
            sqlite_session,
            entry_type="OWN_TRANSFER",
            postings=_valid_postings(avail.id, uuid.uuid4()),
        )


def test_reverse_entry_creates_compensating_entry(sqlite_session: Session):
    from app.modules.ledger import repository as repo

    avail, hold = _seed_accounts(sqlite_session)
    original = repo.post_entry(
        sqlite_session,
        entry_type="OWN_TRANSFER",
        postings=_valid_postings(avail.id, hold.id, 5_000),
    )
    comp = repo.reverse_entry(sqlite_session, original.id)
    assert repo.get_entry(sqlite_session, original.id).status == "REVERSED"
    assert comp.status == "POSTED"
    assert comp.reverses_entry_id == original.id
    assert comp.prev_hash == original.hash
    rows = repo.list_postings(sqlite_session, comp.id)
    # Orden intra-asiento no garantizado (mismo instante); el contenido si.
    assert sorted((r.direction, r.amount_minor) for r in rows) == [
        ("CREDIT", 5_000),
        ("DEBIT", 5_000),
    ]
    # Los postings originales intactos: 2 + 2 filas, sin UPDATE/DELETE.
    assert _count(sqlite_session, "ledger.postings") == 4
    with pytest.raises(ValueError, match="POSTED"):
        repo.reverse_entry(sqlite_session, original.id)
    with pytest.raises(ValueError, match="inexistente"):
        repo.reverse_entry(sqlite_session, uuid.uuid4())


def test_uuid_unique_and_hash_chain(sqlite_session: Session):
    from app.modules.ledger import repository as repo

    avail, hold = _seed_accounts(sqlite_session)
    entries = [
        repo.post_entry(
            sqlite_session,
            entry_type="OWN_TRANSFER",
            postings=_valid_postings(avail.id, hold.id, 1_000 + i),
        )
        for i in range(3)
    ]
    ids = [e.id for e in entries]
    assert len(set(ids)) == 3
    hashes = [e.hash for e in entries]
    assert len(set(hashes)) == 3
    assert entries[0].prev_hash is None
    for prev, current in pairwise(entries):
        assert current.prev_hash == prev.hash, "cada hash referencia al anterior"


def test_facade_delegates_to_repository(sqlite_session: Session):
    from app.modules.ledger import service

    avail, hold = _seed_accounts(sqlite_session)
    entry = service.post_entry(
        sqlite_session,
        entry_type="OWN_TRANSFER",
        postings=_valid_postings(avail.id, hold.id),
    )
    assert service.get_entry(sqlite_session, entry.id).id == entry.id
    assert len(service.list_postings(sqlite_session, entry.id)) == 2
    comp = service.reverse_entry(sqlite_session, entry.id)
    assert comp.reverses_entry_id == entry.id


# ---------------------------------------------------------------- Parte C: Postgres
def test_integration_postgres_post_and_reverse(db_session: Session):
    from app.modules.ledger import repository as repo

    avail, hold = repo.ensure_customer_accounts(db_session, uuid.uuid4(), "PEN")
    entry = repo.post_entry(
        db_session,
        entry_type="OWN_TRANSFER",
        postings=_valid_postings(avail.id, hold.id),
        transaction_id=uuid.uuid4(),
        description="humo E5-T10",
    )
    assert entry.id is not None
    assert re.fullmatch(r"[0-9a-f]{64}", entry.hash)
    comp = repo.reverse_entry(db_session, entry.id)
    assert comp.reverses_entry_id == entry.id
    assert repo.get_entry(db_session, entry.id).status == "REVERSED"
