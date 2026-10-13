"""`entry_type` en movimientos (E2-T35, HU05/HU06/HU08).

- El payload de `ledger.entry.posted` incluye `entry_type` (solo emision;
  el motor de dinero no cambia).
- `handle_movement_event` guarda `entry_type` por fila; `to_movement` /
  `list_account_movements` / `GET /accounts/{id}/movements` lo exponen.
- Atribucion (E2-T35): una pata de libro de sistema/transito
  (`2200-Transito`, `1000-Banco`, `4100-Comisiones`) con `account_ref` del
  origen NO genera fila en la cuenta del cliente; la pata real
  (`2000`/`2100` del cliente) si. Las patas `2200->1000` de
  `INTERBANK_SETTLE` no se proyectan; la pata de comision del cliente si.
- Idempotencia (mismo `event_id` no duplica) y `rebuild_entry_movements`
  reconstruye con `entry_type`.
- Migracion `0027_movements_entry_type` existe con `upgrade`/`downgrade`.

SQLite en memoria + schemas ATTACH (patron de `test_accounts_movements.py`);
sin Postgres, sin red. Sin PII ni secretos en los datos.
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0027_movements_entry_type.py"
)


@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schemas `accounts`/`ledger`/`shared` (ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401
    import app.modules.accounts.repository.movements as _m  # noqa: F401
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "ledger", "shared"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["accounts.accounts"],
            Base.metadata.tables["accounts.account_balances"],
            Base.metadata.tables["accounts.movements_view"],
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["ledger.journal_entries"],
            Base.metadata.tables["ledger.postings"],
            Base.metadata.tables["ledger.ledger_balances"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["shared.processed_events"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _new_account(session, **kwargs):
    from app.modules.accounts import repository as repo

    params = {
        "user_id": uuid.uuid4(),
        "account_number": f"035-{uuid.uuid4().hex[:8]}",
        "type": "AHORRO",
        "currency": "PEN",
    }
    params.update(kwargs)
    return repo.create_account(session, **params)


# ------------------------------------------------- Emision (ledger, solo payload)
def test_emit_entry_posted_payload_incluye_entry_type(sqlite_session: Session):
    """`ledger.entry.posted` lleva `entry_type` sin quitar ni renombrar claves."""
    from app.core import events as domain_events
    from app.modules.ledger import service as ledger_service
    from app.modules.ledger.models import JournalEntry, Posting
    from app.modules.shared.models import OutboxEntry

    entry = JournalEntry(
        id=uuid.uuid4(),
        transaction_id=uuid.uuid4(),
        entry_type="TRANSFER_SETTLE",
        description="settle tx",
        value_date=date(2026, 10, 9),
        status="POSTED",
        hash="x" * 64,
    )
    sqlite_session.add(entry)
    sqlite_session.flush()
    sqlite_session.add(
        Posting(
            journal_entry_id=entry.id,
            ledger_account_id=uuid.uuid4(),
            direction="DEBIT",
            amount_minor=10_000,
            currency="PEN",
            account_ref=None,
        )
    )
    sqlite_session.flush()

    assert ledger_service._emit_entry_posted(sqlite_session, entry) is True
    rows = list(
        sqlite_session.scalars(
            sa.select(OutboxEntry).where(
                OutboxEntry.event_type == domain_events.LEDGER_ENTRY_POSTED
            )
        ).all()
    )
    assert len(rows) == 1
    payload = rows[0].payload
    assert set(payload) == {
        "entry_id",
        "entry_type",
        "transaction_id",
        "description",
        "value_date",
        "postings",
    }
    assert payload["entry_type"] == "TRANSFER_SETTLE"
    assert payload["entry_id"] == str(entry.id)


# ------------------------------------------------- Proyeccion: guarda y expone
def test_handle_guarda_entry_type_y_servicio_lo_expone(sqlite_session: Session):
    """La fila guarda `entry_type`; `to_movement` y el caso de uso lo exponen."""
    from app.modules.accounts import repository as repo
    from app.modules.accounts import service as accounts_service

    source = _new_account(sqlite_session, account_number="035-0001")
    target = _new_account(sqlite_session, account_number="035-0002")
    entry_id, tx_id = uuid.uuid4(), uuid.uuid4()
    assert (
        repo.handle_movement_event(
            sqlite_session,
            event_id=uuid.uuid4(),
            entry_id=entry_id,
            postings=[
                {
                    "ledger_account_id": source.ledger_account_id,
                    "direction": "DEBIT",
                    "amount_minor": 10_000,
                    "currency": "PEN",
                    "account_ref": source.id,
                },
                {
                    "ledger_account_id": target.ledger_account_id,
                    "direction": "CREDIT",
                    "amount_minor": 10_000,
                    "currency": "PEN",
                    "account_ref": target.id,
                },
            ],
            transaction_id=tx_id,
            description="Transferencia",
            entry_type="TRANSFER_SETTLE",
            value_date=date(2026, 10, 9),
        )
        is True
    )
    rows = repo.list_movements(sqlite_session, source.id)
    assert len(rows) == 1
    assert rows[0].entry_type == "TRANSFER_SETTLE"
    assert accounts_service.to_movement(rows[0]).entry_type == "TRANSFER_SETTLE"

    items, total = accounts_service.list_account_movements(
        sqlite_session, source.user_id, source.id
    )
    assert total == 1
    assert items[0].entry_type == "TRANSFER_SETTLE"
    assert items[0].model_dump(mode="json")["entry_type"] == "TRANSFER_SETTLE"


def test_filas_legacy_quedan_entry_type_null(sqlite_session: Session):
    """Eventos sin `entry_type` (legacy) proyectan con `NULL` (fallback en cliente)."""
    from app.modules.accounts import repository as repo

    source = _new_account(sqlite_session, account_number="035-0003")
    assert (
        repo.handle_movement_event(
            sqlite_session,
            event_id=uuid.uuid4(),
            entry_id=uuid.uuid4(),
            postings=[
                {
                    "ledger_account_id": source.ledger_account_id,
                    "direction": "CREDIT",
                    "amount_minor": 5_000,
                    "currency": "PEN",
                    "account_ref": source.id,
                }
            ],
            description="Abono legacy",
        )
        is True
    )
    rows = repo.list_movements(sqlite_session, source.id)
    assert len(rows) == 1
    assert rows[0].entry_type is None


# ------------------------------------------------- Atribucion: sistema/transito
def test_pata_de_transito_con_account_ref_no_se_proyecta(sqlite_session: Session):
    """`INTERBANK_SEND` (`2100-A -> 2200-Transito`): solo la pata real llega.

    La pata `2200` lleva `account_ref` del origen pero su `ledger_account_id`
    no es cuenta de cliente: se omite (antes se atribuia al cliente y el
    asiento aparecia DEBIT+CREDIT neteando 0 con signo descuadrado).
    """
    from app.modules.accounts import repository as repo

    source = _new_account(sqlite_session, account_number="035-0004")
    transit_id = uuid.uuid4()  # libro de sistema: no es cuenta de cliente
    assert (
        repo.handle_movement_event(
            sqlite_session,
            event_id=uuid.uuid4(),
            entry_id=uuid.uuid4(),
            postings=[
                {
                    "ledger_account_id": source.ledger_hold_account_id,
                    "direction": "DEBIT",
                    "amount_minor": 50_000,
                    "currency": "PEN",
                    "account_ref": source.id,
                },
                {
                    "ledger_account_id": transit_id,
                    "direction": "CREDIT",
                    "amount_minor": 50_000,
                    "currency": "PEN",
                    "account_ref": source.id,
                },
            ],
            description="interbank send",
            entry_type="INTERBANK_SEND",
            value_date=date(2026, 10, 9),
        )
        is True
    )
    rows = repo.list_movements(sqlite_session, source.id)
    assert len(rows) == 1
    assert (rows[0].direction, rows[0].amount_minor) == ("DEBIT", 50_000)
    assert rows[0].entry_type == "INTERBANK_SEND"


def test_settle_interbancario_solo_proyecta_la_comision(sqlite_session: Session):
    """`INTERBANK_SETTLE` (`2200->1000` + fee `2100->4100`): solo el fee toca.

    Las patas `2200-Transito`/`1000-Banco` no se proyectan a la cuenta del
    cliente aunque lleven su `account_ref`; la pata DEBIT del fee (`2100`
    del cliente) si, y la pata `4100-Comisiones` no.
    """
    from app.modules.accounts import repository as repo

    source = _new_account(sqlite_session, account_number="035-0005")
    transit_id, bank_id, fee_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert (
        repo.handle_movement_event(
            sqlite_session,
            event_id=uuid.uuid4(),
            entry_id=uuid.uuid4(),
            postings=[
                {
                    "ledger_account_id": transit_id,
                    "direction": "DEBIT",
                    "amount_minor": 50_000,
                    "currency": "PEN",
                    "account_ref": source.id,
                },
                {
                    "ledger_account_id": bank_id,
                    "direction": "CREDIT",
                    "amount_minor": 50_000,
                    "currency": "PEN",
                    "account_ref": source.id,
                },
                {
                    "ledger_account_id": source.ledger_hold_account_id,
                    "direction": "DEBIT",
                    "amount_minor": 500,
                    "currency": "PEN",
                    "account_ref": source.id,
                },
                {
                    "ledger_account_id": fee_id,
                    "direction": "CREDIT",
                    "amount_minor": 500,
                    "currency": "PEN",
                    "account_ref": source.id,
                },
            ],
            description="interbank settle",
            entry_type="INTERBANK_SETTLE",
            value_date=date(2026, 10, 9),
        )
        is True
    )
    rows = repo.list_movements(sqlite_session, source.id)
    assert len(rows) == 1
    assert (rows[0].direction, rows[0].amount_minor) == ("DEBIT", 500)
    assert rows[0].entry_type == "INTERBANK_SETTLE"


def test_account_ref_solo_vale_sin_ledger_account_id(sqlite_session: Session):
    """`account_ref` se usa solo cuando `ledger_account_id` esta ausente."""
    from app.modules.accounts import repository as repo

    source = _new_account(sqlite_session, account_number="035-0006")
    entry_id = uuid.uuid4()
    assert (
        repo.handle_movement_event(
            sqlite_session,
            event_id=uuid.uuid4(),
            entry_id=entry_id,
            postings=[
                # Sin ledger_account_id: resuelve por account_ref.
                {
                    "ledger_account_id": None,
                    "direction": "CREDIT",
                    "amount_minor": 1_000,
                    "currency": "PEN",
                    "account_ref": source.id,
                },
            ],
            entry_type="OWN_TRANSFER",
        )
        is True
    )
    rows = repo.list_movements(sqlite_session, source.id)
    assert len(rows) == 1
    assert rows[0].entry_type == "OWN_TRANSFER"


def test_hold_proyecta_ambas_patas_para_ocultar_en_cliente(sqlite_session: Session):
    """`TRANSFER_HOLD` (`2000->2100` del cliente) proyecta sus 2 patas.

    La visibilidad la decide el cliente (filtro de `entry_type` interno);
    el backend conserva la verdad contable completa.
    """
    from app.modules.accounts import repository as repo

    source = _new_account(sqlite_session, account_number="035-0007")
    assert (
        repo.handle_movement_event(
            sqlite_session,
            event_id=uuid.uuid4(),
            entry_id=uuid.uuid4(),
            postings=[
                {
                    "ledger_account_id": source.ledger_account_id,
                    "direction": "DEBIT",
                    "amount_minor": 8_000,
                    "currency": "PEN",
                    "account_ref": source.id,
                },
                {
                    "ledger_account_id": source.ledger_hold_account_id,
                    "direction": "CREDIT",
                    "amount_minor": 8_000,
                    "currency": "PEN",
                    "account_ref": source.id,
                },
            ],
            description="hold tx",
            entry_type="TRANSFER_HOLD",
            value_date=date(2026, 10, 9),
        )
        is True
    )
    rows = repo.list_movements(sqlite_session, source.id)
    assert {(r.direction, r.entry_type) for r in rows} == {
        ("DEBIT", "TRANSFER_HOLD"),
        ("CREDIT", "TRANSFER_HOLD"),
    }


# ------------------------------------------------- Idempotencia y rebuild
def test_reproceso_y_rebuild_conservan_entry_type(sqlite_session: Session):
    """Reproceso no duplica; el rebuild restaura `entry_type` (gana el origen)."""
    import sqlalchemy as sa

    from app.modules.accounts import repository as repo
    from app.modules.accounts.repository.movements import MovementView

    source = _new_account(sqlite_session, account_number="035-0008")
    entry_id = uuid.uuid4()
    postings = [
        {
            "ledger_account_id": source.ledger_account_id,
            "direction": "DEBIT",
            "amount_minor": 3_000,
            "currency": "PEN",
            "account_ref": source.id,
        }
    ]
    event_id = uuid.uuid4()
    assert (
        repo.handle_movement_event(
            sqlite_session,
            event_id=event_id,
            entry_id=entry_id,
            postings=postings,
            entry_type="TRANSFER_SETTLE",
        )
        is True
    )
    assert (
        repo.handle_movement_event(
            sqlite_session,
            event_id=event_id,
            entry_id=entry_id,
            postings=postings,
            entry_type="TRANSFER_SETTLE",
        )
        is False
    )
    assert len(repo.list_movements(sqlite_session, source.id)) == 1

    sqlite_session.execute(sa.delete(MovementView).where(MovementView.journal_entry_id == entry_id))
    sqlite_session.flush()
    assert repo.list_movements(sqlite_session, source.id) == []
    assert (
        repo.rebuild_entry_movements(
            sqlite_session,
            entry_id=entry_id,
            postings=postings,
            entry_type="TRANSFER_SETTLE",
        )
        == 1
    )
    rows = repo.list_movements(sqlite_session, source.id)
    assert len(rows) == 1
    assert rows[0].entry_type == "TRANSFER_SETTLE"


def test_entry_type_invalido_se_rechaza(sqlite_session: Session):
    """`entry_type` con tipo o longitud invalidos no se proyecta en silencio."""
    from app.modules.accounts import repository as repo

    source = _new_account(sqlite_session, account_number="035-0009")
    postings = [
        {
            "ledger_account_id": source.ledger_account_id,
            "direction": "CREDIT",
            "amount_minor": 1_000,
            "currency": "PEN",
            "account_ref": source.id,
        }
    ]
    with pytest.raises(ValueError):
        repo.handle_movement_event(
            sqlite_session,
            event_id=uuid.uuid4(),
            entry_id=uuid.uuid4(),
            postings=postings,
            entry_type=123,
        )
    with pytest.raises(ValueError):
        repo.rebuild_entry_movements(
            sqlite_session,
            entry_id=uuid.uuid4(),
            postings=postings,
            entry_type="X" * 31,
        )


# ------------------------------------------------- Migracion 0027
def test_migration_0027_exists_and_scopes_accounts_only():
    """`0027_movements_entry_type`: revision acotada (<=32 chars, `VARCHAR(32)`)."""
    assert MIGRATION_PATH.exists(), "falta migracion 0027_movements_entry_type.py"
    assert len("0027_movements_entry_type") <= 32
    content = MIGRATION_PATH.read_text(encoding="utf-8")
    for token in (
        'revision = "0027_movements_entry_type"',
        'down_revision = "0026_drop_preapproved_limit"',
        '"movements_view"',
        '"entry_type"',
        "sa.String(30)",
        "nullable=True",
        "def upgrade",
        "def downgrade",
        "add_column",
        "drop_column",
    ):
        assert token in content, f"migracion sin {token}"
    code = content.split('"""', 2)[2] if content.count('"""') >= 2 else content
    for other in (
        'schema="ledger"',
        "schema='ledger'",
        'schema="transactions"',
        "journal_entries",
        "postings",
        "float(",
    ):
        assert other not in code, f"la migracion no debe tocar otro schema: {other}"


# ------------------------------------------------- Endpoint expone entry_type
def test_endpoint_movements_expone_entry_type():
    """`GET /accounts/{id}/movements` devuelve `entry_type` por item."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401 (registro de tablas)
    import app.modules.accounts.repository.movements as _m  # noqa: F401
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401
    import app.modules.transactions.models as _t  # noqa: F401
    from app.core.db import Base, get_db
    from app.main import app
    from app.modules.accounts import repository as repo
    from app.modules.accounts.api import get_current_user_id

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "ledger", "shared", "transactions"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["accounts.accounts"],
            Base.metadata.tables["accounts.account_balances"],
            Base.metadata.tables["accounts.daily_balance_snapshots"],
            Base.metadata.tables["accounts.movements_view"],
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["ledger.journal_entries"],
            Base.metadata.tables["ledger.postings"],
            Base.metadata.tables["ledger.ledger_balances"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["shared.processed_events"],
            Base.metadata.tables["transactions.transactions"],
            Base.metadata.tables["transactions.transaction_status_history"],
            Base.metadata.tables["transactions.holds"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        user_id = uuid.uuid4()
        account = repo.create_account(
            session,
            user_id=user_id,
            account_number="03510001111",
            type="AHORRO",
            currency="PEN",
        )
        assert (
            repo.handle_movement_event(
                session,
                event_id=uuid.uuid4(),
                entry_id=uuid.uuid4(),
                postings=[
                    {
                        "ledger_account_id": account.ledger_account_id,
                        "direction": "DEBIT",
                        "amount_minor": 10_000,
                        "currency": "PEN",
                        "account_ref": account.id,
                    }
                ],
                description="Transferencia",
                entry_type="TRANSFER_SETTLE",
                value_date=date(2026, 10, 9),
            )
            is True
        )
        session.commit()

        def _override_db():
            yield session

        def _override_user():
            return user_id

        from fastapi.testclient import TestClient

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_user_id] = _override_user
        try:
            with TestClient(app) as client:
                resp = client.get(f"/api/v1/accounts/{account.id}/movements")
                assert resp.status_code == 200
                body = resp.json()
                assert body["meta"]["total"] == 1
                assert body["data"][0]["entry_type"] == "TRANSFER_SETTLE"
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(get_current_user_id, None)
    finally:
        session.close()
        engine.dispose()
