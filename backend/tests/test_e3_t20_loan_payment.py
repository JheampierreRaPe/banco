"""Pago de cuota y aplicacion contable (E3-T20, HU12 CA-02 parte de cuotas).

- Camino feliz: prestamo `ACTIVE` con cuota pendiente -> `POST
  .../pay` -> `200 SETTLED`: el disponible baja por el total, existe
  una `Transaction LOAN_INSTALLMENT` con su asiento `Debe 2000 /
  Haber 5000 + Haber 4000 + Haber 4200` (`sum(debitos) =
  sum(creditos)`), `loan_payments` creado con el desglose, cuota
  `PAID` (`paid_at`), `outstanding` reducido y evento
  `loan.installment.paid` (sin PII) + auditoria.
- Cuota vencida (`OVERDUE`) se paga igual; `schedule_id` explicito o
  proxima pendiente por defecto.
- Errores: cuota ya `PAID` -> `409`; prestamo ajeno -> `404` neutro;
  `schedule_id` de otro prestamo o malformado -> `422`; sin
  `Idempotency-Key` -> `422`; misma clave + cuerpo distinto -> `409`.
- Saldo insuficiente -> `200` con `status=REJECTED` +
  `failure_code=INSUFFICIENT_FUNDS`, sin mover dinero ni marcar la
  cuota (reintentable con la misma clave: replay del rechazo).
- Idempotencia: doble `POST` misma clave -> un asiento y un pago;
  dos pagos de la misma cuota (claves distintas) -> uno gana, el otro
  `CONFLICT`, sin doble debito.
- Atomicidad: fallo antes o despues del asiento -> rollback total.
- Ultima cuota -> `loans.status=PAID` (`outstanding=0`).
- Aislamiento: `credits` no escribe `postings`/`journal_entries`
  directamente (solo via la fachada del motor).

MVP (decision del dueno): pago por **cuota total**; sin abonos
parciales ni prepago libre (`PARTIAL` heredado se salda en total).

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron del
repo); `get_db` y `get_current_user_id` (credits) se inyectan via
`app.dependency_overrides`. Sin red, sin `float`.
"""

from __future__ import annotations

import logging
import pathlib
import uuid
from datetime import UTC, datetime, timedelta

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
from app.modules.credits.models import Loan, LoanSchedule
from app.modules.credits.service import applications as apps_service
from app.modules.credits.service import documents as docs_service
from app.modules.credits.service import loan_payments as pay_service
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


def _disbursed_loan_id(session: Session, client: TestClient, own, key: str) -> str:
    app_id = _signed_app_id(session)
    resp = client.post(
        f"/api/v1/credits/applications/{app_id}/disburse",
        json={"account_id": str(own)},
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["loan_id"]


def _post_pay(client: TestClient, loan_id: str, body: dict | None = None, key: str | None = "k"):
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(
        f"/api/v1/credits/loans/{loan_id}/pay",
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


def _schedules(session: Session, loan_id: uuid.UUID) -> list[LoanSchedule]:
    return list(
        session.scalars(
            sa.select(LoanSchedule)
            .where(LoanSchedule.loan_id == loan_id)
            .order_by(LoanSchedule.installment_no)
        ).all()
    )


def _loan(session: Session, loan_id: str) -> Loan:
    return session.scalars(sa.select(Loan).where(Loan.id == uuid.UUID(loan_id))).one()


def _entry_postings(session: Session, entry_id):
    from app.modules.ledger import service as ledger_service

    return ledger_service.list_postings(session, entry_id)


# ------------------------------------------------------------ camino feliz


def test_happy_pay_settles_squares_and_records(env, caplog):
    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-disb-happy")
    first = _schedules(session, uuid.UUID(loan_id))[0]
    prior_avail = _avail(session, own)
    prior_outstanding = _loan(session, loan_id).outstanding_principal_minor

    with caplog.at_level(logging.INFO):
        resp = _post_pay(client, loan_id, {}, key="k-pay-happy")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data, meta = body["data"], body["meta"]
    assert meta["replayed"] is False
    assert data["loan_id"] == loan_id
    assert data["schedule_id"] == str(first.id)
    assert data["status"] == "SETTLED"
    assert data["amount_minor"] == int(first.total_minor)
    assert data["paid_at"]
    assert data["outstanding_principal_minor"] == prior_outstanding - int(first.principal_minor)
    assert "failure_code" not in data or data.get("failure_code") is None

    # El disponible baja por el total de la cuota.
    assert _avail(session, own) == prior_avail - int(first.total_minor)

    # Una Transaction LOAN_INSTALLMENT SETTLED, sin holds.
    from app.modules.transactions import repository as tx_repo

    tx = tx_repo.get_transaction(session, uuid.UUID(data["transaction_id"]))
    assert tx.type == "LOAN_INSTALLMENT" and tx.status == "SETTLED"
    assert int(tx.amount_minor) == int(first.total_minor) and int(tx.fee_minor) == 0
    assert tx.source_account_id == own
    assert tx.target_account_id is None
    assert tx_repo.list_holds_by_transaction(session, tx.id) == []
    assert [h.to_status for h in tx.history] == [
        "INITIATED",
        "VALIDATED",
        "AUTHORIZED",
        "POSTED",
        "SETTLED",
    ]

    # Un solo asiento Debe 2000 / Haber 5000+4000+4200, cuadrado.
    from app.modules.ledger.models import JournalEntry, LedgerAccount

    entries = list(session.scalars(sa.select(JournalEntry)).all())
    assert len(entries) == 2  # desembolso + pago
    entry = next(e for e in entries if e.entry_type == "LOAN_INSTALLMENT")
    postings = _entry_postings(session, entry.id)
    by_code: dict[str, tuple[str, int]] = {}
    for p in postings:
        acct = session.get(LedgerAccount, p.ledger_account_id)
        by_code.setdefault(acct.code, (p.direction, int(p.amount_minor)))
    debit = sum(v[1] for v in by_code.values() if v[0] == "DEBIT")
    credit = sum(v[1] for v in by_code.values() if v[0] == "CREDIT")
    assert debit == credit == int(first.total_minor)
    assert by_code["5000"] == ("CREDIT", int(first.principal_minor))
    assert by_code.get("4000", ("CREDIT", 0)) == ("CREDIT", int(first.interest_minor))
    assert by_code.get("4200", ("CREDIT", 0)) == ("CREDIT", int(first.insurance_minor))
    if int(first.insurance_minor) == 0:
        assert "4200" not in by_code  # las patas con monto 0 se omiten
    debit_codes = [c for c, v in by_code.items() if v[0] == "DEBIT"]
    assert len(debit_codes) == 1 and debit_codes[0].startswith("2000-")

    # loan_payments con el desglose + cuota PAID + outstanding reducido.
    from app.modules.credits.models import LoanPayment

    payments = list(session.scalars(sa.select(LoanPayment)).all())
    assert len(payments) == 1
    payment = payments[0]
    assert str(payment.loan_id) == loan_id and str(payment.schedule_id) == str(first.id)
    assert str(payment.transaction_id) == str(tx.id)
    assert int(payment.amount_minor) == int(first.total_minor)
    assert int(payment.principal_applied_minor) == int(first.principal_minor)
    assert int(payment.interest_applied_minor) == int(first.interest_minor)
    assert int(payment.insurance_applied_minor) == int(first.insurance_minor)
    fresh_first = session.get(LoanSchedule, first.id)
    assert fresh_first.status == "PAID" and fresh_first.paid_at is not None
    assert int(_loan(session, loan_id).outstanding_principal_minor) == (
        prior_outstanding - int(first.principal_minor)
    )

    # Evento loan.installment.paid sin PII + auditoria.
    events = _outbox_by_type(session, "loan.installment.paid")
    assert len(events) == 1
    payload = events[0].payload
    assert payload["loan_id"] == loan_id and payload["schedule_id"] == str(first.id)
    assert payload["transaction_id"] == str(tx.id)
    assert payload["amount_minor"] == int(first.total_minor) and payload["currency"] == "PEN"
    assert set(payload) == {
        "loan_id",
        "schedule_id",
        "user_id",
        "transaction_id",
        "amount_minor",
        "currency",
    }
    from app.modules.audit.models import AuditLog

    audits = session.scalars(
        sa.select(AuditLog).where(AuditLog.action == "loan.installment.paid")
    ).all()
    assert len(audits) == 1

    # Sin PII en logs: el numero en claro jamas aparece.
    assert ACCOUNT_NUMBER not in caplog.text


def test_pay_explicit_schedule_id(env):
    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-disb-explicit")
    rows = _schedules(session, uuid.UUID(loan_id))
    resp = _post_pay(client, loan_id, {"schedule_id": str(rows[1].id)}, key="k-pay-explicit")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["schedule_id"] == str(rows[1].id)
    assert session.get(LoanSchedule, rows[1].id).status == "PAID"
    # La primera sigue pendiente: el pago explicito no arrastra otras cuotas.
    assert session.get(LoanSchedule, rows[0].id).status == "PENDING"


def test_overdue_installment_is_payable(env):
    from app.modules.credits.service import loans as loans_service

    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-disb-overdue")
    first = _schedules(session, uuid.UUID(loan_id))[0]
    first.due_date = datetime.now(UTC).date() - timedelta(days=10)
    session.commit()
    marked = loans_service.flag_overdue_installments(session, as_of=datetime.now(UTC).date())
    assert marked >= 1
    session.commit()
    assert session.get(LoanSchedule, first.id).status == "OVERDUE"
    resp = _post_pay(client, loan_id, {}, key="k-pay-overdue")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "SETTLED"
    assert session.get(LoanSchedule, first.id).status == "PAID"


# ------------------------------------------------------------------ errores


def test_paid_installment_conflicts_without_double_debit(env):
    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-disb-paid")
    rows = _schedules(session, uuid.UUID(loan_id))
    first = _post_pay(client, loan_id, {"schedule_id": str(rows[0].id)}, key="k-pay-a")
    assert first.status_code == 200, first.text
    avail_after_first = _avail(session, own)
    second = _post_pay(client, loan_id, {"schedule_id": str(rows[0].id)}, key="k-pay-b")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFLICT"
    # Sin doble debito: un asiento de pago, un loan_payments, saldo intacto.
    assert _count(session, "ledger.journal_entries") == 2
    assert _count(session, "credits.loan_payments") == 1
    assert _avail(session, own) == avail_after_first


def test_foreign_loan_is_neutral_404(env):
    client, session, own = env["client"], env["session"], env["own"]
    other_loan = _disbursed_loan_id(session, client, own, key="k-disb-foreign")
    _CURRENT["user"] = OTHER
    try:
        resp = _post_pay(client, other_loan, {}, key="k-pay-foreign")
    finally:
        _CURRENT["user"] = USER
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert _count(session, "credits.loan_payments") == 0


def test_unknown_loan_is_neutral_404(env):
    client = env["client"]
    resp = _post_pay(client, "00000000-0000-0000-0000-000000000000", {}, key="k-pay-unknown")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_schedule_of_other_loan_is_validation_error(env):
    client, session, own = env["client"], env["session"], env["own"]
    loan_a = _disbursed_loan_id(session, client, own, key="k-disb-a")
    loan_b = _disbursed_loan_id(session, client, own, key="k-disb-b")
    foreign_schedule = _schedules(session, uuid.UUID(loan_b))[0]
    resp = _post_pay(client, loan_a, {"schedule_id": str(foreign_schedule.id)}, key="k-pay-cross")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _count(session, "credits.loan_payments") == 0


def test_malformed_schedule_id_is_422(env):
    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-disb-malformed")
    resp = _post_pay(client, loan_id, {"schedule_id": "no-es-uuid"}, key="k-pay-malformed")
    assert resp.status_code == 422


def test_missing_idempotency_key_is_422(env):
    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-disb-nokey")
    resp = _post_pay(client, loan_id, {}, key=None)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_same_key_different_body_conflicts(env):
    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-disb-clash")
    rows = _schedules(session, uuid.UUID(loan_id))
    first = _post_pay(client, loan_id, {"schedule_id": str(rows[0].id)}, key="k-clash")
    assert first.status_code == 200, first.text
    second = _post_pay(client, loan_id, {"schedule_id": str(rows[1].id)}, key="k-clash")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert _count(session, "credits.loan_payments") == 1
    assert session.get(LoanSchedule, rows[1].id).status == "PENDING"


# ------------------------------------------------------- saldo insuficiente


def test_insufficient_funds_rejected_without_changes(env):
    from app.modules.accounts import repository as accounts_repo

    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-disb-poor")
    first = _schedules(session, uuid.UUID(loan_id))[0]
    assert int(first.total_minor) > 100
    # Drena la cuenta (incluido lo desembolsado) hasta 100 centimos.
    accounts_repo.apply_delta(
        session,
        own,
        available_delta=100 - _avail(session, own),
        held_delta=0,
        currency="PEN",
    )
    session.commit()
    assert _avail(session, own) == 100

    resp = _post_pay(client, loan_id, {}, key="k-pay-poor")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "REJECTED"
    assert data["failure_code"] == "INSUFFICIENT_FUNDS"
    assert resp.json()["meta"]["replayed"] is False

    # 0 cambios: saldo intacto, cuota intacta, sin pago ni asiento de cuota.
    assert _avail(session, own) == 100
    assert session.get(LoanSchedule, first.id).status == "PENDING"
    assert _count(session, "credits.loan_payments") == 0
    assert _count(session, "ledger.journal_entries") == 1  # solo el desembolso
    assert len(_outbox_by_type(session, "loan.installment.paid")) == 0
    from app.modules.transactions import repository as tx_repo

    tx = tx_repo.get_transaction(session, uuid.UUID(data["transaction_id"]))
    assert tx.type == "LOAN_INSTALLMENT" and tx.status == "REJECTED"

    # Reintentable con la misma clave: replay del rechazo, sin debito.
    again = _post_pay(client, loan_id, {}, key="k-pay-poor")
    assert again.status_code == 200
    assert again.json()["meta"]["replayed"] is True
    assert again.json()["data"] == data
    assert _avail(session, own) == 100


# ------------------------------------------------------- idempotencia


def test_double_post_same_key_single_entry_payment(env):
    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-disb-twice")
    first = _post_pay(client, loan_id, {}, key="k-twice")
    assert first.status_code == 200, first.text
    second = _post_pay(client, loan_id, {}, key="k-twice")
    assert second.status_code == 200
    assert second.json()["meta"]["replayed"] is True
    assert second.json()["data"] == first.json()["data"]
    assert _count(session, "ledger.journal_entries") == 2
    assert _count(session, "transactions.transactions") == 2  # desembolso + pago
    assert _count(session, "credits.loan_payments") == 1
    assert len(_outbox_by_type(session, "loan.installment.paid")) == 1


def test_motor_same_key_converges_without_double_entry(env):
    from app.modules.transactions.service import loan_payment as motor

    session, own = env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, env["client"], own, key="k-disb-motor")
    first = _schedules(session, uuid.UUID(loan_id))[0]
    kwargs = {
        "source_account_id": own,
        "amount_minor": int(first.total_minor),
        "currency": "PEN",
        "principal_minor": int(first.principal_minor),
        "interest_minor": int(first.interest_minor),
        "insurance_minor": int(first.insurance_minor),
        "loan_ref": uuid.UUID(loan_id),
        "schedule_ref": first.id,
        "initiator_user_id": USER,
        "idempotency_key": "k-motor-twice",
    }
    first_tx = motor.pay_loan_installment(session, **kwargs)
    second_tx = motor.pay_loan_installment(session, **kwargs)
    session.commit()
    assert first_tx.status == second_tx.status == "SETTLED"
    assert first_tx.id == second_tx.id
    assert _count(session, "ledger.journal_entries") == 2


# -------------------------------------------------------- ultima cuota


def test_last_installment_closes_loan(env):
    from app.modules.accounts import repository as accounts_repo

    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-disb-last")
    rows = _schedules(session, uuid.UUID(loan_id))
    # Fondea para cubrir el total (principal + intereses + seguro).
    total = sum(int(r.total_minor) for r in rows)
    accounts_repo.apply_delta(session, own, available_delta=total, held_delta=0, currency="PEN")
    session.commit()
    for pos, row in enumerate(rows):
        resp = _post_pay(client, loan_id, {"schedule_id": str(row.id)}, key=f"k-pay-{pos}")
        assert resp.status_code == 200, resp.text
    loan = _loan(session, loan_id)
    assert loan.status == "PAID"
    assert int(loan.outstanding_principal_minor) == 0
    assert _count(session, "credits.loan_payments") == TERM
    # Sin cuotas pendientes: otro intento es CONFLICT.
    done = _post_pay(client, loan_id, {}, key="k-pay-done")
    assert done.status_code == 409
    assert done.json()["error"]["code"] == "CONFLICT"


# -------------------------------------------------------- atomicidad


def test_motor_failure_before_entry_leaves_zero_residue(env, monkeypatch):
    from app.modules.transactions.service import loan_payment as motor

    session, own = env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, env["client"], own, key="k-disb-boom")
    first = _schedules(session, uuid.UUID(loan_id))[0]
    prior_avail = _avail(session, own)

    def _boom(*args, **kwargs):
        raise RuntimeError("ledger caido")

    monkeypatch.setattr("app.modules.ledger.service.post_entry", _boom)
    with pytest.raises(RuntimeError):
        motor.pay_loan_installment(
            session,
            source_account_id=own,
            amount_minor=int(first.total_minor),
            currency="PEN",
            principal_minor=int(first.principal_minor),
            interest_minor=int(first.interest_minor),
            insurance_minor=int(first.insurance_minor),
            loan_ref=uuid.UUID(loan_id),
            schedule_ref=first.id,
            initiator_user_id=USER,
            idempotency_key="k-motor-boom",
        )
    session.rollback()
    assert _count(session, "transactions.transactions") == 1  # solo el desembolso
    assert _count(session, "transactions.transaction_status_history") == 5
    assert _count(session, "ledger.journal_entries") == 1
    assert _count(session, "ledger.postings") == 2
    assert _avail(session, own) == prior_avail


def test_usecase_failure_after_entry_rolls_back_everything(env, monkeypatch):
    session, own = env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, env["client"], own, key="k-disb-atomic")
    first = _schedules(session, uuid.UUID(loan_id))[0]
    prior_avail = _avail(session, own)
    prior_outstanding = _loan(session, loan_id).outstanding_principal_minor

    def _boom(*args, **kwargs):
        raise RuntimeError("falla tras el asiento")

    monkeypatch.setattr("app.modules.credits.service.loans.mark_installment_paid", _boom)
    with pytest.raises(RuntimeError):
        pay_service.pay_installment_idempotent(
            session, USER, loan_id, schedule_id=str(first.id), idempotency_key="k-atomic"
        )
    session.rollback()
    assert _count(session, "transactions.transactions") == 1
    assert _count(session, "ledger.journal_entries") == 1
    assert _count(session, "credits.loan_payments") == 0
    assert session.get(LoanSchedule, first.id).status == "PENDING"
    assert int(_loan(session, loan_id).outstanding_principal_minor) == int(prior_outstanding)
    assert _avail(session, own) == prior_avail
    assert _outbox_by_type(session, "loan.installment.paid") == []


# -------------------------------------------------------- aislamiento


def test_credits_never_touches_foreign_tables_directly():
    source = pathlib.Path(pay_service.__file__).read_text(encoding="utf-8")
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


def test_motor_never_imports_credits_models():
    from app.modules.transactions.service import loan_payment as motor

    source = pathlib.Path(motor.__file__).read_text(encoding="utf-8")
    assert "credits.models" not in source
    assert "credits.repository" not in source
