"""Lookup previo de destino por DNI/celular (E2-T27, HU06 CA-01..CA-06).

Sobre SQLite en memoria + schemas ATTACH (mismo patron que E2-T07), con
usuarios `identity` (hash HMAC real via `hash_document_number`, dinero de
juguete, sin PII real) y cuentas `accounts` reales: el lookup resuelve por
las fachadas (`identity.service.target_lookup`,
`accounts.service.list_active_by_user`) sin importar modelos ajenos.

Mapeo brief -> test:
- feliz por DNI + transferencia con la referencia ........ `test_resolve_by_document_*`
- feliz por celular ..................................... `test_resolve_by_phone`
- neutro (inexistente x2 + sin cuenta ACTIVE, mismo 404)  `test_not_found_*`
- ambiguo (2 ACTIVE -> 409, sin elegir) .................. `test_ambiguous`
- formato/combinacion (422) .............................. `test_validation_*`
- auth (401) y sin `Idempotency-Key` ..................... `test_auth_*`
- sin PII en respuesta ni logs ........................... `test_no_pii_*`
- regresion third-party intacto .......................... `test_regression_*`
"""

from __future__ import annotations

import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

DOC_DST = "12345678"
PHONE_DST = "987654321"
DOC_PHONE_USER = "87654321"
PHONE_OK = "912345678"
DOC_NAKED = "11112222"
DOC_MULTI = "33334444"
DOC_UNKNOWN = "99998888"
PHONE_UNKNOWN = "900000001"

TABLES = [
    "identity.users",
    "identity.credentials",  # `User.credential` es `selectin`: el ORM la toca al leer usuarios
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


def _make_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # registro de tablas
    import app.modules.identity.models as _i
    import app.modules.ledger.models as _l
    import app.modules.shared.models as _s
    import app.modules.transactions.models as _t

    assert _a is not None and _i is not None and _l is not None
    assert _s is not None and _t is not None
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("identity", "accounts", "ledger", "shared", "transactions", "config"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    return engine


def _make_user(session, *, doc, first, last, email, phone=None, business=None):
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service.kyc_onboarding import hash_document_number

    return identity_repo.create_user(
        session,
        doc_type="DNI",
        doc_number_hash=hash_document_number(doc),
        first_name=first,
        last_name=last,
        business_name=business,
        email=email,
        phone=phone,
    )


def _make_account(session, *, user_id, number, currency="PEN", status="ACTIVE"):
    from app.modules.accounts import repository as accounts_repo

    return accounts_repo.create_account(
        session,
        user_id=user_id,
        account_number=number,
        type="AHORRO",
        currency=currency,
        status=status,
    )


@pytest.fixture()
def svc_session():
    """Sesion aislada: titular + destinos (DNI, celular, sin cuenta, multi)."""
    from app.modules.accounts import repository as accounts_repo

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        src_user = _make_user(
            session,
            doc="00001111",
            first="Ana",
            last="Origen",
            email="ana.origen@example.com",
            phone="911111111",
        )
        src = _make_account(session, user_id=src_user.id, number="00100000000001")
        dst = _make_user(
            session,
            doc=DOC_DST,
            first="Maria",
            last="Torres",
            email="maria.torres@example.com",
            phone=PHONE_DST,
        )
        dst_account = _make_account(session, user_id=dst.id, number="00100000000002")
        by_phone = _make_user(
            session,
            doc=DOC_PHONE_USER,
            first="Bruno",
            last="Paredes",
            email="bruno.paredes@example.com",
            phone="912 345 678",
        )
        phone_account = _make_account(session, user_id=by_phone.id, number="00100000000003")
        _make_user(
            session,
            doc=DOC_NAKED,
            first="Carmen",
            last="Sola",
            email="carmen.sola@example.com",
            phone="922222222",
        )
        multi = _make_user(
            session,
            doc=DOC_MULTI,
            first="Diego",
            last="Doble",
            email="diego.doble@example.com",
            phone="933333333",
        )
        multi_a = _make_account(session, user_id=multi.id, number="00100000000004")
        multi_b = _make_account(session, user_id=multi.id, number="00100000000005")
        blocked = _make_user(
            session,
            doc="44445555",
            first="Elena",
            last="Bloqueada",
            email="elena.bloq@example.com",
            phone="944444444",
        )
        _make_account(session, user_id=blocked.id, number="00100000000006", status="BLOCKED")
        accounts_repo.apply_delta(
            session, src.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, dst_account.id, available_delta=5_000, held_delta=0, currency="PEN"
        )
        session.commit()
        yield {
            "session": session,
            "src_user": src_user.id,
            "src": src.id,
            "dst_account": dst_account.id,
            "phone_account": phone_account.id,
            "multi_a": multi_a.id,
            "multi_b": multi_b.id,
            "dst_full_number": "00100000000002",
        }
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def http_client(svc_session, monkeypatch):
    """TestClient con BD de `svc_session` y auth falsa como titular origen."""
    from app.core.db import get_db
    from app.main import app
    from app.modules.transactions.api import get_current_user_id

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    session = svc_session["session"]

    def _override_db():
        yield session

    def _override_user():
        return svc_session["src_user"]

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = _override_user
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)


def _resolve(session, **payload):
    from app.modules.transactions.service import resolve_target as svc

    document = payload.get("target_document")
    return svc.resolve_transfer_target(
        session, target_document=document, target_phone=payload.get("target_phone")
    )


# ------------------------------------------------------------ CA-01 feliz por DNI
def test_resolve_by_document_ok(svc_session):
    session = svc_session["session"]
    result = _resolve(session, target_document={"type": "DNI", "number": DOC_DST})
    assert result["target_account_id"] == svc_session["dst_account"]
    assert result["currency"] == "PEN"
    assert result["holder_name_masked"] == "M*** T***"
    assert result["account_number_masked"] == "****0002"


def test_resolve_by_document_then_transfer_settles(http_client, svc_session):
    lookup = http_client.post(
        "/api/v1/transfers/third-party/resolve-target",
        json={"target_document": {"type": "DNI", "number": DOC_DST}},
    )
    assert lookup.status_code == 200, lookup.text
    data = lookup.json()["data"]
    assert lookup.json()["meta"]["request_id"]
    body = {
        "source_account_id": str(svc_session["src"]),
        "target_account_id": data["target_account_id"],
        "amount_minor": 2_000,
        "currency": "PEN",
    }
    sent = http_client.post(
        "/api/v1/transfers/third-party",
        json=body,
        headers={"Idempotency-Key": f"e2t27-doc-{uuid.uuid4()}"},
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["data"]["status"] in ("SETTLED", "REJECTED")
    assert sent.json()["data"]["target_account_id"] == data["target_account_id"]


# ------------------------------------------------------------ CA-02 feliz por celular
def test_resolve_by_phone_ok(svc_session, http_client):
    session = svc_session["session"]
    # El almacenado lleva espacios: la comparacion es normalizada.
    result = _resolve(session, target_phone="912345678")
    assert result["target_account_id"] == svc_session["phone_account"]
    assert result["holder_name_masked"] == "B*** P***"
    via_http = http_client.post(
        "/api/v1/transfers/third-party/resolve-target",
        json={"target_phone": "(912) 345-678"},
    )
    assert via_http.status_code == 200, via_http.text
    assert via_http.json()["data"]["target_account_id"] == str(svc_session["phone_account"])


# ------------------------------------------------------------ CA-03 neutro (anti-enumeracion)
def _neutral_cases():
    return [
        {"target_document": {"type": "DNI", "number": DOC_UNKNOWN}},
        {"target_phone": PHONE_UNKNOWN},
        {"target_document": {"type": "DNI", "number": DOC_NAKED}},  # sin cuenta ACTIVE
        {"target_document": {"type": "DNI", "number": "44445555"}},  # solo BLOCKED
    ]


def test_not_found_neutral_same_body(svc_session, http_client):
    from app.modules.transactions.service import resolve_target as svc

    bodies = []
    for payload in _neutral_cases():
        with pytest.raises(svc.TargetNotFoundError) as exc:
            _resolve(svc_session["session"], **payload)
        assert str(exc.value) == svc.TARGET_NOT_FOUND_MESSAGE
        resp = http_client.post("/api/v1/transfers/third-party/resolve-target", json=payload)
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["error"]["code"] == "TARGET_NOT_FOUND"
        bodies.append((resp.status_code, body["error"]["code"], body["error"]["message"]))
    assert len(set(bodies)) == 1  # mismo codigo/cuerpo en todos los casos


# ------------------------------------------------------------ CA-04 ambiguedad
def test_ambiguous_multi_active(svc_session, http_client):
    from app.modules.transactions.service import resolve_target as svc

    with pytest.raises(svc.TargetAmbiguousError):
        _resolve(svc_session["session"], target_document={"type": "DNI", "number": DOC_MULTI})
    resp = http_client.post(
        "/api/v1/transfers/third-party/resolve-target",
        json={"target_document": {"type": "DNI", "number": DOC_MULTI}},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "TARGET_AMBIGUOUS"
    # No elige ninguna: la referencia no es una de sus cuentas.
    assert resp.json()["error"]["message"] == svc.TARGET_AMBIGUOUS_MESSAGE


# ------------------------------------------------------------ CA-06 validacion (422)
@pytest.mark.parametrize(
    "payload",
    [
        {},  # ninguno
        {"target_document": {"type": "DNI", "number": DOC_DST}, "target_phone": PHONE_DST},
        {"target_document": {"type": "DNI", "number": "123"}},  # DNI corto
        {"target_document": {"type": "DNI", "number": "ABCDEFGH"}},  # DNI no numerico
        {"target_document": {"type": "RUC", "number": "12345678901"}},  # tipo fuera de contrato
        {"target_document": {"type": "DNI"}},  # sin numero
        {"target_phone": "12"},  # celular corto
        {"target_phone": "llamame"},  # celular sin digitos
    ],
)
def test_validation_422(svc_session, http_client, payload):
    from app.modules.transactions.service import resolve_target as svc

    with pytest.raises(svc.ResolveTargetValidationError):
        _resolve(svc_session["session"], **payload)
    resp = http_client.post("/api/v1/transfers/third-party/resolve-target", json=payload)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ------------------------------------------------------------ Auth + sin Idempotency-Key
def test_auth_required(http_client, svc_session):
    from app.main import app
    from app.modules.transactions.api import get_current_user_id

    app.dependency_overrides.pop(get_current_user_id, None)
    try:
        resp = http_client.post(
            "/api/v1/transfers/third-party/resolve-target",
            json={"target_document": {"type": "DNI", "number": DOC_DST}},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: svc_session["src_user"]


def test_lookup_needs_no_idempotency_key(http_client):
    resp = http_client.post(
        "/api/v1/transfers/third-party/resolve-target",
        json={"target_document": {"type": "DNI", "number": DOC_DST}},
    )
    assert resp.status_code == 200, resp.text


# ------------------------------------------------------------ CA-05 sin PII
def test_no_pii_in_response(http_client, svc_session):
    resp = http_client.post(
        "/api/v1/transfers/third-party/resolve-target",
        json={"target_document": {"type": "DNI", "number": DOC_DST}},
    )
    assert resp.status_code == 200
    raw = resp.text
    assert DOC_DST not in raw
    assert PHONE_DST not in raw
    assert svc_session["dst_full_number"] not in raw
    assert "Maria" not in raw and "Torres" not in raw


def test_no_pii_in_logs(svc_session, caplog):
    with caplog.at_level(logging.INFO, logger="app.modules.transactions.service.resolve_target"):
        _resolve(svc_session["session"], target_document={"type": "DNI", "number": DOC_DST})
        _resolve(svc_session["session"], target_phone=PHONE_DST)
    for record in caplog.records:
        message = record.getMessage()
        assert DOC_DST not in message and PHONE_DST not in message


# ------------------------------------------------------------ Regresion third-party
def test_regression_third_party_contract(http_client, svc_session):
    body = {
        "source_account_id": str(svc_session["src"]),
        "target_account_id": str(svc_session["dst_account"]),
        "amount_minor": 1_000,
        "currency": "PEN",
    }
    ok = http_client.post(
        "/api/v1/transfers/third-party",
        json=body,
        headers={"Idempotency-Key": f"e2t27-reg-{uuid.uuid4()}"},
    )
    assert ok.status_code == 201, ok.text
    # El POST de dinero no acepta identificadores del lookup.
    smuggled = dict(body, target_document={"type": "DNI", "number": DOC_DST})
    del smuggled["target_account_id"]
    denied = http_client.post(
        "/api/v1/transfers/third-party",
        json=smuggled,
        headers={"Idempotency-Key": f"e2t27-reg2-{uuid.uuid4()}"},
    )
    assert denied.status_code == 422
    # Sin clave sigue exigiendo Idempotency-Key.
    assert http_client.post("/api/v1/transfers/third-party", json=body).status_code == 422
    spec = http_client.get("/openapi.json")
    assert "/api/v1/transfers/third-party/resolve-target" in spec.json()["paths"]
