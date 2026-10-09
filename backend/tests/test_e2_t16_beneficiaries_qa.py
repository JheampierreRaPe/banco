"""QA de beneficiarios: busqueda, edicion/eliminacion y contrato HTTP (E2-T16, HU07).

Cierra el QA de HU07 CA-01..CA-04 sobre lo ya probado por `E2-T13` (CRUD
cifrado + busqueda indexada) y `E2-T15` (agenda Flutter). No duplica esas
suites: aqui solo se cubren los huecos.

E2-T33 (retiro del tope pre-aprobado): los tests de edicion/validacion del
tope y de exencion del gate se retiraron con la feature (ver
`test_e2_t33_no_preapproved_exemption.py` como reemplazo); aqui solo quedan
busqueda, edicion de campos vigentes, errores y seguridad.

Mapeo brief -> test:
- busqueda `?q=` por alias/prefijo/nombre via HTTP, tope 100, < 1 s,
  aislamiento y enmascarado ................ `test_api_search_*`
- edicion solo por el titular ............... `test_*_edit_*_owner*`
- edicion/eliminacion: confirmacion, soft-delete, reutilizacion de alias,
  errores 400/403/404/409 .................. `test_api_edit_*`, `test_api_delete_*`,
  `test_api_soft_delete_then_alias_reusable`
- seguridad: sin claro en BD/respuestas/logs, `beneficiary.saved` sin PII,
  duplicados 409 ............................ `test_api_no_cleartext_*`,
  `test_api_saved_event_without_pii`, `test_api_edit_duplicate_*`

Datos 100 % simulados (alias/CCI/nombres de fantasia, sin PII real).
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base, get_db
from app.main import app
from app.modules.accounts.api import get_current_user_id as accounts_auth
from app.modules.transactions.api import get_current_user_id as transfers_auth

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()

CCI_1 = "00110001111111111111"
CCI_2 = "00220002222222222222"

THRESHOLD_KEY = "transfer.biometric_threshold_minor"
DAILY_KEY = "transfer.daily_limit_minor"
DAILY_STANDARD_KEY = "transfer.daily_limit_STANDARD_minor"
MAX_ATTEMPTS_KEY = "auth.max_failed_attempts"

LITE_TABLES = [
    "accounts.beneficiaries",
    "accounts.beneficiary_search_tokens",
    "shared.outbox",
    "shared.idempotency_keys",
]

FULL_TABLES = [
    "accounts.accounts",
    "accounts.account_balances",
    "accounts.daily_balance_snapshots",
    "accounts.beneficiaries",
    "accounts.beneficiary_search_tokens",
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


def _attach_schemas(*schemas: str):
    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in schemas:
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    return _attach


def _make_engine(tables: list[str], extra_schemas: tuple[str, ...]):
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
    event.listen(engine, "connect", _attach_schemas(*extra_schemas))
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in tables])
    return engine


def _seed_params(session: Session) -> None:
    for key, value, module in (
        (THRESHOLD_KEY, "2000", "transactions"),
        (DAILY_KEY, "500000", "transactions"),
        (DAILY_STANDARD_KEY, "500000", "transactions"),
        (MAX_ATTEMPTS_KEY, "5", "identity"),
    ):
        session.execute(
            sa.text(
                "INSERT INTO config.parameters (id, key, value_json, module) "
                "VALUES (:id, :key, :value, :module)"
            ),
            {"id": uuid.uuid4().hex, "key": key, "value": value, "module": module},
        )
    session.flush()


@pytest.fixture()
def lite_session():
    """Sesion SQLite ligera (beneficiarios + outbox + idempotencia)."""
    engine = _make_engine(LITE_TABLES, ("accounts", "shared"))
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
        session.rollback()
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def api_ctx(monkeypatch):
    """App completa sobre SQLite + auth simulada (conmutable USER_A/B)."""
    from app.modules.accounts import repository as accounts_repo

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    engine = _make_engine(FULL_TABLES, ("accounts", "ledger", "shared", "transactions", "config"))
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE config.parameters (id TEXT PRIMARY KEY, "
                "key TEXT NOT NULL UNIQUE, value_json TEXT NOT NULL, "
                "module TEXT NOT NULL, description TEXT, updated_at TEXT, "
                "created_at TEXT, updated_by TEXT)"
            )
        )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    a1 = accounts_repo.create_account(
        session, user_id=USER_A, account_number="00991112222", type="AHORRO", currency="PEN"
    )
    b1 = accounts_repo.create_account(
        session, user_id=USER_B, account_number="00881113333", type="AHORRO", currency="PEN"
    )
    accounts_repo.apply_delta(session, a1.id, available_delta=200_000, held_delta=0, currency="PEN")
    _seed_params(session)
    session.commit()
    current = {"user": USER_A}

    def _override_db():
        yield session

    def _override_user():
        return current["user"]

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[accounts_auth] = _override_user
    app.dependency_overrides[transfers_auth] = _override_user
    try:
        with TestClient(app) as client:
            yield {
                "client": client,
                "session": session,
                "current": current,
                "a1": str(a1.id),
                "b1": str(b1.id),
            }
            session.rollback()
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(accounts_auth, None)
        app.dependency_overrides.pop(transfers_auth, None)
        session.close()
        engine.dispose()


def _auth(key: str | None = None) -> dict:
    headers = {"Authorization": "Bearer x"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


# ------------------------------------------------- Parte A: edicion solo del titular


def test_edit_editable_only_by_owner_service(lite_session):
    """La edicion solo la hace el titular (CA-03): un tercero recibe 403."""
    from app.modules.accounts.service import beneficiaries as beneficiaries_service

    view = beneficiaries_service.create_beneficiary(
        lite_session,
        USER_A,
        alias="Mama",
        account_or_cci=CCI_1,
        holder_name="Titular Original",
        currency="PEN",
    )
    updated = beneficiaries_service.update_beneficiary(
        lite_session, USER_A, view["id"], holder_name="Titular Nuevo"
    )
    assert updated["holder_name"] == "Titular Nuevo"
    with pytest.raises(beneficiaries_service.BeneficiaryForbiddenError):
        beneficiaries_service.update_beneficiary(
            lite_session, USER_B, view["id"], holder_name="Intruso"
        )
    # El intento ajeno se rechaza antes de mutar: el nombre sigue intacto.
    fresh = beneficiaries_service.list_beneficiaries(lite_session, USER_A)[0]
    assert fresh["holder_name"] == "Titular Nuevo"  # el ajeno no lo movio


def test_update_rejects_invalid_cci_currency_service(lite_session):
    """Editar con CCI/currency invalidos se rechaza (400 del router)."""
    from app.modules.accounts.service import beneficiaries as beneficiaries_service

    view = beneficiaries_service.create_beneficiary(
        lite_session, USER_A, alias="Mama", account_or_cci=CCI_1
    )
    with pytest.raises((ValueError, TypeError)):
        beneficiaries_service.update_beneficiary(
            lite_session, USER_A, view["id"], account_or_cci="ABC123"
        )
    with pytest.raises((ValueError, TypeError)):
        beneficiaries_service.update_beneficiary(
            lite_session, USER_A, view["id"], account_or_cci="123"
        )
    with pytest.raises((ValueError, TypeError)):
        beneficiaries_service.update_beneficiary(lite_session, USER_A, view["id"], currency="XX")
    # E2-T33: el tope pre-aprobado dejo de existir; el contrato HTTP ignora
    # el campo historico (ver `test_api_legacy_limit_field_ignored` en E2-T13).


# ------------------------------------------------- Parte C: busqueda via HTTP


def test_api_search_by_alias_prefix_and_holder_name(api_ctx):
    """`GET /beneficiaries?q=` resuelve alias, prefijo y nombre (CA-02)."""
    from app.modules.accounts.service import beneficiaries as beneficiaries_service

    session = api_ctx["session"]
    client = api_ctx["client"]
    beneficiaries_service.create_beneficiary(
        session,
        USER_A,
        alias="Mama",
        account_or_cci=CCI_1,
        holder_name="Testonia Sintetica",
    )
    beneficiaries_service.create_beneficiary(
        session,
        USER_A,
        alias="Alquiler",
        account_or_cci=CCI_2,
        holder_name="Casero Sintetico",
    )
    session.commit()

    assert [
        r["alias"]
        for r in client.get("/api/v1/beneficiaries", params={"q": "mam"}, headers=_auth()).json()[
            "data"
        ]
    ] == ["Mama"]
    assert [
        r["alias"]
        for r in client.get("/api/v1/beneficiaries", params={"q": "alqui"}, headers=_auth()).json()[
            "data"
        ]
    ] == ["Alquiler"]
    assert [
        r["alias"]
        for r in client.get(
            "/api/v1/beneficiaries", params={"q": "sintetica"}, headers=_auth()
        ).json()["data"]
    ] == ["Mama"]
    assert (
        client.get("/api/v1/beneficiaries", params={"q": "zzz-nadie"}, headers=_auth()).json()[
            "meta"
        ]["total"]
        == 0
    )


def test_api_search_isolates_owners_and_masks(api_ctx):
    """La busqueda no cruza titulares y jamas expone el CCI (CA-02 + G16)."""
    from app.modules.accounts.service import beneficiaries as beneficiaries_service

    session = api_ctx["session"]
    client = api_ctx["client"]
    beneficiaries_service.create_beneficiary(session, USER_A, alias="Mama", account_or_cci=CCI_1)
    beneficiaries_service.create_beneficiary(session, USER_B, alias="Mama", account_or_cci=CCI_2)
    session.commit()

    mine = client.get("/api/v1/beneficiaries", params={"q": "mama"}, headers=_auth())
    assert mine.status_code == 200
    assert mine.json()["meta"]["total"] == 1  # el de USER_B no aparece
    body = mine.text
    assert CCI_1 not in body and CCI_2 not in body
    assert mine.json()["data"][0]["account_masked"] == "****1111"
    assert "holder_doc" not in body

    api_ctx["current"]["user"] = USER_B
    try:
        other = client.get("/api/v1/beneficiaries", params={"q": "mama"}, headers=_auth())
        assert other.json()["meta"]["total"] == 1
        assert CCI_1 not in other.text and CCI_2 not in other.text
    finally:
        api_ctx["current"]["user"] = USER_A


def test_api_search_caps_at_100_under_one_second(api_ctx):
    """La busqueda via HTTP topa en 100 y responde en < 1 s (CA-02)."""
    from app.modules.accounts.service import beneficiaries as beneficiaries_service

    session = api_ctx["session"]
    client = api_ctx["client"]
    for i in range(105):
        beneficiaries_service.create_beneficiary(
            session,
            USER_A,
            alias=f"Qa Benef {i:03d}",
            # E2-T28: `normalize_account_or_cci` solo acepta cuenta de 14
            # digitos con DV o CCI de 20 (se retira el rango laxo `8..20`).
            account_or_cci=f"{20000000000000000000 + i:020d}",
        )
    session.commit()

    start = time.perf_counter()
    resp = client.get("/api/v1/beneficiaries", params={"q": "qa benef"}, headers=_auth())
    elapsed = time.perf_counter() - start
    assert resp.status_code == 200
    assert resp.json()["meta"]["total"] == 100
    assert elapsed < 1.0
    assert client.get("/api/v1/beneficiaries", headers=_auth()).json()["meta"]["total"] == 100


def test_api_search_rejects_overlong_q_and_empty_lists_all(api_ctx):
    """`q` > 80 se rechaza (422); sin `q` lista todo el titular."""
    from app.modules.accounts.service import beneficiaries as beneficiaries_service

    session = api_ctx["session"]
    client = api_ctx["client"]
    beneficiaries_service.create_beneficiary(session, USER_A, alias="Mama", account_or_cci=CCI_1)
    session.commit()

    assert (
        client.get("/api/v1/beneficiaries", params={"q": "q" * 81}, headers=_auth()).status_code
        == 422
    )
    full = client.get("/api/v1/beneficiaries", headers=_auth())
    assert full.status_code == 200 and full.json()["meta"]["total"] == 1


# ------------------------------------------------- Parte D: edicion via HTTP


def test_api_edit_owner_only(api_ctx):
    """La edicion via `PUT` solo la hace el titular; el ajeno recibe 403."""
    client = api_ctx["client"]
    created = client.post(
        "/api/v1/beneficiaries",
        json={
            "alias": "Mama",
            "account_or_cci": CCI_1,
            "holder_name": "Titular Original",
            "currency": "PEN",
        },
        headers=_auth(),
    )
    assert created.status_code == 201
    bid = created.json()["data"]["id"]

    ok = client.put(
        f"/api/v1/beneficiaries/{bid}", json={"holder_name": "Titular Nuevo"}, headers=_auth()
    )
    assert ok.status_code == 200
    assert ok.json()["data"]["holder_name"] == "Titular Nuevo"

    api_ctx["current"]["user"] = USER_B
    try:
        denied = client.put(
            f"/api/v1/beneficiaries/{bid}", json={"holder_name": "Intruso"}, headers=_auth()
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "NOT_AUTHORIZED"
    finally:
        api_ctx["current"]["user"] = USER_A

    assert (
        client.get("/api/v1/beneficiaries", headers=_auth()).json()["data"][0]["holder_name"]
        == "Titular Nuevo"
    )


def test_api_edit_error_contract(api_ctx):
    """Contrato de errores de edicion: 400/404/409 (CA-04)."""
    client = api_ctx["client"]
    first = client.post(
        "/api/v1/beneficiaries", json={"alias": "Mama", "account_or_cci": CCI_1}, headers=_auth()
    )
    second = client.post(
        "/api/v1/beneficiaries", json={"alias": "Otro", "account_or_cci": CCI_2}, headers=_auth()
    )
    bid = second.json()["data"]["id"]
    missing = str(uuid.uuid4())

    bad_cci = client.put(
        f"/api/v1/beneficiaries/{bid}", json={"account_or_cci": "ABC"}, headers=_auth()
    )
    assert bad_cci.status_code == 400
    assert bad_cci.json()["error"]["code"] == "VALIDATION_ERROR"

    # E2-T33: el tope historico se ignora (sin 422) y no se devuelve.
    legacy = client.put(
        f"/api/v1/beneficiaries/{bid}", json={"preapproved_limit_minor": -5}, headers=_auth()
    )
    assert legacy.status_code == 200
    assert "preapproved_limit_minor" not in legacy.json()["data"]

    bad_currency = client.put(
        f"/api/v1/beneficiaries/{bid}", json={"currency": "XX"}, headers=_auth()
    )
    assert bad_currency.status_code == 400

    assert first.status_code == 201
    dup = client.put(f"/api/v1/beneficiaries/{bid}", json={"alias": "mama"}, headers=_auth())
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "DUPLICATE_BENEFICIARY"

    assert (
        client.put(
            f"/api/v1/beneficiaries/{missing}", json={"alias": "X"}, headers=_auth()
        ).status_code
        == 404
    )

    api_ctx["current"]["user"] = USER_B
    try:
        foreign = client.put(f"/api/v1/beneficiaries/{bid}", json={"alias": "X"}, headers=_auth())
        assert foreign.status_code == 403
        assert foreign.json()["error"]["code"] == "NOT_AUTHORIZED"
    finally:
        api_ctx["current"]["user"] = USER_A


def test_api_delete_error_contract(api_ctx):
    """Baja: exige `confirm=true`; inexistente -> 404; ajeno -> 403."""
    client = api_ctx["client"]
    created = client.post(
        "/api/v1/beneficiaries", json={"alias": "Mama", "account_or_cci": CCI_1}, headers=_auth()
    )
    bid = created.json()["data"]["id"]
    missing = str(uuid.uuid4())

    refused = client.delete(f"/api/v1/beneficiaries/{bid}", headers=_auth())
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "VALIDATION_ERROR"

    assert (
        client.delete(
            f"/api/v1/beneficiaries/{missing}", params={"confirm": True}, headers=_auth()
        ).status_code
        == 404
    )

    api_ctx["current"]["user"] = USER_B
    try:
        foreign = client.delete(
            f"/api/v1/beneficiaries/{bid}", params={"confirm": True}, headers=_auth()
        )
        assert foreign.status_code == 403
    finally:
        api_ctx["current"]["user"] = USER_A

    done = client.delete(f"/api/v1/beneficiaries/{bid}", params={"confirm": True}, headers=_auth())
    assert done.status_code == 200
    assert done.json()["data"]["status"] == "DELETED"
    again = client.delete(f"/api/v1/beneficiaries/{bid}", params={"confirm": True}, headers=_auth())
    assert again.status_code == 404  # ya dado de baja: no existe


def test_api_soft_delete_then_alias_reusable(api_ctx):
    """Soft-delete con traza + reutilizacion del alias (CA-04)."""
    client = api_ctx["client"]
    session = api_ctx["session"]
    created = client.post(
        "/api/v1/beneficiaries", json={"alias": "Mama", "account_or_cci": CCI_1}, headers=_auth()
    )
    bid = created.json()["data"]["id"]

    deleted = client.delete(
        f"/api/v1/beneficiaries/{bid}", params={"confirm": True}, headers=_auth()
    )
    assert deleted.json()["data"]["status"] == "DELETED"
    assert client.get("/api/v1/beneficiaries", headers=_auth()).json()["meta"]["total"] == 0
    assert (
        client.get("/api/v1/beneficiaries", params={"q": "mama"}, headers=_auth()).json()["meta"][
            "total"
        ]
        == 0
    )

    status = session.execute(
        sa.text("SELECT status FROM accounts.beneficiaries WHERE id = :id"),
        {"id": bid.replace("-", "")},
    ).scalar()
    assert status == "DELETED"  # la fila sigue (trazabilidad)

    reused = client.post(
        "/api/v1/beneficiaries", json={"alias": "Mama", "account_or_cci": CCI_1}, headers=_auth()
    )
    assert reused.status_code == 201
    assert reused.json()["data"]["status"] == "ACTIVE"


def test_api_third_party_requires_idempotency_key(api_ctx):
    """La ruta de dinero exige `Idempotency-Key` (-> 422 sin clave)."""
    client = api_ctx["client"]
    a1, b1 = api_ctx["a1"], api_ctx["b1"]
    resp = client.post(
        "/api/v1/transfers/third-party",
        json={
            "source_account_id": a1,
            "target_account_id": b1,
            "amount_minor": 1_000,
            "currency": "PEN",
            "fee_minor": 0,
        },
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ------------------------------------------------- Parte F: seguridad


def test_api_no_cleartext_in_beneficiary_responses(api_ctx, caplog):
    """CCI/documento nunca en claro en respuestas ni en logs (docs/15#3)."""
    import logging as _logging

    client = api_ctx["client"]
    secrets = (CCI_1, CCI_2, "87654321")
    with caplog.at_level(_logging.DEBUG):
        created = client.post(
            "/api/v1/beneficiaries",
            json={
                "alias": "Mama",
                "account_or_cci": CCI_1,
                "holder_name": "Testonia Sintetica",
                "holder_doc": "87654321",
                "currency": "PEN",
            },
            headers=_auth(),
        )
        bid = created.json()["data"]["id"]
        listed = client.get("/api/v1/beneficiaries", headers=_auth())
        found = client.get("/api/v1/beneficiaries", params={"q": "testonia"}, headers=_auth())
        updated = client.put(
            f"/api/v1/beneficiaries/{bid}", json={"holder_name": "Otra Sintetica"}, headers=_auth()
        )
        deleted = client.delete(
            f"/api/v1/beneficiaries/{bid}", params={"confirm": True}, headers=_auth()
        )

    for resp in (created, listed, found, updated, deleted):
        assert resp.status_code in (200, 201)
        for secret in secrets:
            assert secret not in resp.text
        assert "holder_doc" not in resp.text
    assert listed.json()["data"][0]["account_masked"] == "****1111"
    for secret in secrets:
        assert secret not in caplog.text


def test_api_saved_event_without_pii(api_ctx):
    """El alta via HTTP emite `beneficiary.saved` sin PII en `outbox`."""
    client = api_ctx["client"]
    session = api_ctx["session"]
    created = client.post(
        "/api/v1/beneficiaries",
        json={
            "alias": "Mama",
            "account_or_cci": CCI_1,
            "holder_name": "Testonia Sintetica",
            "holder_doc": "87654321",
        },
        headers=_auth(),
    )
    assert created.status_code == 201
    bid = created.json()["data"]["id"]

    rows = session.execute(
        sa.text(
            "SELECT event_type, status, payload FROM shared.outbox "
            "WHERE event_type = 'beneficiary.saved'"
        )
    ).all()
    assert rows and all(r[0] == "beneficiary.saved" and r[1] == "PENDING" for r in rows)

    def _as_dict(payload):
        return json.loads(payload) if isinstance(payload, str) else dict(payload)

    blob = json.dumps([_as_dict(r[2]) for r in rows])
    assert bid in blob  # solo UUIDs
    for secret in ("Mama", "Testonia Sintetica", "87654321", CCI_1):
        assert secret not in blob
