"""Resolucion beneficiario -> cuenta real en `transfer_third_party` (E2-T30).

Sobre el motor y el ledger con adaptadores reales (`ACCOUNTS_BALANCE_PORT`,
cuentas por `accounts.repository`, beneficiario por la fachada
`accounts.service.beneficiaries.get_for_operation` y cuenta por la nueva
fachada `accounts.service.get_account_by_number`): dinero de juguete (UUIDs
sinteticos y numeros de cuenta de prueba con DV valido, sin PII), SQLite en
memoria + schemas ATTACH, sin red, sin `float`.

Mapeo brief -> test:
- feliz campo `beneficiary_id` -> `SETTLED` a la cuenta real ... `test_happy_*`
- fallback `metadata["beneficiary_id"]` ...................... `test_metadata_*`
- precedencia: `target_account_id` real gana ................. `test_precedence_*`
- `target` no resuelto + beneficiario -> cae al beneficiario . `test_unresolved_*`
- CCI de 20 / numero inexistente -> 404 neutro ............... `test_*_neutral_*`
- beneficiario ajeno/inexistente/dado de baja -> mismo 404 ... `test_*_neutral_*`
- sin destino -> 400 / moneda incoherente -> 400 ............. `test_*_validation_*`
- regresion directo + DNI/celular + E2-T16 .................. `test_regression_*`
- exencion por tope E2-T14 intacta ........................... `test_exemption_*`
- idempotencia y concurrencia sin doble debito ............... `test_idempot*`, `test_concurrent_*`
- fachada `get_account_by_number` ............................ `test_facade_*`
- HTTP `POST /transfers/third-party` ......................... `test_http_*`
"""

from __future__ import annotations

import threading
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

OWNER_A = uuid.uuid4()
OWNER_B = uuid.uuid4()

# Cuentas CuyCash de 14 digitos con DV valido (dominio puro, sin PII).
SRC_A = "00100000000110"
DST_B = "00100000000226"
OTHER = "00100000000331"
GHOST = "00100000000552"
CCI_20 = "12345678901234567890"

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


def _set_param(session: Session, key: str, value: int) -> None:
    session.execute(
        sa.text("UPDATE config.parameters SET value_json = :value WHERE key = :key"),
        {"value": str(value), "key": key},
    )
    session.flush()


@pytest.fixture()
def t30_session():
    """Sesion SQLite aislada con origen fondeado, destino real y agenda."""
    from app.modules.accounts import repository as accounts_repo
    from app.modules.accounts.service import beneficiaries as beneficiaries_service

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        src = accounts_repo.create_account(
            session, user_id=OWNER_A, account_number=SRC_A, type="AHORRO", currency="PEN"
        )
        dst = accounts_repo.create_account(
            session, user_id=OWNER_B, account_number=DST_B, type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, src.id, available_delta=200_000, held_delta=0, currency="PEN"
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
            OWNER_A,
            alias="Agenda Destino",
            account_or_cci=DST_B,
            preapproved_limit_minor=50_000,
            currency="PEN",
        )
        cci = beneficiaries_service.create_beneficiary(
            session, OWNER_A, alias="Agenda Interbancaria", account_or_cci=CCI_20
        )
        ghost = beneficiaries_service.create_beneficiary(
            session, OWNER_A, alias="Agenda Fantasma", account_or_cci=GHOST
        )
        foreign = beneficiaries_service.create_beneficiary(
            session, OWNER_B, alias="Agenda Ajena", account_or_cci=DST_B
        )
        session.commit()
        yield {
            "session": session,
            "engine": engine,
            "src": src.id,
            "dst": dst.id,
            "agenda": agenda["id"],
            "cci": cci["id"],
            "ghost": ghost["id"],
            "foreign": foreign["id"],
        }
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def http_client(t30_session, monkeypatch):
    """TestClient con BD de `t30_session` y auth falsa como OWNER_A."""
    from app.core.db import get_db
    from app.main import app
    from app.modules.transactions.api import get_current_user_id

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    session = t30_session["session"]

    def _override_db():
        yield session

    def _override_user():
        return OWNER_A

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_id] = _override_user
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)


def _real_port():
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT

    return ACCOUNTS_BALANCE_PORT


def _avail(session, account_id) -> int:
    from app.modules.accounts import repository as accounts_repo

    return int(accounts_repo.get_balance(session, account_id).available_minor)


def _count(session, key: str) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[key]))


def _third_party(session, src, amount, **overrides):
    from app.modules.transactions import service as svc

    params = {
        "initiator_user_id": OWNER_A,
        "source_account_id": src,
        "amount_minor": amount,
        "currency": "PEN",
        "fee_minor": 0,
        "idempotency_key": f"e2t30-{uuid.uuid4()}",
        "balance_port": _real_port(),
    }
    params.update(overrides)
    return svc.transfer_third_party(session, **params)


# ------------------------------------------------- Fachada nueva


def test_facade_by_number_roundtrip(t30_session):
    from app.modules.accounts import service as accounts_service

    session = t30_session["session"]
    view = accounts_service.get_account_by_number(session, DST_B)
    assert view is not None
    assert view["account_id"] == t30_session["dst"]
    assert view["user_id"] == OWNER_B
    assert set(view) == {"account_id", "user_id", "status", "currency", "account_number"}
    assert accounts_service.get_account_by_number(session, GHOST) is None
    assert accounts_service.get_account_by_number(session, "") is None
    assert accounts_service.get_account_by_number(session, None) is None  # type: ignore[arg-type]


# ------------------------------------------------- Camino feliz


def test_happy_beneficiary_field_settles_to_real_account(t30_session):
    session = t30_session["session"]
    tx = _third_party(session, t30_session["src"], 1_000, beneficiary_id=t30_session["agenda"])
    session.commit()
    assert tx.status == "SETTLED"
    assert tx.target_account_id == t30_session["dst"]
    assert _avail(session, t30_session["src"]) == 199_000
    assert _avail(session, t30_session["dst"]) == 51_000


def test_metadata_fallback_settles_to_real_account(t30_session):
    session = t30_session["session"]
    tx = _third_party(
        session,
        t30_session["src"],
        1_000,
        metadata={"beneficiary_id": t30_session["agenda"]},
    )
    session.commit()
    assert tx.status == "SETTLED"
    assert tx.target_account_id == t30_session["dst"]
    assert isinstance(tx.meta, dict) and tx.meta.get("beneficiary_id") == t30_session["agenda"]


def test_precedence_real_target_wins_over_cci_beneficiary(t30_session):
    """`target_account_id` real + beneficiario con CCI: gana el target (E2-T16)."""
    session = t30_session["session"]
    tx = _third_party(
        session,
        t30_session["src"],
        1_000,
        target_account_id=t30_session["dst"],
        beneficiary_id=t30_session["cci"],
    )
    session.commit()
    assert tx.status == "SETTLED"
    assert tx.target_account_id == t30_session["dst"]


def test_unresolved_target_falls_back_to_beneficiary(t30_session):
    """Cliente antiguo que manda `beneficiary.id` como `target`: resuelve igual."""
    session = t30_session["session"]
    tx = _third_party(
        session,
        t30_session["src"],
        1_000,
        target_account_id=t30_session["agenda"],
        beneficiary_id=t30_session["agenda"],
    )
    session.commit()
    assert tx.status == "SETTLED"
    assert tx.target_account_id == t30_session["dst"]


# ------------------------------------------------- Neutros 404


def test_cci_and_ghost_share_neutral_not_found(t30_session):
    from app.modules.transactions import service as svc

    session = t30_session["session"]
    with pytest.raises(svc.AccountNotFoundError) as cci_exc:
        _third_party(session, t30_session["src"], 1_000, beneficiary_id=t30_session["cci"])
    session.rollback()
    with pytest.raises(svc.AccountNotFoundError) as ghost_exc:
        _third_party(session, t30_session["src"], 1_000, beneficiary_id=t30_session["ghost"])
    session.rollback()
    assert str(cci_exc.value) == str(ghost_exc.value)
    assert _count(session, "transactions.transactions") == 0


def test_unknown_and_foreign_share_neutral_not_found(t30_session):
    from app.modules.transactions import service as svc

    session = t30_session["session"]
    with pytest.raises(svc.AccountNotFoundError) as unknown_exc:
        _third_party(session, t30_session["src"], 1_000, beneficiary_id=uuid.uuid4())
    session.rollback()
    with pytest.raises(svc.AccountNotFoundError) as foreign_exc:
        _third_party(session, t30_session["src"], 1_000, beneficiary_id=t30_session["foreign"])
    session.rollback()
    assert str(unknown_exc.value) == str(foreign_exc.value)
    assert _count(session, "transactions.transactions") == 0


def test_deleted_beneficiary_neutral_not_found(t30_session):
    from app.modules.accounts.service import beneficiaries as beneficiaries_service
    from app.modules.transactions import service as svc

    session = t30_session["session"]
    beneficiaries_service.delete_beneficiary(session, OWNER_A, t30_session["agenda"], confirm=True)
    session.commit()
    with pytest.raises(svc.AccountNotFoundError):
        _third_party(session, t30_session["src"], 1_000, beneficiary_id=t30_session["agenda"])
    session.rollback()
    assert _count(session, "transactions.transactions") == 0


# ------------------------------------------------- 400


def test_no_destination_is_validation_error(t30_session):
    session = t30_session["session"]
    with pytest.raises(ValueError, match="destino requerido"):
        _third_party(session, t30_session["src"], 1_000)
    session.rollback()


def test_currency_mismatch_is_validation_error(t30_session):
    from app.modules.transactions import service as svc

    session = t30_session["session"]
    with pytest.raises(svc.CurrencyMismatchError):
        _third_party(
            session,
            t30_session["src"],
            1_000,
            currency="USD",
            beneficiary_id=t30_session["agenda"],
        )
    session.rollback()


# ------------------------------------------------- Regresion


def test_regression_direct_target_without_beneficiary(t30_session):
    session = t30_session["session"]
    tx = _third_party(session, t30_session["src"], 1_000, target_account_id=t30_session["dst"])
    session.commit()
    assert tx.status == "SETTLED"
    assert tx.target_account_id == t30_session["dst"]


def test_regression_e2t16_real_target_plus_metadata_beneficiary(t30_session):
    """E2-T16: `target_account_id` real + `metadata["beneficiary_id"]` sigue verde."""
    session = t30_session["session"]
    tx = _third_party(
        session,
        t30_session["src"],
        1_000,
        target_account_id=t30_session["dst"],
        metadata={"beneficiary_id": t30_session["agenda"]},
    )
    session.commit()
    assert tx.status == "SETTLED"
    assert tx.target_account_id == t30_session["dst"]


def test_regression_direct_target_on_second_account(t30_session):
    """El flujo DNI/celular (`resolve-target` -> `target_account_id`) sigue
    intacto: el endpoint `resolve-target` no cambia (lo cubre `E2-T27`) y el
    `target_account_id` que devuelve liquida normal (aqui, segunda cuenta)."""
    from app.modules.accounts import repository as accounts_repo

    session = t30_session["session"]
    other = accounts_repo.create_account(
        session, user_id=OWNER_B, account_number=OTHER, type="AHORRO", currency="PEN"
    )
    session.commit()
    tx = _third_party(session, t30_session["src"], 1_000, target_account_id=other.id)
    session.commit()
    assert tx.status == "SETTLED"
    assert tx.target_account_id == other.id


# ------------------------------------------------- E2-T14 intacta


def test_exemption_by_explicit_beneficiary_field(t30_session):
    from app.modules.shared.models import OutboxEntry
    from app.modules.transactions import repository as tx_repository
    from app.modules.transactions.service import biometric as bio_svc

    session = t30_session["session"]
    tx = _third_party(session, t30_session["src"], 10_000, beneficiary_id=t30_session["agenda"])
    session.commit()
    assert tx.status == "SETTLED"
    reasons = [h.reason or "" for h in tx_repository.list_history(session, tx.id)]
    assert bio_svc.PREAPPROVED_EXEMPT_REASON in reasons
    assert bio_svc.REQUIRED_REASON not in reasons
    payloads = [
        r.payload
        for r in session.scalars(
            sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx.id)
        ).all()
        if r.event_type == "transaction.initiated"
    ]
    assert len(payloads) == 1
    assert payloads[0]["exemption_reason"] == "PREAPPROVED_LIMIT"
    assert payloads[0]["beneficiary_id"] == t30_session["agenda"]


# ------------------------------------------------- Idempotencia y concurrencia


def test_idempotency_same_key_same_body_single_debit(t30_session):
    session = t30_session["session"]
    key = f"e2t30-idem-{uuid.uuid4()}"
    first = _third_party(
        session,
        t30_session["src"],
        1_000,
        beneficiary_id=t30_session["agenda"],
        idempotency_key=key,
    )
    session.commit()
    second = _third_party(
        session,
        t30_session["src"],
        1_000,
        beneficiary_id=t30_session["agenda"],
        idempotency_key=key,
    )
    session.commit()
    assert second.id == first.id
    assert _avail(session, t30_session["src"]) == 199_000  # un solo debito
    assert _count(session, "transactions.transactions") == 1


def test_concurrent_same_beneficiary_no_overdraft(t30_session):
    """Dos debitos concurrentes al mismo beneficiario: sin sobregiro."""
    from app.modules.transactions import service as svc

    _set_param(t30_session["session"], THRESHOLD_KEY, 500_000)
    t30_session["session"].commit()
    engine = t30_session["engine"]
    gate = threading.Lock()
    outcomes: dict[str, str] = {}

    def _worker(name: str):
        worker = Session(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            with gate:
                tx = svc.transfer_third_party(
                    worker,
                    initiator_user_id=OWNER_A,
                    source_account_id=t30_session["src"],
                    beneficiary_id=t30_session["agenda"],
                    amount_minor=150_000,
                    currency="PEN",
                    fee_minor=0,
                    idempotency_key=f"e2t30-conc-{name}-{uuid.uuid4()}",
                    balance_port=_real_port(),
                )
                worker.commit()
                outcomes[name] = tx.status
        except Exception as exc:  # noqa: BLE001 - se reporta como fallo del hilo
            worker.rollback()
            outcomes[name] = f"ERROR:{type(exc).__name__}"
        finally:
            worker.close()

    threads = [threading.Thread(target=_worker, args=(n,)) for n in ("t1", "t2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes.values()) == ["REJECTED", "SETTLED"]
    check = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        assert _avail(check, t30_session["src"]) == 50_000
    finally:
        check.close()


# ------------------------------------------------- HTTP


def test_http_beneficiary_field_settles_to_real_account(http_client, t30_session):
    body = {
        "source_account_id": str(t30_session["src"]),
        "beneficiary_id": t30_session["agenda"],
        "amount_minor": 1_000,
        "currency": "PEN",
    }
    resp = http_client.post(
        "/api/v1/transfers/third-party",
        json=body,
        headers={"Idempotency-Key": f"e2t30-http-{uuid.uuid4()}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "SETTLED"
    assert data["target_account_id"] == str(t30_session["dst"])


def test_http_metadata_fallback_settles(http_client, t30_session):
    body = {
        "source_account_id": str(t30_session["src"]),
        "amount_minor": 1_000,
        "currency": "PEN",
        "metadata": {"beneficiary_id": t30_session["agenda"]},
    }
    resp = http_client.post(
        "/api/v1/transfers/third-party",
        json=body,
        headers={"Idempotency-Key": f"e2t30-httpmeta-{uuid.uuid4()}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["target_account_id"] == str(t30_session["dst"])


def test_http_cci_and_foreign_share_neutral_404(http_client, t30_session):
    def _post(payload, key):
        return http_client.post(
            "/api/v1/transfers/third-party",
            json=payload,
            headers={"Idempotency-Key": key},
        )

    base = {"source_account_id": str(t30_session["src"]), "amount_minor": 1_000, "currency": "PEN"}
    cci = _post({**base, "beneficiary_id": t30_session["cci"]}, f"e2t30-hcci-{uuid.uuid4()}")
    foreign = _post(
        {**base, "beneficiary_id": t30_session["foreign"]}, f"e2t30-hfor-{uuid.uuid4()}"
    )
    unknown = _post({**base, "beneficiary_id": str(uuid.uuid4())}, f"e2t30-hunk-{uuid.uuid4()}")
    for resp in (cci, foreign, unknown):
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "NOT_FOUND"
    bodies = [
        {k: v for k, v in resp.json()["error"].items() if k != "request_id"}
        for resp in (cci, foreign, unknown)
    ]
    assert bodies[0] == bodies[1] == bodies[2]


def test_http_no_destination_is_400(http_client, t30_session):
    resp = http_client.post(
        "/api/v1/transfers/third-party",
        json={
            "source_account_id": str(t30_session["src"]),
            "amount_minor": 1_000,
            "currency": "PEN",
        },
        headers={"Idempotency-Key": f"e2t30-hnodest-{uuid.uuid4()}"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
