"""Listado paginado del titular (E2-T24, HU06/HU15/HU17).

Sobre SQLite en memoria + schemas ATTACH (mismo patron que
`test_e2_t10_receipt.py`): dinero de juguete (UUIDs sinteticos, sin
PII), sin red, sin `float`.

Mapeo brief -> test:
- feliz + paginacion estable (`meta.total`, sin duplicados/huecos) .. `test_list_*`
- filtros fecha/tipo/estado (+ invalidos -> 422) .................. `test_list_*`
- aislamiento (cada titular ve solo las suyas) ................... `test_list_isolation_*`
- enmascarado (sin PII ni cuenta completa) ....................... `test_list_masking_*`
- vacio + solo lectura (repetir no altera) ....................... `test_list_*`
- errores 401/422 ............................................... `test_list_errors_*`
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()

TABLES = [
    "accounts.accounts",
    "accounts.account_balances",
    "accounts.daily_balance_snapshots",
    "ledger.ledger_accounts",
    "ledger.journal_entries",
    "ledger.postings",
    "ledger.ledger_balances",
    "shared.outbox",
    "shared.processed_events",
    "shared.idempotency_keys",
    "transactions.transactions",
    "transactions.transaction_status_history",
    "transactions.holds",
]

FULL_NUMBERS = ("00110001111", "00110002222", "00220003333")


def _make_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # registro de tablas
    import app.modules.ledger.models as _l
    import app.modules.shared.models as _s
    import app.modules.transactions.models as _t

    assert _a is not None and _l is not None and _s is not None and _t is not None
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "ledger", "shared", "transactions", "config"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    return engine


@pytest.fixture()
def svc_session():
    """Sesion SQLite aislada con cuentas fondeadas (sin PII)."""
    from app.modules.accounts import repository as accounts_repo

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        a1 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="00110001111", type="AHORRO", currency="PEN"
        )
        a2 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="00110002222", type="CORRIENTE", currency="PEN"
        )
        b1 = accounts_repo.create_account(
            session, user_id=USER_B, account_number="00220003333", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a1.id, available_delta=100_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a2.id, available_delta=1_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, b1.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        session.commit()
        yield {"session": session, "a1": a1.id, "a2": a2.id, "b1": b1.id}
    finally:
        session.close()
        engine.dispose()


class _Api:
    """Cliente + auth conmutable (un solo juego de overrides por prueba)."""

    def __init__(self, client, state, app, user_dep):
        self.client = client
        self._state = state
        self._app = app
        self._user_dep = user_dep

    def as_user(self, user_id):
        self._state["user"] = user_id
        self._app.dependency_overrides[self._user_dep] = self._override
        return self.client

    def as_anon(self):
        self._app.dependency_overrides.pop(self._user_dep, None)
        return self.client

    def _override(self):
        return self._state["user"]


@pytest.fixture()
def api(svc_session, monkeypatch):
    """TestClient con BD de `svc_session`; auth falsa conmutable (defecto USER_A)."""
    from app.core.db import get_db
    from app.main import app
    from app.modules.transactions.api import get_current_user_id

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    session = svc_session["session"]
    state = {"user": USER_A}
    holder: dict = {}

    def _override_db():
        yield session

    def _override_user():
        return holder.get("user", state["user"])

    holder["user"] = USER_A
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = _override_user
    try:
        with TestClient(app) as client:
            yield _Api(client, state, app, get_current_user_id)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)


@pytest.fixture()
def http_client(api):
    yield api.as_user(USER_A)


def _real_port():
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT

    return ACCOUNTS_BALANCE_PORT


def _own(svc_session, amount_minor=1_000, **kw):
    from app.modules.transactions import service as svc

    return svc.transfer_own(
        svc_session["session"],
        initiator_user_id=kw.pop("initiator", USER_A),
        source_account_id=kw.pop("source", svc_session["a1"]),
        target_account_id=kw.pop("target", svc_session["a2"]),
        amount_minor=amount_minor,
        currency="PEN",
        idempotency_key=f"e2t24-own-{uuid.uuid4()}",
        balance_port=_real_port(),
        **kw,
    )


def _third(svc_session, amount_minor=1_000, **kw):
    from app.modules.transactions import service as svc

    return svc.transfer_third_party(
        svc_session["session"],
        initiator_user_id=kw.pop("initiator", USER_A),
        source_account_id=kw.pop("source", svc_session["a1"]),
        target_account_id=kw.pop("target", svc_session["b1"]),
        amount_minor=amount_minor,
        currency="PEN",
        fee_minor=kw.pop("fee_minor", 0),
        idempotency_key=f"e2t24-third-{uuid.uuid4()}",
        balance_port=_real_port(),
        **kw,
    )


def _count_tx(session) -> int:
    return session.scalar(
        sa.select(sa.func.count()).select_from(Base.metadata.tables["transactions.transactions"])
    )


# ------------------------------------------------------------ CA-01: paginado
def test_list_pagination_meta_and_stable_order(svc_session, http_client):
    for i in range(5):
        _own(svc_session, amount_minor=1_000 + i)
    # Orden de referencia: el propio endpoint con pagina amplia (una sola
    # consulta ordenada `created_at DESC, id DESC`).
    full = http_client.get("/api/v1/transactions", params={"page": 1, "page_size": 10})
    assert full.status_code == 200, full.text
    expected_ids = [it["transaction_id"] for it in full.json()["data"]]
    assert len(expected_ids) == 5 and len(set(expected_ids)) == 5

    first = http_client.get("/api/v1/transactions", params={"page": 1, "page_size": 2})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["meta"]["total"] == 5
    assert body["meta"]["page"] == 1 and body["meta"]["page_size"] == 2
    assert body["meta"]["request_id"]
    assert [it["transaction_id"] for it in body["data"]] == expected_ids[:2]

    second = http_client.get("/api/v1/transactions", params={"page": 2, "page_size": 2})
    third = http_client.get("/api/v1/transactions", params={"page": 3, "page_size": 2})
    assert [it["transaction_id"] for it in second.json()["data"]] == expected_ids[2:4]
    assert [it["transaction_id"] for it in third.json()["data"]] == expected_ids[4:]
    # Sin duplicados ni huecos entre paginas.
    seen = (
        [it["transaction_id"] for it in body["data"]]
        + [it["transaction_id"] for it in second.json()["data"]]
        + [it["transaction_id"] for it in third.json()["data"]]
    )
    assert sorted(seen) == sorted(expected_ids) and len(set(seen)) == 5


def test_list_item_shape_and_repeat_is_read_only(svc_session, http_client):
    _own(svc_session, amount_minor=2_000, external_ref="e2t24-ref-1")
    before = _count_tx(svc_session["session"])
    resp = http_client.get("/api/v1/transactions")
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"][0]
    assert set(item) == {
        "transaction_id",
        "type",
        "status",
        "amount_minor",
        "fee_minor",
        "currency",
        "created_at",
        "settled_at",
        "source_account_masked",
        "target_account_masked",
        "external_ref",
    }
    assert item["type"] == "OWN_TRANSFER" and item["status"] == "SETTLED"
    assert item["amount_minor"] == 2_000 and item["currency"] == "PEN"
    assert item["external_ref"] == "e2t24-ref-1"
    again = http_client.get("/api/v1/transactions")
    assert again.json()["data"] == resp.json()["data"]  # repetir no altera datos
    assert again.json()["meta"]["total"] == resp.json()["meta"]["total"]
    assert _count_tx(svc_session["session"]) == before


# ------------------------------------------------------------ CA-02: filtros
def test_list_filters_type_status_and_date(svc_session, http_client):
    _own(svc_session, amount_minor=1_000)
    _own(svc_session, amount_minor=1_100)
    _third(svc_session, amount_minor=1_200)
    rejected = _own(svc_session, amount_minor=999_999)  # sin fondos -> REJECTED
    assert rejected.status == "REJECTED"

    own = http_client.get("/api/v1/transactions", params={"type": "OWN_TRANSFER"})
    assert own.json()["meta"]["total"] == 3
    assert {it["type"] for it in own.json()["data"]} == {"OWN_TRANSFER"}

    third = http_client.get("/api/v1/transactions", params={"type": "THIRD_PARTY_TRANSFER"})
    assert third.json()["meta"]["total"] == 1

    settled = http_client.get("/api/v1/transactions", params={"status": "SETTLED"})
    assert settled.json()["meta"]["total"] == 3
    bad = http_client.get("/api/v1/transactions", params={"status": "REJECTED"})
    assert bad.json()["meta"]["total"] == 1
    assert bad.json()["data"][0]["transaction_id"] == str(rejected.id)

    future = http_client.get("/api/v1/transactions", params={"date_from": "2999-01-01T00:00:00Z"})
    assert future.json()["meta"]["total"] == 0 and future.json()["data"] == []
    past = http_client.get("/api/v1/transactions", params={"date_to": "2000-01-01T00:00:00Z"})
    assert past.json()["meta"]["total"] == 0 and past.json()["data"] == []

    wide = http_client.get(
        "/api/v1/transactions",
        params={"date_from": "2000-01-01T00:00:00Z", "date_to": "2999-01-01T00:00:00Z"},
    )
    assert wide.json()["meta"]["total"] == 4


def test_list_empty_for_unknown_combination(svc_session, http_client):
    _own(svc_session)
    resp = http_client.get("/api/v1/transactions", params={"type": "REVERSAL"})
    assert resp.status_code == 200
    assert resp.json() == {
        "data": [],
        "meta": {
            "page": 1,
            "page_size": 20,
            "total": 0,
            "request_id": resp.json()["meta"]["request_id"],
        },
    }


# ------------------------------------------------- CA-03: enmascarado (sin PII)
def test_list_masking_no_full_numbers(svc_session, http_client):
    _own(svc_session, amount_minor=3_000)
    _third(svc_session, amount_minor=2_000, fee_minor=100)
    resp = http_client.get("/api/v1/transactions", params={"page_size": 10})
    assert resp.status_code == 200, resp.text
    for item in resp.json()["data"]:
        for key in ("source_account_masked", "target_account_masked"):
            masked = item[key]
            assert masked is not None and masked.startswith("****")
    by_type = {it["type"]: it for it in resp.json()["data"]}
    assert by_type["OWN_TRANSFER"]["source_account_masked"] == "****1111"
    assert by_type["OWN_TRANSFER"]["target_account_masked"] == "****2222"
    assert by_type["THIRD_PARTY_TRANSFER"]["target_account_masked"] == "****3333"
    for full in FULL_NUMBERS:  # numero completo jamas en claro
        assert full not in resp.text


def test_list_interbank_target_masked(svc_session, http_client):
    from app.modules.transactions import repository as tx_repo

    session = svc_session["session"]
    tx = tx_repo.create_transaction(
        session,
        type="INTERBANK_TRANSFER",
        amount_minor=5_000,
        currency="PEN",
        initiator_user_id=USER_A,
        source_account_id=svc_session["a1"],
        fee_minor=50,
        metadata={
            "interbank": {
                "cci_hash": "hash-no-reversible",
                "bank_code": "003",
                "holder_name_masked": "J*** P***",
            }
        },
    )
    for target in ("VALIDATED", "AUTHORIZED", "FUNDS_HELD", "POSTED"):
        tx = tx_repo.transition_transaction(session, tx.id, target, reason="e2t24")
    resp = http_client.get("/api/v1/transactions", params={"type": "INTERBANK_TRANSFER"})
    assert resp.status_code == 200, resp.text
    (item,) = resp.json()["data"]
    assert item["source_account_masked"] == "****1111"
    assert item["target_account_masked"] == "CCI **** (banco 003)"
    assert "hash-no-reversible" not in resp.text


# ------------------------------------------------- CA-04: aislamiento
def test_list_isolation_between_holders(svc_session, api):
    mine = _own(svc_session, amount_minor=1_000)
    foreign = _third(
        svc_session, initiator=USER_B, source=svc_session["b1"], target=svc_session["a1"]
    )
    assert foreign.initiator_user_id == USER_B

    body_a = api.as_user(USER_A).get("/api/v1/transactions").json()
    ids_a = {it["transaction_id"] for it in body_a["data"]}
    assert str(mine.id) in ids_a and str(foreign.id) not in ids_a

    body_b = api.as_user(USER_B).get("/api/v1/transactions").json()
    ids_b = {it["transaction_id"] for it in body_b["data"]}
    assert str(foreign.id) in ids_b and str(mine.id) not in ids_b


# ------------------------------------------------------------------ Errores
def test_list_errors_401_422(svc_session, api):
    _own(svc_session)

    naked = api.as_anon().get("/api/v1/transactions")
    assert naked.status_code == 401
    assert naked.json()["error"]["code"] == "NOT_AUTHENTICATED"

    bad_token = api.as_anon().get(
        "/api/v1/transactions", headers={"Authorization": "Bearer no-es-jwt"}
    )
    assert bad_token.status_code == 401

    http_client = api.as_user(USER_A)

    inverted = http_client.get(
        "/api/v1/transactions",
        params={"date_from": "2026-09-02T00:00:00Z", "date_to": "2026-09-01T00:00:00Z"},
    )
    assert inverted.status_code == 422
    assert inverted.json()["error"]["code"] == "VALIDATION_ERROR"

    unknown_status = http_client.get("/api/v1/transactions", params={"status": "BOGUS"})
    assert unknown_status.status_code == 422
    assert unknown_status.json()["error"]["code"] == "VALIDATION_ERROR"

    unknown_type = http_client.get("/api/v1/transactions", params={"type": "BOGUS"})
    assert unknown_type.status_code == 422
    assert unknown_type.json()["error"]["code"] == "VALIDATION_ERROR"

    page_zero = http_client.get("/api/v1/transactions", params={"page": 0})
    assert page_zero.status_code == 422

    oversize = http_client.get("/api/v1/transactions", params={"page_size": 101})
    assert oversize.status_code == 422


# ------------------------------------------------------- Unidad: validacion
def test_validate_listing_filters_unit():
    from app.modules.transactions.service import listing as listing_svc

    with pytest.raises(listing_svc.ListingValidationError):
        listing_svc.validate_listing_filters(
            date_from=None, date_to=None, type="BOGUS", status=None, page=1, page_size=20
        )
    with pytest.raises(listing_svc.ListingValidationError):
        listing_svc.validate_listing_filters(
            date_from=None, date_to=None, type=None, status="BOGUS", page=1, page_size=20
        )
    with pytest.raises(listing_svc.ListingValidationError):
        listing_svc.validate_listing_filters(
            date_from=None, date_to=None, type=None, status=None, page=0, page_size=20
        )
    with pytest.raises(listing_svc.ListingValidationError):
        listing_svc.validate_listing_filters(
            date_from=None, date_to=None, type=None, status=None, page=1, page_size=101
        )
