"""Verificacion anti-manipulacion de la cadena de hashes (E5-T14, HU18 CA-02).

- Cadena valida (incluye reverso: el `REVERSED` no rompe el hash).
- Alteracion simulada detectada (descripcion / monto / hash / prev_hash).
- Reorden y borrado detectados (enlaces `prev_hash`, no `created_at`).
- Detalle sin PII (solo ids + codigo de motivo) para alerta critica.
- Solo lectura: el verificador no modifica registros.
"""

from __future__ import annotations

import copy
import uuid
from datetime import date

import pytest
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


def _post_map(session, entries):
    from app.modules.ledger import repository as repo

    return {e.id: repo.list_postings(session, e.id) for e in entries}


def _make_chain(session, n=3):
    from app.modules.ledger import repository as repo

    avail, hold = _seed_accounts(session)
    entries = [
        repo.post_entry(
            session,
            entry_type="OWN_TRANSFER",
            postings=_valid_postings(avail.id, hold.id, 1_000 + i),
            value_date=date(2026, 9, 18),
        )
        for i in range(n)
    ]
    return entries, _post_map(session, entries)


def test_valid_chain(sqlite_session: Session):
    from app.modules.ledger import service

    entries, post_map = _make_chain(sqlite_session, 3)
    assert service.verify_chain(entries, post_map) is True
    detailed = service.verify_chain_detailed(entries, post_map)
    assert detailed == {
        "valid": True,
        "reason": None,
        "failed_entry_id": None,
        "checked": 3,
    }


def test_empty_chain_is_valid():
    from app.modules.ledger import service

    assert service.verify_chain([], {}) is True


def test_reversed_chain_still_valid(sqlite_session: Session):
    from app.modules.ledger import repository as repo
    from app.modules.ledger import service

    avail, hold = _seed_accounts(sqlite_session)
    original = repo.post_entry(
        sqlite_session,
        entry_type="OWN_TRANSFER",
        postings=_valid_postings(avail.id, hold.id, 5_000),
    )
    comp = repo.reverse_entry(sqlite_session, original.id)
    entries = [original, comp]
    assert service.verify_chain(entries, _post_map(sqlite_session, entries)) is True


def test_tampered_description_detected(sqlite_session: Session):
    from app.modules.ledger import service

    entries, post_map = _make_chain(sqlite_session, 3)
    entries[1].description = "monto manipulado"
    assert service.verify_chain(entries, post_map) is False
    detailed = service.verify_chain_detailed(entries, post_map)
    assert detailed["valid"] is False
    assert detailed["reason"] == "hash_mismatch"
    assert detailed["failed_entry_id"] == str(entries[1].id)


def test_tampered_posting_amount_detected(sqlite_session: Session):
    from app.modules.ledger import service

    entries, post_map = _make_chain(sqlite_session, 2)
    tampered = copy.deepcopy(post_map)
    first_posting = tampered[entries[0].id][0]
    first_posting.amount_minor = first_posting.amount_minor + 1
    assert service.verify_chain(entries, tampered) is False
    assert service.verify_chain_detailed(entries, tampered)["reason"] == "hash_mismatch"


def test_tampered_hash_detected(sqlite_session: Session):
    from app.modules.ledger import service

    entries, post_map = _make_chain(sqlite_session, 2)
    entries[0].hash = "0" * 64
    assert service.verify_chain(entries, post_map) is False


def test_reordered_chain_detected(sqlite_session: Session):
    """Intercambiar `prev_hash` entre eslabones rompe los enlaces."""
    from app.modules.ledger import service

    entries, post_map = _make_chain(sqlite_session, 3)
    entries[1].prev_hash, entries[2].prev_hash = (
        entries[2].prev_hash,
        entries[1].prev_hash,
    )
    assert service.verify_chain(entries, post_map) is False
    reason = service.verify_chain_detailed(entries, post_map)["reason"]
    assert reason in ("link_break", "hash_mismatch", "orphan_entries")


def test_deleted_entry_detected(sqlite_session: Session):
    """Quitar el eslabon intermedio deja un `prev_hash` huerfano."""
    from app.modules.ledger import service

    entries, post_map = _make_chain(sqlite_session, 3)
    subset = [entries[0], entries[2]]
    sub_map = {e.id: post_map[e.id] for e in subset}
    assert service.verify_chain(subset, sub_map) is False
    detailed = service.verify_chain_detailed(subset, sub_map)
    assert detailed["valid"] is False
    assert detailed["reason"] in ("link_break", "orphan_entries", "hash_mismatch")


def test_fork_detected(sqlite_session: Session):
    """Dos asientos colgando del mismo `prev_hash` es bifurcacion."""
    from app.modules.ledger import service

    entries, post_map = _make_chain(sqlite_session, 2)
    entries[1].prev_hash = entries[0].prev_hash
    assert service.verify_chain(entries, post_map) is False


def test_missing_genesis_detected(sqlite_session: Session):
    from app.modules.ledger import service

    entries, post_map = _make_chain(sqlite_session, 2)
    entries[0].prev_hash = "f" * 64
    assert service.verify_chain(entries, post_map) is False
    assert service.verify_chain_detailed(entries, post_map)["reason"] in (
        "missing_genesis",
        "link_break",
        "hash_mismatch",
    )


def test_detailed_has_no_pii(sqlite_session: Session):
    import json

    from app.modules.ledger import service

    entries, post_map = _make_chain(sqlite_session, 2)
    entries[1].description = "alterada"
    detailed = service.verify_chain_detailed(entries, post_map)
    blob = json.dumps(detailed, sort_keys=True, default=str)
    assert "alterada" not in blob
    assert set(detailed) == {"valid", "reason", "failed_entry_id", "checked"}


def test_verify_does_not_modify_records(sqlite_session: Session):
    from app.modules.ledger import service

    entries, post_map = _make_chain(sqlite_session, 2)
    before = [(e.id, e.entry_type, e.description, e.prev_hash, e.hash) for e in entries]
    assert service.verify_chain(entries, post_map) is True
    after = [(e.id, e.entry_type, e.description, e.prev_hash, e.hash) for e in entries]
    assert before == after
    assert _post_map(sqlite_session, entries).keys() == post_map.keys()


def test_verify_ledger_chain_db_helper(sqlite_session: Session):
    from app.modules.ledger import service

    entries, _ = _make_chain(sqlite_session, 3)
    assert service.verify_ledger_chain(sqlite_session) is True
    assert service.verify_ledger_chain_detailed(sqlite_session)["checked"] == 3
    # Manipulacion persistida (simula UPDATE malicioso) se detecta.
    entries[1].description = "evil"
    sqlite_session.flush()
    sqlite_session.expire_all()
    assert service.verify_ledger_chain(sqlite_session) is False
