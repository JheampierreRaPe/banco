"""CRUD de beneficiarios con cifrado y busqueda indexada (E2-T13, HU07).

- Parte A (sin BD): cripto (`encrypt/decrypt`, sobre != claro, clave
  incorrecta falla), normalizacion e indices ciegos (deterministas,
  por dominio, con prefijos), validaciones puras.
- Parte B (SQLite en memoria + schemas ATTACH): alta/listado/busqueda
  (limite 100 y < 1 s), edicion, soft-delete, aislamiento por titular,
  cifrado en BD (sin claro), busqueda sin descifrar, idempotencia y
  evento `beneficiary.saved` sin PII.
- Parte C (API TestClient): contrato REST (201/200/400/401/403/404/409),
  confirmacion de seguridad, `Idempotency-Key`, enmascaramiento.
- Parte D (Postgres `db_session`): humo de integracion; se omite sin BD.
"""

from __future__ import annotations

import time
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base, get_db
from app.main import app
from app.modules.accounts import repository as _accounts_repository  # noqa: F401
from app.modules.accounts.api import get_current_user_id
from app.modules.accounts.repository import beneficiaries as beneficiaries_repo
from app.modules.accounts.service import beneficiaries as beneficiaries_service
from app.modules.accounts.service import beneficiaries_crypto as crypto

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()

CCI_A1 = "00110001111111111111"
CCI_A2 = "00220002222222222222"
CCI_B1 = "00330003333333333333"


def _payload(alias="Mama", cci=CCI_A1, **overrides):
    body = {"alias": alias, "account_or_cci": cci}
    body.update(overrides)
    return body


# ---------------------------------------------------------------- Parte A: cripto e indices


def test_crypto_roundtrip_and_opaque_envelope():
    token = crypto.encrypt_value("00110001111111111111")
    assert "00110001111111111111" not in token
    assert crypto.decrypt_value(token) == "00110001111111111111"


def test_crypto_uses_aes_when_available():
    token = crypto.encrypt_value("hola")
    assert token.startswith("B1.") if crypto.using_aes() else token.startswith("B0.")


def test_crypto_wrong_key_fails(monkeypatch):
    token = crypto.encrypt_value("secreto")
    monkeypatch.setenv(crypto.CRYPTO_KEY_ENV, "otra-clave-de-prueba-diferente-123")
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_value(token)


def test_crypto_fallback_envelope_roundtrip(monkeypatch):
    monkeypatch.setattr(crypto, "_AES_AVAILABLE", False)
    long_text = "texto-largo-para-multibloque-" * 5
    token = crypto.encrypt_value(long_text)
    assert token.startswith("B0.")
    assert long_text not in token
    assert crypto.decrypt_value(token) == long_text


def test_crypto_envelope_errors():
    with pytest.raises(ValueError):
        crypto.encrypt_value("")
    with pytest.raises(ValueError):
        crypto.blind_index("   ", domain="alias")
    with pytest.raises(ValueError):
        crypto.blind_index("Mama", domain="  ")
    with pytest.raises(ValueError):
        crypto.mask_account_or_cci("123")
    assert crypto.normalize_search_term(None) == ""
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_value("B9." + crypto.encrypt_value("x").partition(".")[2])
    token = crypto.encrypt_value("secreto")
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_value(tampered)
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_value("no-es-un-sobre")


def test_normalize_search_term_strips_accents_and_case():
    assert crypto.normalize_search_term("  María   José  ") == "MARIA JOSE"


def test_blind_index_deterministic_and_domained():
    assert crypto.alias_index("Mama") == crypto.alias_index("  mamá ")
    assert crypto.alias_index("Mama") != crypto.name_index("Mama")
    assert crypto.account_index(CCI_A1) != crypto.account_index(CCI_A2)


def test_blind_index_changes_with_pepper(monkeypatch):
    first = crypto.alias_index("Mama")
    monkeypatch.setenv(crypto.INDEX_PEPPER_ENV, "pepper-distinto-de-prueba")
    assert crypto.alias_index("Mama") != first


def test_token_hashes_include_prefixes():
    full = crypto.token_hashes_for("Maria", "Torres Quispe")
    assert crypto.query_token_hashes("mar") <= full
    assert crypto.query_token_hashes("maria torres") <= full
    assert crypto.query_token_hashes("q") == set()
    assert crypto.query_token_hashes("zzz-no-existe") - full == crypto.query_token_hashes(
        "zzz-no-existe"
    )


def test_pure_validations():
    assert beneficiaries_service.normalize_account_or_cci(" 0011-0001 1111-1111 1111 ") == CCI_A1
    with pytest.raises(ValueError):
        beneficiaries_service.normalize_account_or_cci("ABC123")
    with pytest.raises(ValueError):
        beneficiaries_service.normalize_account_or_cci("123")
    with pytest.raises(ValueError):
        beneficiaries_service.normalize_account_or_cci("1" * 21)
    with pytest.raises(TypeError):
        beneficiaries_service.normalize_account_or_cci(123)
    assert crypto.mask_account_or_cci("1111") == "****1111"


# ---------------------------------------------------------------- Parte B: SQLite


@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con `accounts` + `shared` (patron E2-T02)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.shared.models as _s  # noqa: F401 (registro de tablas)

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "shared"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["accounts.beneficiaries"],
            Base.metadata.tables["accounts.beneficiary_search_tokens"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["shared.idempotency_keys"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def test_create_list_roundtrip_masked(sqlite_session):
    view = beneficiaries_service.create_beneficiary(
        sqlite_session,
        USER_A,
        **_payload(
            holder_name="Maria Torres",
            holder_doc="12345678",
            preapproved_limit_minor=50_000,
            currency="PEN",
        ),
    )
    assert view["alias"] == "Mama"
    assert view["account_masked"] == "****1111"
    assert view["holder_name"] == "Maria Torres"
    assert "holder_doc" not in view
    assert CCI_A1 not in str(view.values())
    items = beneficiaries_service.list_beneficiaries(sqlite_session, USER_A)
    assert len(items) == 1 and items[0]["id"] == view["id"]
    assert beneficiaries_service.list_beneficiaries(sqlite_session, USER_B) == []


def test_sensitive_data_encrypted_at_rest(sqlite_session):
    beneficiaries_service.create_beneficiary(
        sqlite_session, USER_A, **_payload(holder_name="Maria Torres", holder_doc="12345678")
    )
    row = sqlite_session.execute(
        sa.text(
            "SELECT alias_enc, account_enc, holder_name_enc, holder_doc_hash,"
            " alias_search_hash, account_hash FROM accounts.beneficiaries"
        )
    ).one()
    blob = " ".join(str(v) for v in row if v is not None)
    assert CCI_A1 not in blob and "Maria Torres" not in blob and "Mama" not in blob
    assert "12345678" not in blob
    assert len(row[3]) == 64 and row[3] != "12345678"  # holder_doc: solo HMAC
    assert crypto.decrypt_value(row[0]) == "Mama"
    assert crypto.decrypt_value(row[1]) == CCI_A1
    assert crypto.decrypt_value(row[2]) == "Maria Torres"


def test_search_by_alias_name_and_prefix(sqlite_session):
    beneficiaries_service.create_beneficiary(
        sqlite_session, USER_A, **_payload(alias="Mama", cci=CCI_A1)
    )
    beneficiaries_service.create_beneficiary(
        sqlite_session,
        USER_A,
        **_payload(alias="Alquiler", cci=CCI_A2, holder_name="Juan Perez Quispe"),
    )
    assert [
        v["alias"]
        for v in beneficiaries_service.search_beneficiaries(sqlite_session, USER_A, "mama")
    ] == ["Mama"]
    assert [
        v["alias"]
        for v in beneficiaries_service.search_beneficiaries(sqlite_session, USER_A, "alqui")
    ] == [
        "Alquiler"
    ]  # prefijo
    assert [
        v["alias"]
        for v in beneficiaries_service.search_beneficiaries(sqlite_session, USER_A, "perez")
    ] == [
        "Alquiler"
    ]  # por nombre
    assert [
        v["alias"]
        for v in beneficiaries_service.search_beneficiaries(sqlite_session, USER_A, "juan quispe")
    ] == [
        "Alquiler"
    ]  # AND
    assert beneficiaries_service.search_beneficiaries(sqlite_session, USER_A, "zzz") == []
    assert len(beneficiaries_service.search_beneficiaries(sqlite_session, USER_A, "")) == 2
    assert beneficiaries_service.search_beneficiaries(sqlite_session, USER_B, "mama") == []


def test_search_filters_without_decrypting(sqlite_session, monkeypatch):
    beneficiaries_service.create_beneficiary(
        sqlite_session, USER_A, **_payload(holder_name="Maria Torres")
    )

    def _boom(_token: str) -> str:
        raise AssertionError("la busqueda no debe descifrar para filtrar")

    monkeypatch.setattr(crypto, "decrypt_value", _boom)
    rows = beneficiaries_repo.search_active(
        sqlite_session, USER_A, crypto.query_token_hashes("mar")
    )
    assert len(rows) == 1
    assert rows[0].alias_search_hash == crypto.alias_index("Mama")


def test_search_limit_100_and_under_one_second(sqlite_session):
    for i in range(105):
        beneficiaries_service.create_beneficiary(
            sqlite_session,
            USER_A,
            alias=f"Benef {i:03d}",
            account_or_cci=f"{10000000 + i:08d}",
        )
    start = time.perf_counter()
    found = beneficiaries_service.search_beneficiaries(sqlite_session, USER_A, "benef")
    elapsed = time.perf_counter() - start
    assert len(found) == 100
    assert elapsed < 1.0
    assert len(beneficiaries_service.list_beneficiaries(sqlite_session, USER_A)) == 100


def test_update_reindexes_and_validates(sqlite_session):
    view = beneficiaries_service.create_beneficiary(
        sqlite_session, USER_A, **_payload(preapproved_limit_minor=None)
    )
    updated = beneficiaries_service.update_beneficiary(
        sqlite_session,
        USER_A,
        view["id"],
        alias="Tia Rosa",
        preapproved_limit_minor=99_00,
        currency="PEN",
    )
    assert updated["alias"] == "Tia Rosa"
    assert updated["preapproved_limit_minor"] == 99_00
    assert [
        v["alias"]
        for v in beneficiaries_service.search_beneficiaries(sqlite_session, USER_A, "rosa")
    ] == ["Tia Rosa"]
    # El alias viejo como palabra exacta ya no matchea el registro.
    assert beneficiaries_service.search_beneficiaries(sqlite_session, USER_A, "mama") == []
    cleared = beneficiaries_service.update_beneficiary(
        sqlite_session, USER_A, view["id"], preapproved_limit_minor=None, holder_name=None
    )
    assert cleared["preapproved_limit_minor"] is None
    assert cleared["holder_name"] is None


def test_update_duplicate_alias_or_account(sqlite_session):
    first = beneficiaries_service.create_beneficiary(sqlite_session, USER_A, **_payload())
    second = beneficiaries_service.create_beneficiary(
        sqlite_session, USER_A, **_payload(alias="Otro", cci=CCI_A2)
    )
    with pytest.raises(beneficiaries_service.DuplicateBeneficiaryError):
        beneficiaries_service.update_beneficiary(sqlite_session, USER_A, second["id"], alias="MAMA")
    with pytest.raises(beneficiaries_service.DuplicateBeneficiaryError):
        beneficiaries_service.update_beneficiary(
            sqlite_session, USER_A, second["id"], account_or_cci=CCI_A1
        )
    with pytest.raises(beneficiaries_service.BeneficiaryNotFoundError):
        beneficiaries_service.update_beneficiary(sqlite_session, USER_A, uuid.uuid4(), alias="X")
    with pytest.raises(beneficiaries_service.BeneficiaryForbiddenError):
        beneficiaries_service.update_beneficiary(sqlite_session, USER_B, first["id"], alias="X")


def test_duplicates_case_and_format_insensitive(sqlite_session):
    beneficiaries_service.create_beneficiary(sqlite_session, USER_A, **_payload())
    with pytest.raises(beneficiaries_service.DuplicateBeneficiaryError):
        beneficiaries_service.create_beneficiary(
            sqlite_session, USER_A, **_payload(alias="  MAMÁ ", cci=CCI_A2)
        )
    with pytest.raises(beneficiaries_service.DuplicateBeneficiaryError):
        beneficiaries_service.create_beneficiary(
            sqlite_session, USER_A, **_payload(alias="Otro", cci="0011 0001-1111 1111 1111")
        )
    # Otro titular puede repetir alias/CCI (aislamiento).
    other = beneficiaries_service.create_beneficiary(
        sqlite_session, USER_B, **_payload(alias="Mama", cci=CCI_A1)
    )
    assert other["alias"] == "Mama"


def test_create_validation_errors(sqlite_session):
    with pytest.raises(ValueError):
        beneficiaries_service.create_beneficiary(sqlite_session, USER_A, **_payload(alias="   "))
    with pytest.raises(ValueError):
        beneficiaries_service.create_beneficiary(sqlite_session, USER_A, **_payload(cci="ABC"))
    with pytest.raises(ValueError):
        beneficiaries_service.create_beneficiary(
            sqlite_session, USER_A, **_payload(preapproved_limit_minor=-1)
        )
    with pytest.raises(TypeError):
        beneficiaries_service.create_beneficiary(
            sqlite_session, USER_A, **_payload(preapproved_limit_minor=10.5)
        )
    with pytest.raises(ValueError):
        beneficiaries_service.create_beneficiary(sqlite_session, USER_A, **_payload(currency="XX"))


def test_soft_delete_requires_confirm(sqlite_session):
    view = beneficiaries_service.create_beneficiary(sqlite_session, USER_A, **_payload())
    with pytest.raises(ValueError):
        beneficiaries_service.delete_beneficiary(sqlite_session, USER_A, view["id"])
    with pytest.raises(ValueError):
        beneficiaries_service.delete_beneficiary(sqlite_session, USER_A, view["id"], confirm=False)
    deleted = beneficiaries_service.delete_beneficiary(
        sqlite_session, USER_A, view["id"], confirm=True
    )
    assert deleted["status"] == "DELETED"
    assert beneficiaries_service.list_beneficiaries(sqlite_session, USER_A) == []
    assert beneficiaries_service.search_beneficiaries(sqlite_session, USER_A, "mama") == []
    with pytest.raises(beneficiaries_service.BeneficiaryNotFoundError):
        beneficiaries_service.delete_beneficiary(sqlite_session, USER_A, view["id"], confirm=True)
    with pytest.raises(beneficiaries_service.BeneficiaryNotFoundError):
        beneficiaries_service.update_beneficiary(sqlite_session, USER_A, view["id"], alias="Z")
    # Trazabilidad: la fila sigue en BD como DELETED (UUID sin guiones en SQLite).
    status = sqlite_session.execute(
        sa.text("SELECT status FROM accounts.beneficiaries WHERE id = :id"),
        {"id": str(view["id"]).replace("-", "")},
    ).scalar()
    assert status == "DELETED"
    # Tras la baja se puede reutilizar el alias (el duplicado solo mira ACTIVE).
    reused = beneficiaries_service.create_beneficiary(sqlite_session, USER_A, **_payload())
    assert reused["status"] == "ACTIVE"


def test_foreign_delete_forbidden(sqlite_session):
    view = beneficiaries_service.create_beneficiary(sqlite_session, USER_A, **_payload())
    with pytest.raises(beneficiaries_service.BeneficiaryForbiddenError):
        beneficiaries_service.delete_beneficiary(sqlite_session, USER_B, view["id"], confirm=True)


def test_idempotent_create(sqlite_session):
    body = _payload(holder_name="Maria")
    first, replayed = beneficiaries_service.create_beneficiary_idempotent(
        sqlite_session, USER_A, body=body, idempotency_key="clave-alta-1"
    )
    assert replayed is False
    second, replayed = beneficiaries_service.create_beneficiary_idempotent(
        sqlite_session, USER_A, body=body, idempotency_key="clave-alta-1"
    )
    assert replayed is True and second["id"] == first["id"]
    assert len(beneficiaries_service.list_beneficiaries(sqlite_session, USER_A)) == 1
    with pytest.raises(beneficiaries_service.IdempotencyConflictError):
        beneficiaries_service.create_beneficiary_idempotent(
            sqlite_session, USER_A, body=_payload(alias="Otro"), idempotency_key="clave-alta-1"
        )
    plain, replayed = beneficiaries_service.create_beneficiary_idempotent(
        sqlite_session, USER_A, body=_payload(alias="SinClave", cci=CCI_A2), idempotency_key=None
    )
    assert replayed is False and plain["alias"] == "SinClave"


def test_idempotent_create_renews_expired_key(sqlite_session):
    from datetime import UTC, datetime, timedelta

    body = _payload()
    _first, replayed = beneficiaries_service.create_beneficiary_idempotent(
        sqlite_session, USER_A, body=body, idempotency_key="clave-vencida"
    )
    assert replayed is False
    sqlite_session.execute(
        sa.text("UPDATE shared.idempotency_keys SET expires_at = :past WHERE key = :key"),
        {"key": "clave-vencida", "past": datetime.now(UTC) - timedelta(seconds=1)},
    )
    sqlite_session.execute(
        sa.text("DELETE FROM accounts.beneficiary_search_tokens"),
    )
    sqlite_session.execute(sa.text("DELETE FROM accounts.beneficiaries"))
    sqlite_session.execute(sa.text("DELETE FROM shared.outbox"))
    second, replayed = beneficiaries_service.create_beneficiary_idempotent(
        sqlite_session, USER_A, body=body, idempotency_key="clave-vencida"
    )
    assert replayed is False
    assert second["alias"] == "Mama"
    assert len(beneficiaries_service.list_beneficiaries(sqlite_session, USER_A)) == 1


def test_repo_get_active_and_limit(sqlite_session):
    view = beneficiaries_service.create_beneficiary(sqlite_session, USER_A, **_payload())
    row = beneficiaries_repo.get_active(sqlite_session, USER_A, view["id"])
    assert row is not None and str(row.id) == view["id"]
    assert beneficiaries_repo.get_active(sqlite_session, USER_B, view["id"]) is None
    assert beneficiaries_repo.get_active(sqlite_session, USER_A, uuid.uuid4()) is None
    assert len(beneficiaries_repo.list_active(sqlite_session, USER_A, limit=1)) == 1


def test_saved_event_without_pii(sqlite_session):
    view = beneficiaries_service.create_beneficiary(
        sqlite_session, USER_A, **_payload(holder_name="Maria Torres", holder_doc="12345678")
    )
    rows = sqlite_session.execute(
        sa.text("SELECT event_type, status, payload FROM shared.outbox")
    ).all()
    assert rows and all(r[0] == "beneficiary.saved" and r[1] == "PENDING" for r in rows)
    import json as _json

    def _as_dict(payload):
        return _json.loads(payload) if isinstance(payload, str) else dict(payload)

    payload_text = _json.dumps([_as_dict(r[2]) for r in rows])
    assert view["id"] in payload_text
    for secret in (CCI_A1, "Maria Torres", "Mama", "12345678"):
        assert secret not in payload_text


def test_get_for_operation_returns_clear_only_internally(sqlite_session):
    view = beneficiaries_service.create_beneficiary(
        sqlite_session, USER_A, **_payload(holder_name="Maria")
    )
    internal = beneficiaries_service.get_for_operation(sqlite_session, USER_A, view["id"])
    assert internal["account_or_cci"] == CCI_A1
    assert internal["holder_name"] == "Maria"
    with pytest.raises(beneficiaries_service.BeneficiaryForbiddenError):
        beneficiaries_service.get_for_operation(sqlite_session, USER_B, view["id"])


def test_no_pii_in_logs(sqlite_session, caplog):
    import logging as _logging

    with caplog.at_level(_logging.DEBUG):
        beneficiaries_service.create_beneficiary(
            sqlite_session, USER_A, **_payload(holder_name="Maria Torres", holder_doc="12345678")
        )
        beneficiaries_service.search_beneficiaries(sqlite_session, USER_A, "maria")
    assert CCI_A1 not in caplog.text
    assert "12345678" not in caplog.text


# ---------------------------------------------------------------- Parte C: API


@pytest.fixture()
def api_client(sqlite_session, monkeypatch):
    """TestClient con BD SQLite y auth falsa como USER_A."""
    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")

    def _override_db():
        yield sqlite_session

    def _override_user():
        return USER_A

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = _override_user
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)


def _auth_headers(extra: dict | None = None):
    return {"Authorization": "Bearer x", **(extra or {})}


def test_api_crud_happy_path(api_client):
    created = api_client.post(
        "/api/v1/beneficiaries",
        json={
            "alias": "Mama",
            "account_or_cci": CCI_A1,
            "holder_name": "Maria Torres",
            "currency": "PEN",
            "preapproved_limit_minor": 50000,
        },
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["account_masked"] == "****1111"
    assert CCI_A1 not in created.text
    assert "holder_doc" not in created.text

    listed = api_client.get("/api/v1/beneficiaries")
    assert listed.status_code == 200 and listed.json()["meta"]["total"] == 1

    found = api_client.get("/api/v1/beneficiaries", params={"q": "mam"})
    assert found.status_code == 200 and len(found.json()["data"]) == 1

    updated = api_client.put(
        f"/api/v1/beneficiaries/{data['id']}",
        json={"alias": "Mama Nueva", "preapproved_limit_minor": 60000},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["alias"] == "Mama Nueva"

    refused = api_client.delete(f"/api/v1/beneficiaries/{data['id']}")
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "VALIDATION_ERROR"

    deleted = api_client.delete(f"/api/v1/beneficiaries/{data['id']}", params={"confirm": True})
    assert deleted.status_code == 200
    assert deleted.json()["data"]["status"] == "DELETED"
    assert api_client.get("/api/v1/beneficiaries").json()["meta"]["total"] == 0


def test_api_errors(api_client):
    assert (
        api_client.post(
            "/api/v1/beneficiaries", json={"alias": "M", "account_or_cci": "ABC"}
        ).json()["error"]["code"]
        == "VALIDATION_ERROR"
    )
    created = api_client.post(
        "/api/v1/beneficiaries", json={"alias": "Mama", "account_or_cci": CCI_A1}
    )
    assert created.status_code == 201
    dup = api_client.post("/api/v1/beneficiaries", json={"alias": "Mama", "account_or_cci": CCI_A2})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "DUPLICATE_BENEFICIARY"
    dup_cci = api_client.post(
        "/api/v1/beneficiaries", json={"alias": "Otro", "account_or_cci": CCI_A1}
    )
    assert dup_cci.status_code == 409
    missing = uuid.uuid4()
    assert (
        api_client.put(f"/api/v1/beneficiaries/{missing}", json={"alias": "X"}).status_code == 404
    )
    assert (
        api_client.delete(f"/api/v1/beneficiaries/{missing}", params={"confirm": True}).status_code
        == 404
    )


def test_api_isolation_and_auth(api_client):
    created = api_client.post(
        "/api/v1/beneficiaries", json={"alias": "Mama", "account_or_cci": CCI_A1}
    )
    bid = created.json()["data"]["id"]

    def _user_b():
        return USER_B

    app.dependency_overrides[get_current_user_id] = _user_b
    try:
        assert api_client.get("/api/v1/beneficiaries").json()["meta"]["total"] == 0
        assert (
            api_client.put(f"/api/v1/beneficiaries/{bid}", json={"alias": "X"}).status_code == 403
        )
        assert (
            api_client.delete(f"/api/v1/beneficiaries/{bid}", params={"confirm": True}).status_code
            == 403
        )
    finally:

        def _user_a():
            return USER_A

        app.dependency_overrides[get_current_user_id] = _user_a

    app.dependency_overrides.pop(get_current_user_id, None)
    try:
        assert api_client.get("/api/v1/beneficiaries").status_code == 401
    finally:

        def _user_a2():
            return USER_A

        app.dependency_overrides[get_current_user_id] = _user_a2


def test_api_idempotency_key(api_client):
    headers = {"Idempotency-Key": "alta-benef-1"}
    first = api_client.post(
        "/api/v1/beneficiaries", json={"alias": "Mama", "account_or_cci": CCI_A1}, headers=headers
    )
    second = api_client.post(
        "/api/v1/beneficiaries", json={"alias": "Mama", "account_or_cci": CCI_A1}, headers=headers
    )
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    assert second.json()["meta"]["replayed"] is True
    assert api_client.get("/api/v1/beneficiaries").json()["meta"]["total"] == 1
    conflict = api_client.post(
        "/api/v1/beneficiaries", json={"alias": "Otro", "account_or_cci": CCI_A2}, headers=headers
    )
    assert conflict.status_code == 409


# ---------------------------------------------------------------- Parte D: Postgres


def test_postgres_crud_smoke(db_session):
    """Humo en `banca_test` (migraciones incl. 0021 via conftest)."""
    owner = uuid.uuid4()
    view = beneficiaries_service.create_beneficiary(
        db_session, owner, **_payload(holder_name="Maria Torres")
    )
    assert view["account_masked"] == "****1111"
    assert len(beneficiaries_service.search_beneficiaries(db_session, owner, "mar")) == 1
    updated = beneficiaries_service.update_beneficiary(
        db_session, owner, view["id"], preapproved_limit_minor=10_00
    )
    assert updated["preapproved_limit_minor"] == 10_00
    deleted = beneficiaries_service.delete_beneficiary(db_session, owner, view["id"], confirm=True)
    assert deleted["status"] == "DELETED"
