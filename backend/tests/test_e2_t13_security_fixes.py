"""Regresion de los fixes de seguridad de E2-T13 (hallazgos del validador).

- (a) El snapshot de `shared.idempotency_keys` no persiste PII en claro y el
  replay reconstruye la respuesta desde el almacen cifrado (misma forma).
- (b) El arranque/config falla en entorno no-local sin `cryptography` o sin
  `BENEFICIARY_CRYPTO_KEY`/`BENEFICIARY_INDEX_PEPPER` (y no falla en `local`).
- (c) Los UNIQUE parciales `WHERE status='ACTIVE'` (`0022`) bloquean altas
  duplicadas concurrentes; tras soft-delete se puede reutilizar.

Cada prueba FALLA sin su fix: (a) el snapshot contenia alias/nombre en
claro; (b) no habia validacion fail-fast; (c) el duplicado por carrera se
insertaba sin error.
"""

from __future__ import annotations

import json
import sys
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.db import Base
from app.modules.accounts.repository import beneficiaries as beneficiaries_repo
from app.modules.accounts.service import beneficiaries as beneficiaries_service
from app.modules.accounts.service import beneficiaries_crypto as crypto

USER = uuid.uuid4()
CCI_1 = "00110001111111111111"
CCI_2 = "00220002222222222222"


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


def _snapshot_for(session: Session, key: str) -> dict:
    raw = session.execute(
        sa.text("SELECT response_snapshot FROM shared.idempotency_keys WHERE key = :key"),
        {"key": key},
    ).scalar()
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw)


# ------------------------------------------------ (a) snapshot sin PII + replay


def test_idempotency_snapshot_has_no_pii_and_replay_rebuilds(sqlite_session):
    body = {
        "alias": "Mama Secreta",
        "account_or_cci": CCI_1,
        "holder_name": "Maria Torres Quispe",
    }
    first, replayed = beneficiaries_service.create_beneficiary_idempotent(
        sqlite_session, USER, body=dict(body), idempotency_key="k-sin-pii-1"
    )
    assert replayed is False
    assert first["alias"] == "Mama Secreta"

    snapshot = _snapshot_for(sqlite_session, "k-sin-pii-1")
    blob = json.dumps(snapshot)
    for secret in ("Mama Secreta", "Maria Torres Quispe", CCI_1):
        assert secret not in blob
    assert snapshot["beneficiary_id"] == first["id"]
    assert snapshot["status"] == "ACTIVE"

    second, replayed = beneficiaries_service.create_beneficiary_idempotent(
        sqlite_session, USER, body=dict(body), idempotency_key="k-sin-pii-1"
    )
    assert replayed is True
    assert second == first  # misma forma y valores, reconstruida desde BD
    assert len(beneficiaries_service.list_beneficiaries(sqlite_session, USER)) == 1


# ------------------------------------------------ (b) fail-fast fuera de local


def _prod_env(monkeypatch, *, with_keys: bool):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("JWT_SECRET", "secreto-fuerte-de-prueba")
    if with_keys:
        monkeypatch.setenv("BENEFICIARY_CRYPTO_KEY", "clave-de-prueba-123")
        monkeypatch.setenv("BENEFICIARY_INDEX_PEPPER", "pepper-de-prueba-123")
    else:
        monkeypatch.delenv("BENEFICIARY_CRYPTO_KEY", raising=False)
        monkeypatch.delenv("BENEFICIARY_INDEX_PEPPER", raising=False)


def test_config_ok_in_local_without_keys(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("BENEFICIARY_CRYPTO_KEY", raising=False)
    monkeypatch.delenv("BENEFICIARY_INDEX_PEPPER", raising=False)
    assert Settings().app_env == "local"


def test_config_fails_in_production_without_keys(monkeypatch):
    from pydantic import ValidationError

    _prod_env(monkeypatch, with_keys=False)
    with pytest.raises(ValidationError, match="BENEFICIARY_"):
        Settings()


def test_config_fails_in_production_without_cryptography(monkeypatch):
    from pydantic import ValidationError

    _prod_env(monkeypatch, with_keys=True)
    monkeypatch.setitem(sys.modules, "cryptography", None)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.ciphers.aead", None)
    with pytest.raises(ValidationError, match="cryptography"):
        Settings()


def test_config_ok_in_production_with_keys(monkeypatch):
    _prod_env(monkeypatch, with_keys=True)
    settings = Settings()
    assert settings.beneficiary_crypto_key == "clave-de-prueba-123"


def test_config_fails_via_environment_alias(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("BENEFICIARY_CRYPTO_KEY", raising=False)
    monkeypatch.delenv("BENEFICIARY_INDEX_PEPPER", raising=False)
    with pytest.raises(ValidationError, match="BENEFICIARY_"):
        Settings()


def test_crypto_production_guard(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    crypto.assert_production_ready()  # local: no-op aunque falten claves

    _prod_env(monkeypatch, with_keys=False)
    with pytest.raises(RuntimeError, match="BENEFICIARY_"):
        crypto.assert_production_ready()

    _prod_env(monkeypatch, with_keys=True)
    monkeypatch.setattr(crypto, "_AES_AVAILABLE", False)
    with pytest.raises(RuntimeError, match="fallback"):
        crypto.encrypt_value("dato-sensible")


# ------------------------------------------------ (c) UNIQUE parciales 0022


def _repo_kwargs(owner, alias="Mama", cci=CCI_1):
    return {
        "owner_user_id": owner,
        "alias_enc": crypto.encrypt_value(alias),
        "alias_search_hash": crypto.alias_index(alias),
        "photo_url": None,
        "bank_code": None,
        "account_enc": crypto.encrypt_value(cci),
        "account_hash": crypto.account_index(cci),
        "account_last4": cci[-4:],
        "holder_name_enc": None,
        "name_search_hash": None,
        "holder_doc_hash": None,
        "preapproved_limit_minor": None,
        "currency": None,
        "token_hashes": set(),
    }


def test_duplicate_race_hits_partial_unique(sqlite_session, monkeypatch):
    """Con el chequeo por SELECT cegado (carrera), el UNIQUE 0022 gana (-> 409)."""
    beneficiaries_service.create_beneficiary(
        sqlite_session, USER, alias="Mama", account_or_cci=CCI_1
    )
    sqlite_session.commit()
    # Simula la ventana de carrera: ambos hilos pasan el SELECT previo.
    monkeypatch.setattr(beneficiaries_repo, "find_active_duplicate_alias", lambda *a, **k: None)
    monkeypatch.setattr(beneficiaries_repo, "find_active_duplicate_account", lambda *a, **k: None)
    with pytest.raises(beneficiaries_service.DuplicateBeneficiaryError):
        beneficiaries_service.create_beneficiary(
            sqlite_session, USER, alias="Mama", account_or_cci=CCI_2
        )
    sqlite_session.rollback()
    assert len(beneficiaries_service.list_beneficiaries(sqlite_session, USER)) == 1


def test_partial_unique_allows_reuse_after_delete(sqlite_session):
    """El UNIQUE es parcial: DELETED no bloquea; ACTIVE duplicado si."""
    row = beneficiaries_repo.create_beneficiary(sqlite_session, **_repo_kwargs(USER))
    beneficiaries_repo.soft_delete(sqlite_session, row)
    reused = beneficiaries_repo.create_beneficiary(sqlite_session, **_repo_kwargs(USER))
    assert reused.status == "ACTIVE"
    sqlite_session.commit()

    other = uuid.uuid4()
    beneficiaries_repo.create_beneficiary(sqlite_session, **_repo_kwargs(other))
    with pytest.raises(IntegrityError):
        beneficiaries_repo.create_beneficiary(sqlite_session, **_repo_kwargs(other))
