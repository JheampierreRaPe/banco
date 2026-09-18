"""Reintento portable de `audit.service.record` ante colision de `seq` (E1-T04).

Simula la carrera `max(seq)+1`: pre-inserta `seq=1` y fuerza un primer
intento con `seq` obsoleto (colision UQ); verifica que `record` revierte al
savepoint, recomputa y persiste con `seq` valido y cadena intacta.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base


@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schema `audit` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.audit.models as _a  # noqa: F401 (registro)

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS audit")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["audit.audit_log"]])
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_record_retries_on_seq_collision(sqlite_session: Session, monkeypatch):
    from app.modules.audit import repository as audit_repo
    from app.modules.audit.service import record, verify_chain

    first = record(
        sqlite_session,
        actor=uuid.uuid4(),
        action="kyc.verified",
        entity="kyc_verification",
        entity_id=uuid.uuid4(),
        metadata={"overall_result": True},
    )
    assert first.seq == 1

    real_latest_seq = audit_repo.latest_seq
    calls = {"n": 0}

    def _stale_once(session: Session) -> int:
        # Primer intento: `seq` obsoleto (0 -> intenta seq=1, colisiona con la
        # fila pre-insertada); el savepoint revierte solo ese intento y el
        # reintento recomputa el maximo real. Resto: valor real.
        calls["n"] += 1
        if calls["n"] == 1:
            return 0
        return real_latest_seq(session)

    monkeypatch.setattr(audit_repo, "latest_seq", _stale_once)

    second = record(
        sqlite_session,
        actor=uuid.uuid4(),
        action="kyc.failed",
        entity="kyc_verification",
        entity_id=uuid.uuid4(),
        metadata={"overall_result": False},
    )
    assert calls["n"] >= 2, "debe haber reintentado tras la colision"
    assert second.seq == 2
    assert second.prev_hash == first.hash

    rows = list(
        sqlite_session.scalars(
            sa.select(audit_repo.AuditLog).order_by(audit_repo.AuditLog.seq.asc())
        ).all()
    )
    assert [row.seq for row in rows] == [1, 2]
    assert verify_chain(rows) is True
