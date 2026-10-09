"""Sin exencion por tope pre-aprobado (E2-T33, HU07/HU08).

Reemplazo de `test_e2_t14_preapproved.py` (20 pruebas) y
`test_e2_t32_preapproved_ceiling.py` (techo absoluto), retirados con la
feature (decision del dueno 2026-10-08, `8-10-2026-2.txt` D1): el tope
pre-aprobado por beneficiario dejo de existir (columna eliminada por la
migracion `0026`) y la unica regla es el gate de `E2-T17`
(umbral `transfer.biometric_threshold_minor` + limite diario por perfil).

Sobre el motor y el ledger con adaptadores reales
(`ACCOUNTS_BALANCE_PORT`, fachadas de `accounts` y `shared.config`):
dinero de juguete (UUIDs sinteticos y numeros de cuenta de prueba con DV
valido, sin PII), SQLite en memoria + schemas ATTACH, sin red, sin `float`.

Mapeo brief -> test:
- beneficiario + `effective > threshold` -> `PENDING_AUTHORIZATION`,
  sin `preapproved_exemption` en `meta` ni exencion en el evento
  (CA-01) ................................. `test_over_threshold_requires_biometry`
- beneficiario + `effective <= threshold` -> `SETTLED` directo
  (CA-02) ................................. `test_under_threshold_settles_directly`
- `effective > daily_limit` -> `REJECTED`/`DAILY_LIMIT_EXCEEDED`
  (el limite diario manda, CA-02) ......... `test_daily_limit_still_rejects`
- `POST`/`PUT /beneficiaries` con el campo historico -> 2xx
  (ignorado) y respuesta sin el campo (CA-03)
  ....................................... `test_api_legacy_limit_field_ignored`
- columna/`CHECK` ausentes tras la migracion (CA-04, solo Postgres;
  se omite sin BD) ........................ `test_postgres_column_dropped`
- idempotencia: misma clave + mismo cuerpo no duplica
  ......................................... `test_idempotency_no_duplicate`
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

# Cuentas CuyCash de 14 digitos con DV valido (dominio puro, sin PII).
SRC_A = "00100000000110"
DST_B = "00100000000226"

THRESHOLD_KEY = "transfer.biometric_threshold_minor"
DAILY_KEY = "transfer.daily_limit_minor"
DAILY_STANDARD_KEY = "transfer.daily_limit_STANDARD_minor"
MAX_ATTEMPTS_KEY = "auth.max_failed_attempts"

TABLES = [
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
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE config.parameters (id TEXT PRIMARY KEY, "
                "key TEXT NOT NULL UNIQUE, value_json TEXT NOT NULL, "
                "module TEXT NOT NULL, description TEXT, updated_at TEXT, "
                "created_at TEXT, updated_by TEXT)"
            )
        )
    return engine


def _seed_param(session: Session, key: str, value: str, module: str = "transactions") -> None:
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, :module)"
        ),
        {"id": uuid.uuid4().hex, "key": key, "value": value, "module": module},
    )
    session.flush()


@pytest.fixture()
def t33_session():
    """Motor completo en SQLite: origen fondeado, destino real y agenda."""
    from app.modules.accounts import repository as accounts_repo
    from app.modules.accounts.service import beneficiaries as beneficiaries_service

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        src = accounts_repo.create_account(
            session, user_id=USER_A, account_number=SRC_A, type="AHORRO", currency="PEN"
        )
        dst = accounts_repo.create_account(
            session, user_id=USER_B, account_number=DST_B, type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, src.id, available_delta=2_000_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, dst.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        _seed_param(session, THRESHOLD_KEY, "2000")
        _seed_param(session, DAILY_KEY, "500000")
        _seed_param(session, DAILY_STANDARD_KEY, "500000")
        _seed_param(session, MAX_ATTEMPTS_KEY, "5", module="identity")
        agenda = beneficiaries_service.create_beneficiary(
            session,
            USER_A,
            alias="Agenda Sin Tope",
            account_or_cci=DST_B,
            currency="PEN",
        )
        session.commit()
        yield {
            "session": session,
            "engine": engine,
            "src": src.id,
            "dst": dst.id,
            "agenda": agenda["id"],
        }
    finally:
        session.close()
        engine.dispose()


def _third_party(session, src, amount, *, beneficiary_id=None, target_id=None, key=None):
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT
    from app.modules.transactions import service as svc

    return svc.transfer_third_party(
        session,
        initiator_user_id=USER_A,
        source_account_id=src,
        target_account_id=target_id,
        amount_minor=amount,
        currency="PEN",
        fee_minor=0,
        idempotency_key=key or f"e2t33-{uuid.uuid4()}",
        balance_port=ACCOUNTS_BALANCE_PORT,
        beneficiary_id=beneficiary_id,
    )


def _initiated_payloads(session, tx_id):
    from app.modules.shared.models import OutboxEntry

    return [
        r.payload
        for r in session.scalars(
            sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx_id)
        ).all()
        if r.event_type == "transaction.initiated"
    ]


def test_over_threshold_requires_biometry(t33_session):
    """CA-01: con beneficiario y `effective > threshold` se exige
    autorizacion (`PENDING_AUTHORIZATION`); nunca se omite por tope."""
    from app.modules.transactions import repository as tx_repository
    from app.modules.transactions.service import biometric as bio_svc

    session = t33_session["session"]
    tx = _third_party(session, t33_session["src"], 10_000, beneficiary_id=t33_session["agenda"])
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    assert "preapproved_exemption" not in (tx.meta or {})
    reasons = [h.reason or "" for h in tx_repository.list_history(session, tx.id)]
    assert bio_svc.REQUIRED_REASON in reasons
    payloads = _initiated_payloads(session, tx.id)
    assert len(payloads) == 1
    assert "exemption_reason" not in payloads[0]
    assert "beneficiary_id" not in payloads[0]


def test_under_threshold_settles_directly(t33_session):
    """CA-02: con beneficiario y `effective <= threshold` se autoriza
    directo (`SETTLED`), como siempre."""
    session = t33_session["session"]
    tx = _third_party(session, t33_session["src"], 1_000, beneficiary_id=t33_session["agenda"])
    session.commit()
    assert tx.status == "SETTLED"
    assert "preapproved_exemption" not in (tx.meta or {})


def test_daily_limit_still_rejects(t33_session):
    """CA-02: el limite diario sigue mandando (`effective > daily_limit`
    -> `REJECTED`/`DAILY_LIMIT_EXCEEDED`), con beneficiario o sin el."""
    session = t33_session["session"]
    tx = _third_party(session, t33_session["src"], 600_000, beneficiary_id=t33_session["agenda"])
    session.commit()
    assert tx.status == "REJECTED"
    assert tx.failure_reason == "DAILY_LIMIT_EXCEEDED"
    assert "preapproved_exemption" not in (tx.meta or {})


def test_idempotency_no_duplicate(t33_session):
    """Mismo `Idempotency-Key` + mismo cuerpo no duplica ni reevalua."""
    session = t33_session["session"]
    key = f"e2t33-idem-{uuid.uuid4()}"
    first = _third_party(
        session, t33_session["src"], 1_000, beneficiary_id=t33_session["agenda"], key=key
    )
    session.commit()
    second = _third_party(
        session, t33_session["src"], 1_000, beneficiary_id=t33_session["agenda"], key=key
    )
    session.commit()
    assert second.id == first.id
    assert second.status == first.status == "SETTLED"


def test_api_legacy_limit_field_ignored():
    """CA-03: `POST`/`PUT /beneficiaries` con el campo historico no falla
    (se ignora) y la respuesta ya no lo expone; `GET` tampoco."""
    from app.core.db import get_db
    from app.main import app
    from app.modules.accounts.api import get_current_user_id

    engine = _make_engine_lite()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)

    def _override_db():
        yield session

    def _override_user():
        return USER_A

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = _override_user
    try:
        with TestClient(app) as client:
            created = client.post(
                "/api/v1/beneficiaries",
                json={
                    "alias": "Mama",
                    "account_or_cci": "00110001111111111111",
                    "currency": "PEN",
                    "preapproved_limit_minor": 50000,
                },
            )
            assert created.status_code == 201, created.text
            assert "preapproved_limit_minor" not in created.json()["data"]
            bid = created.json()["data"]["id"]

            updated = client.put(
                f"/api/v1/beneficiaries/{bid}",
                json={"alias": "Mama Nueva", "preapproved_limit_minor": 60000},
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["data"]["alias"] == "Mama Nueva"
            assert "preapproved_limit_minor" not in updated.json()["data"]

            listed = client.get("/api/v1/beneficiaries")
            assert listed.status_code == 200
            assert "preapproved_limit_minor" not in listed.json()["data"][0]
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        session.close()
        engine.dispose()


def _make_engine_lite():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.shared.models as _s  # registro de tablas

    assert _s is not None
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
    return engine


def test_postgres_column_dropped(db_session):
    """CA-04 (solo Postgres; se omite sin BD): la migracion `0026`
    elimina la columna y su `CHECK`."""
    inspector = sa.inspect(db_session.bind)
    columns = [c["name"] for c in inspector.get_columns("beneficiaries", schema="accounts")]
    assert "preapproved_limit_minor" not in columns
    checks = inspector.get_check_constraints("beneficiaries", schema="accounts")
    names = {c["name"] for c in checks}
    assert "ck_beneficiaries_preapproved_limit_min" not in names
