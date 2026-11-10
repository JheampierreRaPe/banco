"""Cableado en produccion del consumidor de `movements_view` (E2-T23, HU05).

- Emision: la fachada `ledger.service.post_entry`/`reverse_entry` registra
  `ledger.entry.posted` en el outbox en la misma transaccion del asiento.
- Handler `on_ledger_entry_posted(entry)`: abre su propia sesion con
  `commit` propio, deriva `event_id = entry.id` y delega en
  `handle_movement_event` (el bus entrega solo el `entry`, sin sesion).
- Arranque: `subscribe_accounts_movements` + worker `run_outbox_once` /
  `start_outbox_worker` (sesion/`commit` propios, sin bloquear).
- Idempotencia: reproceso con el mismo `event_id` no duplica; dos workers
  no duplican (claim atomico); el canal solo alimenta la tabla de lectura
  (sin doble conteo: no toca `available`/`held`).

SQLite con archivos temporales por schema (ATTACH): cada sesion tiene su
propia conexion, como en produccion (commits independientes).
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

TABLES = [
    "accounts.accounts",
    "accounts.account_balances",
    "accounts.movements_view",
    "ledger.ledger_accounts",
    "ledger.journal_entries",
    "ledger.postings",
    "ledger.ledger_balances",
    "shared.outbox",
    "shared.processed_events",
]


@pytest.fixture()
def file_engine(tmp_path):
    """Engine SQLite con schemas en archivos (una sola conexion compartida).

    `StaticPool` + una unica conexion: todas las sesiones (incluida la
    propia del handler) comparten la conexion, como las row-locks de
    Postgres en produccion (el worker mantiene el claim sin bloquear al
    consumidor). Sin esto, SQLite serializa por archivo y el segundo
    escritor falla con `database is locked`.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401 (registro)
    import app.modules.accounts.repository.movements as _m  # noqa: F401
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401

    paths = {name: tmp_path / f"{name}.db" for name in ("main", "accounts", "ledger", "shared")}
    engine = create_engine(
        f"sqlite:///{paths['main']}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "ledger", "shared"):
            cur.execute(f"ATTACH DATABASE '{paths[schema]}' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def make_session(file_engine):
    """Fabrica de sesiones independientes sobre el mismo engine."""

    def _factory() -> Session:
        return Session(bind=file_engine, autoflush=False, expire_on_commit=False)

    return _factory


def _new_account(session, **kwargs):
    from app.modules.accounts import repository as repo

    params = {
        "user_id": uuid.uuid4(),
        "account_number": f"023-{uuid.uuid4().hex[:8]}",
        "type": "AHORRO",
        "currency": "PEN",
    }
    params.update(kwargs)
    return repo.create_account(session, **params)


def _transfer_postings(source, target, amount=10_000):
    return [
        {
            "ledger_account_id": source.ledger_account_id,
            "direction": "DEBIT",
            "amount_minor": amount,
            "currency": "PEN",
            "account_ref": source.id,
        },
        {
            "ledger_account_id": target.ledger_account_id,
            "direction": "CREDIT",
            "amount_minor": amount,
            "currency": "PEN",
            "account_ref": target.id,
        },
    ]


def _outbox_rows(session):
    from app.modules.shared.models import OutboxEntry

    return list(session.scalars(sa.select(OutboxEntry).order_by(OutboxEntry.created_at)).all())


# ------------------------------------------------- Emision (ledger.service)
def test_post_entry_registra_ledger_entry_posted_en_outbox(make_session):
    from app.core import events as domain_events
    from app.modules.ledger import service as ledger_service

    session = make_session()
    try:
        source = _new_account(session)
        target = _new_account(session)
        entry = ledger_service.post_entry(
            session,
            entry_type="TRANSFER_SETTLE",
            postings=_transfer_postings(source, target),
            transaction_id=uuid.uuid4(),
            description="Transferencia",
            value_date=date(2026, 9, 17),
        )
        rows = [
            e for e in _outbox_rows(session) if e.event_type == domain_events.LEDGER_ENTRY_POSTED
        ]
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "PENDING" and row.aggregate_type == "journal_entry"
        assert row.aggregate_id == entry.id
        payload = row.payload
        assert payload["entry_id"] == str(entry.id)
        assert payload["description"] == "Transferencia"
        assert payload["value_date"] == "2026-09-17"
        assert payload["transaction_id"] == str(entry.transaction_id)
        assert len(payload["postings"]) == 2
        for item in payload["postings"]:
            assert set(item) == {
                "ledger_account_id",
                "direction",
                "amount_minor",
                "currency",
                "account_ref",
            }
            uuid.UUID(item["ledger_account_id"])
        # Misma transaccion: el rollback revierte asiento + outbox juntos.
        session.rollback()
        assert session.get(type(entry), entry.id) is None
        assert _outbox_rows(session) == []
    finally:
        session.close()


def test_reverse_entry_emite_evento_del_compensatorio(make_session):
    from app.core import events as domain_events
    from app.modules.ledger import service as ledger_service

    session = make_session()
    try:
        source = _new_account(session)
        target = _new_account(session)
        entry = ledger_service.post_entry(
            session,
            entry_type="TRANSFER_SETTLE",
            postings=_transfer_postings(source, target),
            value_date=date(2026, 9, 17),
        )
        comp = ledger_service.reverse_entry(session, entry.id, value_date=date(2026, 9, 18))
        rows = [
            e for e in _outbox_rows(session) if e.event_type == domain_events.LEDGER_ENTRY_POSTED
        ]
        assert len(rows) == 2
        assert {r.aggregate_id for r in rows} == {entry.id, comp.id}
        comp_payload = next(r.payload for r in rows if r.aggregate_id == comp.id)
        assert comp_payload["entry_id"] == str(comp.id)
        assert comp_payload["value_date"] == "2026-09-18"
    finally:
        session.close()


# ------------------------------------------------- End-to-end via worker + handler
def _wired_bus(make_session):
    """Bus con el handler real atado a sesiones del engine de prueba."""
    from app.core import events as domain_events
    from app.core.bus import EventBus
    from app.modules.accounts.events import on_ledger_entry_posted

    bus = EventBus()
    bus.subscribe(
        domain_events.LEDGER_ENTRY_POSTED,
        lambda e: on_ledger_entry_posted(e, session_factory=make_session),
    )
    return bus


def test_end_to_end_asiento_a_movements_via_worker(make_session):
    from app.composition import run_outbox_once
    from app.modules.accounts import repository as repo
    from app.modules.ledger import service as ledger_service

    session = make_session()
    try:
        source = _new_account(session, account_number="023-0001")
        target = _new_account(session, account_number="023-0002")
        tx_id = uuid.uuid4()
        entry = ledger_service.post_entry(
            session,
            entry_type="TRANSFER_SETTLE",
            postings=_transfer_postings(source, target),
            transaction_id=tx_id,
            description="Transferencia",
            value_date=date(2026, 9, 17),
        )
        entry_id = entry.id
        session.commit()
    finally:
        session.close()

    result = run_outbox_once(session_factory=make_session, bus=_wired_bus(make_session))
    assert result == {"published": 1, "failed": 0, "skipped": 0}

    check = make_session()
    try:
        debit = repo.list_movements(check, source.id)
        assert len(debit) == 1
        assert (debit[0].direction, debit[0].amount_minor, debit[0].currency) == (
            "DEBIT",
            10_000,
            "PEN",
        )
        assert debit[0].journal_entry_id == entry_id
        assert debit[0].transaction_id == tx_id
        assert debit[0].description == "Transferencia"
        assert debit[0].value_date == date(2026, 9, 17)
        credit = repo.list_movements(check, target.id)
        assert len(credit) == 1
        assert (credit[0].direction, credit[0].amount_minor) == ("CREDIT", 10_000)
        assert credit[0].journal_entry_id == entry_id
        # Idempotencia registrada con event_id = outbox id.
        from app.modules.shared.repository import is_processed

        (outbox_id,) = [e.id for e in _outbox_rows(check)]
        assert is_processed(check, event_id=outbox_id, consumer="accounts-movements")
    finally:
        check.close()


def test_reproceso_y_segunda_pasada_no_duplican(make_session):
    from app.composition import run_outbox_once
    from app.modules.accounts import repository as repo
    from app.modules.ledger import service as ledger_service

    session = make_session()
    try:
        source = _new_account(session)
        target = _new_account(session)
        ledger_service.post_entry(
            session,
            entry_type="TRANSFER_SETTLE",
            postings=_transfer_postings(source, target),
            value_date=date(2026, 9, 17),
        )
        session.commit()
        source_id, target_id = source.id, target.id
    finally:
        session.close()

    bus = _wired_bus(make_session)
    assert run_outbox_once(session_factory=make_session, bus=bus)["published"] == 1
    again = run_outbox_once(session_factory=make_session, bus=bus)
    assert again == {"published": 0, "failed": 0, "skipped": 0}

    check = make_session()
    try:
        assert len(repo.list_movements(check, source_id)) == 1
        assert len(repo.list_movements(check, target_id)) == 1
    finally:
        check.close()


def test_sin_doble_conteo_no_toca_available_ni_held(make_session):
    from app.composition import run_outbox_once
    from app.modules.accounts import repository as repo
    from app.modules.ledger import service as ledger_service

    session = make_session()
    try:
        source = _new_account(session)
        target = _new_account(session)
        repo.apply_delta(session, source.id, available_delta=50_000, currency="PEN")
        ledger_service.post_entry(
            session,
            entry_type="TRANSFER_SETTLE",
            postings=_transfer_postings(source, target),
            value_date=date(2026, 9, 17),
        )
        session.commit()
        source_id = source.id
    finally:
        session.close()

    run_outbox_once(session_factory=make_session, bus=_wired_bus(make_session))

    check = make_session()
    try:
        row = repo.get_balance(check, source_id)
        assert (row.available_minor, row.held_minor) == (50_000, 0)
        assert len(repo.list_movements(check, source_id)) == 1
    finally:
        check.close()


def test_dos_workers_no_duplican_proyeccion(make_session):
    from app.core.bus import EventBus
    from app.modules.accounts import repository as repo
    from app.modules.ledger import service as ledger_service
    from app.modules.shared.jobs import publish_pending

    setup = make_session()
    try:
        source = _new_account(setup)
        target = _new_account(setup)
        ledger_service.post_entry(
            session=setup,
            entry_type="TRANSFER_SETTLE",
            postings=_transfer_postings(source, target),
            value_date=date(2026, 9, 17),
        )
        setup.commit()
        source_id, target_id = source.id, target.id
    finally:
        setup.close()

    from app.core import events as domain_events
    from app.modules.accounts.events import on_ledger_entry_posted

    bus = EventBus()
    bus.subscribe(
        domain_events.LEDGER_ENTRY_POSTED,
        lambda e: on_ledger_entry_posted(e, session_factory=make_session),
    )
    first, second = make_session(), make_session()
    try:
        assert publish_pending(first, bus=bus)["published"] == 1
        first.commit()
        # El segundo worker ya no encuentra pendiente: claim atomico.
        assert publish_pending(second, bus=bus) == {"published": 0, "failed": 0, "skipped": 0}
        second.commit()
    finally:
        first.close()
        second.close()

    check = make_session()
    try:
        assert len(repo.list_movements(check, source_id)) == 1
        assert len(repo.list_movements(check, target_id)) == 1
    finally:
        check.close()


def test_fallo_del_consumidor_reintenta_y_pasa_a_failed(make_session):
    from app.core import events as domain_events
    from app.core.bus import EventBus
    from app.modules.accounts import repository as repo
    from app.modules.ledger import service as ledger_service
    from app.modules.shared.jobs import publish_pending
    from app.modules.shared.models import OutboxEntry

    setup = make_session()
    try:
        source = _new_account(setup)
        target = _new_account(setup)
        ledger_service.post_entry(
            session=setup,
            entry_type="TRANSFER_SETTLE",
            postings=_transfer_postings(source, target),
            value_date=date(2026, 9, 17),
        )
        setup.commit()
        source_id = source.id
    finally:
        setup.close()

    bus = EventBus()

    def _boom(_entry):
        raise RuntimeError("caida del consumidor")

    bus.subscribe(domain_events.LEDGER_ENTRY_POSTED, _boom)
    worker = make_session()
    try:
        first = publish_pending(worker, bus=bus, max_attempts=2, backoff_base_seconds=0)
        assert first["failed"] == 1
        worker.commit()
        stored = worker.get(OutboxEntry, next(e.id for e in _outbox_rows(worker)))
        assert stored.status == "PENDING" and stored.attempts == 1
        second = publish_pending(worker, bus=bus, max_attempts=2, backoff_base_seconds=0)
        assert second["failed"] == 1
        worker.commit()
        stored = worker.get(OutboxEntry, next(e.id for e in _outbox_rows(worker)))
        assert stored.status == "FAILED" and stored.attempts == 2
        third = publish_pending(worker, bus=bus, max_attempts=2, backoff_base_seconds=0)
        assert third == {"published": 0, "failed": 0, "skipped": 0}
        assert repo.list_movements(worker, source_id) == []
    finally:
        worker.close()


def test_asiento_solo_cuentas_del_sistema_se_omite_sin_error(make_session):
    from app.composition import run_outbox_once
    from app.modules.ledger import repository as ledger_repo
    from app.modules.ledger import service as ledger_service

    setup = make_session()
    try:
        catalog = ledger_repo.ensure_system_catalog(setup)
        sys_a, sys_b = catalog["4000"], catalog["5000"]
        ledger_service.post_entry(
            session=setup,
            entry_type="ADJUSTMENT",
            postings=[
                {
                    "ledger_account_id": sys_a.id,
                    "direction": "DEBIT",
                    "amount_minor": 5_000,
                    "currency": "PEN",
                    "account_ref": None,
                },
                {
                    "ledger_account_id": sys_b.id,
                    "direction": "CREDIT",
                    "amount_minor": 5_000,
                    "currency": "PEN",
                    "account_ref": None,
                },
            ],
            value_date=date(2026, 9, 17),
        )
        setup.commit()
    finally:
        setup.close()

    result = run_outbox_once(session_factory=make_session, bus=_wired_bus(make_session))
    assert result == {"published": 1, "failed": 0, "skipped": 0}

    check = make_session()
    try:
        from app.modules.accounts.repository.movements import MovementView

        assert check.scalar(sa.select(sa.func.count()).select_from(MovementView)) == 0
    finally:
        check.close()


# ------------------------------------------------- Handler: sesion propia (D-04)
def test_handler_abre_su_propia_sesion_con_commit_propio(make_session):
    from app.modules.accounts import repository as repo
    from app.modules.accounts.events import on_ledger_entry_posted
    from app.modules.ledger import service as ledger_service

    setup = make_session()
    try:
        source = _new_account(setup)
        target = _new_account(setup)
        ledger_service.post_entry(
            session=setup,
            entry_type="TRANSFER_SETTLE",
            postings=_transfer_postings(source, target),
            value_date=date(2026, 9, 17),
        )
        setup.commit()
        source_id = source.id
    finally:
        setup.close()

    created: list = []

    class _Tracking:
        def __init__(self, inner: Session) -> None:
            self._inner = inner
            self.commits = 0
            self.rollbacks = 0
            self.closes = 0

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def commit(self) -> None:
            self.commits += 1
            self._inner.commit()

        def rollback(self) -> None:
            self.rollbacks += 1
            self._inner.rollback()

        def close(self) -> None:
            self.closes += 1
            self._inner.close()

    def _factory():
        tracking = _Tracking(make_session())
        created.append(tracking)
        return tracking

    reader = make_session()
    try:
        from app.core import events as domain_events

        outbox_entry = next(
            e for e in _outbox_rows(reader) if e.event_type == domain_events.LEDGER_ENTRY_POSTED
        )
    finally:
        reader.close()

    # El bus entrega solo el entry (un unico argumento posicional).
    assert on_ledger_entry_posted(outbox_entry, session_factory=_factory) is True
    assert len(created) == 1
    assert created[0].commits == 1 and created[0].rollbacks == 0 and created[0].closes == 1

    check = make_session()
    try:
        assert len(repo.list_movements(check, source_id)) == 1
    finally:
        check.close()


def test_handler_revierte_y_relanza_ante_fallo(make_session):
    from types import SimpleNamespace

    from app.modules.accounts.events import on_ledger_entry_posted

    fake_entry = SimpleNamespace(
        id=uuid.uuid4(),
        payload={"entry_id": str(uuid.uuid4()), "postings": [{"direction": "DEBIT"}]},
    )

    states: dict = {}

    class _Failing:
        def commit(self):
            states["commit"] = True

        def rollback(self):
            states["rollback"] = True

        def close(self):
            states["close"] = True

        def begin_nested(self):
            raise RuntimeError("BD caida")

    with pytest.raises(RuntimeError):
        on_ledger_entry_posted(fake_entry, session_factory=_Failing)
    assert states.get("rollback") is True and states.get("close") is True


# ------------------------------------------------- Arranque (suscripcion + worker)
def test_arranque_suscribe_el_handler_idempotente():
    from app.composition import is_movements_wired, subscribe_accounts_movements
    from app.core import events as domain_events
    from app.core.bus import EventBus
    from app.modules.accounts.events import on_ledger_entry_posted

    bus = EventBus()
    assert bus.handler_count(domain_events.LEDGER_ENTRY_POSTED) == 0
    assert is_movements_wired(bus) is False
    assert subscribe_accounts_movements(bus) is on_ledger_entry_posted
    assert bus.handler_count(domain_events.LEDGER_ENTRY_POSTED) == 1
    assert is_movements_wired(bus) is True
    subscribe_accounts_movements(bus)
    assert bus.handler_count(domain_events.LEDGER_ENTRY_POSTED) == 1


def test_arranque_en_bus_global_y_retenidos_no_suscritos():
    from app.composition import is_movements_wired, subscribe_accounts_movements
    from app.core import bus as bus_mod
    from app.core import events as domain_events
    from app.modules.accounts import repository as repo

    handler = subscribe_accounts_movements()
    try:
        assert is_movements_wired() is True
        assert bus_mod.get_default_bus().handler_count(domain_events.LEDGER_ENTRY_POSTED) >= 1
        # El canal de retenidos sigue sin suscribirse (evita doble conteo).
        for registered in bus_mod.get_default_bus()._handlers.get(
            domain_events.LEDGER_ENTRY_POSTED, []
        ):
            assert getattr(
                registered, "__name__", ""
            ) != repo.handle_ledger_entry_posted.__name__ or (
                registered is not repo.handle_ledger_entry_posted
            )
    finally:
        bus_mod.get_default_bus().unsubscribe(domain_events.LEDGER_ENTRY_POSTED, handler)


def test_worker_loop_publica_en_segundo_plano(make_session):
    import time

    from app.composition import run_outbox_once, start_outbox_worker
    from app.modules.accounts import repository as repo
    from app.modules.ledger import service as ledger_service

    setup = make_session()
    try:
        source = _new_account(setup)
        target = _new_account(setup)
        ledger_service.post_entry(
            session=setup,
            entry_type="TRANSFER_SETTLE",
            postings=_transfer_postings(source, target),
            value_date=date(2026, 9, 17),
        )
        setup.commit()
        source_id = source.id
    finally:
        setup.close()

    bus = _wired_bus(make_session)
    thread, stopper = start_outbox_worker(
        session_factory=make_session, bus=bus, interval_seconds=0.05
    )
    try:
        # El worker corre solo en segundo plano: esperar sin tocar la BD
        # desde este hilo (una sola conexion compartida en SQLite).
        time.sleep(2.0)
        assert thread.daemon is True
        assert thread.is_alive()
    finally:
        stopper.set()
        thread.join(timeout=5)

    check = make_session()
    try:
        assert len(repo.list_movements(check, source_id)) == 1
    finally:
        check.close()

    # Una pasada manual confirma que no queda pendiente duplicado.
    assert run_outbox_once(session_factory=make_session, bus=bus)["published"] == 0
