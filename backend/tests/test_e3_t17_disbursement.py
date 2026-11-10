"""Desembolso transaccional del prestamo (E3-T17, HU12 CA-01/CA-02).

- Camino feliz: expediente `APPROVED` + juego `SIGNED` -> `POST
  .../disburse` -> `201 SETTLED`: el disponible sube por el monto
  aprobado, existe una `Transaction LOAN_DISBURSEMENT` con su asiento
  `Debe 5000 / Haber 2000` (`sum(debitos)=sum(creditos)`), `loans` +
  `loan_schedules` creados, `contracts.loan_id` poblado y evento
  `credit.disbursed` (sin PII) + auditoria.
- Errores: no `APPROVED` -> `409`; no `SIGNED` -> `409`; ya
  desembolsado -> replay sin duplicar; cuenta ajena -> `404` neutro sin
  mover dinero; cuenta no `ACTIVE` -> `422`; sin `Idempotency-Key` ->
  `422`; misma clave + cuerpo distinto -> `409`.
- Idempotencia: doble `POST` misma clave -> un prestamo, un asiento,
  un evento; reintento con otra clave -> replay de negocio; clave
  reclamada en curso -> `409`.
- Atomicidad: fallo inyectado antes del asiento -> rollback total (0
  filas, saldo intacto, `contracts.loan_id` sin poblar).
- Lectura: `GET /credits/loans` y `GET .../schedule` solo del titular
  (`404` neutro si ajeno).
- Aislamiento: `credits` no escribe `postings`/`journal_entries`
  directamente (solo via la fachada del motor).

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron del
repo); `get_db` y `get_current_user_id` (credits) se inyectan via
`app.dependency_overrides`. Sin red, sin `float`.
"""

from __future__ import annotations

import logging
import pathlib
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.modules.accounts.models as _accounts
import app.modules.audit.models as _audit
import app.modules.credits.models as _credits
import app.modules.ledger.models as _ledger
import app.modules.shared.models as _shared
import app.modules.transactions.models as _transactions
from app.adapters.credit_bureau import BureauScore, MockCreditBureau
from app.core.db import Base, get_db
from app.main import app
from app.modules.credits import repository as repo
from app.modules.credits.api import get_current_user_id
from app.modules.credits.service import applications as apps_service
from app.modules.credits.service import disbursement as disburse_service
from app.modules.credits.service import documents as docs_service
from app.modules.credits.service import signing as signing_service

assert _accounts is not None and _audit is not None and _credits is not None
assert _ledger is not None and _shared is not None and _transactions is not None

USER = uuid.uuid4()
OTHER = uuid.uuid4()
ACCOUNT_NUMBER = "00110009999"
OTHER_ACCOUNT_NUMBER = "00220001111"
AMOUNT = 1_000_000
TERM = 12
FUNDED = 50_000
PIN = "7319"

TABLES = [
    "credits.loan_products",
    "credits.loan_applications",
    "credits.contracts",
    "credits.loans",
    "credits.loan_schedules",
    "credits.loan_payments",
    "accounts.accounts",
    "accounts.account_balances",
    "ledger.ledger_accounts",
    "ledger.journal_entries",
    "ledger.postings",
    "ledger.ledger_balances",
    "transactions.transactions",
    "transactions.transaction_status_history",
    "transactions.holds",
    "shared.outbox",
    "shared.processed_events",
    "shared.idempotency_keys",
    "audit.audit_log",
]


def _bureau_for(user_id: uuid.UUID, score: int = 780):
    return MockCreditBureau(
        profiles={str(user_id): BureauScore(score=score, deudas_minor=0, morosidad=False)}
    )


def _make_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("credits", "accounts", "ledger", "transactions", "shared", "audit"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    return engine


_CURRENT = {"user": USER}


@pytest.fixture()
def env(monkeypatch):
    """Sesion SQLite + TestClient con catalogo, cuentas fondeadas y auth falsa."""
    from app.modules.accounts import repository as accounts_repo

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        repo.seed_demo_products(session)
        own = accounts_repo.create_account(
            session, user_id=USER, account_number=ACCOUNT_NUMBER, type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, own.id, available_delta=FUNDED, held_delta=0, currency="PEN"
        )
        stranger = accounts_repo.create_account(
            session,
            user_id=OTHER,
            account_number=OTHER_ACCOUNT_NUMBER,
            type="AHORRO",
            currency="PEN",
        )
        accounts_repo.apply_delta(
            session, stranger.id, available_delta=FUNDED, held_delta=0, currency="PEN"
        )
        session.commit()

        def _override_db():
            yield session

        def _override_user():
            return _CURRENT["user"]

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_user_id] = _override_user
        _CURRENT["user"] = USER
        with TestClient(app) as client:
            yield {"client": client, "session": session, "own": own.id, "stranger": stranger.id}
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        _CURRENT["user"] = USER
        session.close()
        engine.dispose()


def _product_id(session: Session) -> str:
    from app.modules.credits import service as catalog

    return str(catalog.get_product(session, code="PERSONAL_LIBRE")["id"])


def _application_id(session: Session, user: uuid.UUID = USER, score: int = 780) -> str:
    view = apps_service.submit_application(
        session,
        user,
        product_id=_product_id(session),
        requested_amount_minor=AMOUNT,
        term_months=TERM,
        total_income_minor=2_000_000,
        total_debt_minor=100_000,
        bureau_authorized=True,
        bureau_provider=_bureau_for(user, score),
        notify=False,
    )
    session.commit()
    return view["id"]


def _signed_app_id(session: Session, user: uuid.UUID = USER) -> str:
    app_id = _application_id(session, user)
    payload, reused = docs_service.generate_contracts(session, user, app_id)
    assert not reused and len(payload["documents"]) == 3
    signing_service.sign_contract_with_pin(
        session, user, app_id, pin=PIN, pin_verifier=lambda s, u, p: p == PIN
    )
    session.commit()
    return app_id


def _post_disburse(
    client: TestClient, app_id: str, body: dict | None = None, key: str | None = "k"
):
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(
        f"/api/v1/credits/applications/{app_id}/disburse",
        json={} if body is None else body,
        headers=headers,
    )


def _count(session: Session, key: str) -> int:
    return int(session.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[key])))


def _avail(session: Session, account_id: uuid.UUID) -> int:
    from app.modules.accounts import repository as accounts_repo

    return int(accounts_repo.get_balance(session, account_id).available_minor)


def _outbox_by_type(session: Session, event_type: str) -> list:
    from app.modules.shared.models import OutboxEntry

    return list(
        session.scalars(sa.select(OutboxEntry).where(OutboxEntry.event_type == event_type)).all()
    )


def _entries(session: Session):
    from app.modules.ledger.models import JournalEntry

    return list(session.scalars(sa.select(JournalEntry)).all())


# ------------------------------------------------------------ camino feliz


def test_happy_disburse_settles_squares_and_links(env, caplog):
    client, session, own = env["client"], env["session"], env["own"]
    app_id = _signed_app_id(session)
    with caplog.at_level(logging.INFO):
        resp = _post_disburse(client, app_id, {"account_id": str(own)}, key="k-happy")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    data, meta = body["data"], body["meta"]
    assert meta["replayed"] is False
    assert data["status"] == "SETTLED"
    assert data["amount_minor"] == AMOUNT and data["currency"] == "PEN"
    assert data["account_masked"] == f"****{ACCOUNT_NUMBER[-4:]}"
    assert data["disbursed_at"]

    # El disponible sube por el monto aprobado.
    assert _avail(session, own) == FUNDED + AMOUNT

    # Una Transaction LOAN_DISBURSEMENT SETTLED, sin holds.
    from app.modules.transactions import repository as tx_repo

    tx = tx_repo.get_transaction(session, uuid.UUID(data["transaction_id"]))
    assert tx.type == "LOAN_DISBURSEMENT" and tx.status == "SETTLED"
    assert int(tx.amount_minor) == AMOUNT and int(tx.fee_minor) == 0
    assert tx.target_account_id == own
    assert tx_repo.list_holds_by_transaction(session, tx.id) == []
    assert [h.to_status for h in tx.history] == [
        "INITIATED",
        "VALIDATED",
        "AUTHORIZED",
        "POSTED",
        "SETTLED",
    ]

    # Un solo asiento Debe 5000 / Haber 2000, cuadrado.
    entries = _entries(session)
    assert len(entries) == 1 and entries[0].entry_type == "LOAN_DISBURSEMENT"
    from app.modules.ledger import service as ledger_service

    postings = ledger_service.list_postings(session, entries[0].id)
    assert len(postings) == 2
    by_direction = {p.direction: p for p in postings}
    debit_account = ledger_service.get_entry(session, entries[0].id)  # solo lectura
    assert debit_account is not None
    from app.modules.ledger.repository import get_by_code

    portfolio = get_by_code(session, "5000")
    assert by_direction["DEBIT"].ledger_account_id == portfolio.id
    assert by_direction["DEBIT"].amount_minor == AMOUNT
    assert str(by_direction["CREDIT"].account_ref) == str(own)
    assert by_direction["CREDIT"].amount_minor == AMOUNT
    assert by_direction["CREDIT"].ledger_account_id != portfolio.id
    debit = sum(p.amount_minor for p in postings if p.direction == "DEBIT")
    credit = sum(p.amount_minor for p in postings if p.direction == "CREDIT")
    assert debit == credit == AMOUNT

    # loans + cronograma + contracts.loan_id (G54).
    from app.modules.credits.models import Contract, Loan

    loan = session.scalars(sa.select(Loan).where(Loan.id == uuid.UUID(data["loan_id"]))).one()
    assert int(loan.principal_minor) == int(loan.outstanding_principal_minor) == AMOUNT
    assert loan.status == "ACTIVE" and str(loan.account_id) == str(own)
    assert _count(session, "credits.loan_schedules") == TERM
    schedules = session.scalars(sa.select(Base.metadata.tables["credits.loan_schedules"])).all()
    assert len(schedules) == TERM
    rows = session.scalars(
        sa.select(Contract).where(Contract.application_id == loan.application_id)
    ).all()
    assert len(rows) == 3 and {str(r.loan_id) for r in rows} == {str(loan.id)}

    # Evento credit.disbursed sin PII + auditoria.
    events = _outbox_by_type(session, "credit.disbursed")
    assert len(events) == 1
    payload = events[0].payload
    assert payload["loan_id"] == str(loan.id) and payload["transaction_id"] == str(tx.id)
    assert payload["amount_minor"] == AMOUNT and payload["currency"] == "PEN"
    assert set(payload) == {
        "loan_id",
        "application_id",
        "user_id",
        "transaction_id",
        "amount_minor",
        "currency",
    }
    from app.modules.audit.models import AuditLog

    audits = session.scalars(sa.select(AuditLog).where(AuditLog.action == "credit.disbursed")).all()
    assert len(audits) == 1

    # Sin PII en logs: el numero en claro jamas aparece.
    assert ACCOUNT_NUMBER not in caplog.text


def test_disburse_defaults_to_primary_account(env):
    from app.modules.accounts import repository as accounts_repo

    client, session = env["client"], env["session"]
    second = accounts_repo.create_account(
        session, user_id=USER, account_number="00110007777", type="CORRIENTE", currency="PEN"
    )
    session.commit()
    from app.modules.accounts.service import list_active_by_user

    expected = list_active_by_user(session, USER)[0]
    other_id = env["own"] if str(env["own"]) != str(expected["account_id"]) else second.id
    prior_expected = _avail(session, expected["account_id"])
    prior_other = _avail(session, other_id)
    app_id = _signed_app_id(session)
    resp = _post_disburse(client, app_id, {}, key="k-default")
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["account_masked"] == f"****{str(expected['account_number'])[-4:]}"
    assert _avail(session, expected["account_id"]) == prior_expected + AMOUNT
    assert _avail(session, other_id) == prior_other


# ------------------------------------------------------------------ errores


def test_rejected_application_conflicts_without_money(env):
    client, session, own = env["client"], env["session"], env["own"]
    app_id = _application_id(session, score=500)
    from app.modules.credits.repository import applications as apps_repo

    assert apps_repo.get_application(session, uuid.UUID(app_id)).status == "REJECTED"
    resp = _post_disburse(client, app_id, {"account_id": str(own)}, key="k-rejected")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"
    assert _count(session, "credits.loans") == 0
    assert _count(session, "transactions.transactions") == 0
    assert _avail(session, own) == FUNDED


def test_unsigned_game_conflicts_without_money(env):
    client, session, own = env["client"], env["session"], env["own"]
    app_id = _application_id(session)
    payload, _ = docs_service.generate_contracts(session, USER, app_id)
    assert len(payload["documents"]) == 3
    session.commit()
    resp = _post_disburse(client, app_id, {"account_id": str(own)}, key="k-unsigned")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"
    assert _count(session, "credits.loans") == 0
    assert _avail(session, own) == FUNDED


def test_foreign_account_rejected_neutrally_without_money(env):
    client, session, own, stranger = (
        env["client"],
        env["session"],
        env["own"],
        env["stranger"],
    )
    app_id = _signed_app_id(session)
    resp = _post_disburse(client, app_id, {"account_id": str(stranger)}, key="k-foreign-acct")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert _count(session, "credits.loans") == 0
    assert _count(session, "transactions.transactions") == 0
    assert _avail(session, own) == FUNDED
    assert _avail(session, stranger) == FUNDED


def test_inactive_account_validation_error(env):
    from app.modules.accounts import repository as accounts_repo

    client, session, own = env["client"], env["session"], env["own"]
    closed = accounts_repo.create_account(
        session,
        user_id=USER,
        account_number="00110006666",
        type="AHORRO",
        currency="PEN",
        status="CLOSED",
    )
    session.commit()
    app_id = _signed_app_id(session)
    resp = _post_disburse(client, app_id, {"account_id": str(closed.id)}, key="k-closed")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _count(session, "credits.loans") == 0
    assert _avail(session, own) == FUNDED


def test_missing_idempotency_key_is_422(env):
    client, session = env["client"], env["session"]
    app_id = _signed_app_id(session)
    resp = _post_disburse(client, app_id, {"account_id": str(env["own"])}, key=None)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_same_key_different_body_conflicts(env):
    from app.modules.accounts import repository as accounts_repo

    client, session, own = env["client"], env["session"], env["own"]
    second = accounts_repo.create_account(
        session, user_id=USER, account_number="00110007777", type="CORRIENTE", currency="PEN"
    )
    session.commit()
    app_id = _signed_app_id(session)
    first = _post_disburse(client, app_id, {"account_id": str(own)}, key="k-clash")
    assert first.status_code == 201, first.text
    second_resp = _post_disburse(client, app_id, {"account_id": str(second.id)}, key="k-clash")
    assert second_resp.status_code == 409
    assert second_resp.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert _count(session, "credits.loans") == 1
    assert _avail(session, own) == FUNDED + AMOUNT
    assert _avail(session, second.id) == 0


def test_foreign_application_is_neutral_404(env):
    client, session, own = env["client"], env["session"], env["own"]
    other_app = _signed_app_id(session, OTHER)
    resp = _post_disburse(client, other_app, {"account_id": str(own)}, key="k-foreign-app")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ------------------------------------------------------- idempotencia


def test_double_post_same_key_single_loan_entry_event(env):
    client, session, own = env["client"], env["session"], env["own"]
    app_id = _signed_app_id(session)
    first = _post_disburse(client, app_id, {"account_id": str(own)}, key="k-twice")
    assert first.status_code == 201, first.text
    second = _post_disburse(client, app_id, {"account_id": str(own)}, key="k-twice")
    assert second.status_code == 200
    assert second.json()["meta"]["replayed"] is True
    assert second.json()["data"] == first.json()["data"]
    assert _count(session, "credits.loans") == 1
    assert _count(session, "ledger.journal_entries") == 1
    assert len(_outbox_by_type(session, "credit.disbursed")) == 1
    assert _avail(session, own) == FUNDED + AMOUNT


def test_repeat_with_new_key_replays_business_without_new_money(env):
    client, session, own = env["client"], env["session"], env["own"]
    app_id = _signed_app_id(session)
    first = _post_disburse(client, app_id, {"account_id": str(own)}, key="k-first")
    assert first.status_code == 201, first.text
    again = _post_disburse(client, app_id, {"account_id": str(own)}, key="k-second")
    assert again.status_code == 200
    assert again.json()["meta"]["replayed"] is True
    assert again.json()["data"]["loan_id"] == first.json()["data"]["loan_id"]
    assert again.json()["data"]["transaction_id"] == first.json()["data"]["transaction_id"]
    assert _count(session, "credits.loans") == 1
    assert _count(session, "ledger.journal_entries") == 1
    assert len(_outbox_by_type(session, "credit.disbursed")) == 1
    assert _avail(session, own) == FUNDED + AMOUNT


def test_inflight_key_reports_conflict(env):
    from app.modules.shared.repository.idempotency import (
        compute_request_hash,
        try_claim,
    )

    session, own = env["session"], env["own"]
    app_id = _signed_app_id(session)
    digest = compute_request_hash(
        {
            "application_id": str(app_id),
            "account_id": str(own),
            "amount_minor": AMOUNT,
            "currency": "PEN",
        }
    )
    row, created = try_claim(
        session,
        key="k-inflight",
        user_id=USER,
        endpoint=disburse_service.IDEMPOTENCY_ENDPOINT,
        method="POST",
        request_hash=digest,
    )
    assert created and row.response_snapshot in (None, {})
    with pytest.raises(Exception) as excinfo:
        disburse_service.disburse_application_idempotent(
            session, USER, app_id, account_id=str(own), idempotency_key="k-inflight"
        )
    assert "en curso" in str(excinfo.value)
    session.rollback()


# -------------------------------------------------------- atomicidad


def test_motor_disburse_failure_leaves_zero_residue(env, monkeypatch):
    """El motor no deja residuo ante un fallo tecnico (sin savepoints en su ruta)."""
    from app.modules.transactions.service import disbursement as motor

    session, own = env["session"], env["own"]

    def _boom(*args, **kwargs):
        raise RuntimeError("ledger caido")

    monkeypatch.setattr("app.modules.ledger.service.post_entry", _boom)
    with pytest.raises(RuntimeError):
        motor.disburse_loan(
            session,
            target_account_id=own,
            amount_minor=AMOUNT,
            currency="PEN",
            loan_ref=uuid.uuid4(),
            initiator_user_id=USER,
            idempotency_key="k-motor-boom",
        )
    session.rollback()
    assert _count(session, "transactions.transactions") == 0
    assert _count(session, "transactions.transaction_status_history") == 0
    assert _count(session, "ledger.journal_entries") == 0
    assert _count(session, "ledger.postings") == 0
    assert _avail(session, own) == FUNDED


def test_failed_disbursement_recovers_forward_without_double_money(env, monkeypatch):
    """Fallo tras crear `loans` y antes del asiento: sin dinero parcial y
    el reintento completa hacia adelante (exactamente una acreditacion).

    Nota de harness: en SQLite los bloques `begin_nested()` de las
    fachadas (`shared`/`E3-T18`) confirman al liberarse a nivel driver,
    asi que el `rollback` externo no retira la fila `loans` aqui; en
    Postgres (una sola transaccion) el rollback es total (0 filas). El
    caso de uso es tolerante a ese residuo: lo completa sin duplicar
    (clave del motor anclada al `loan_id`).
    """
    session, own = env["session"], env["own"]
    app_id = _signed_app_id(session)

    def _boom(*args, **kwargs):
        raise RuntimeError("ledger caido")

    monkeypatch.setattr("app.modules.ledger.service.post_entry", _boom)
    with pytest.raises(RuntimeError):
        disburse_service.disburse_application_idempotent(
            session, USER, app_id, account_id=str(own), idempotency_key="k-boom"
        )
    session.rollback()
    # Nada de dinero ni trazas de dinero: sin tx, sin asiento, sin evento,
    # saldo intacto y juego sin vincular.
    assert _count(session, "transactions.transactions") == 0
    assert _count(session, "ledger.journal_entries") == 0
    assert _avail(session, own) == FUNDED
    from app.modules.credits.models import Contract

    rows = session.scalars(
        sa.select(Contract).where(Contract.application_id == uuid.UUID(app_id))
    ).all()
    assert rows and all(r.loan_id is None for r in rows)
    assert _outbox_by_type(session, "credit.disbursed") == []

    # El reintento (otra clave) completa hacia adelante: exactamente una
    # acreditacion, un asiento cuadrado y el juego vinculado.
    monkeypatch.undo()
    view, replayed = disburse_service.disburse_application_idempotent(
        session, USER, app_id, account_id=str(own), idempotency_key="k-retry"
    )
    session.commit()
    assert replayed is True
    assert view["status"] == "SETTLED"
    assert _avail(session, own) == FUNDED + AMOUNT
    assert _count(session, "credits.loans") == 1
    assert _count(session, "transactions.transactions") == 1
    assert _count(session, "ledger.journal_entries") == 1
    assert len(_outbox_by_type(session, "credit.disbursed")) == 1
    rows = session.scalars(
        sa.select(Contract).where(Contract.application_id == uuid.UUID(app_id))
    ).all()
    assert {str(r.loan_id) for r in rows} == {view["loan_id"]}


# ------------------------------------------------------------ lectura


def test_list_and_schedule_only_owner(env):
    client, session, own = env["client"], env["session"], env["own"]
    app_id = _signed_app_id(session)
    created = _post_disburse(client, app_id, {"account_id": str(own)}, key="k-read")
    assert created.status_code == 201, created.text
    loan_id = created.json()["data"]["loan_id"]

    listed = client.get("/api/v1/credits/loans")
    assert listed.status_code == 200
    items = listed.json()["data"]
    assert len(items) == 1 and items[0]["id"] == loan_id
    assert items[0]["principal_minor"] == AMOUNT and items[0]["status"] == "ACTIVE"

    detail = client.get(f"/api/v1/credits/loans/{loan_id}/schedule")
    assert detail.status_code == 200
    schedule = detail.json()["data"]["schedule"]
    assert len(schedule) == TERM
    assert [r["installment_no"] for r in schedule] == list(range(1, TERM + 1))
    for row in schedule:
        assert (
            row["principal_minor"] + row["interest_minor"] + row["insurance_minor"]
            == row["total_minor"]
        )
    assert sum(r["principal_minor"] for r in schedule) == AMOUNT

    assert (
        client.get(
            "/api/v1/credits/loans/00000000-0000-0000-0000-000000000000/schedule"
        ).status_code
        == 404
    )
    _CURRENT["user"] = OTHER
    try:
        assert client.get("/api/v1/credits/loans").json()["data"] == []
        assert client.get(f"/api/v1/credits/loans/{loan_id}/schedule").status_code == 404
    finally:
        _CURRENT["user"] = USER


# -------------------------------------------------------- aislamiento


def test_credits_never_touches_foreign_tables_directly():
    source = pathlib.Path(disburse_service.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "journal_entries",
        "postings",
        "Posting",
        "transactions.repository",
        "transactions.models",
        "ledger.repository",
        "ledger.models",
        "accounts.repository",
        "accounts.models",
        "post_entry",
        "apply_delta",
    ):
        assert forbidden not in source, forbidden
